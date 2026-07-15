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


DEFAULT_PORT_OFFSET = 20000


def port_offset() -> int:
    """Host-port shift for the remap overlay. 0 disables remap (pure run.sh path)."""
    try:
        return int(os.environ.get("GAMMA_PORT_OFFSET", str(DEFAULT_PORT_OFFSET)))
    except ValueError:
        return DEFAULT_PORT_OFFSET


def port_keep() -> set[str]:
    """Services to leave on their original host ports (e.g. keep nginx on :80)."""
    raw = os.environ.get("GAMMA_PORT_KEEP", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


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


def compose_config_json() -> dict:
    """Merged, canonical compose config (honours include: + .env substitution).

    Used to read the real published ports and resolved image names — so the remap
    overlay tracks whatever the compose files actually publish (drift-proof).
    """
    import json

    p = subprocess.run(
        ["docker", "compose", *compose_file_args(), "config", "--format", "json"],
        cwd=str(docker_dir()),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if p.returncode != 0:
        raise RuntimeError(f"`docker compose config` failed: {p.stderr.strip()[:400]}")
    return json.loads(p.stdout)


def generate_ports_override(offset: int, keep: set[str]) -> tuple[Path, list[dict]]:
    """Write a compose overlay that shifts every published host port by `offset`.

    Returns (overlay_path, mapping) where mapping is a list of
    {service, container, protocol, old_host, new_host}. Uses the `!override` YAML
    tag so the overlay REPLACES each service's ports list instead of appending to
    it (compose merges `ports:` by default, which would leave the original port
    still bound). Services in `keep` are left untouched.
    """
    cfg = compose_config_json()
    services = cfg.get("services", {})

    mapping: list[dict] = []
    blocks: list[str] = []
    for name in sorted(services):
        if name in keep:
            continue
        ports = services[name].get("ports") or []
        entries = []
        for pt in ports:
            published = pt.get("published")
            target = pt.get("target")
            if published in (None, "") or target is None:
                continue
            proto = pt.get("protocol", "tcp")
            old_host = int(published)
            new_host = old_host + offset
            if new_host > 65535:
                raise ValueError(
                    f"remapped port {new_host} for {name} exceeds 65535 "
                    f"(offset={offset}); lower GAMMA_PORT_OFFSET."
                )
            entries.append((new_host, int(target), proto))
            mapping.append({
                "service": name, "container": int(target), "protocol": proto,
                "old_host": old_host, "new_host": new_host,
            })
        if entries:
            lines = [f"  {name}:", "    ports: !override"]
            for new_host, target, proto in entries:
                lines += [
                    f"      - target: {target}",
                    f"        published: \"{new_host}\"",
                    f"        protocol: {proto}",
                ]
            blocks.append("\n".join(lines))

    header = (
        "# GENERATED by gamma-stack-mcp — do not edit; regenerated on each stack_up.\n"
        f"# Host ports shifted by offset={offset}. keep={sorted(keep) or '[]'}.\n"
        "services:\n"
    )
    path = state_dir() / "ports.override.yml"
    path.write_text(header + "\n".join(blocks) + "\n")
    return path, mapping


def published_host_ports(cfg: Optional[dict] = None) -> list[int]:
    """The stack's original published host ports (for the default-mode preflight)."""
    cfg = cfg if cfg is not None else compose_config_json()
    ports: set[int] = set()
    for s in cfg.get("services", {}).values():
        for pt in (s.get("ports") or []):
            if pt.get("published"):
                ports.add(int(pt["published"]))
    return sorted(ports)


def acr_probe(cfg: dict) -> Optional[str]:
    """Mirror run.sh's registry-auth check: probe a real ACR image manifest.

    Returns an error string if the private registry is unreachable/expired, else
    None. Uses the resolved image from the merged config so REGISTRY/AM_VERSION
    overrides in ./.env are respected. No-op on the public hub.
    """
    image = (cfg.get("services", {}).get("management", {}) or {}).get("image", "")
    if "azurecr.io" not in image:
        return None
    r = subprocess.run(
        ["docker", "manifest", "inspect", image],
        cwd=str(docker_dir()), env=_child_env(),
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        return (
            f"cannot reach {image} — not logged in to the private registry, or the "
            "token has expired. Run `az acr login --name graviteeio` (or set "
            "REGISTRY=graviteeio in docker/.env to use the public hub)."
        )
    return None


def ports_in_use(ports: list[int]) -> list[int]:
    """Which of these host ports currently have a LISTEN socket (like run.sh's preflight)."""
    busy = []
    for p in ports:
        r = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{p}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            busy.append(p)
    return busy


def launch_up_compose_background(extra_files: list[str], pull: bool, log_path: Path) -> subprocess.Popen:
    """Background `docker compose <files> [pull &&] up -d` with the remap overlay.

    Mirrors run.sh's pull + `up -d` (the two orchestration steps that actually bind
    ports), detached, output -> log_path. We do NOT run run.sh's blocking health
    poll here — stack_status polls health instead (the whole non-blocking design).
    """
    files = compose_file_args() + extra_files
    fileargs = " ".join(_shquote(a) for a in files)
    if pull:
        cmd = f"docker compose {fileargs} pull && docker compose {fileargs} up -d"
    else:
        cmd = f"docker compose {fileargs} up -d"

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=str(docker_dir()),
            env=_child_env(),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        log_fh.close()
    return proc


def _shquote(s: str) -> str:
    import shlex
    return shlex.quote(s)


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


def run_foreground(run_sh_args: list[str], timeout: int,
                   extra_env: Optional[dict[str, str]] = None) -> dict:
    """Run `bash run.sh <args>` to completion, capturing output.

    For the shorter-lived subcommands (setup / down). `extra_env` overlays onto the
    child environment (e.g. remapped AM_URL/APIM_URL for coexist-mode setup).
    Returns a structured result; a timeout is reported, not raised.
    """
    env = _child_env()
    if extra_env:
        env.update(extra_env)
    try:
        p = subprocess.run(
            ["bash", str(run_sh()), *run_sh_args],
            cwd=str(docker_dir()),
            env=env,
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


def run_setup_script_direct(timeout: int, extra_env: dict[str, str]) -> dict:
    """Run `bash setup.sh` directly (coexist mode), bypassing run.sh's setup wrapper.

    run.sh's `setup` subcommand health-gates on HARDCODED canonical URLs
    (localhost:8093/8083/18443) before exec'ing setup.sh — those ports aren't bound
    when the stack runs on remapped ports, so the wrapper would hang. setup.sh itself
    does not health-gate (it assumes AM+APIM are already up) and routes every call
    through $AM_URL/$APIM_URL, so we invoke it directly with the remapped env. Call
    only after stack_status reports healthy.
    """
    env = _child_env()
    env.update(extra_env)
    setup_sh = docker_dir() / "setup.sh"
    try:
        p = subprocess.run(
            ["bash", str(setup_sh)],
            cwd=str(docker_dir()),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {"timed_out": False, "returncode": p.returncode,
                "stdout": p.stdout, "stderr": p.stderr}
    except subprocess.TimeoutExpired as e:
        return {
            "timed_out": True, "returncode": None,
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
