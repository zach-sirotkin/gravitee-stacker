"""Standalone Gravitee APIM stack management (separate from the Gamma stack).

Ships self-contained composes pinned by ``APIM_VERSION``:
  * variant "default" — apim-compose.yml (project gravitee-apim): OSS mongo + es +
    gateway + management-api + console + portal.
  * variant "kafka"   — apim-kafka-compose.yml (project gravitee-apim-kafka): the
    native-Kafka gateway stack (adds a KRaft broker + kafka-client; requires an EE
    license with the Kafka feature). Vendored from Gravitee's official native-kafka
    quickstart with the automation gotchas designed out.

Provides: resolve the latest release, detect host-port conflicts, and bring the
stack up/down (non-blocking background pattern).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from . import runner

_HERE = Path(__file__).resolve().parent
APIM_COMPOSE = _HERE / "apim-compose.yml"
APIM_LICENSE_COMPOSE = _HERE / "apim-license.yml"
APIM_KAFKA_COMPOSE = _HERE / "apim-kafka-compose.yml"
KAFKA_DIR = _HERE / "kafka"  # vendored certs/config/client-config
APIM_PROJECT = "gravitee-apim"  # fallback; real name is read from the compose `name:`
APIM_REPO = "https://github.com/gravitee-io/gravitee-api-management.git"
DEFAULT_PORT_OFFSET = 20000

VARIANTS = ("default", "kafka")

# compose service name -> (coexist port env var, canonical default host port, url role)
_ROLE = {
    "apim-gateway":        ("APIM_GATEWAY_PORT", 8082, "gateway"),
    "apim-management-api": ("APIM_MGMT_PORT",    8083, "management API"),
    "apim-console":        ("APIM_CONSOLE_PORT", 8084, "console"),
    "apim-portal":         ("APIM_PORTAL_PORT",  8085, "portal"),
}


def requires_license(variant: str) -> bool:
    """The Kafka gateway won't bind its :9092 listener without an EE Kafka license."""
    return variant == "kafka"


# ── paths / env ───────────────────────────────────────────────────────────────
def apim_state_dir() -> Path:
    d = runner.state_dir() / "apim"
    d.mkdir(parents=True, exist_ok=True)
    return d


def up_log_path() -> Path:
    return apim_state_dir() / "up.log"


def _meta_path() -> Path:
    return apim_state_dir() / "up.json"


def compose_file(variant: str = "default") -> Path:
    """The compose to use for the variant. For "default", APIM_COMPOSE_FILE overrides."""
    if variant == "kafka":
        return APIM_KAFKA_COMPOSE
    override = os.environ.get("APIM_COMPOSE_FILE", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p.resolve()
    return APIM_COMPOSE


def compose_args(license_path: Optional[str] = None, variant: str = "default") -> list[str]:
    args = ["-f", str(compose_file(variant))]
    # The kafka compose mounts the license directly; only "default" uses the overlay.
    if variant == "default" and license_path:
        args += ["-f", str(APIM_LICENSE_COMPOSE)]
    return args


def port_offset() -> int:
    try:
        return int(os.environ.get("APIM_PORT_OFFSET", str(DEFAULT_PORT_OFFSET))) or DEFAULT_PORT_OFFSET
    except ValueError:
        return DEFAULT_PORT_OFFSET


# The conventional place to drop a license so `apim_up` finds it with no arg/env.
DEFAULT_LICENSE_PATH = Path.home() / ".gravitee" / "license.key"


def resolve_license(license_arg: str) -> tuple[Optional[str], str]:
    """Resolve the license file path. Returns (abs_path_or_None, source).

    Order: explicit arg -> APIM_LICENSE env -> ~/.gravitee/license.key -> none.
    A candidate is used only if it's a real, non-empty file.
    """
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
         variant: str = "default", license_path: Optional[str] = None) -> dict:
    env = runner._child_env()
    env["APIM_VERSION"] = version
    if variant == "kafka":
        # Absolute paths to the vendored assets the kafka compose bind-mounts.
        env["KAFKA_SSL_DIR"] = str(KAFKA_DIR / "ssl")
        env["KAFKA_SERVER_PROPS"] = str(KAFKA_DIR / "config" / "server.properties")
        env["KAFKA_CLIENT_CONFIG_DIR"] = str(KAFKA_DIR / "client-config")
        # ${APIM_LICENSE} must render so `docker compose config/ps/down` can parse the
        # mount; real license at up-time, a harmless existing file otherwise.
        env["APIM_LICENSE"] = license_path or str(KAFKA_DIR / "ssl" / "kafka_server_jaas.conf")
    if license_path:
        env["APIM_LICENSE"] = license_path
    if extra:
        env.update(extra)
    return env


# ── config-driven port/URL resolution (follows the actual compose file) ────────
def _config(extra_env: Optional[dict] = None, variant: str = "default") -> dict:
    p = subprocess.run(
        ["docker", "compose", *compose_args(variant=variant), "config", "--format", "json"],
        cwd=str(apim_state_dir()), env=_env("latest", extra_env, variant),
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


def project_name(variant: str = "default") -> str:
    """The compose project name (from the file's `name:`), for conflict exclusion."""
    fallback = "gravitee-apim-kafka" if variant == "kafka" else APIM_PROJECT
    try:
        return _config(variant=variant).get("name") or fallback
    except (RuntimeError, ValueError):
        return fallback


def plan_ports(coexist: bool, offset: int, variant: str = "default") -> dict:
    """Resolve effective published ports + URLs by reading the ACTUAL compose.

    Returns {port_env, ports, urls}. In coexist mode each known service's canonical
    port (from the file) is shifted by `offset` via the APIM_*_PORT env vars.
    """
    base = _service_ports(_config(variant=variant))
    port_env = {}
    if coexist:
        for svc, (var, default, _role) in _ROLE.items():
            canon = base.get(svc, [default])[0]
            port_env[var] = str(canon + offset)
    eff = _service_ports(_config(port_env, variant))
    ports = sorted({p for lst in eff.values() for p in lst})
    urls = {role: eff[svc][0] for svc, (_v, _d, role) in _ROLE.items() if svc in eff}
    return {"port_env": port_env, "ports": ports, "urls": urls}


# ── version resolution ────────────────────────────────────────────────────────
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def resolve_version(version: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Resolve a requested version. 'latest'/None -> newest stable release tag."""
    if version and version.lower() != "latest":
        return version.lstrip("v"), None
    try:
        p = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", APIM_REPO],
            capture_output=True, text=True, timeout=30,
        )
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
def compose_ps(variant: str = "default") -> list[dict]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(variant=variant), "ps", "--all", "--format", "json"],
        cwd=str(apim_state_dir()), env=_env("latest", variant=variant),
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


def compose_logs(service: str, lines: int, variant: str = "default") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *compose_args(variant=variant), "logs", "--no-color",
         f"--tail={lines}", service],
        cwd=str(apim_state_dir()), env=_env("latest", variant=variant),
        capture_output=True, text=True, timeout=60,
    )


def service_names(variant: str = "default") -> list[str]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(variant=variant), "config", "--services"],
        cwd=str(apim_state_dir()), env=_env("latest", variant=variant),
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0:
        return []
    return sorted(s.strip() for s in p.stdout.splitlines() if s.strip())


# ── port-conflict detection across ALL compose projects ───────────────────────
def project_holding_port(port: int) -> Optional[dict]:
    ids = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"publish={port}"],
        capture_output=True, text=True, timeout=15,
    )
    for cid in ids.stdout.split():
        insp = subprocess.run(
            ["docker", "inspect", "--format",
             '{{.Name}}\t{{index .Config.Labels "com.docker.compose.project"}}\t'
             '{{index .Config.Labels "com.docker.compose.project.working_dir"}}', cid],
            capture_output=True, text=True, timeout=15,
        )
        parts = insp.stdout.strip().split("\t")
        name = parts[0].lstrip("/") if parts else cid
        project = parts[1] if len(parts) > 1 and parts[1] else "(no compose project)"
        workdir = parts[2] if len(parts) > 2 else ""
        return {"port": port, "container": name, "project": project, "working_dir": workdir}
    return None


def detect_conflicts(ports: list[int], variant: str = "default") -> list[dict]:
    """Conflicts on the given ports held by projects OTHER than this APIM project."""
    proj = project_name(variant)
    conflicts = []
    for port in ports:
        holder = project_holding_port(port)
        if holder and holder["project"] != proj:
            conflicts.append(holder)
    return conflicts


def down_project(project: str) -> dict:
    """`docker compose -p <project> down` (NO -v — data volumes preserved)."""
    p = subprocess.run(
        ["docker", "compose", "-p", project, "down"],
        env=runner._child_env(), capture_output=True, text=True, timeout=180,
    )
    return {"project": project, "returncode": p.returncode,
            "output": (p.stdout or "") + (p.stderr or "")}


# ── up / down lifecycle ───────────────────────────────────────────────────────
def launch_up_background(version: str, pull: bool, recreate: bool, port_env: dict,
                         license_path: Optional[str], log_path: Path,
                         variant: str = "default") -> subprocess.Popen:
    files = " ".join(compose_args(license_path, variant))
    up = f"docker compose {files} up -d" + (" --force-recreate" if recreate else "")
    cmd = (f"docker compose {files} pull && {up}") if pull else up

    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=str(apim_state_dir()),
            env=_env(version, port_env, variant, license_path),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        fh.close()
    return proc


def run_down(timeout: int, variant: str = "default") -> dict:
    try:
        p = subprocess.run(
            ["docker", "compose", *compose_args(variant=variant), "down"],
            cwd=str(apim_state_dir()), env=_env("latest", variant=variant),
            capture_output=True, text=True, timeout=timeout,
        )
        return {"timed_out": False, "returncode": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except subprocess.TimeoutExpired as e:
        return {"timed_out": True, "returncode": None,
                "stdout": e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                "stderr": e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")}


# ── tracked up-process state ───────────────────────────────────────────────────
@dataclass
class ApimUp:
    pid: int
    log_path: str
    version: str
    variant: str = "default"
    coexist: bool = False
    offset: int = 0
    license: Optional[str] = None
    urls: Optional[dict] = None
    ports: Optional[list] = None
    compose: Optional[str] = None
    started: Optional[str] = None
    exit_code: Optional[int] = None


_proc: Optional[subprocess.Popen] = None
_rec: Optional[ApimUp] = None


def record_up(proc: subprocess.Popen, version: str, variant: str, coexist: bool, offset: int,
              license_path: Optional[str], urls: Optional[dict], ports: Optional[list],
              log_path: Path, started: Optional[str]) -> ApimUp:
    global _proc, _rec
    _proc = proc
    _rec = ApimUp(pid=proc.pid, log_path=str(log_path), version=version, variant=variant,
                  coexist=coexist, offset=(offset if coexist else 0),
                  license=license_path, urls=urls, ports=ports,
                  compose=str(compose_file(variant)), started=started)
    _meta_path().write_text(json.dumps(asdict(_rec), indent=2))
    return _rec


def _load() -> Optional[ApimUp]:
    p = _meta_path()
    if not p.is_file():
        return None
    try:
        return ApimUp(**json.loads(p.read_text()))
    except (json.JSONDecodeError, TypeError, OSError):
        return None


def is_up_running() -> bool:
    if _proc is not None:
        return _proc.poll() is None
    rec = _rec or _load()
    return runner.pid_alive(rec.pid) if rec else False


def up_process_status() -> dict:
    global _rec
    rec = _rec or _load()
    if rec is None:
        return {"tracked": False}
    common = {"pid": rec.pid, "version": rec.version, "variant": rec.variant,
              "coexist": rec.coexist, "offset": rec.offset, "license": rec.license,
              "urls": rec.urls, "ports": rec.ports, "compose": rec.compose,
              "log_path": rec.log_path, "started": rec.started}
    if _proc is not None:
        code = _proc.poll()
        if code is not None and rec.exit_code != code:
            rec.exit_code = code
            _meta_path().write_text(json.dumps(asdict(rec), indent=2))
        return {"tracked": True, "running": code is None, "exit_code": code, **common}
    running = runner.pid_alive(rec.pid)
    return {"tracked": True, "running": running,
            "exit_code": None if running else rec.exit_code, **common}


def forget_up() -> None:
    global _proc, _rec
    _proc = None
    _rec = None
    _meta_path().unlink(missing_ok=True)


def current_version() -> Optional[str]:
    rec = _rec or _load()
    return rec.version if rec else None


def current_variant() -> str:
    rec = _rec or _load()
    return (rec.variant if rec else None) or "default"


def current_mode() -> dict:
    rec = _rec or _load()
    if rec is None:
        return {"coexist": False, "offset": 0}
    return {"coexist": rec.coexist, "offset": rec.offset}
