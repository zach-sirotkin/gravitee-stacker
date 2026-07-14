"""Subprocess + docker-compose plumbing for the Gamma demo stack.

Everything here mirrors what ``docker/run.sh`` actually does, verified against the
real script (not the README):

* Effective compose set is ``-f docker-compose.yml`` plus ``-f docker-compose.esm.yml``
  only when ``ESM_MESH`` is set. ``docker-compose.apim.yml`` is pulled in via an
  ``include:`` directive inside ``docker-compose.yml`` — it is NOT passed with ``-f``,
  so we must not add it ourselves or compose would treat it as a separate project.
* All invocations run with ``cwd = $GAMMA_STACK_DIR/docker``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

DEFAULT_STACK_DIR = "/Users/zachary.sirotkin/Documents/gravitee-gamma-modules-sdk"


# ── Paths / config ────────────────────────────────────────────────────────────
def stack_dir() -> Path:
    return Path(os.environ.get("GAMMA_STACK_DIR", DEFAULT_STACK_DIR)).expanduser()


def docker_dir() -> Path:
    """The cwd for every run.sh / docker compose call."""
    return stack_dir() / "docker"


def state_dir() -> Path:
    """Where we keep the tracked up-process metadata + captured logs (.run/).

    Deliberately kept OUT of the stack repo so it never shows up as untracked
    noise there. Defaults to ``<mcp project root>/.run`` (gitignored by this
    project); override with GAMMA_MCP_STATE_DIR.
    """
    override = os.environ.get("GAMMA_MCP_STATE_DIR")
    if override:
        d = Path(override).expanduser()
    else:
        # <project root>/.run — parent of the gamma_stack_mcp package dir.
        d = Path(__file__).resolve().parent.parent / ".run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def up_log_path() -> Path:
    return state_dir() / "up.log"


def run_sh() -> Path:
    return docker_dir() / "run.sh"


def compose_file_args() -> list[str]:
    """Replicate run.sh's compose_files array exactly."""
    args = ["-f", "docker-compose.yml"]
    if os.environ.get("ESM_MESH"):
        args += ["-f", "docker-compose.esm.yml"]
    return args


def _child_env() -> dict[str, str]:
    """Environment for child processes.

    Force UTF-8 so run.sh's unicode ✓/✘ output is captured cleanly regardless of
    the MCP client's locale, and keep everything else from the parent (so ACR
    login in the user's docker config, REGISTRY overrides, ESM_MESH, etc. all
    flow through exactly as they would in the user's own shell).
    """
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("LC_ALL", "en_US.UTF-8")
    env.setdefault("LANG", "en_US.UTF-8")
    return env


# ── Environment sanity ────────────────────────────────────────────────────────
def check_environment() -> dict:
    """Cheap pre-flight so failures read clearly instead of cryptically.

    Never raises; returns a structured report. Per the repo README the stack needs
    Docker Desktop running, an ACR login (or REGISTRY=graviteeio), and a license at
    docker/license/license.key.
    """
    problems: list[str] = []
    warnings: list[str] = []

    dd = docker_dir()
    if not dd.is_dir():
        problems.append(
            f"stack docker dir not found: {dd} "
            f"(set GAMMA_STACK_DIR; currently {stack_dir()})"
        )
    if not run_sh().is_file():
        problems.append(f"run.sh not found: {run_sh()}")

    if shutil.which("docker") is None:
        problems.append("`docker` not on PATH")
    else:
        info = subprocess.run(
            ["docker", "info"],
            cwd=str(dd) if dd.is_dir() else None,
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=20,
        )
        if info.returncode != 0:
            problems.append(
                "Docker does not appear to be running "
                "(`docker info` failed). Start Docker Desktop. "
                + (info.stderr.strip().splitlines()[-1] if info.stderr.strip() else "")
            )

    # License: only relevant to a full up; a warning, not a hard block.
    license_key = dd / "license" / "license.key"
    if dd.is_dir() and not license_key.is_file():
        warnings.append(
            f"no license found at {license_key} — the stack may not fully start. "
            "Drop a valid Gravitee license there."
        )

    # Registry hint: on the default ACR path an expired token makes run.sh hard-stop.
    registry = os.environ.get("REGISTRY", "graviteeio.azurecr.io")
    if "azurecr.io" in registry:
        warnings.append(
            f"using private registry {registry}: if `stack_up` fails at the manifest "
            "check, run `az acr login --name graviteeio` (or set REGISTRY=graviteeio "
            "in docker/.env to use the public hub)."
        )

    return {"ok": not problems, "problems": problems, "warnings": warnings}


# ── docker compose introspection ──────────────────────────────────────────────
def compose_services() -> list[str]:
    """Live, authoritative service list (honours the include: directive)."""
    p = subprocess.run(
        ["docker", "compose", *compose_file_args(), "config", "--services"],
        cwd=str(docker_dir()),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if p.returncode != 0:
        return []
    return sorted(s.strip() for s in p.stdout.splitlines() if s.strip())


def compose_ps() -> list[dict]:
    """Parse `docker compose ps` (all services, including stopped) as JSON lines."""
    import json

    p = subprocess.run(
        ["docker", "compose", *compose_file_args(), "ps", "--all", "--format", "json"],
        cwd=str(docker_dir()),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if p.returncode != 0:
        return []

    out = p.stdout.strip()
    if not out:
        return []

    rows: list[dict] = []
    # Newer compose emits one JSON object per line; older emits a single JSON array.
    try:
        parsed = json.loads(out)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def compose_logs(service: str, lines: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *compose_file_args(),
         "logs", "--no-color", f"--tail={lines}", service],
        cwd=str(docker_dir()),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )


# ── run.sh invocations ────────────────────────────────────────────────────────
def launch_background(run_sh_args: list[str], log_path: Path) -> subprocess.Popen:
    """Launch `bash run.sh <args>` detached, stdout+stderr -> log_path.

    Returns immediately with the Popen handle. We deliberately do NOT wait().
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            ["bash", str(run_sh()), *run_sh_args],
            cwd=str(docker_dir()),
            env=_child_env(),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach from the MCP server's process group
        )
    finally:
        # The child holds its own dup'd fd; we can close ours.
        log_fh.close()
    return proc


def run_foreground(run_sh_args: list[str], timeout: int) -> dict:
    """Run `bash run.sh <args>` to completion, capturing output.

    For the shorter-lived subcommands (setup / down). Returns a structured result;
    a timeout is reported, not raised.
    """
    try:
        p = subprocess.run(
            ["bash", str(run_sh()), *run_sh_args],
            cwd=str(docker_dir()),
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "timed_out": False,
            "returncode": p.returncode,
            "stdout": p.stdout,
            "stderr": p.stderr,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "timed_out": True,
            "returncode": None,
            "stdout": e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
            "stderr": e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or ""),
            "timeout": timeout,
        }


def tail_file(path: Path, lines: int) -> str:
    if not path.is_file():
        return ""
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else
    return True
