"""Standalone Gravitee APIM stack management (separate from the Gamma stack).

Ships a self-contained compose (``apim-compose.yml``, project ``gravitee-apim``) on
OSS images pinned by ``APIM_VERSION``. Provides: resolve the latest release, detect
host-port conflicts with other running compose projects (and identify/down them),
and bring the stack up/down. All lifecycle mirrors the Gamma tools' non-blocking
background pattern.
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

APIM_COMPOSE = Path(__file__).resolve().parent / "apim-compose.yml"
APIM_LICENSE_COMPOSE = Path(__file__).resolve().parent / "apim-license.yml"
APIM_PROJECT = "gravitee-apim"
APIM_REPO = "https://github.com/gravitee-io/gravitee-api-management.git"
# Canonical host ports: gateway, mgmt-api, console, portal (container ports fixed).
APIM_PORTS = [8082, 8083, 8084, 8085]
DEFAULT_PORT_OFFSET = 20000

# compose env var name -> canonical host port (used to build the coexist port env)
_PORT_VARS = {
    "APIM_GATEWAY_PORT": 8082,
    "APIM_MGMT_PORT": 8083,
    "APIM_CONSOLE_PORT": 8084,
    "APIM_PORTAL_PORT": 8085,
}


# ── paths / env ───────────────────────────────────────────────────────────────
def apim_state_dir() -> Path:
    d = runner.state_dir() / "apim"
    d.mkdir(parents=True, exist_ok=True)
    return d


def up_log_path() -> Path:
    return apim_state_dir() / "up.log"


def _meta_path() -> Path:
    return apim_state_dir() / "up.json"


def compose_args(license_path: Optional[str] = None) -> list[str]:
    args = ["-f", str(APIM_COMPOSE)]
    if license_path:
        args += ["-f", str(APIM_LICENSE_COMPOSE)]
    return args


def port_offset() -> int:
    try:
        return int(os.environ.get("APIM_PORT_OFFSET", str(DEFAULT_PORT_OFFSET))) or DEFAULT_PORT_OFFSET
    except ValueError:
        return DEFAULT_PORT_OFFSET


def ports_for(coexist: bool, offset: int) -> list[int]:
    return [p + (offset if coexist else 0) for p in APIM_PORTS]


def _port_env(coexist: bool, offset: int) -> dict:
    if not coexist:
        return {}
    return {var: str(base + offset) for var, base in _PORT_VARS.items()}


def resolve_license(license_arg: str) -> tuple[Optional[str], str]:
    """Resolve the license file path. Returns (abs_path_or_None, source).

    Order: explicit arg -> APIM_LICENSE env -> the Gamma stack's license.key (a
    convenience default) -> none (OSS mode).
    """
    candidates = [
        (license_arg, "argument"),
        (os.environ.get("APIM_LICENSE", ""), "APIM_LICENSE env"),
        (str(runner.stack_dir() / "docker" / "license" / "license.key"), "Gamma stack license"),
    ]
    for raw, source in candidates:
        if not raw:
            continue
        p = Path(raw).expanduser()
        if p.is_file() and p.stat().st_size > 0:
            return str(p.resolve()), source
    return None, "none (OSS mode)"


def _env(version: Optional[str] = None, coexist: bool = False,
         offset: int = 0, license_path: Optional[str] = None) -> dict:
    env = runner._child_env()
    if version:
        env["APIM_VERSION"] = version
    env.update(_port_env(coexist, offset))
    if license_path:
        env["APIM_LICENSE"] = license_path
    return env


# ── version resolution ────────────────────────────────────────────────────────
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def resolve_version(version: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Resolve a requested version. 'latest'/None -> newest stable release tag.

    Returns (version, error). Uses `git ls-remote --tags` against the APIM repo so
    it tracks real releases without cloning or GitHub API auth.
    """
    if version and version.lower() != "latest":
        v = version.lstrip("v")
        if not _SEMVER.match(v):
            # allow rolling tags like '4' or '4.12' too, but warn via no-match is harsh;
            # accept anything non-empty and let docker pull surface a bad tag.
            return v, None
        return v, None

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
        # <sha>\trefs/tags/<tag>
        tag = line.rsplit("/", 1)[-1].strip()
        m = _SEMVER.match(tag)
        if m:
            versions.append((tuple(int(x) for x in m.groups()), tag))
    if not versions:
        return None, "no stable release tags found; pass an explicit version."
    return max(versions)[1], None


# ── docker compose introspection (APIM project) ───────────────────────────────
def compose_ps() -> list[dict]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(), "ps", "--all", "--format", "json"],
        cwd=str(apim_state_dir()), env=_env("latest"),
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
        cwd=str(apim_state_dir()), env=_env("latest"),
        capture_output=True, text=True, timeout=60,
    )


def service_names() -> list[str]:
    p = subprocess.run(
        ["docker", "compose", *compose_args(), "config", "--services"],
        cwd=str(apim_state_dir()), env=_env("latest"),
        capture_output=True, text=True, timeout=30,
    )
    if p.returncode != 0:
        return []
    return sorted(s.strip() for s in p.stdout.splitlines() if s.strip())


# ── port-conflict detection across ALL compose projects ───────────────────────
def project_holding_port(port: int) -> Optional[dict]:
    """Which container/compose-project (if any) publishes this host port."""
    ids = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"publish={port}"],
        capture_output=True, text=True, timeout=15,
    )
    for cid in ids.stdout.split():
        insp = subprocess.run(
            ["docker", "inspect", "--format",
             '{{.Name}}\t{{index .Config.Labels "com.docker.compose.project"}}\t'
             '{{index .Config.Labels "com.docker.compose.project.working_dir"}}',
             cid],
            capture_output=True, text=True, timeout=15,
        )
        parts = insp.stdout.strip().split("\t")
        name = parts[0].lstrip("/") if parts else cid
        project = parts[1] if len(parts) > 1 and parts[1] else "(no compose project)"
        workdir = parts[2] if len(parts) > 2 else ""
        return {"port": port, "container": name, "project": project, "working_dir": workdir}
    return None


def detect_conflicts(ports: list[int]) -> list[dict]:
    """Conflicts on the given ports held by projects OTHER than gravitee-apim itself."""
    conflicts = []
    for port in ports:
        holder = project_holding_port(port)
        if holder and holder["project"] != APIM_PROJECT:
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
def launch_up_background(version: str, pull: bool, recreate: bool, coexist: bool,
                         offset: int, license_path: Optional[str], log_path: Path) -> subprocess.Popen:
    files = " ".join(compose_args(license_path))
    up = f"docker compose {files} up -d" + (" --force-recreate" if recreate else "")
    cmd = (f"docker compose {files} pull && {up}") if pull else up

    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=str(apim_state_dir()),
            env=_env(version, coexist, offset, license_path),
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
            cwd=str(apim_state_dir()), env=_env("latest"),
            capture_output=True, text=True, timeout=timeout,
        )
        return {"timed_out": False, "returncode": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except subprocess.TimeoutExpired as e:
        return {"timed_out": True, "returncode": None,
                "stdout": e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                "stderr": e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")}


# ── tracked up-process state (mirrors state.py, APIM-scoped) ───────────────────
@dataclass
class ApimUp:
    pid: int
    log_path: str
    version: str
    coexist: bool = False
    offset: int = 0
    license: Optional[str] = None
    started: Optional[str] = None
    exit_code: Optional[int] = None


_proc: Optional[subprocess.Popen] = None
_rec: Optional[ApimUp] = None


def record_up(proc: subprocess.Popen, version: str, coexist: bool, offset: int,
              license_path: Optional[str], log_path: Path, started: Optional[str]) -> ApimUp:
    global _proc, _rec
    _proc = proc
    _rec = ApimUp(pid=proc.pid, log_path=str(log_path), version=version,
                  coexist=coexist, offset=(offset if coexist else 0),
                  license=license_path, started=started)
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
    common = {"pid": rec.pid, "version": rec.version, "coexist": rec.coexist,
              "offset": rec.offset, "license": rec.license,
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


def current_mode() -> dict:
    """Last up's coexist/offset (for URL/port reporting when no up-process handle)."""
    rec = _rec or _load()
    if rec is None:
        return {"coexist": False, "offset": 0}
    return {"coexist": rec.coexist, "offset": rec.offset}
