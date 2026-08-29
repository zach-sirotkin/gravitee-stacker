"""Public, self-contained Gravitee Gamma platform management.

Ships a self-contained compose (``gamma-compose.yml``) built from the CUSTOMER-FACING
docker-compose in the Gravitee docs — PUBLIC Docker Hub images (``graviteeio/*``,
``graviteeio/gamma-ui``), NO ACR, and NO license needed (Agent Management is the one
module that wants a license; the rest run without one). This is the public counterpart
to the internal SDK-repo ``stack_*`` path (which needs graviteeio.azurecr.io + run.sh).

Single instance / canonical ports (8082-8086): gateway 8082, mgmt-api 8083, APIM console
8084, portal 8085, Gamma console 8086.
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
PROJECT = "gravitee-gamma-public"
DEFAULT_VERSION = "4.12"  # gamma-ui/apim images publish on the minor tag in the docs
CANONICAL_PORTS = [8082, 8083, 8084, 8085, 8086]


# ── paths / project / env ─────────────────────────────────────────────────────
def gamma_state_dir() -> Path:
    d = runner.state_dir() / "gamma"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_for() -> str:
    return PROJECT


def up_log_path() -> Path:
    return gamma_state_dir() / "up.log"


def _meta_path() -> Path:
    return gamma_state_dir() / "up.json"


def compose_file() -> Path:
    override = os.environ.get("GAMMA_COMPOSE_FILE", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p.resolve()
    return GAMMA_COMPOSE


def compose_args(with_license: bool = False) -> list[str]:
    args = ["-p", PROJECT, "-f", str(compose_file())]
    if with_license:
        args += ["-f", str(GAMMA_LICENSE_COMPOSE)]
    return args


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


def _env(version: str = DEFAULT_VERSION, license_path: Optional[str] = None,
         extra: Optional[dict] = None) -> dict:
    env = runner._child_env()
    env["GAMMA_VERSION"] = version
    if license_path:
        env["GAMMA_LICENSE"] = license_path
    if extra:
        env.update(extra)
    return env


# ── docker compose introspection ──────────────────────────────────────────────
def compose_ps() -> list[dict]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(), "ps", "--all", "--format", "json"],
        cwd=str(gamma_state_dir()), env=_env(),
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


def compose_logs(service: str, lines: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *compose_args(), "logs", "--no-color", f"--tail={lines}", service],
        cwd=str(gamma_state_dir()), env=_env(),
        capture_output=True, text=True, timeout=60,
    )


def service_names() -> list[str]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(), "config", "--services"],
        cwd=str(gamma_state_dir()), env=_env(),
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


def detect_conflicts() -> list[dict]:
    out = []
    for port in CANONICAL_PORTS:
        holder = project_holding_port(port)
        if holder and holder["project"] != PROJECT:
            out.append(holder)
    return out


def stack_running() -> bool:
    """Whether the Gamma project has RUNNING containers (container-based, authoritative)."""
    return bool(runner.project_running_containers(PROJECT))


def down_project(project: str) -> dict:
    p = subprocess.run(["docker", "compose", "-p", project, "down"],
                       env=runner._child_env(), capture_output=True, text=True, timeout=180)
    return {"project": project, "returncode": p.returncode}


# ── up / down lifecycle ───────────────────────────────────────────────────────
def launch_up_background(version: str, pull: bool, recreate: bool, license_path: Optional[str],
                         log_path: Path) -> subprocess.Popen:
    args = compose_args(with_license=bool(license_path))
    files = " ".join(shlex.quote(a) for a in args)
    up = f"docker compose {files} up -d" + (" --force-recreate" if recreate else "")
    cmd = (f"docker compose {files} pull && {up}") if pull else up

    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd], cwd=str(gamma_state_dir()), env=_env(version, license_path),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        fh.close()
    return proc


def run_down(timeout: int, volumes: bool = False) -> dict:
    cmd = ["docker", "compose", *compose_args(), "down"] + (["-v"] if volumes else [])
    try:
        p = subprocess.run(cmd, cwd=str(gamma_state_dir()), env=_env(),
                           capture_output=True, text=True, timeout=timeout)
        return {"timed_out": False, "returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    except subprocess.TimeoutExpired as e:
        return {"timed_out": True, "returncode": None,
                "stdout": e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                "stderr": e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")}


def urls() -> dict:
    return {
        "gamma console": "http://localhost:8086 (admin/admin)",
        "APIM console": "http://localhost:8084 (admin/admin)",
        "portal": "http://localhost:8085",
        "management API": "http://localhost:8083/management",
        "gateway": "http://localhost:8082",
    }


# ── tracked up-process state ───────────────────────────────────────────────────
@dataclass
class GammaUp:
    pid: int
    log_path: str
    version: str
    project: Optional[str] = None
    compose: Optional[str] = None
    license: Optional[str] = None
    started: Optional[str] = None
    exit_code: Optional[int] = None


_proc: Optional[subprocess.Popen] = None
_rec: Optional[GammaUp] = None


def record_up(proc: subprocess.Popen, version: str, license_path: Optional[str],
              log_path: Path, started: Optional[str]) -> GammaUp:
    global _proc, _rec
    rec = GammaUp(pid=proc.pid, log_path=str(log_path), version=version, project=PROJECT,
                  compose=str(compose_file()), license=license_path, started=started)
    _proc, _rec = proc, rec
    _meta_path().write_text(json.dumps(asdict(rec), indent=2))
    return rec


def _load() -> Optional[GammaUp]:
    p = _meta_path()
    if not p.is_file():
        return None
    try:
        return GammaUp(**json.loads(p.read_text()))
    except (json.JSONDecodeError, TypeError, OSError):
        return None


def _rec_for() -> Optional[GammaUp]:
    return _rec or _load()


def is_up_running() -> bool:
    if _proc is not None:
        return _proc.poll() is None
    rec = _rec_for()
    return runner.pid_alive(rec.pid) if rec else False


def up_process_status() -> dict:
    rec = _rec_for()
    if rec is None:
        return {"tracked": False}
    common = {"pid": rec.pid, "version": rec.version, "project": rec.project,
              "compose": rec.compose, "license": rec.license,
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
    _proc, _rec = None, None
    _meta_path().unlink(missing_ok=True)


def current_version() -> Optional[str]:
    rec = _rec_for()
    return rec.version if rec else None
