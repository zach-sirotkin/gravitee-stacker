"""Public, self-contained Gravitee Gamma platform management.

Ships a self-contained compose (``gamma-compose.yml``) built from the CUSTOMER-FACING
docker-compose in the Gravitee docs — PUBLIC Docker Hub images (``graviteeio/*``,
``graviteeio/gamma-ui``), NO ACR, and NO license needed (Agent Management is the one
module that wants a license; the rest run without one).

Supports named INSTANCES (generalized coexist): each instance gets its own compose
project (``-p gravitee-gamma-public[-<name>]``), its own data volumes, and an
auto-allocated host-port band (+20000, +40000). instance "default" uses canonical ports
8082-8086; a named instance shifts all five by the offset — the baked-in localhost console
URLs + CORS origins shift with them. So a named Gamma runs alongside a canonical standalone
APIM/AM (or another Gamma version). Container-internal ports never change.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from . import apim, runner

_HERE = Path(__file__).resolve().parent
GAMMA_COMPOSE = _HERE / "gamma-compose.yml"
GAMMA_LICENSE_COMPOSE = _HERE / "gamma-license.yml"
PROJECT_BASE = "gravitee-gamma-public"
DEFAULT_VERSION = "4.12"  # gamma-ui/apim images publish on the minor tag in the docs
DEFAULT_PORT_OFFSET = 20000
MAX_OFFSET = 40000  # 8086+40000=48086 stays a valid port; up to ~3 concurrent instances

# (compose env var, canonical host port, url role) for each published port.
_PORTS = [
    ("GAMMA_GATEWAY_PORT", 8082, "gateway"),
    ("GAMMA_MGMT_PORT", 8083, "management API"),
    ("GAMMA_CONSOLE_PORT", 8084, "console"),
    ("GAMMA_PORTAL_PORT", 8085, "portal"),
    ("GAMMA_UI_PORT", 8086, "gamma console"),
]

# Composable feature overlays (`gamma-feature-<name>.yml`), layered onto gamma-compose.yml
# with `-f`. Each adds a capability's service(s) + management-api env; ports shift with the
# instance offset (see _FEATURE_PORTS) so they coexist.
FEATURES = ("mailpit",)
_FEATURE_PORTS = {"mailpit": [("GAMMA_MAILPIT_PORT", 8027)]}  # web UI; SMTP :1025 internal


def features_dir() -> Path:
    return Path(os.environ.get("APIM_FEATURES_DIR")
                or Path.home() / ".gravitee" / "stacker-features").expanduser()


def normalize_features(features) -> list[str]:
    seen, out = set(), []
    for f in (features or []):
        f = (f or "").strip()
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def feature_compose(name: str) -> Path:
    """Overlay file for a feature. A user overlay in features_dir() wins over a bundled one."""
    ext = features_dir() / f"gamma-feature-{name}.yml"
    return ext if ext.is_file() else _HERE / f"gamma-feature-{name}.yml"


def is_known_feature(name: str) -> bool:
    return name in FEATURES or (features_dir() / f"gamma-feature-{name}.yml").is_file()


def unknown_features(features) -> list[str]:
    return [f for f in normalize_features(features) if not is_known_feature(f)]


# ── paths / project / env ─────────────────────────────────────────────────────
def gamma_state_dir() -> Path:
    d = runner.state_dir() / "gamma"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_for(instance: str = "default") -> str:
    return PROJECT_BASE if instance == "default" else f"{PROJECT_BASE}-{instance}"


def up_log_path(instance: str = "default") -> Path:
    return gamma_state_dir() / ("up.log" if instance == "default" else f"up-{instance}.log")


def _meta_path(instance: str = "default") -> Path:
    return gamma_state_dir() / ("up.json" if instance == "default" else f"{instance}.json")


def compose_file() -> Path:
    override = os.environ.get("GAMMA_COMPOSE_FILE", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p.resolve()
    return GAMMA_COMPOSE


# Services that carry Gravitee config (gravitee_* env) — the config-override view targets them.
CONFIG_SERVICES = ("gateway", "management_api")


def config_override_path(instance: str = "default") -> Path:
    """Editable docker-compose override holding this project's rendered gravitee_* values;
    auto-layered onto every compose call for the project when present (see compose_args)."""
    return runner.config_dir() / f"{project_for(instance)}.override.yml"


def compose_args(instance: str = "default", features=None, with_license: bool = False) -> list[str]:
    args = ["-p", project_for(instance), "-f", str(compose_file())]
    for f in normalize_features(features):
        args += ["-f", str(feature_compose(f))]
    if with_license:
        args += ["-f", str(GAMMA_LICENSE_COMPOSE)]
    override = config_override_path(instance)
    if override.is_file():
        args += ["-f", str(override)]
    return args


def rendered_overrides(instance: str = "default", features=None) -> dict:
    """The effective gravitee_* env per config service, fully interpolated (overrides layer)."""
    p = subprocess.run(
        ["docker", "compose", *compose_args(instance, features), "config", "--format", "json"],
        cwd=str(gamma_state_dir()), env=_env(features=features),
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0:
        raise RuntimeError(f"`docker compose config` failed: {p.stderr.strip()[:200]}")
    cfg = json.loads(p.stdout)
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


def recreate_config_services(instance: str = "default", timeout: int = 300) -> dict:
    """Force-recreate the config services (gateway + management_api) so an edited config
    override takes effect. Reads version/offset/features/license from the record."""
    rec = _rec_for(instance)
    if rec is None:
        return {"ok": False, "error": f"instance '{instance}' is not tracked/running."}
    features = normalize_features(rec.features)
    p = subprocess.run(
        ["docker", "compose", *compose_args(instance, features, with_license=bool(rec.license)),
         "up", "-d", "--force-recreate", *CONFIG_SERVICES],
        cwd=str(gamma_state_dir()), env=_env(rec.version or DEFAULT_VERSION, rec.offset or 0, rec.license, features),
        capture_output=True, text=True, timeout=timeout,
    )
    return {"ok": p.returncode == 0, "returncode": p.returncode,
            "output": ((p.stdout or "") + (p.stderr or ""))[-1500:]}


def resolve_version(version: Optional[str]) -> tuple[str, Optional[str]]:
    """Gamma tracks the published minor tag (e.g. 4.12) that gamma-ui + apim images share.
    'latest'/'' -> DEFAULT_VERSION; anything else passes through (strip a leading v)."""
    if not version or version.lower() == "latest":
        return DEFAULT_VERSION, None
    return version.lstrip("v"), None


def resolve_license(license: str = "") -> tuple[Optional[str], Optional[str]]:
    """Reuse APIM's license resolution (arg -> GAMMA_LICENSE/APIM_LICENSE env -> conventional
    path). A license is OPTIONAL for Gamma — only Agent Management needs it."""
    if license:
        p = Path(license).expanduser()
        return (str(p), f"arg ({license})") if p.is_file() else (None, None)
    env_lic = os.environ.get("GAMMA_LICENSE", "").strip()
    if env_lic and Path(env_lic).expanduser().is_file():
        return str(Path(env_lic).expanduser()), "GAMMA_LICENSE env"
    return apim.resolve_license("")


def plan_ports(offset: int, features=None) -> dict:
    """Host ports + URLs + compose env for a given offset band (incl. feature ports)."""
    ports = [base + offset for _, base, _ in _PORTS]
    port_env = {var: str(base + offset) for var, base, _ in _PORTS}
    urls = {role: base + offset for _, base, role in _PORTS}
    for f in normalize_features(features):
        for var, default in _FEATURE_PORTS.get(f, []):
            port_env[var] = str(default + offset)
            ports.append(default + offset)
            urls[f] = default + offset
    return {"ports": ports, "port_env": port_env, "urls": urls}


def default_offset() -> int:
    try:
        return int(os.environ.get("GAMMA_PORT_OFFSET", str(DEFAULT_PORT_OFFSET))) or DEFAULT_PORT_OFFSET
    except ValueError:
        return DEFAULT_PORT_OFFSET


def allocate_offset(instance: str, features=None) -> Optional[int]:
    """Pick a host-port band. A re-up keeps the instance's existing band; default -> 0
    (canonical); named -> lowest offset whose ports are free AND not already claimed by
    another tracked instance (avoids a start-up race). Probing real ports means a named
    Gamma lands on a band that a standalone APIM/AM isn't already holding."""
    existing = _rec_for(instance)
    if existing is not None:
        return existing.offset
    if instance == "default":
        return 0
    claimed = {r.offset for i in known_instances() if (r := _rec_for(i))}
    for off in range(DEFAULT_PORT_OFFSET, MAX_OFFSET + 1, DEFAULT_PORT_OFFSET):
        if off in claimed:
            continue
        if not runner.ports_in_use(plan_ports(off, features)["ports"]):
            return off
    return None


def _env(version: str = DEFAULT_VERSION, offset: int = 0, license_path: Optional[str] = None,
         features=None, extra: Optional[dict] = None) -> dict:
    env = runner._child_env()
    env["GAMMA_VERSION"] = version
    env.update(plan_ports(offset, features)["port_env"])
    if license_path:
        env["GAMMA_LICENSE"] = license_path
    if extra:
        env.update(extra)
    return env


# ── docker compose introspection ──────────────────────────────────────────────
def compose_ps(instance: str = "default", features=None) -> list[dict]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(instance, features), "ps", "--all", "--format", "json"],
        cwd=str(gamma_state_dir()), env=_env(features=features),
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


def compose_logs(service: str, lines: int, instance: str = "default", features=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *compose_args(instance, features), "logs", "--no-color", f"--tail={lines}", service],
        cwd=str(gamma_state_dir()), env=_env(features=features),
        capture_output=True, text=True, timeout=60,
    )


def service_names(instance: str = "default", features=None) -> list[str]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(instance, features), "config", "--services"],
        cwd=str(gamma_state_dir()), env=_env(features=features),
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0:
        return []
    return sorted(s.strip() for s in p.stdout.splitlines() if s.strip())


# ── port-conflict detection ───────────────────────────────────────────────────
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


def detect_conflicts(ports: list[int], instance: str = "default") -> list[dict]:
    mine = project_for(instance)
    out = []
    for port in ports:
        holder = project_holding_port(port)
        if holder and holder["project"] != mine:
            out.append(holder)
    return out


def stack_running(instance: str = "default") -> bool:
    """Whether this instance's Gamma project has RUNNING containers (authoritative)."""
    return bool(runner.project_running_containers(project_for(instance)))


def down_project(project: str) -> dict:
    p = subprocess.run(["docker", "compose", "-p", project, "down"],
                       env=runner._child_env(), capture_output=True, text=True, timeout=180)
    return {"project": project, "returncode": p.returncode}


# ── up / down lifecycle ───────────────────────────────────────────────────────
def launch_up_background(version: str, offset: int, pull: bool, recreate: bool,
                         license_path: Optional[str], log_path: Path,
                         instance: str = "default", features=None) -> subprocess.Popen:
    args = compose_args(instance, features, with_license=bool(license_path))
    files = " ".join(shlex.quote(a) for a in args)
    up = f"docker compose {files} up -d" + (" --force-recreate" if recreate else "")
    cmd = (f"docker compose {files} pull && {up}") if pull else up

    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd], cwd=str(gamma_state_dir()),
            env=_env(version, offset, license_path, features),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        fh.close()
    return proc


def run_down(timeout: int, instance: str = "default", volumes: bool = False, features=None) -> dict:
    cmd = ["docker", "compose", *compose_args(instance, features), "down"] + (["-v"] if volumes else [])
    try:
        p = subprocess.run(cmd, cwd=str(gamma_state_dir()), env=_env(features=features),
                           capture_output=True, text=True, timeout=timeout)
        return {"timed_out": False, "returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    except subprocess.TimeoutExpired as e:
        return {"timed_out": True, "returncode": None,
                "stdout": e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                "stderr": e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")}


def urls_for(offset: int, features=None) -> dict:
    p = plan_ports(offset, features)["urls"]
    out = {
        "gamma console": f"http://localhost:{p['gamma console']} (admin/admin)",
        "APIM console": f"http://localhost:{p['console']} (admin/admin)",
        "portal": f"http://localhost:{p['portal']}",
        "management API": f"http://localhost:{p['management API']}/management",
        "gateway": f"http://localhost:{p['gateway']}",
    }
    if "mailpit" in normalize_features(features) and "mailpit" in p:
        out["mailpit"] = f"http://localhost:{p['mailpit']}"
    return out


# ── tracked up-process state (per instance) ────────────────────────────────────
@dataclass
class GammaUp:
    pid: int
    log_path: str
    version: str
    offset: int = 0
    instance: str = "default"
    features: Optional[list] = None
    project: Optional[str] = None
    compose: Optional[str] = None
    license: Optional[str] = None
    started: Optional[str] = None
    exit_code: Optional[int] = None


_procs: dict[str, subprocess.Popen] = {}
_recs: dict[str, GammaUp] = {}


def record_up(proc: subprocess.Popen, version: str, offset: int, license_path: Optional[str],
              log_path: Path, started: Optional[str], instance: str = "default",
              features=None) -> GammaUp:
    rec = GammaUp(pid=proc.pid, log_path=str(log_path), version=version, offset=offset,
                  instance=instance, features=list(features or []), project=project_for(instance),
                  compose=str(compose_file()), license=license_path, started=started)
    _procs[instance] = proc
    _recs[instance] = rec
    _meta_path(instance).write_text(json.dumps(asdict(rec), indent=2))
    return rec


def _load(instance: str) -> Optional[GammaUp]:
    p = _meta_path(instance)
    if not p.is_file():
        return None
    try:
        return GammaUp(**json.loads(p.read_text()))
    except (json.JSONDecodeError, TypeError, OSError):
        return None


def _rec_for(instance: str) -> Optional[GammaUp]:
    return _recs.get(instance) or _load(instance)


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
    common = {"pid": rec.pid, "version": rec.version, "offset": rec.offset, "instance": rec.instance,
              "features": list(rec.features or []), "project": rec.project, "compose": rec.compose,
              "license": rec.license, "log_path": rec.log_path, "started": rec.started}
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


def current_offset(instance: str = "default") -> int:
    rec = _rec_for(instance)
    return rec.offset if rec else 0


def current_features(instance: str = "default") -> list[str]:
    rec = _rec_for(instance)
    return list(rec.features or []) if rec else []


def known_instances() -> list[str]:
    names = []
    for f in gamma_state_dir().glob("*.json"):
        names.append("default" if f.name == "up.json" else f.stem)
    return sorted(set(names))
