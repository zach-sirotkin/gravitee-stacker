"""Gravitee APIM plugin management for the curated apim_* stack.

Three views of plugins, each from its authoritative source:
  * CATALOG   — everything downloadable from download.gravitee.io. The site is a JS
                front-end over an S3 bucket; the listing is reachable by requesting
                `?list-type=2&prefix=…` with `Accept: text/xml` and parsing
                <ListBucketResult> (exactly what the site's own browser does). Covers
                all APIM plugins, OSS *and* EE (EE ones just need a license at runtime).
  * BUNDLED   — what ships in a given image: `ls plugins/` inside
                graviteeio/apim-<component>:<version> (159-ish zips in the gateway).
  * INSTALLED — what a running instance has: bundled (container plugins/) + user-added
                (the plugins-ext dir this tool bind-mounts, per instance).

Install follows Gravitee's documented approach (docs: APIM → Plugins → Deployment):
drop the plugin zip into an extra `plugins-ext` directory and RESTART the node. We
bind-mount a per-instance host dir to /opt/graviteeio-{gateway,management-api}/plugins-ext
and recreate the two services to load it.

Compatibility: a plugin's zip embeds its build pom.xml (META-INF/maven/**/pom.xml inside
the plugin jar). Modern plugins declare `<gravitee-apim.version>` (the APIM baseline they
target); older ones only declare `<gravitee-gateway-api.version>` (a looser signal).

NOT covered (Phase 1): plugins that live ONLY in private GitHub repos (e.g. the AM EE
IdP/MFA packs) — their GitHub releases carry no binary asset, so they'd need a
build-from-source step. Those are out of scope here.
"""

from __future__ import annotations

import io
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Optional

from . import apim, runner

DOWNLOAD_BASE = "https://download.gravitee.io"
APIM_PLUGINS_PREFIX = "graviteeio-apim/plugins"
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

# The plugin `type` dirs under graviteeio-apim/plugins/ (from a live listing).
PLUGIN_TYPES = ("connectors", "endpoints", "entrypoints", "fetchers", "notifiers",
                "policies", "reporters", "repositories", "resources",
                "service-discovery", "services", "tracers")

_VERSIONED = re.compile(r"^(?P<name>.+)-(?P<version>\d+\.\d+\.\d+(?:[.\-][0-9A-Za-z.\-]+)?)\.zip$")


# ── S3 catalog client (download.gravitee.io) ──────────────────────────────────
def _s3_list(prefix: str, delimiter: str = "/") -> tuple[list[str], list[str]]:
    """Return (common_prefixes, keys) under `prefix`, following continuation tokens."""
    prefixes: list[str] = []
    keys: list[str] = []
    token = None
    while True:
        url = f"{DOWNLOAD_BASE}/?list-type=2&max-keys=1000&prefix={urllib.parse.quote(prefix)}"
        if delimiter:
            url += f"&delimiter={delimiter}"
        if token:
            url += f"&continuation-token={urllib.parse.quote(token)}"
        req = urllib.request.Request(url, headers={"Accept": "text/xml"})
        with urllib.request.urlopen(req, timeout=25) as r:
            root = ET.fromstring(r.read())
        for cp in root.iter(_S3_NS + "CommonPrefixes"):
            p = cp.find(_S3_NS + "Prefix")
            if p is not None and p.text:
                prefixes.append(p.text)
        for c in root.iter(_S3_NS + "Contents"):
            k = c.find(_S3_NS + "Key")
            if k is not None and k.text:
                keys.append(k.text)
        nxt = root.find(_S3_NS + "NextContinuationToken")
        trunc = root.find(_S3_NS + "IsTruncated")
        if trunc is not None and trunc.text == "true" and nxt is not None and nxt.text:
            token = nxt.text
        else:
            break
    return prefixes, keys


def _ver_key(v: str) -> tuple:
    parts = re.split(r"[.\-]", v)
    out = []
    for p in parts:
        out.append((0, int(p)) if p.isdigit() else (1, p))  # numerics sort before/under text
    return tuple(out)


def artifacts_in_type(ptype: str) -> list[str]:
    """Plugin artifact names (e.g. gravitee-policy-oauth2) available under a type."""
    prefixes, _ = _s3_list(f"{APIM_PLUGINS_PREFIX}/{ptype}/")
    names = []
    for p in prefixes:
        name = p.rstrip("/").split("/")[-1]
        if name.startswith("gravitee-"):
            names.append(name)
    return sorted(names)


def versions_of(ptype: str, artifact: str) -> list[str]:
    """All published versions of an artifact, newest last."""
    _, keys = _s3_list(f"{APIM_PLUGINS_PREFIX}/{ptype}/{artifact}/", delimiter="")
    versions = []
    for k in keys:
        m = _VERSIONED.match(k.split("/")[-1])
        if m and m.group("name") == artifact:
            versions.append(m.group("version"))
    return sorted(set(versions), key=_ver_key)


def latest_version(ptype: str, artifact: str) -> Optional[str]:
    vs = versions_of(ptype, artifact)
    return vs[-1] if vs else None


def plugin_url(ptype: str, artifact: str, version: str) -> str:
    return f"{DOWNLOAD_BASE}/{APIM_PLUGINS_PREFIX}/{ptype}/{artifact}/{artifact}-{version}.zip"


def find_type(artifact: str) -> Optional[str]:
    """Which type dir holds this artifact (best-effort: scan the type listings)."""
    for t in PLUGIN_TYPES:
        if artifact in artifacts_in_type(t):
            return t
    return None


def search(query: str = "", ptype: str = "") -> list[dict]:
    """Catalog search. Lists artifact names (cheap CommonPrefixes) across the given type
    (or all types), filters by `query` substring, then resolves latest version only for
    the matches (so it stays fast even for a broad query)."""
    types = [ptype] if ptype else list(PLUGIN_TYPES)
    q = (query or "").lower()
    out = []
    for t in types:
        try:
            arts = artifacts_in_type(t)
        except (urllib.error.URLError, ET.ParseError):
            continue
        for a in arts:
            if q and q not in a.lower():
                continue
            out.append({"name": a, "type": t, "latest_version": latest_version(t, a)})
    return out


# ── compatibility introspection (download + read embedded pom) ─────────────────
def plugin_info(ptype: str, artifact: str, version: str) -> dict:
    """Download the plugin zip and read its manifest + build pom for compatibility.

    Returns id/name/type/version from plugin.properties and the APIM baseline it was
    built against (gravitee-apim.version, or gravitee-gateway-api.version for old ones).
    """
    url = plugin_url(ptype, artifact, version)
    info = {"artifact": artifact, "type": ptype, "version": version, "url": url}
    with tempfile.TemporaryDirectory(prefix="gqs-plugin-") as tmp:
        zpath = Path(tmp) / "plugin.zip"
        try:
            urllib.request.urlretrieve(url, zpath)
        except urllib.error.HTTPError as e:
            return {**info, "error": f"download failed (HTTP {e.code}) — check name/type/version"}
        try:
            with zipfile.ZipFile(zpath) as z:
                jar_name = next((n for n in z.namelist()
                                 if n.endswith(".jar") and "/" not in n and artifact in n), None)
                if not jar_name:
                    return {**info, "error": "no plugin jar found in zip"}
                jar_bytes = z.read(jar_name)
            with zipfile.ZipFile(io.BytesIO(jar_bytes)) as jz:
                names = jz.namelist()
                props = next((n for n in names if n.endswith("plugin.properties")), None)
                if props:
                    for line in jz.read(props).decode(errors="replace").splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            if k in ("id", "name", "type", "category", "description"):
                                info[f"manifest_{k}"] = v
                pom = next((n for n in names if n.endswith("pom.xml")), None)
                if pom:
                    text = jz.read(pom).decode(errors="replace")
                    for tag in ("gravitee-apim.version", "gravitee-gateway-api.version"):
                        m = re.search(rf"<{re.escape(tag)}>([^<]+)</{re.escape(tag)}>", text)
                        if m:
                            info[tag.replace(".", "_").replace("-", "_")] = m.group(1)
        except (zipfile.BadZipFile, StopIteration, KeyError) as e:
            return {**info, "error": f"could not introspect plugin: {e}"}
    apim_base = info.get("gravitee_apim_version")
    gw_api = info.get("gravitee_gateway_api_version")
    info["compatibility"] = (
        f"built for APIM {apim_base}" if apim_base
        else f"gateway-api {gw_api} (older scheme — no explicit APIM version)" if gw_api
        else "unknown (no version metadata in pom)")
    return info


# ── bundled plugins (image inspection) ─────────────────────────────────────────
def bundled_plugins(version: str, component: str = "gateway") -> tuple[list[dict], Optional[str]]:
    """`ls plugins/` inside graviteeio/apim-<component>:<version>. Pulls the image if
    it isn't cached. component: 'gateway' or 'management-api'."""
    image = f"graviteeio/apim-{component}:{version}"
    p = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", "ls -1 plugins/"],
        capture_output=True, text=True, timeout=300, env=runner._child_env(),
    )
    if p.returncode != 0:
        return [], f"could not read bundled plugins from {image}: {p.stderr.strip()[-300:]}"
    out = []
    for line in p.stdout.splitlines():
        line = line.strip()
        if not line.endswith(".zip"):
            continue
        m = _VERSIONED.match(line)
        out.append({"name": m.group("name"), "version": m.group("version")} if m
                   else {"name": line[:-4], "version": None})
    return sorted(out, key=lambda d: d["name"]), None


# ── install / list / remove on a running instance ──────────────────────────────
def _valid_download_url(url: str) -> bool:
    try:
        return urllib.parse.urlparse(url).netloc.endswith("download.gravitee.io")
    except ValueError:
        return False


def install(instance: str, url: str) -> tuple[Optional[str], Optional[str]]:
    """Download `url` (must be on download.gravitee.io) into the instance's plugins-ext
    dir. Returns (filename, error). Does NOT restart — caller recreates the services."""
    if not _valid_download_url(url):
        return None, "refusing to download from a non-download.gravitee.io URL."
    fname = url.split("/")[-1]
    if not fname.endswith(".zip"):
        return None, f"URL does not point at a .zip plugin: {fname}"
    dst = apim.plugins_dir(instance) / fname
    try:
        urllib.request.urlretrieve(url, dst)
    except urllib.error.HTTPError as e:
        return None, f"download failed (HTTP {e.code}) for {url}"
    return fname, None


def installed(instance: str) -> list[str]:
    """User-added plugin zips in the instance's plugins-ext dir."""
    d = apim.plugins_dir(instance)
    return sorted(f.name for f in d.glob("*.zip"))


def remove(instance: str, name: str) -> bool:
    """Delete an added plugin zip (exact filename or artifact-name prefix)."""
    d = apim.plugins_dir(instance)
    hit = d / name if (d / name).is_file() else next(iter(d.glob(f"{name}*.zip")), None)
    if hit and hit.is_file():
        hit.unlink()
        return True
    return False
