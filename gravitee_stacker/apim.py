"""Standalone Gravitee APIM stack management (separate from the Gamma stack).

Supports named INSTANCES so multiple APIM stacks run at once (generalized coexist):
each instance gets its own compose project (via `docker compose -p`), its own data
volumes, an auto-allocated host-port band, and its own tracked up-record. Instance
"default" keeps the canonical ports/project (backward compatible); named instances
land on a shifted band.

Variants:
  * "default" — apim-compose.yml (OSS mongo+es+gateway+mgmt-api+console+portal).
  * "kafka"   — apim-kafka-compose.yml (native-Kafka gateway; EE license required;
    single-instance / fixed ports).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from . import runner

_HERE = Path(__file__).resolve().parent
APIM_COMPOSE = _HERE / "apim-compose.yml"
APIM_LICENSE_COMPOSE = _HERE / "apim-license.yml"
APIM_KAFKA_COMPOSE = _HERE / "apim-kafka-compose.yml"
KAFKA_DIR = _HERE / "kafka"
APIM_REPO = "https://github.com/gravitee-io/gravitee-api-management.git"
DEFAULT_PORT_OFFSET = 20000
MAX_OFFSET = 40000  # 8085+40000=48085 stays a valid port; up to 3 concurrent instances

VARIANTS = ("default", "kafka")

# Composable feature overlays: extra `-f apim-feature-<name>.yml` files layered onto the
# base compose (OSS or kafka). Each adds a capability's service(s) + gateway env. They
# attach to the `storage` network and reach apim-gateway by name. Coexist works because
# the base + overlays are all port-parameterized (see _FEATURE_PORTS).
FEATURES = ("prometheus", "redis-rate-limit", "debug-logging", "alert-engine", "mailpit")

# Features that need an EE license (like the kafka variant). alert-engine (AE) is EE.
FEATURES_REQUIRING_LICENSE = ("alert-engine",)

# Features layered onto EVERY deploy unless opted out. Override machine-wide with
# APIM_DEFAULT_FEATURES (comma-separated; set it empty to disable all defaults), or
# per-deploy by passing the feature prefixed with "-" (e.g. features=["-debug-logging"]).
DEFAULT_FEATURES = ("debug-logging",)

# feature -> [(coexist port env var, canonical default host port), …] for offset remap.
# Features with only internal services (e.g. redis) contribute no host ports.
_FEATURE_PORTS = {
    "prometheus": [("APIM_PROMETHEUS_PORT", 9090)],
    "redis-rate-limit": [],
    "debug-logging": [],
    "alert-engine": [("APIM_AE_MGMT_PORT", 18072)],  # AE node/management API (/_node/*)
    "mailpit": [("APIM_MAILPIT_PORT", 8025)],         # Mailpit web UI (SMTP :1025 internal)
}

# Feature-level gotchas surfaced on apim_up (when the feature is layered on) + referenced
# by quicksetup for the AE case. Curated from live end-to-end testing.
FEATURE_GOTCHAS = {
    "alert-engine": {
        "severity": "warning",
        "summary": "After a FRESH-VOLUME start (down -v then up, or first-ever up), the "
                   "gateway must be RESTARTED once the stack is healthy or alerts silently "
                   "never fire. On a cold boot against an empty Mongo the gateway resolves "
                   "installation=null (the mgmt-api hasn't written the installation record "
                   "yet) and caches it for the life of the process. Every console alert "
                   "carries an auto-injected `installation EQUALS <uuid>` filter, so every "
                   "REQUEST event is dropped at the filter stage — with NO error anywhere: "
                   "traffic flows, analytics populate, events reach AE typed REQUEST, the "
                   "trigger registers, and alert history just stays empty.",
        "fix": "Once healthy: apim_alert_engine_fix(instance) (restarts apim-gateway), or "
               "`docker compose -p <project> restart apim-gateway`. Confirm gateway "
               "NODE_HEARTBEAT events carry a non-null installation. NB: 'Events successfully "
               "sent.' in the gateway log is only 5s node-heartbeat traffic — it proves "
               "transport, NOT that request events flow. Real signals: gateway "
               "'processor-alert in processor chain post-platform'; AE 'EventListenerVerticle "
               "- Received alert event ... type=REQUEST' → DampeningState → "
               "NotificationServiceImpl → 'Webhook sent!'.",
    },
}


def feature_gotcha(name: str) -> Optional[dict]:
    g = FEATURE_GOTCHAS.get(name)
    return {"feature": name, **g} if g else None

# compose service name -> (coexist port env var, canonical default host port, url role)
_ROLE = {
    "apim-gateway":        ("APIM_GATEWAY_PORT", 8082, "gateway"),
    "apim-management-api": ("APIM_MGMT_PORT",    8083, "management API"),
    "apim-console":        ("APIM_CONSOLE_PORT", 8084, "console"),
    "apim-portal":         ("APIM_PORTAL_PORT",  8085, "portal"),
}


def requires_license(variant: str) -> bool:
    return variant == "kafka"


def features_require_license(features) -> bool:
    return any(f in FEATURES_REQUIRING_LICENSE for f in normalize_features(features))


def ae_version_for(apim_version: str) -> str:
    """AE engine image tag tracking the gateway's bundled alert-engine-connectors-ws:
    APIM 4.12+ → connector 3.x → AE '3'; 4.11 → 2.3.x → '2.3'; ≤4.10 → 2.x → '2'.
    Uses the floating major/minor tags so it auto-updates."""
    m = re.match(r"(\d+)\.(\d+)", apim_version or "")
    if not m:
        return "3"
    mm = (int(m.group(1)), int(m.group(2)))
    if mm >= (4, 12):
        return "3"
    if mm == (4, 11):
        return "2.3"
    return "2"


def supports_instances(variant: str) -> bool:
    """The kafka variant is single-instance (fixed *.kafka.local cert + broker ports)."""
    return variant != "kafka"


def features_dir() -> Path:
    """Where USER/experimental feature overlays live — OUTSIDE the package, so custom
    features never pollute (or ship with) the tool. Override with APIM_FEATURES_DIR;
    default ~/.gravitee/stacker-features/. Drop an `apim-feature-<name>.yml` here and use
    it via apim_up(features=["<name>"]) — no need to touch the installed package."""
    return Path(os.environ.get("APIM_FEATURES_DIR")
                or Path.home() / ".gravitee" / "stacker-features").expanduser()


def feature_compose(name: str) -> Path:
    """Overlay file for a feature. A user overlay in features_dir() takes precedence over a
    bundled one of the same name."""
    external = features_dir() / f"apim-feature-{name}.yml"
    return external if external.is_file() else _HERE / f"apim-feature-{name}.yml"


def is_known_feature(name: str) -> bool:
    """Built-in (in FEATURES) OR a user overlay present in features_dir()."""
    return name in FEATURES or (features_dir() / f"apim-feature-{name}.yml").is_file()


def normalize_features(features) -> list[str]:
    """De-dupe + order-preserve a features list; drop falsy. Validation is the caller's."""
    seen, out = set(), []
    for f in (features or []):
        f = (f or "").strip()
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def default_features() -> list[str]:
    """Features applied to every deploy. APIM_DEFAULT_FEATURES overrides (empty = none)."""
    raw = os.environ.get("APIM_DEFAULT_FEATURES")
    if raw is not None:
        return [f.strip() for f in raw.split(",") if f.strip()]
    return list(DEFAULT_FEATURES)


def resolve_features(requested) -> list[str]:
    """Defaults-on resolution: start from the default set, then apply the caller's list.
    An entry prefixed with "-" opts OUT of a default (e.g. "-debug-logging")."""
    out = default_features()
    for f in normalize_features(requested):
        if f.startswith("-"):
            out = [x for x in out if x != f[1:]]
        elif f not in out:
            out.append(f)
    return out


def unknown_features(features) -> list[str]:
    """Unknown names in an already-resolved list (bare names, no '-' prefixes). A name is
    known if it's built-in OR has a user overlay in features_dir()."""
    return [f for f in normalize_features(features) if not is_known_feature(f)]


# ── paths / project / env ─────────────────────────────────────────────────────
def apim_state_dir() -> Path:
    d = runner.state_dir() / "apim"
    d.mkdir(parents=True, exist_ok=True)
    return d


def plugins_dir(instance: str = "default") -> Path:
    """Per-instance host dir bind-mounted into the gateway + mgmt-api as `plugins-ext`
    (where user-added plugins go). Created here so the bind-mount source exists + is
    user-owned before compose up."""
    d = apim_state_dir() / ("plugins" if instance == "default" else f"plugins-{instance}")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _base_project(variant: str) -> str:
    return "gravitee-apim-kafka" if variant == "kafka" else "gravitee-apim"


def project_for(variant: str = "default", instance: str = "default") -> str:
    base = _base_project(variant)
    return base if instance == "default" else f"{base}-{instance}"


def up_log_path(instance: str = "default") -> Path:
    return apim_state_dir() / ("up.log" if instance == "default" else f"up-{instance}.log")


def _meta_path(instance: str = "default") -> Path:
    # "default" keeps the historical up.json name (backward compatible).
    return apim_state_dir() / ("up.json" if instance == "default" else f"{instance}.json")


def compose_file(variant: str = "default") -> Path:
    if variant == "kafka":
        return APIM_KAFKA_COMPOSE
    override = os.environ.get("APIM_COMPOSE_FILE", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p.resolve()
    return APIM_COMPOSE


# Services that carry Gravitee config (gravitee_* env). The config-override view + edit
# targets these; recreating them applies edited values.
CONFIG_SERVICES = ("apim-gateway", "apim-management-api")


def config_dir() -> Path:
    """Where per-project config-override files live (editable rendered overrides)."""
    return runner.config_dir()


def config_override_path(variant: str = "default", instance: str = "default") -> Path:
    """Editable docker-compose override holding this project's rendered gravitee_* values.
    When present it is auto-layered onto every compose call for the project (see compose_args)."""
    return config_dir() / f"{project_for(variant, instance)}.override.yml"


def compose_args(variant: str = "default", instance: str = "default",
                 license_path: Optional[str] = None, features=None) -> list[str]:
    args = ["-p", project_for(variant, instance), "-f", str(compose_file(variant))]
    if variant == "default" and license_path:
        args += ["-f", str(APIM_LICENSE_COMPOSE)]
    for f in normalize_features(features):
        args += ["-f", str(feature_compose(f))]
    # A per-project config override (edited rendered values) wins over everything above.
    override = config_override_path(variant, instance)
    if override.is_file():
        args += ["-f", str(override)]
    return args


def rendered_overrides(variant: str = "default", instance: str = "default", features=None) -> dict:
    """The effective gravitee_* env per config service, fully interpolated (the rendered
    OVERRIDES layer — not the image's hidden gravitee.yml defaults)."""
    cfg = _config(variant=variant, instance=instance, features=features)
    out = {}
    for svc in CONFIG_SERVICES:
        s = cfg.get("services", {}).get(svc)
        if not s:
            continue
        env = s.get("environment") or {}
        if isinstance(env, list):
            env = dict(e.split("=", 1) for e in env if "=" in e)
        gk = {k: v for k, v in env.items() if str(k).lower().startswith("gravitee_")}
        if gk:
            out[svc] = gk
    return out


def rendered_compose(instance: str = "default") -> Optional[str]:
    """The FULL rendered compose (`docker compose config` YAML) for a tracked instance —
    every service, image, port, volume, network + all env, fully interpolated (base + feature
    overlays + license + config override). Reads the record so ports/features/license match."""
    rec = _rec_for(instance)
    version = rec.version if rec else "latest"
    variant = rec.variant if rec else "default"
    features = normalize_features(rec.features) if rec else None
    license_path = rec.license if rec else None
    try:
        port_env = plan_ports(rec.offset if rec else 0, variant, features)["port_env"]
    except (RuntimeError, ValueError):
        port_env = {}
    p = subprocess.run(
        ["docker", "compose", *compose_args(variant, instance, license_path, features), "config"],
        cwd=str(apim_state_dir()),
        env=_env(version, port_env, variant, license_path, features, instance),
        capture_output=True, text=True, timeout=30,
    )
    return p.stdout if p.returncode == 0 else None


def port_offset() -> int:
    try:
        return int(os.environ.get("APIM_PORT_OFFSET", str(DEFAULT_PORT_OFFSET))) or DEFAULT_PORT_OFFSET
    except ValueError:
        return DEFAULT_PORT_OFFSET


DEFAULT_LICENSE_PATH = Path.home() / ".gravitee" / "license.key"


def resolve_license(license_arg: str) -> tuple[Optional[str], str]:
    candidates = [
        (license_arg, "argument"),
        (os.environ.get("APIM_LICENSE", ""), "APIM_LICENSE env"),
        (str(DEFAULT_LICENSE_PATH), f"default path ({DEFAULT_LICENSE_PATH})"),
    ]
    for raw, source in candidates:
        if not raw:
            continue
        p = Path(raw).expanduser()
        if p.is_file() and p.stat().st_size > 0:
            return str(p.resolve()), source
    return None, "none (OSS mode)"


def _env(version: str = "latest", extra: Optional[dict] = None,
         variant: str = "default", license_path: Optional[str] = None, features=None,
         instance: str = "default") -> dict:
    env = runner._child_env()
    env["APIM_VERSION"] = version
    env["APIM_PLUGINS_DIR"] = str(plugins_dir(instance))
    if variant == "kafka":
        env["KAFKA_SSL_DIR"] = str(KAFKA_DIR / "ssl")
        env["KAFKA_SERVER_PROPS"] = str(KAFKA_DIR / "config" / "server.properties")
        env["KAFKA_CLIENT_CONFIG_DIR"] = str(KAFKA_DIR / "client-config")
        env["APIM_LICENSE"] = license_path or str(KAFKA_DIR / "ssl" / "kafka_server_jaas.conf")
    if license_path:
        env["APIM_LICENSE"] = license_path
    # Feature overlays that mount bundled assets by absolute path need their env set so
    # `${...}` interpolation resolves even for read-only `docker compose config`/`ps`.
    _feats = normalize_features(features)
    if "prometheus" in _feats:
        env["APIM_PROMETHEUS_CONFIG"] = str(_HERE / "prometheus.yml")
    if "debug-logging" in _feats:
        env["APIM_LOGBACK_DEBUG"] = str(_HERE / "logback-debug.xml")
    if "alert-engine" in _feats:
        # Pick the AE engine version to match the gateway's connector; and make sure
        # APIM_LICENSE is defined (the overlay mounts it into AE) even on read paths that
        # weren't passed a license_path — AE is EE, so a license is present at `up`.
        env["AE_VERSION"] = os.environ.get("AE_VERSION") or ae_version_for(version)
    # Always define APIM_LICENSE so a USER overlay can reference ${APIM_LICENSE} (mirroring the
    # built-in apim-license.yml) and still pass `docker compose config` validation on a plain
    # default stack — otherwise the unset var yields an empty bind spec ("empty section between
    # colons"). Points at the resolved license, else the conventional path. HOME and the other
    # feature vars above are likewise available to overlays; see apim_up's "Custom overlays" note.
    if not env.get("APIM_LICENSE"):
        env["APIM_LICENSE"] = resolve_license("")[0] or str(DEFAULT_LICENSE_PATH)
    if extra:
        env.update(extra)
    return env


# ── config-driven port/URL resolution ─────────────────────────────────────────
def _config(extra_env: Optional[dict] = None, variant: str = "default",
            instance: str = "default", features=None) -> dict:
    p = subprocess.run(
        ["docker", "compose", *compose_args(variant, instance, features=features),
         "config", "--format", "json"],
        cwd=str(apim_state_dir()), env=_env("latest", extra_env, variant, features=features, instance=instance),
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0:
        raise RuntimeError(f"`docker compose config` failed: {p.stderr.strip()[:300]}")
    return json.loads(p.stdout)


def _service_ports(cfg: dict) -> dict:
    out = {}
    for name, s in cfg.get("services", {}).items():
        ports = [int(pt["published"]) for pt in (s.get("ports") or []) if pt.get("published")]
        if ports:
            out[name] = ports
    return out


def project_name(variant: str = "default", instance: str = "default") -> str:
    """The effective compose project name (with -p it's deterministic)."""
    return project_for(variant, instance)


def plan_ports(offset: int, variant: str = "default", features=None) -> dict:
    """Effective published ports + URLs for the given host-port `offset` (0 = canonical).

    Ports don't depend on the instance (only the offset), so this is instance-free.
    Feature overlays contribute their own host ports (e.g. prometheus :9090), remapped
    by the same offset so a coexisting instance stays conflict-free.
    """
    features = normalize_features(features)
    base = _service_ports(_config(variant=variant, features=features))
    port_env = {}
    if offset:
        for svc, (var, default, _role) in _ROLE.items():
            port_env[var] = str(base.get(svc, [default])[0] + offset)
        for f in features:
            for var, default in _FEATURE_PORTS.get(f, []):
                port_env[var] = str(default + offset)
    eff = _service_ports(_config(port_env, variant, features=features))
    ports = sorted({p for lst in eff.values() for p in lst})
    urls = {role: eff[svc][0] for svc, (_v, _d, role) in _ROLE.items() if svc in eff}
    if "prometheus" in features and "apim-prometheus" in eff:
        urls["prometheus"] = eff["apim-prometheus"][0]
    if "alert-engine" in features and "apim-alert-engine" in eff:
        urls["alert-engine node API"] = eff["apim-alert-engine"][0]
    if "mailpit" in features and "apim-mailpit" in eff:
        urls["mailpit"] = eff["apim-mailpit"][0]
    return {"port_env": port_env, "ports": ports, "urls": urls}


def allocate_offset(variant: str, instance: str, features=None) -> Optional[int]:
    """Pick a host-port band for this instance. A re-up keeps the instance's existing
    band; default -> 0 (canonical); named -> lowest offset whose ports are free AND
    not already claimed by another tracked instance (avoids a start-up race)."""
    existing = _rec_for(instance)
    if existing is not None:
        return existing.offset
    if instance == "default":
        return 0
    claimed = {r.offset for i in known_instances() if (r := _rec_for(i))}
    for off in range(DEFAULT_PORT_OFFSET, MAX_OFFSET + 1, DEFAULT_PORT_OFFSET):
        if off in claimed:
            continue
        try:
            ports = plan_ports(off, variant, features)["ports"]
        except (RuntimeError, ValueError):
            return None
        if not runner.ports_in_use(ports):
            return off
    return None


# ── version resolution ────────────────────────────────────────────────────────
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _semver_tuple(v: Optional[str]) -> Optional[tuple]:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_downgrade(new_version: Optional[str], old_version: Optional[str]) -> bool:
    """True if new_version is an OLDER release than old_version (semver). Used to warn that
    an older management-api is about to run against Mongo volumes written by a newer one."""
    n, o = _semver_tuple(new_version), _semver_tuple(old_version)
    return bool(n and o and n < o)


def resolve_version(version: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if version and version.lower() != "latest":
        return version.lstrip("v"), None
    try:
        p = subprocess.run(["git", "ls-remote", "--tags", "--refs", APIM_REPO],
                           capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return None, f"could not resolve latest version ({e}); pass an explicit version."
    if p.returncode != 0:
        return None, f"git ls-remote failed: {p.stderr.strip()[:200]}; pass an explicit version."
    versions = []
    for line in p.stdout.splitlines():
        tag = line.rsplit("/", 1)[-1].strip()
        m = _SEMVER.match(tag)
        if m:
            versions.append((tuple(int(x) for x in m.groups()), tag))
    if not versions:
        return None, "no stable release tags found; pass an explicit version."
    return max(versions)[1], None


# ── docker compose introspection ──────────────────────────────────────────────
def compose_ps(variant: str = "default", instance: str = "default", features=None) -> list[dict]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(variant, instance, features=features),
         "ps", "--all", "--format", "json"],
        cwd=str(apim_state_dir()), env=_env("latest", variant=variant, features=features, instance=instance),
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0 or not p.stdout.strip():
        return []
    out = p.stdout.strip()
    rows: list[dict] = []
    try:
        parsed = json.loads(out)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        for line in out.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def compose_logs(service: str, lines: int, variant: str = "default",
                 instance: str = "default", features=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *compose_args(variant, instance, features=features),
         "logs", "--no-color", f"--tail={lines}", service],
        cwd=str(apim_state_dir()), env=_env("latest", variant=variant, features=features, instance=instance),
        capture_output=True, text=True, timeout=60,
    )


def service_names(variant: str = "default", instance: str = "default", features=None) -> list[str]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(variant, instance, features=features), "config", "--services"],
        cwd=str(apim_state_dir()), env=_env("latest", variant=variant, features=features, instance=instance),
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0:
        return []
    return sorted(s.strip() for s in p.stdout.splitlines() if s.strip())


# ── port-conflict detection across ALL compose projects ───────────────────────
def project_holding_port(port: int) -> Optional[dict]:
    ids = subprocess.run(["docker", "ps", "-q", "--filter", f"publish={port}"],
                         capture_output=True, text=True, timeout=15)
    for cid in ids.stdout.split():
        insp = subprocess.run(
            ["docker", "inspect", "--format",
             '{{.Name}}\t{{index .Config.Labels "com.docker.compose.project"}}', cid],
            capture_output=True, text=True, timeout=15)
        parts = insp.stdout.strip().split("\t")
        name = parts[0].lstrip("/") if parts else cid
        project = parts[1] if len(parts) > 1 and parts[1] else "(no compose project)"
        return {"port": port, "container": name, "project": project}
    return None


def detect_conflicts(ports: list[int], variant: str = "default",
                     instance: str = "default") -> list[dict]:
    proj = project_for(variant, instance)
    conflicts = []
    for port in ports:
        holder = project_holding_port(port)
        if holder and holder["project"] != proj:
            conflicts.append(holder)
    return conflicts


def down_project(project: str) -> dict:
    p = subprocess.run(["docker", "compose", "-p", project, "down"],
                       env=runner._child_env(), capture_output=True, text=True, timeout=180)
    return {"project": project, "returncode": p.returncode,
            "output": (p.stdout or "") + (p.stderr or "")}


def foreign_apim_port_holders() -> list[dict]:
    """Docker projects actually holding the canonical APIM ports (8082-8085) that are NOT
    tracked apim instances — e.g. quick-setups (gravitee-qs-*), gamma, or an untracked
    stack. apim_list is otherwise blind to these: it reports run-records, not reality."""
    tracked = {project_for(current_variant(i), i) for i in known_instances()}
    out, seen = [], set()
    for port in (8082, 8083, 8084, 8085):
        h = project_holding_port(port)
        if h and h["project"] not in tracked and h["project"] not in seen:
            seen.add(h["project"])
            out.append(h)
    return out


# ── up / down lifecycle ───────────────────────────────────────────────────────
def launch_up_background(version: str, pull: bool, recreate: bool, port_env: dict,
                         license_path: Optional[str], log_path: Path,
                         variant: str = "default", instance: str = "default",
                         features=None) -> subprocess.Popen:
    files = " ".join(shlex.quote(a) for a in compose_args(variant, instance, license_path, features))
    up = f"docker compose {files} up -d" + (" --force-recreate" if recreate else "")
    cmd = (f"docker compose {files} pull && {up}") if pull else up

    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd], cwd=str(apim_state_dir()),
            env=_env(version, port_env, variant, license_path, features, instance),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        fh.close()
    return proc


def recreate_gateway(instance: str = "default", timeout: int = 300) -> dict:
    """Force-recreate the gateway + management-api of a tracked instance (to load newly
    added plugins). Reads the instance's version/variant/features/license from its record."""
    rec = _rec_for(instance)
    if rec is None:
        return {"ok": False, "error": f"instance '{instance}' is not tracked/running."}
    version = rec.version or "latest"
    variant = rec.variant or "default"
    features = normalize_features(rec.features)
    # Reconstruct the instance's shifted host-port band, or the recreate would rebind the
    # canonical ports (8082-8085) and collide with the default stack in coexist mode.
    try:
        port_env = plan_ports(rec.offset or 0, variant, features)["port_env"]
    except (RuntimeError, ValueError):
        port_env = {}
    args = compose_args(variant, instance, rec.license, features)
    p = subprocess.run(
        ["docker", "compose", *args, "up", "-d", "--force-recreate",
         "apim-gateway", "apim-management-api"],
        cwd=str(apim_state_dir()),
        env=_env(version, port_env, variant, rec.license, features, instance),
        capture_output=True, text=True, timeout=timeout,
    )
    return {"ok": p.returncode == 0, "returncode": p.returncode,
            "output": ((p.stdout or "") + (p.stderr or ""))[-1500:]}


def volumes_fresh(variant: str = "default", instance: str = "default") -> bool:
    """True if this instance's data volumes don't exist yet (a fresh/cold start). Check it
    BEFORE `up`. Used to warn that alert-engine needs a post-healthy gateway restart on a
    cold Mongo (else the gateway caches installation=null and alerts never fire)."""
    proj = project_for(variant, instance)
    r = subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"name={proj}_apim-mongo-data"],
                       capture_output=True, text=True, timeout=15)
    return not (r.returncode == 0 and r.stdout.strip())


def restart_gateway(instance: str = "default", timeout: int = 150) -> dict:
    """Restart apim-gateway (NOT recreate) — re-resolves the installation id, which fixes
    the alert-engine fresh-volume bug where the gateway cached installation=null."""
    rec = _rec_for(instance)
    if rec is None:
        return {"ok": False, "error": f"instance '{instance}' is not tracked/running."}
    variant = rec.variant or "default"
    features = normalize_features(rec.features)
    args = compose_args(variant, instance, rec.license, features)
    p = subprocess.run(
        ["docker", "compose", *args, "restart", "apim-gateway"],
        cwd=str(apim_state_dir()),
        env=_env(rec.version or "latest", None, variant, rec.license, features, instance),
        capture_output=True, text=True, timeout=timeout,
    )
    return {"ok": p.returncode == 0, "returncode": p.returncode,
            "output": ((p.stdout or "") + (p.stderr or ""))[-1000:]}


def ae_mgmt_url(instance: str = "default") -> Optional[str]:
    """Host URL for the AE node/management API of a tracked alert-engine instance."""
    rec = _rec_for(instance)
    if rec is None or "alert-engine" not in normalize_features(rec.features):
        return None
    try:
        port = plan_ports(rec.offset or 0, "default", rec.features)["urls"].get("alert-engine node API")
    except (RuntimeError, ValueError):
        port = None
    return f"http://localhost:{port}" if port else None


def run_down(timeout: int, variant: str = "default", instance: str = "default", features=None,
             volumes: bool = False) -> dict:
    try:
        cmd = ["docker", "compose", *compose_args(variant, instance, features=features), "down"]
        if volumes:
            cmd.append("-v")
        p = subprocess.run(
            cmd,
            cwd=str(apim_state_dir()), env=_env("latest", variant=variant, features=features, instance=instance),
            capture_output=True, text=True, timeout=timeout,
        )
        return {"timed_out": False, "returncode": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except subprocess.TimeoutExpired as e:
        return {"timed_out": True, "returncode": None,
                "stdout": e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                "stderr": e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")}


# ── tracked up-process state (per instance) ────────────────────────────────────
@dataclass
class ApimUp:
    pid: int
    log_path: str
    version: str
    instance: str = "default"
    variant: str = "default"
    features: Optional[list] = None
    coexist: bool = False
    offset: int = 0
    license: Optional[str] = None
    urls: Optional[dict] = None
    ports: Optional[list] = None
    project: Optional[str] = None
    compose: Optional[str] = None
    started: Optional[str] = None
    exit_code: Optional[int] = None


_procs: dict[str, subprocess.Popen] = {}
_recs: dict[str, ApimUp] = {}


def record_up(proc: subprocess.Popen, version: str, variant: str, instance: str, offset: int,
              license_path: Optional[str], urls: Optional[dict], ports: Optional[list],
              log_path: Path, started: Optional[str], features=None) -> ApimUp:
    rec = ApimUp(pid=proc.pid, log_path=str(log_path), version=version, instance=instance,
                 variant=variant, features=normalize_features(features),
                 coexist=(offset > 0), offset=offset, license=license_path,
                 urls=urls, ports=ports, project=project_for(variant, instance),
                 compose=str(compose_file(variant)), started=started)
    _procs[instance] = proc
    _recs[instance] = rec
    _meta_path(instance).write_text(json.dumps(asdict(rec), indent=2))
    return rec


def _load(instance: str) -> Optional[ApimUp]:
    p = _meta_path(instance)
    if not p.is_file():
        return None
    try:
        return ApimUp(**json.loads(p.read_text()))
    except (json.JSONDecodeError, TypeError, OSError):
        return None


def _rec_for(instance: str) -> Optional[ApimUp]:
    return _recs.get(instance) or _load(instance)


def is_tracked(instance: str = "default") -> bool:
    """Whether this instance has a persisted up-record (was brought up) — i.e. its compose
    project exists and can be recreated. Unlike is_up_running (which tracks the transient
    `up -d` launcher process), this stays true for a running stack after launch completes."""
    return _rec_for(instance) is not None


def stack_running(variant: str = "default", instance: str = "default") -> bool:
    """Whether this instance's compose project has RUNNING containers (container-based,
    not launcher-PID-based). The safe 'is it already up?' check for preflight/up."""
    return bool(runner.project_running_containers(project_for(variant, instance)))


def is_up_running(instance: str = "default") -> bool:
    proc = _procs.get(instance)
    if proc is not None:
        return proc.poll() is None
    rec = _rec_for(instance)
    return runner.pid_alive(rec.pid) if rec else False


def up_process_status(instance: str = "default") -> dict:
    rec = _rec_for(instance)
    if rec is None:
        return {"tracked": False}
    common = {"pid": rec.pid, "version": rec.version, "instance": rec.instance,
              "variant": rec.variant, "coexist": rec.coexist, "offset": rec.offset,
              "license": rec.license, "urls": rec.urls, "ports": rec.ports,
              "project": rec.project, "compose": rec.compose,
              "log_path": rec.log_path, "started": rec.started}
    proc = _procs.get(instance)
    if proc is not None:
        code = proc.poll()
        if code is not None and rec.exit_code != code:
            rec.exit_code = code
            _meta_path(instance).write_text(json.dumps(asdict(rec), indent=2))
        return {"tracked": True, "running": code is None, "exit_code": code, **common}
    running = runner.pid_alive(rec.pid)
    return {"tracked": True, "running": running,
            "exit_code": None if running else rec.exit_code, **common}


def forget_up(instance: str = "default") -> None:
    _procs.pop(instance, None)
    _recs.pop(instance, None)
    _meta_path(instance).unlink(missing_ok=True)


def current_version(instance: str = "default") -> Optional[str]:
    rec = _rec_for(instance)
    return rec.version if rec else None


def current_variant(instance: str = "default") -> str:
    rec = _rec_for(instance)
    return (rec.variant if rec else None) or "default"


def current_features(instance: str = "default") -> list[str]:
    rec = _rec_for(instance)
    return normalize_features(rec.features if rec else None)


def current_offset(instance: str = "default") -> int:
    rec = _rec_for(instance)
    return rec.offset if rec else 0


def known_instances() -> list[str]:
    """Instance names with a persisted record ('up.json' -> 'default')."""
    names = []
    for f in apim_state_dir().glob("*.json"):
        names.append("default" if f.name == "up.json" else f.stem)
    return sorted(set(names))
