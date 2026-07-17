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

# compose service name -> (coexist port env var, canonical default host port, url role)
_ROLE = {
    "apim-gateway":        ("APIM_GATEWAY_PORT", 8082, "gateway"),
    "apim-management-api": ("APIM_MGMT_PORT",    8083, "management API"),
    "apim-console":        ("APIM_CONSOLE_PORT", 8084, "console"),
    "apim-portal":         ("APIM_PORTAL_PORT",  8085, "portal"),
}


def requires_license(variant: str) -> bool:
    return variant == "kafka"


def supports_instances(variant: str) -> bool:
    """The kafka variant is single-instance (fixed *.kafka.local cert + broker ports)."""
    return variant != "kafka"


# ── paths / project / env ─────────────────────────────────────────────────────
def apim_state_dir() -> Path:
    d = runner.state_dir() / "apim"
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


def compose_args(variant: str = "default", instance: str = "default",
                 license_path: Optional[str] = None) -> list[str]:
    args = ["-p", project_for(variant, instance), "-f", str(compose_file(variant))]
    if variant == "default" and license_path:
        args += ["-f", str(APIM_LICENSE_COMPOSE)]
    return args


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
         variant: str = "default", license_path: Optional[str] = None) -> dict:
    env = runner._child_env()
    env["APIM_VERSION"] = version
    if variant == "kafka":
        env["KAFKA_SSL_DIR"] = str(KAFKA_DIR / "ssl")
        env["KAFKA_SERVER_PROPS"] = str(KAFKA_DIR / "config" / "server.properties")
        env["KAFKA_CLIENT_CONFIG_DIR"] = str(KAFKA_DIR / "client-config")
        env["APIM_LICENSE"] = license_path or str(KAFKA_DIR / "ssl" / "kafka_server_jaas.conf")
    if license_path:
        env["APIM_LICENSE"] = license_path
    if extra:
        env.update(extra)
    return env


# ── config-driven port/URL resolution ─────────────────────────────────────────
def _config(extra_env: Optional[dict] = None, variant: str = "default",
            instance: str = "default") -> dict:
    p = subprocess.run(
        ["docker", "compose", *compose_args(variant, instance), "config", "--format", "json"],
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


def project_name(variant: str = "default", instance: str = "default") -> str:
    """The effective compose project name (with -p it's deterministic)."""
    return project_for(variant, instance)


def plan_ports(offset: int, variant: str = "default") -> dict:
    """Effective published ports + URLs for the given host-port `offset` (0 = canonical).

    Ports don't depend on the instance (only the offset), so this is instance-free.
    """
    base = _service_ports(_config(variant=variant))
    port_env = {}
    if offset:
        for svc, (var, default, _role) in _ROLE.items():
            port_env[var] = str(base.get(svc, [default])[0] + offset)
    eff = _service_ports(_config(port_env, variant))
    ports = sorted({p for lst in eff.values() for p in lst})
    urls = {role: eff[svc][0] for svc, (_v, _d, role) in _ROLE.items() if svc in eff}
    return {"port_env": port_env, "ports": ports, "urls": urls}


def allocate_offset(variant: str, instance: str) -> Optional[int]:
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
            ports = plan_ports(off, variant)["ports"]
        except (RuntimeError, ValueError):
            return None
        if not runner.ports_in_use(ports):
            return off
    return None


# ── version resolution ────────────────────────────────────────────────────────
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


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
def compose_ps(variant: str = "default", instance: str = "default") -> list[dict]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(variant, instance), "ps", "--all", "--format", "json"],
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


def compose_logs(service: str, lines: int, variant: str = "default",
                 instance: str = "default") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *compose_args(variant, instance), "logs", "--no-color",
         f"--tail={lines}", service],
        cwd=str(apim_state_dir()), env=_env("latest", variant=variant),
        capture_output=True, text=True, timeout=60,
    )


def service_names(variant: str = "default", instance: str = "default") -> list[str]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(variant, instance), "config", "--services"],
        cwd=str(apim_state_dir()), env=_env("latest", variant=variant),
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


# ── up / down lifecycle ───────────────────────────────────────────────────────
def launch_up_background(version: str, pull: bool, recreate: bool, port_env: dict,
                         license_path: Optional[str], log_path: Path,
                         variant: str = "default", instance: str = "default") -> subprocess.Popen:
    files = " ".join(shlex.quote(a) for a in compose_args(variant, instance, license_path))
    up = f"docker compose {files} up -d" + (" --force-recreate" if recreate else "")
    cmd = (f"docker compose {files} pull && {up}") if pull else up

    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd], cwd=str(apim_state_dir()),
            env=_env(version, port_env, variant, license_path),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        fh.close()
    return proc


def run_down(timeout: int, variant: str = "default", instance: str = "default") -> dict:
    try:
        p = subprocess.run(
            ["docker", "compose", *compose_args(variant, instance), "down"],
            cwd=str(apim_state_dir()), env=_env("latest", variant=variant),
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
              log_path: Path, started: Optional[str]) -> ApimUp:
    rec = ApimUp(pid=proc.pid, log_path=str(log_path), version=version, instance=instance,
                 variant=variant, coexist=(offset > 0), offset=offset, license=license_path,
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


def current_offset(instance: str = "default") -> int:
    rec = _rec_for(instance)
    return rec.offset if rec else 0


def known_instances() -> list[str]:
    """Instance names with a persisted record ('up.json' -> 'default')."""
    names = []
    for f in apim_state_dir().glob("*.json"):
        names.append("default" if f.name == "up.json" else f.stem)
    return sorted(set(names))
