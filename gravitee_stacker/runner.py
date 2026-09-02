"""Shared docker / subprocess helpers used by the stack modules (apim, am, gamma,
quicksetup, plugins).

Small, dependency-light utilities: the child-process environment, docker readiness
checks, port probing, compose-project introspection, log tailing, and reading a running
stack's license entitlements. Each stack module owns its own compose plumbing; this module
holds only what they share.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional


def state_dir() -> Path:
    """Where tracked up-process metadata + captured logs live (.run/).

    Defaults to ``<project root>/.run`` (gitignored); override with GAMMA_MCP_STATE_DIR.
    Each stack module namespaces a subdir under here (apim/, am/, gamma/).
    """
    override = os.environ.get("GAMMA_MCP_STATE_DIR")
    if override:
        d = Path(override).expanduser()
    else:
        d = Path(__file__).resolve().parent.parent / ".run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    """Where per-project config-override files live (editable rendered gravitee_* values).
    Shared across stacks; override with STACKER_CONFIG_DIR (default ~/.gravitee/stacker-config)."""
    d = Path(os.environ.get("STACKER_CONFIG_DIR")
             or Path.home() / ".gravitee" / "stacker-config").expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _child_env() -> dict[str, str]:
    """Environment for child processes.

    Force UTF-8 so unicode output is captured cleanly regardless of the MCP client's
    locale, and keep everything else from the parent (so the user's docker config,
    REGISTRY overrides, etc. flow through exactly as they would in their own shell).
    """
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("LC_ALL", "en_US.UTF-8")
    env.setdefault("LANG", "en_US.UTF-8")
    return env


# ── Environment sanity ────────────────────────────────────────────────────────
def docker_running_error() -> Optional[str]:
    """None if docker is on PATH and `docker info` succeeds, else a clear message."""
    if shutil.which("docker") is None:
        return "`docker` not on PATH."
    info = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=20)
    if info.returncode != 0:
        last = info.stderr.strip().splitlines()[-1] if info.stderr.strip() else ""
        return f"Docker does not appear to be running (`docker info` failed). {last}".strip()
    return None


def docker_total_memory_gib() -> Optional[float]:
    """Docker VM total memory in GiB (for the Kafka stack's >=16 GiB advisory), or None."""
    r = subprocess.run(["docker", "info", "--format", "{{.MemTotal}}"],
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip()) / (1024 ** 3)
    except ValueError:
        return None


def ports_in_use(ports: list[int]) -> list[int]:
    """Which of these host ports currently have a LISTEN socket."""
    busy = []
    for p in ports:
        r = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{p}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            busy.append(p)
    return busy


def tail_file(path: Path, lines: int) -> str:
    if not path.is_file():
        return ""
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def project_running_containers(project: str) -> list[str]:
    """Names of RUNNING containers in a compose project, by docker label — the
    authoritative 'is this stack up?' signal.

    Independent of the tracked launcher process (which exits the moment `up -d`
    returns, so PID-liveness reports a genuinely-running stack as down) AND of the
    compose -f files. Used so preflight/up never treat an already-running stack as
    absent and recreate its containers out from under the user.
    """
    r = subprocess.run(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
         "--filter", "status=running", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=15,
    )
    return [n for n in r.stdout.split() if n] if r.returncode == 0 else []


def find_mgmt_api_container(project: str) -> Optional[str]:
    """Name of the RUNNING management-api container in a compose project (works across the
    apim/kafka/gamma naming: `...-management-api-1` and gamma's `...-management_api-1`).
    Excludes the management-UI container."""
    for n in project_running_containers(project):
        low = n.lower()
        if "management-api" in low or "management_api" in low:
            return n
    return None


def read_stack_license(project: str) -> dict:
    """Read the enterprise license entitlements from a running stack's management-api, via its
    node license endpoint (basic admin:adminadmin on the in-container node port 18083). Returns
    tier/packs/features/expiry, or a no_license/not_running status. Generic across APIM-family
    stacks (apim, gamma, quicksetups) — pass the compose project name."""
    import json as _json

    cid = find_mgmt_api_container(project)
    if not cid:
        return {"status": "not_running", "project": project,
                "message": f"no running management-api container for project '{project}' — is the stack up?"}
    # Try the node license endpoint (both known paths); succeed on the first body carrying a licenseId.
    script = ('for u in http://localhost:18083/_node/license http://localhost:18083/license; do '
              'body=$(curl -s -u admin:adminadmin "$u" 2>/dev/null); '
              'case "$body" in *licenseId*) printf "%s" "$body"; exit 0;; esac; done; exit 3')
    try:
        p = subprocess.run(["docker", "exec", cid, "sh", "-c", script],
                           capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return {"status": "error", "project": project, "container": cid,
                "message": "timed out querying the node license endpoint."}
    if p.returncode != 0 or "licenseId" not in (p.stdout or ""):
        return {"status": "no_license", "project": project, "container": cid,
                "message": "management-api is running but no enterprise license is loaded (OSS mode), "
                           "or the node license endpoint is unreachable."}
    try:
        lic = _json.loads(p.stdout.strip())
    except _json.JSONDecodeError:
        return {"status": "error", "project": project, "container": cid, "raw": p.stdout[:400]}
    packs = [x.strip() for x in (lic.get("packs") or "").split(",") if x.strip()]
    features = [x.strip() for x in (lic.get("features") or "").split(",") if x.strip()]
    return {
        "status": "ok", "project": project, "container": cid,
        "tier": lic.get("tier"), "packs": packs, "features": features,
        "entitlements": sorted(set(packs + features)),
        "expiryDate": lic.get("expiryDate"), "licenseId": lic.get("licenseId"),
        "company": lic.get("company"), "email": lic.get("email"),
        "note": "packs + features are this license's enterprise entitlements. A console module shown "
                "as 'Upgrade to access' means its required pack is NOT in this list — an entitlement "
                "gap, not a mount/load problem.",
    }


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
