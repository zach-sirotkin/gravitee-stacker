"""Standalone Gravitee Access Management (AM) stack management.

Ships a self-contained compose (``am-compose.yml``) + nginx routing (``am-nginx.conf``).
Only nginx is published to the host; version via ``GIO_AM_VERSION``.

Supports named INSTANCES (generalized coexist): each instance gets its own compose
project (via ``docker compose -p``), its own data volume, and its own host port.
Instance "default" keeps project ``gravitee-am`` on ``AM_NGINX_PORT`` (8086); a named
instance uses project ``gravitee-am-<name>`` on an auto-allocated free port.
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
AM_COMPOSE = _HERE / "am-compose.yml"
AM_NGINX_CONF = _HERE / "am-nginx.conf"
AM_REPO = "https://github.com/gravitee-io/gravitee-access-management.git"
DEFAULT_NGINX_PORT = 8086

# Composable feature overlays (`am-feature-<name>.yml`), layered onto am-compose.yml with
# `-f`. AM has no offset bands (one nginx port), so a feature host port shifts by the
# nginx-port delta from the canonical 8086 — keeping coexisting instances conflict-free.
FEATURES = ("mailpit",)
_FEATURE_PORTS = {"mailpit": [("AM_MAILPIT_PORT", 8029)]}  # web UI; SMTP :1025 internal


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
    ext = features_dir() / f"am-feature-{name}.yml"
    return ext if ext.is_file() else _HERE / f"am-feature-{name}.yml"


def is_known_feature(name: str) -> bool:
    return name in FEATURES or (features_dir() / f"am-feature-{name}.yml").is_file()


def unknown_features(features) -> list[str]:
    return [f for f in normalize_features(features) if not is_known_feature(f)]


def feature_port_env(features, nginx_port: int) -> dict:
    """Env vars for feature host ports, shifted by the nginx-port delta from canonical."""
    shift = nginx_port - DEFAULT_NGINX_PORT
    return {var: str(default + shift)
            for f in normalize_features(features) for var, default in _FEATURE_PORTS.get(f, [])}


def feature_host_ports(features, nginx_port: int) -> list[int]:
    """Feature host ports (for conflict detection), shifted like feature_port_env."""
    shift = nginx_port - DEFAULT_NGINX_PORT
    return [default + shift
            for f in normalize_features(features) for _, default in _FEATURE_PORTS.get(f, [])]


# ── paths / project / env ─────────────────────────────────────────────────────
def am_state_dir() -> Path:
    d = runner.state_dir() / "am"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_for(instance: str = "default") -> str:
    return "gravitee-am" if instance == "default" else f"gravitee-am-{instance}"


def up_log_path(instance: str = "default") -> Path:
    return am_state_dir() / ("up.log" if instance == "default" else f"up-{instance}.log")


def _meta_path(instance: str = "default") -> Path:
    return am_state_dir() / ("up.json" if instance == "default" else f"{instance}.json")


def compose_file() -> Path:
    override = os.environ.get("AM_COMPOSE_FILE", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p.resolve()
    return AM_COMPOSE


# Services that carry Gravitee config (gravitee_* env) — the config-override view targets them.
CONFIG_SERVICES = ("gateway", "management")


def config_override_path(instance: str = "default") -> Path:
    """Editable docker-compose override holding this project's rendered gravitee_* values;
    auto-layered onto every compose call for the project when present (see compose_args)."""
    return runner.config_dir() / f"{project_for(instance)}.override.yml"


def compose_args(instance: str = "default", features=None) -> list[str]:
    args = ["-p", project_for(instance), "-f", str(compose_file())]
    for f in normalize_features(features):
        args += ["-f", str(feature_compose(f))]
    override = config_override_path(instance)
    if override.is_file():
        args += ["-f", str(override)]
    return args


def rendered_overrides(instance: str = "default", features=None) -> dict:
    """The effective gravitee_* env per config service, fully interpolated (overrides layer)."""
    p = subprocess.run(
        ["docker", "compose", *compose_args(instance, features), "config", "--format", "json"],
        cwd=str(am_state_dir()), env=_env("latest", default_port(), features),
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


def rendered_compose(instance: str = "default") -> Optional[str]:
    """The FULL rendered compose (`docker compose config` YAML) for a tracked instance — every
    service, image, port, volume, network + all env, fully interpolated. Reads the record."""
    rec = _rec_for(instance)
    version = rec.version if rec else "latest"
    port = rec.port if rec else default_port()
    features = normalize_features(rec.features) if rec else None
    p = subprocess.run(
        ["docker", "compose", *compose_args(instance, features), "config"],
        cwd=str(am_state_dir()), env=_env(version, port, features),
        capture_output=True, text=True, timeout=30,
    )
    return p.stdout if p.returncode == 0 else None


def recreate_config_services(instance: str = "default", timeout: int = 300) -> dict:
    """Force-recreate the config services (gateway + management) of a tracked instance so an
    edited config override takes effect. Reads version/features/port from the record."""
    rec = _rec_for(instance)
    if rec is None:
        return {"ok": False, "error": f"instance '{instance}' is not tracked/running."}
    features = normalize_features(rec.features)
    p = subprocess.run(
        ["docker", "compose", *compose_args(instance, features), "up", "-d", "--force-recreate", *CONFIG_SERVICES],
        cwd=str(am_state_dir()), env=_env(rec.version or "latest", rec.port, features),
        capture_output=True, text=True, timeout=timeout,
    )
    return {"ok": p.returncode == 0, "returncode": p.returncode,
            "output": ((p.stdout or "") + (p.stderr or ""))[-1500:]}


def default_port() -> int:
    try:
        return int(os.environ.get("AM_NGINX_PORT", str(DEFAULT_NGINX_PORT))) or DEFAULT_NGINX_PORT
    except ValueError:
        return DEFAULT_NGINX_PORT


def allocate_port(instance: str, requested: int = 0) -> Optional[int]:
    """Pick the host port. Explicit `requested` wins; a re-up keeps the instance's
    existing port; default -> AM_NGINX_PORT; named -> lowest port that's free AND not
    already claimed by another tracked instance (avoids a start-up allocation race)."""
    if requested:
        return requested
    existing = _rec_for(instance)
    if existing is not None:
        return existing.port
    if instance == "default":
        return default_port()
    claimed = {r.port for i in known_instances() if (r := _rec_for(i))}
    start = default_port() + 1
    for p in range(start, start + 60):
        if p not in claimed and not runner.ports_in_use([p]):
            return p
    return None


def _env(version: str = "latest", port: Optional[int] = None, features=None,
         extra: Optional[dict] = None) -> dict:
    env = runner._child_env()
    env["GIO_AM_VERSION"] = version
    env["AM_NGINX_CONF"] = str(AM_NGINX_CONF)
    if port is not None:
        env["AM_NGINX_PORT"] = str(port)
    env.update(feature_port_env(features, port if port is not None else DEFAULT_NGINX_PORT))
    if extra:
        env.update(extra)
    return env


# ── version resolution ────────────────────────────────────────────────────────
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def resolve_version(version: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if version and version.lower() != "latest":
        return version.lstrip("v"), None
    try:
        p = subprocess.run(["git", "ls-remote", "--tags", "--refs", AM_REPO],
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
def compose_ps(instance: str = "default", features=None) -> list[dict]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(instance, features), "ps", "--all", "--format", "json"],
        cwd=str(am_state_dir()), env=_env("latest", default_port(), features),
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
        ["docker", "compose", *compose_args(instance, features), "logs", "--no-color",
         f"--tail={lines}", service],
        cwd=str(am_state_dir()), env=_env("latest", default_port(), features),
        capture_output=True, text=True, timeout=60,
    )


def service_names(instance: str = "default", features=None) -> list[str]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(instance, features), "config", "--services"],
        cwd=str(am_state_dir()), env=_env("latest", default_port(), features),
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


def conflict_on(port: int, instance: str = "default") -> Optional[dict]:
    holder = project_holding_port(port)
    if holder and holder["project"] != project_for(instance):
        return holder
    return None


def stack_running(instance: str = "default") -> bool:
    """Whether this instance's compose project has RUNNING containers (container-based,
    not launcher-PID-based) — the safe 'is it already up?' check for preflight/up."""
    return bool(runner.project_running_containers(project_for(instance)))


def down_project(project: str) -> dict:
    p = subprocess.run(["docker", "compose", "-p", project, "down"],
                       env=runner._child_env(), capture_output=True, text=True, timeout=180)
    return {"project": project, "returncode": p.returncode}


# ── up / down lifecycle ───────────────────────────────────────────────────────
def launch_up_background(version: str, port: int, pull: bool, recreate: bool,
                         log_path: Path, instance: str = "default", features=None) -> subprocess.Popen:
    files = " ".join(shlex.quote(a) for a in compose_args(instance, features))
    up = f"docker compose {files} up -d" + (" --force-recreate" if recreate else "")
    cmd = (f"docker compose {files} pull && {up}") if pull else up

    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd], cwd=str(am_state_dir()), env=_env(version, port, features),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        fh.close()
    return proc


def run_down(timeout: int, instance: str = "default", features=None) -> dict:
    try:
        p = subprocess.run(
            ["docker", "compose", *compose_args(instance, features), "down"],
            cwd=str(am_state_dir()), env=_env("latest", default_port(), features),
            capture_output=True, text=True, timeout=timeout,
        )
        return {"timed_out": False, "returncode": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except subprocess.TimeoutExpired as e:
        return {"timed_out": True, "returncode": None,
                "stdout": e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                "stderr": e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")}


def urls_for(port: int) -> dict:
    base = f"http://localhost:{port}"
    return {
        "console": f"{base}/am/ui/ (admin/adminadmin)",
        "management API": f"{base}/am/management/",
        "gateway": f"{base}/am/",
    }


# ── tracked up-process state (per instance) ────────────────────────────────────
@dataclass
class AmUp:
    pid: int
    log_path: str
    version: str
    port: int
    instance: str = "default"
    features: Optional[list] = None
    project: Optional[str] = None
    compose: Optional[str] = None
    started: Optional[str] = None
    exit_code: Optional[int] = None


_procs: dict[str, subprocess.Popen] = {}
_recs: dict[str, AmUp] = {}


def record_up(proc: subprocess.Popen, version: str, port: int, instance: str,
              log_path: Path, started: Optional[str], features=None) -> AmUp:
    rec = AmUp(pid=proc.pid, log_path=str(log_path), version=version, port=port,
               instance=instance, features=list(features or []), project=project_for(instance),
               compose=str(compose_file()), started=started)
    _procs[instance] = proc
    _recs[instance] = rec
    _meta_path(instance).write_text(json.dumps(asdict(rec), indent=2))
    return rec


def _load(instance: str) -> Optional[AmUp]:
    p = _meta_path(instance)
    if not p.is_file():
        return None
    try:
        return AmUp(**json.loads(p.read_text()))
    except (json.JSONDecodeError, TypeError, OSError):
        return None


def _rec_for(instance: str) -> Optional[AmUp]:
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
    common = {"pid": rec.pid, "version": rec.version, "port": rec.port, "instance": rec.instance,
              "features": list(rec.features or []), "project": rec.project, "compose": rec.compose,
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


def current_port(instance: str = "default") -> int:
    rec = _rec_for(instance)
    return rec.port if rec else default_port()


def current_version(instance: str = "default") -> Optional[str]:
    rec = _rec_for(instance)
    return rec.version if rec else None


def current_features(instance: str = "default") -> list[str]:
    rec = _rec_for(instance)
    return list(rec.features or []) if rec else []


def known_instances() -> list[str]:
    names = []
    for f in am_state_dir().glob("*.json"):
        names.append("default" if f.name == "up.json" else f.stem)
    return sorted(set(names))
