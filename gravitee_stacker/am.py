"""Standalone Gravitee Access Management (AM) stack management.

Ships a self-contained compose (``am-compose.yml``, project ``gravitee-am``) plus
the nginx routing config (``am-nginx.conf``). Only nginx is published to the host
(``AM_NGINX_PORT``, default 8086); everything else is internal. Version is pinned
via ``GIO_AM_VERSION``. Mirrors the APIM tools' non-blocking background pattern.
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

AM_COMPOSE = Path(__file__).resolve().parent / "am-compose.yml"
AM_NGINX_CONF = Path(__file__).resolve().parent / "am-nginx.conf"
AM_PROJECT = "gravitee-am"  # fallback; real name read from the compose `name:`
AM_REPO = "https://github.com/gravitee-io/gravitee-access-management.git"
DEFAULT_NGINX_PORT = 8086


# ── paths / env ───────────────────────────────────────────────────────────────
def am_state_dir() -> Path:
    d = runner.state_dir() / "am"
    d.mkdir(parents=True, exist_ok=True)
    return d


def up_log_path() -> Path:
    return am_state_dir() / "up.log"


def _meta_path() -> Path:
    return am_state_dir() / "up.json"


def compose_file() -> Path:
    """AM_COMPOSE_FILE if it points at a real file, else the shipped am-compose.yml."""
    override = os.environ.get("AM_COMPOSE_FILE", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p.resolve()
    return AM_COMPOSE


def compose_args() -> list[str]:
    return ["-f", str(compose_file())]


def default_port() -> int:
    try:
        return int(os.environ.get("AM_NGINX_PORT", str(DEFAULT_NGINX_PORT))) or DEFAULT_NGINX_PORT
    except ValueError:
        return DEFAULT_NGINX_PORT


def _env(version: str = "latest", port: Optional[int] = None, extra: Optional[dict] = None) -> dict:
    env = runner._child_env()
    env["GIO_AM_VERSION"] = version
    env["AM_NGINX_CONF"] = str(AM_NGINX_CONF)      # stable shipped config, always set
    if port is not None:
        env["AM_NGINX_PORT"] = str(port)
    if extra:
        env.update(extra)
    return env


# ── version resolution ────────────────────────────────────────────────────────
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def resolve_version(version: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Resolve a requested version. 'latest'/None -> newest stable AM release tag."""
    if version and version.lower() != "latest":
        v = version.lstrip("v")
        return v, None
    try:
        p = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", AM_REPO],
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
def _config(extra_env: Optional[dict] = None) -> dict:
    p = subprocess.run(
        ["docker", "compose", *compose_args(), "config", "--format", "json"],
        cwd=str(am_state_dir()), env=_env("latest", default_port(), extra_env),
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0:
        raise RuntimeError(f"`docker compose config` failed: {p.stderr.strip()[:300]}")
    return json.loads(p.stdout)


def project_name() -> str:
    try:
        return _config().get("name") or AM_PROJECT
    except (RuntimeError, ValueError):
        return AM_PROJECT


def compose_ps() -> list[dict]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(), "ps", "--all", "--format", "json"],
        cwd=str(am_state_dir()), env=_env("latest", default_port()),
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
        ["docker", "compose", *compose_args(), "logs", "--no-color",
         f"--tail={lines}", service],
        cwd=str(am_state_dir()), env=_env("latest", default_port()),
        capture_output=True, text=True, timeout=60,
    )


def service_names() -> list[str]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(), "config", "--services"],
        cwd=str(am_state_dir()), env=_env("latest", default_port()),
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0:
        return []
    return sorted(s.strip() for s in p.stdout.splitlines() if s.strip())


# ── port-conflict detection ───────────────────────────────────────────────────
def project_holding_port(port: int) -> Optional[dict]:
    ids = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"publish={port}"],
        capture_output=True, text=True, timeout=15,
    )
    for cid in ids.stdout.split():
        insp = subprocess.run(
            ["docker", "inspect", "--format",
             '{{.Name}}\t{{index .Config.Labels "com.docker.compose.project"}}', cid],
            capture_output=True, text=True, timeout=15,
        )
        parts = insp.stdout.strip().split("\t")
        name = parts[0].lstrip("/") if parts else cid
        project = parts[1] if len(parts) > 1 and parts[1] else "(no compose project)"
        return {"port": port, "container": name, "project": project}
    return None


def conflict_on(port: int) -> Optional[dict]:
    """Return the conflicting holder of `port` if it's another project, else None."""
    holder = project_holding_port(port)
    if holder and holder["project"] != project_name():
        return holder
    return None


def down_project(project: str) -> dict:
    p = subprocess.run(
        ["docker", "compose", "-p", project, "down"],
        env=runner._child_env(), capture_output=True, text=True, timeout=180,
    )
    return {"project": project, "returncode": p.returncode}


# ── up / down lifecycle ───────────────────────────────────────────────────────
def launch_up_background(version: str, port: int, pull: bool, recreate: bool,
                         log_path: Path) -> subprocess.Popen:
    files = " ".join(compose_args())
    up = f"docker compose {files} up -d" + (" --force-recreate" if recreate else "")
    cmd = (f"docker compose {files} pull && {up}") if pull else up

    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=str(am_state_dir()), env=_env(version, port),
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        fh.close()
    return proc


def run_down(timeout: int) -> dict:
    try:
        p = subprocess.run(
            ["docker", "compose", *compose_args(), "down"],
            cwd=str(am_state_dir()), env=_env("latest", default_port()),
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


# ── tracked up-process state ───────────────────────────────────────────────────
@dataclass
class AmUp:
    pid: int
    log_path: str
    version: str
    port: int
    compose: Optional[str] = None
    started: Optional[str] = None
    exit_code: Optional[int] = None


_proc: Optional[subprocess.Popen] = None
_rec: Optional[AmUp] = None


def record_up(proc: subprocess.Popen, version: str, port: int, log_path: Path,
              started: Optional[str]) -> AmUp:
    global _proc, _rec
    _proc = proc
    _rec = AmUp(pid=proc.pid, log_path=str(log_path), version=version, port=port,
                compose=str(compose_file()), started=started)
    _meta_path().write_text(json.dumps(asdict(_rec), indent=2))
    return _rec


def _load() -> Optional[AmUp]:
    p = _meta_path()
    if not p.is_file():
        return None
    try:
        return AmUp(**json.loads(p.read_text()))
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
    common = {"pid": rec.pid, "version": rec.version, "port": rec.port,
              "compose": rec.compose, "log_path": rec.log_path, "started": rec.started}
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


def current_port() -> int:
    rec = _rec or _load()
    return rec.port if rec else default_port()


def current_version() -> Optional[str]:
    rec = _rec or _load()
    return rec.version if rec else None
