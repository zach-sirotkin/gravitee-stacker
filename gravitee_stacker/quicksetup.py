"""Generic runner for the official APIM `docker/quick-setup/*` compose configs.

The gravitee-api-management repo ships ~two dozen ready-made compose configs under
`docker/quick-setup/` (mongodb, postgresql, redis-rate-limit, keycloak, native-kafka,
opensearch, prometheus, opentelemetry-jaeger, https-*, distributed-sync*, …). Rather
than vendor and maintain 26 copies, this module fetches ONE config on demand at the
pinned APIM version and runs it as-is.

Design (mirrors apim.py's machinery so behaviour is consistent):
  * fetch     — sparse + blobless + depth-1 clone at the tag, checkout just the one
                subdir, copy it into `.run/quicksetup/<name>/` (the workdir). If its
                compose mounts `./.license`, drop ~/.gravitee/license.key in.
  * up/down   — background `docker compose -p gravitee-qs-<name> up -d` with cwd set to
                the workdir (so the relative `./.logs` / `./.plugins` / `./.license`
                binds resolve), APIM_VERSION exported. Two-signal status + per-instance
                tracked-process state, same as apim.py.
  * conflicts — reuse apim.project_holding_port; published ports come straight from
                `docker compose config --format json`.

BOUNDARIES (surfaced in the tool docstrings + README):
  1. Runs the upstream config verbatim → inherits its gotchas + manual steps (keycloak
     realm import, native-kafka console setup, mssql/postgres backends). The curated
     apim_*/am_* stacks stay the polished happy-path; this is the "everything else".
  2. NO coexist / no remap FOR THE RAW RUNNER: these upstream composes hardcode host
     ports (mostly 8082–8085) AND container names (gio_apim_*), so only one quick-setup
     runs at a time. Conflict detection + down-the-other work; shifting ports does not.
     (To coexist or COMBINE capabilities, use the curated apim_up(features=[…]) instead —
     that path is port-parameterized and coexist-safe. This runner stays deliberately
     one-at-a-time: its job is fidelity to a single upstream config.)
  3. EE configs (ee-with-alert-engine, native-kafka, …) need a license dropped in.
  4. The fetched README carries any manual steps — it's returned by up/status.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from . import apim, runner

APIM_REPO = apim.APIM_REPO
QUICK_SETUP_PATH = "docker/quick-setup"
LICENSE_MARKER = "./.license"  # a compose that mounts this wants a license in <workdir>/.license/


# ── known gotchas + auto-remediation ──────────────────────────────────────────
# Curated from a deep-functional sweep at 4.12.9 (see project memory). `severity`:
#   "broken"     — non-functional as shipped until fixed (auto-fixed where the fix
#                  is a deterministic, download-free compose edit).
#   "misleading" — runs fine, but docker health / tool status misreports it.
#   "info"       — works; a heads-up that saves debugging time.
# A config with `auto_fix` in _AUTOFIX gets patched in the workdir at fetch time.
GOTCHAS = {
    "postgresql": {
        "severity": "broken",
        "summary": "Needs a PostgreSQL JDBC driver in ./.driver. Without it management-api "
                   "+ gateway crash-loop 'Unable to load repository repository-jdbc' and the "
                   "API never serves — even though every container reports 'healthy'.",
        "fix": "Download the Postgres JDBC jar (https://jdbc.postgresql.org/download/) into "
               "<workdir>/.driver/ and restart. Needs a download — NOT auto-applied.",
    },
    "mssql": {
        "severity": "broken",
        "summary": "TWO defects (both verified): (1) like postgresql, needs a SQL Server JDBC "
                   "driver in ./.driver or management-api crash-loops 'Unable to load repository "
                   "repository-jdbc'. (2) the bundled init-db.sh (which creates the 'gravitee' DB) "
                   "calls the OLD /opt/mssql-tools/bin/sqlcmd path (the 2019-latest image ships "
                   "mssql-tools18) and omits -C, so the DB is never created and mgmt-api fails with "
                   "'Cannot open database gravitee'. Both while every container reports 'healthy'.",
        "fix": "init-db.sh is AUTO-FIXED at fetch (sqlcmd path + -C). You still must download the "
               "MSSQL JDBC jar (learn.microsoft.com → 'Download Microsoft JDBC Driver for SQL "
               "Server') into <workdir>/.driver/ and restart management_api + gateway.",
    },
    "redis-rate-limit": {
        "severity": "broken",
        "summary": "Compose sets gravitee_ratelimit_redis_host=redis-rate-limit but the redis "
                   "service is named redis_rate_limit → gateway UnknownHostException, the "
                   "rate-limit store is unreachable and the policy silently fails OPEN (no 429).",
        "fix": "Set gravitee_ratelimit_redis_host=redis_rate_limit. AUTO-APPLIED at fetch.",
    },
    "keycloak": {
        "severity": "broken",
        "summary": "Keycloak 26 image but legacy KEYCLOAK_IMPORT env + realm mounted to /tmp "
                   "(KC26 imports from /opt/keycloak/data/import/) → the 'gio' realm never "
                   "imports, /realms/gio 404s, no tokens issue.",
        "fix": "Mount realm-gio.json into the KC26 import dir (AUTO-APPLIED at fetch). For the "
               "GATEWAY to validate tokens you must also run download-plugins-ext.sh to fetch "
               "gravitee-resource-oauth2-provider-keycloak (needs a download — not auto-applied). "
               "README's token URL /auth/realms/gio is stale; KC26 uses /realms/gio.",
    },
    "ee-with-alert-engine": {
        "severity": "broken",
        "summary": "NOT usable for end-to-end alert testing as shipped. alert_engine starts, the "
                   "license is accepted, and it DOES register trigger definitions from the mgmt API "
                   "(AE log 'Register trigger [...]') — but the gateway's event stream never reaches "
                   "it: alerts never evaluate (alert HISTORY stays empty) and notifications never "
                   "fire. Root cause: the alert_engine service has NO `networks:` key (it lands on "
                   "the compose default net, isolated from storage/frontend), and "
                   "ws_discovery=true makes the gateway follow AE's announced (unroutable) container "
                   "IP after the host.docker.internal bootstrap. Its image healthcheck on :8072 also "
                   "401s, so the container shows 'unhealthy' and overall stays 'partial'. (Verified "
                   "on Gravitee Cloud with the same alert config → environmental, not a product bug.)",
        "fix": "For real AE testing use the curated stack instead: apim_up(features=['alert-engine']) "
               "— AE on the shared storage network + ws_discovery=false + a container endpoint, which "
               "fixes the isolation the quick-setup can't. This raw config stays broken for AE. "
               "OTHER QUIRKS of this config: `frontend`/`storage` are declared external:true (must "
               "pre-exist or compose fails); AE's healthcheck is baked into the image (can't override "
               "in the compose — only ignore it); `./.plugins` is mounted into AE's plugins-ext but "
               "APIM plugins there are inert for AE (separate product); AE correctly has NO Mongo/ES "
               "dependency (it stores nothing — don't add one); alerts are entirely manual console "
               "work (~5 min each, not scriptable). Diagnostic signals: AE log 'Register trigger "
               "[<id>] [<name>]' = the alert reached the engine; alert HISTORY populated = events "
               "arrived + evaluated (empty = not arriving). 'Channel is ready'/'Events successfully "
               "sent.' in the gateway log = bootstrap/heartbeat only, NOT proof request events flow.",
    },
    "prometheus": {
        "severity": "info",
        "summary": "README says the scrape endpoint is on :18092 but it's actually :18082 (which "
                   "401s to a manual curl — Prometheus scrapes it internally). Gateway request "
                   "metric is http_server_requests_total (Micrometer), not http_requests_total.",
        "fix": None,
    },
    "opensearch": {
        "severity": "info",
        "summary": "v4 APIs report analytics to the gravitee-v4-metrics-* index, not the legacy "
                   "gravitee-request-* (which stays empty and looks like a failure but isn't).",
        "fix": None,
    },
}

# Deterministic, download-free (file, old, new) edits for `broken` configs whose fix
# is a pure string change in a bundled file. Applied under <workdir>/ at fetch time.
_AUTOFIX = {
    "redis-rate-limit": [
        ("docker-compose.yml",
         "gravitee_ratelimit_redis_host=redis-rate-limit",
         "gravitee_ratelimit_redis_host=redis_rate_limit")],
    "keycloak": [
        ("docker-compose.yml",
         "./realm/realm-gio.json:/tmp/realm-gio.json",
         "./realm/realm-gio.json:/opt/keycloak/data/import/realm-gio.json")],
    "mssql": [
        # init-db.sh creates the `gravitee` DB, but calls the OLD mssql-tools sqlcmd
        # path (gone in the 2019-latest image → tools18) AND omits -C (tools18/ODBC18
        # default to mandatory encryption + reject the self-signed cert). Both → the DB
        # is never created and management-api can't connect. Fix path + add -C.
        ("init-db.sh",
         "/opt/mssql-tools/bin/sqlcmd -S localhost -U SA -P 'Sql@2024Pass' -Q 'CREATE DATABASE gravitee'",
         "/opt/mssql-tools18/bin/sqlcmd -S localhost -U SA -P 'Sql@2024Pass' -C -Q 'CREATE DATABASE gravitee'")],
}


def gotcha_for(name: str) -> Optional[dict]:
    g = GOTCHAS.get(name)
    return {"name": name, **g} if g else None


def apply_autofixes(name: str) -> list[dict]:
    """Apply the deterministic, download-free fixes for `name` under the workdir.

    Each fix targets a bundled file (compose, init script, …). Returns a list of
    {file, fix, applied[, note]}; applied=False means the pattern wasn't found
    (upstream may have fixed/changed it) — surfaced so drift is visible, not masked.
    """
    edits = _AUTOFIX.get(name)
    if not edits:
        return []
    by_file: dict[str, list] = {}
    for fname, old, new in edits:
        by_file.setdefault(fname, []).append((old, new))
    results = []
    for fname, pairs in by_file.items():
        fpath = workdir(name) / fname
        if not fpath.is_file():
            results += [{"file": fname, "fix": new, "applied": False,
                         "note": f"{fname} not found — left as-is"} for _old, new in pairs]
            continue
        text = fpath.read_text()
        for old, new in pairs:
            if old in text:
                text = text.replace(old, new)
                results.append({"file": fname, "fix": new, "applied": True})
            else:
                results.append({"file": fname, "fix": new, "applied": False,
                                "note": "pattern not found (upstream may have changed) — left as-is"})
        fpath.write_text(text)
    return results


# ── paths / project / env ─────────────────────────────────────────────────────
def _root() -> Path:
    d = runner.state_dir() / "quicksetup"
    d.mkdir(parents=True, exist_ok=True)
    return d


def workdir(name: str) -> Path:
    """Where the fetched config's files live (the compose's cwd)."""
    return _root() / name


def compose_path(name: str) -> Path:
    return workdir(name) / "docker-compose.yml"


def readme_path(name: str) -> Path:
    return workdir(name) / "README.md"


def up_log_path(name: str) -> Path:
    return _root() / f"{name}.up.log"


def _meta_path(name: str) -> Path:
    return _root() / f"{name}.json"


def project_for(name: str) -> str:
    return f"gravitee-qs-{name}"


def is_fetched(name: str) -> bool:
    return compose_path(name).is_file()


def resolve_version(version: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Same repo as APIM, so reuse its resolver (latest → newest stable tag)."""
    return apim.resolve_version(version)


def compose_args(name: str) -> list[str]:
    return ["-p", project_for(name), "-f", str(compose_path(name))]


def _env(version: str = "latest", extra: Optional[dict] = None) -> dict:
    env = runner._child_env()
    env["APIM_VERSION"] = version
    if extra:
        env.update(extra)
    return env


# ── fetch (sparse + blobless clone of a single subdir at the tag) ──────────────
def _sparse_clone(version: str, dest: Path, sub: Optional[str] = None) -> tuple[bool, str]:
    """Blobless depth-1 sparse clone of APIM at `version`; optionally check out `sub`.

    Returns (ok, message). ~1–2s in practice — trees only, blobs fetched on demand.
    """
    clone = subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", version,
         "--filter=blob:none", "--sparse", APIM_REPO, str(dest)],
        capture_output=True, text=True, timeout=180,
    )
    if clone.returncode != 0:
        return False, f"git clone failed for version '{version}': {clone.stderr.strip()[-300:]}"
    if sub:
        sc = subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", sub],
                            capture_output=True, text=True, timeout=60)
        if sc.returncode != 0:
            return False, f"sparse-checkout failed: {sc.stderr.strip()[-300:]}"
    return True, "ok"


def list_configs(version: str) -> tuple[list[str], Optional[str]]:
    """Names of every `docker/quick-setup/<name>` at the given version."""
    with tempfile.TemporaryDirectory(prefix="gqs-list-") as tmp:
        repo = Path(tmp) / "repo"
        ok, msg = _sparse_clone(version, repo)
        if not ok:
            return [], msg
        ls = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "--name-only", "HEAD", QUICK_SETUP_PATH + "/"],
            capture_output=True, text=True, timeout=60,
        )
        if ls.returncode != 0:
            return [], f"could not list configs: {ls.stderr.strip()[-300:]}"
        names = sorted(line.rsplit("/", 1)[-1] for line in ls.stdout.splitlines() if line.strip())
        return names, None


@dataclass
class FetchResult:
    name: str
    version: str
    workdir: str
    needs_license: bool
    license_mounted: bool
    license_source: Optional[str]
    gotcha: Optional[dict] = None
    autofixes: Optional[list] = None


def fetch(name: str, version: str) -> tuple[Optional[FetchResult], Optional[str]]:
    """Clone just `docker/quick-setup/<name>` at `version` into the workdir.

    Wipes any prior copy first (so a re-fetch is clean). If the config mounts
    `./.license`, drop ~/.gravitee/license.key into <workdir>/.license/license.key.
    """
    sub = f"{QUICK_SETUP_PATH}/{name}"
    with tempfile.TemporaryDirectory(prefix="gqs-fetch-") as tmp:
        repo = Path(tmp) / "repo"
        ok, msg = _sparse_clone(version, repo, sub)
        if not ok:
            return None, msg
        src = repo / QUICK_SETUP_PATH / name
        if not src.is_dir():
            avail, _ = list_configs(version)
            hint = f" Available: {avail}." if avail else ""
            return None, f"no quick-setup config named '{name}' at version {version}.{hint}"

        dst = workdir(name)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)

    # Auto-apply the deterministic, download-free fixes for known-broken configs
    # BEFORE anything reads the compose (license detection, port resolution, up).
    autofixes = apply_autofixes(name)

    needs_license = LICENSE_MARKER in compose_path(name).read_text(errors="replace")
    license_mounted, license_source = False, None
    if needs_license:
        lic_path, lic_src = apim.resolve_license("")
        if lic_path:
            lic_dir = dst / ".license"
            lic_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(lic_path, lic_dir / "license.key")
            license_mounted, license_source = True, lic_src

    return FetchResult(name=name, version=version, workdir=str(dst),
                       needs_license=needs_license, license_mounted=license_mounted,
                       license_source=license_source,
                       gotcha=gotcha_for(name), autofixes=autofixes or None), None


def readme(name: str, limit: int = 6000) -> Optional[str]:
    p = readme_path(name)
    if not p.is_file():
        return None
    text = p.read_text(errors="replace")
    return text if len(text) <= limit else text[:limit] + "\n… (truncated)"


# ── config-driven port resolution ──────────────────────────────────────────────
def _config(name: str) -> dict:
    p = subprocess.run(
        ["docker", "compose", *compose_args(name), "config", "--format", "json"],
        cwd=str(workdir(name)), env=_env("latest"),
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0:
        raise RuntimeError(f"`docker compose config` failed: {p.stderr.strip()[:300]}")
    return json.loads(p.stdout)


def published_ports(name: str) -> list[int]:
    cfg = _config(name)
    ports: set[int] = set()
    for s in cfg.get("services", {}).values():
        for pt in (s.get("ports") or []):
            if pt.get("published"):
                ports.add(int(pt["published"]))
    return sorted(ports)


def detect_conflicts(name: str, ports: list[int]) -> list[dict]:
    """Ports held by any OTHER compose project (reuses apim's docker introspection)."""
    proj = project_for(name)
    conflicts = []
    for port in ports:
        holder = apim.project_holding_port(port)
        if holder and holder["project"] != proj:
            conflicts.append(holder)
    return conflicts


# ── docker compose introspection ──────────────────────────────────────────────
def compose_ps(name: str) -> list[dict]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(name), "ps", "--all", "--format", "json"],
        cwd=str(workdir(name)), env=_env("latest"),
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


def service_names(name: str) -> list[str]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(name), "config", "--services"],
        cwd=str(workdir(name)), env=_env("latest"),
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0:
        return []
    return sorted(s.strip() for s in p.stdout.splitlines() if s.strip())


def compose_logs(name: str, service: str, lines: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *compose_args(name), "logs", "--no-color",
         f"--tail={lines}", service],
        cwd=str(workdir(name)), env=_env("latest"),
        capture_output=True, text=True, timeout=60,
    )


# ── up / down lifecycle ───────────────────────────────────────────────────────
def launch_up_background(name: str, version: str, pull: bool, recreate: bool,
                         log_path: Path) -> subprocess.Popen:
    files = " ".join(shlex.quote(a) for a in compose_args(name))
    up = f"docker compose {files} up -d" + (" --force-recreate" if recreate else "")
    cmd = (f"docker compose {files} pull && {up}") if pull else up

    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd], cwd=str(workdir(name)), env=_env(version),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        fh.close()
    return proc


def run_down(name: str, timeout: int, volumes: bool = False) -> dict:
    cmd = ["docker", "compose", *compose_args(name), "down"] + (["-v"] if volumes else [])
    try:
        p = subprocess.run(cmd, cwd=str(workdir(name)), env=_env("latest"),
                           capture_output=True, text=True, timeout=timeout)
        return {"timed_out": False, "returncode": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except subprocess.TimeoutExpired as e:
        return {"timed_out": True, "returncode": None,
                "stdout": e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                "stderr": e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")}


# ── tracked up-process state (per config) ──────────────────────────────────────
@dataclass
class QuickSetupUp:
    pid: int
    log_path: str
    version: str
    name: str
    project: str
    ports: Optional[list] = None
    license: Optional[str] = None
    workdir: Optional[str] = None
    started: Optional[str] = None
    exit_code: Optional[int] = None


_procs: dict[str, subprocess.Popen] = {}
_recs: dict[str, QuickSetupUp] = {}


def record_up(proc: subprocess.Popen, name: str, version: str, ports: Optional[list],
              license_path: Optional[str], log_path: Path, started: Optional[str]) -> QuickSetupUp:
    rec = QuickSetupUp(pid=proc.pid, log_path=str(log_path), version=version, name=name,
                       project=project_for(name), ports=ports, license=license_path,
                       workdir=str(workdir(name)), started=started)
    _procs[name] = proc
    _recs[name] = rec
    _meta_path(name).write_text(json.dumps(asdict(rec), indent=2))
    return rec


def _load(name: str) -> Optional[QuickSetupUp]:
    p = _meta_path(name)
    if not p.is_file():
        return None
    try:
        return QuickSetupUp(**json.loads(p.read_text()))
    except (json.JSONDecodeError, TypeError, OSError):
        return None


def _rec_for(name: str) -> Optional[QuickSetupUp]:
    return _recs.get(name) or _load(name)


def is_up_running(name: str) -> bool:
    proc = _procs.get(name)
    if proc is not None:
        return proc.poll() is None
    rec = _rec_for(name)
    return runner.pid_alive(rec.pid) if rec else False


def up_process_status(name: str) -> dict:
    rec = _rec_for(name)
    if rec is None:
        return {"tracked": False}
    common = {"pid": rec.pid, "version": rec.version, "name": rec.name,
              "project": rec.project, "ports": rec.ports, "license": rec.license,
              "workdir": rec.workdir, "log_path": rec.log_path, "started": rec.started}
    proc = _procs.get(name)
    if proc is not None:
        code = proc.poll()
        if code is not None and rec.exit_code != code:
            rec.exit_code = code
            _meta_path(name).write_text(json.dumps(asdict(rec), indent=2))
        return {"tracked": True, "running": code is None, "exit_code": code, **common}
    running = runner.pid_alive(rec.pid)
    return {"tracked": True, "running": running,
            "exit_code": None if running else rec.exit_code, **common}


def forget_up(name: str) -> None:
    _procs.pop(name, None)
    _recs.pop(name, None)
    _meta_path(name).unlink(missing_ok=True)


def current_version(name: str) -> Optional[str]:
    rec = _rec_for(name)
    return rec.version if rec else None


def known_configs() -> list[str]:
    """Config names with a persisted up-record (i.e. started at least once)."""
    return sorted(f.stem for f in _root().glob("*.json"))
