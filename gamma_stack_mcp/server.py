"""FastMCP server exposing the Gravitee Gamma demo-stack tools.

Thin wrapper over docker/run.sh. Tools:
  stack_up, stack_status, stack_setup, stack_down, stack_logs,
  stack_install_daemon, stack_uninstall_daemon
"""

from __future__ import annotations

from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from . import runner, state

mcp = FastMCP("gamma-stack")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ── health classification ─────────────────────────────────────────────────────
_OK = {"running", "completed"}
_PENDING = {"starting", "created"}
_BAD = {"unhealthy", "exited", "other"}


def _classify(state_str: str, health: str, exit_code) -> str:
    s = (state_str or "").lower()
    h = (health or "").lower()
    if s == "running":
        if h == "unhealthy":
            return "unhealthy"
        if h == "starting":
            return "starting"
        return "running"  # healthy, or no healthcheck defined
    if s == "exited":
        # init containers (spire-perms-init / spire-bootstrap) exit 0 by design,
        # so a clean exit is "completed", not a failure.
        return "completed" if _looks_zero(exit_code) else "exited"
    if s == "created":
        return "created"
    if s in ("restarting", "paused", "dead"):
        return "other"
    return s or "other"


def _looks_zero(exit_code) -> bool:
    try:
        return int(exit_code) == 0
    except (TypeError, ValueError):
        return exit_code in (None, "")


def _service_rows() -> list[dict]:
    """Normalise `docker compose ps` rows into a stable shape."""
    rows = runner.compose_ps()
    out = []
    for r in rows:
        state_str = r.get("State", r.get("Status", ""))
        health = r.get("Health", "")
        exit_code = r.get("ExitCode")
        out.append({
            "service": r.get("Service") or r.get("Name", "?"),
            "state": state_str,
            "health": health or None,
            "exit_code": exit_code,
            "status_text": r.get("Status", ""),
            "label": _classify(state_str, health, exit_code),
        })
    return out


def _summarize() -> dict:
    """The two-independent-signals status the whole tool exists for."""
    up = state.up_process_status()
    services = _service_rows()
    expected = runner.compose_services()

    by_service = {row["service"]: row for row in services}
    labels = {svc: (by_service[svc]["label"] if svc in by_service else "missing")
              for svc in expected}

    any_ok = any(l in _OK for l in labels.values())
    any_bad = any(l in _BAD for l in labels.values())
    all_ok = bool(labels) and all(l in _OK for l in labels.values())
    nothing_created = bool(labels) and all(l == "missing" for l in labels.values())

    # Signal fusion: process liveness first, then compose health.
    if up.get("tracked") and up.get("running"):
        overall = "starting"
    elif up.get("tracked") and up.get("exit_code") not in (None, 0):
        overall = "failed"
    elif not expected:
        overall = "down"  # couldn't even read compose (docker down?)
    elif nothing_created:
        overall = "down"
    elif all_ok:
        overall = "healthy"
    else:
        overall = "partial"

    problems = [
        {"service": svc, "label": labels[svc],
         "state": by_service.get(svc, {}).get("state"),
         "exit_code": by_service.get(svc, {}).get("exit_code")}
        for svc in expected
        if labels[svc] in _BAD or labels[svc] == "missing"
    ]

    return {
        "overall": overall,
        "up_process": up,
        "services": [
            {"service": svc, "label": labels[svc],
             "state": by_service.get(svc, {}).get("state"),
             "health": by_service.get(svc, {}).get("health"),
             "exit_code": by_service.get(svc, {}).get("exit_code")}
            for svc in expected
        ],
        "unexpected_containers": [
            row for row in services if row["service"] not in set(expected)
        ],
        "problems": problems,
        "hint": _overall_hint(overall),
        "up_log_tail": runner.tail_file(runner.up_log_path(), 40),
        "compose_files": runner.compose_file_args(),
        "checked_at": _now_iso(),
    }


def _overall_hint(overall: str) -> str:
    return {
        "starting": "run.sh is still working (pull/up/health-poll). Call stack_status again shortly.",
        "healthy": "All services up. UIs via nginx on :80 (gamma.localhost / apim.localhost / am.localhost).",
        "partial": "Some services are up, others are down/pending. Check `problems` and stack_logs(<service>).",
        "down": "Nothing running. Call stack_up to start the stack.",
        "failed": "The up-process exited non-zero. See up_log_tail (likely ACR login, license, or a port conflict).",
    }.get(overall, "")


# ── tools ─────────────────────────────────────────────────────────────────────
@mcp.tool()
def stack_up(pull: bool = True) -> dict:
    """Start the Gamma demo stack in the background (non-blocking).

    Launches the up in a detached subprocess, redirecting output to .run/up.log,
    and returns immediately — it never waits, so cold image pulls won't time out
    your MCP client. Poll `stack_status` to learn when the stack is actually ready.

    Ports: by default every published host port is shifted by GAMMA_PORT_OFFSET
    (default 20000) via a generated compose overlay, so the stack never collides
    with other local stacks. The remap path drives `docker compose` directly (the
    stack's ports are hardcoded and run.sh takes no overlay). Set GAMMA_PORT_OFFSET=0
    to disable remapping and use the plain `run.sh` path instead. GAMMA_PORT_KEEP
    (comma-separated services) leaves those on their original ports.

    Args:
        pull: Pull images first (default). False uses whatever images are cached.

    Only one up at a time: if a tracked up-process is still running this refuses
    and points you at stack_status.
    """
    if state.is_up_running():
        rec = state.current_record()
        return {
            "status": "already_running",
            "message": "An up-process is already running. Use stack_status to check progress.",
            "pid": rec.pid if rec else None,
            "log_path": rec.log_path if rec else None,
        }

    env = runner.check_environment()
    if not env["ok"]:
        return {
            "status": "blocked",
            "message": "Pre-flight checks failed; not launching.",
            "problems": env["problems"],
            "warnings": env["warnings"],
        }

    offset = runner.port_offset()
    log_path = runner.up_log_path()

    # ── Plain run.sh path (remap disabled) ──────────────────────────────────
    if offset == 0:
        args = [] if pull else ["--no-pull"]
        log_path.write_bytes(b"")
        proc = runner.launch_background(args, log_path)
        started = _now_iso()
        state.record_up(proc, log_path, args, started)
        return {
            "status": "starting", "mode": "run.sh", "pid": proc.pid,
            "log_path": str(log_path), "pull": pull, "started": started,
            "warnings": env["warnings"],
            "next": "Poll stack_status to see when services become healthy.",
        }

    # ── Remap path: generate overlay + drive docker compose directly ────────
    try:
        cfg = runner.compose_config_json()
    except (RuntimeError, ValueError) as e:
        return {"status": "blocked", "message": f"could not read compose config: {e}"}

    acr_err = runner.acr_probe(cfg)
    if acr_err:
        return {"status": "blocked", "message": acr_err, "warnings": env["warnings"]}

    try:
        overlay_path, mapping = runner.generate_ports_override(offset, runner.port_keep())
    except ValueError as e:
        return {"status": "blocked", "message": str(e)}

    # Port pre-flight on the REMAPPED host ports (mirrors run.sh's own preflight).
    new_ports = sorted({m["new_host"] for m in mapping})
    busy = runner.ports_in_use(new_ports)
    if busy:
        return {
            "status": "blocked",
            "message": f"remapped port(s) already in use: {busy}. "
                       "Free them or adjust GAMMA_PORT_OFFSET / GAMMA_PORT_KEEP.",
            "port_mapping": mapping,
        }

    log_path.write_bytes(b"")
    proc = runner.launch_up_compose_background(["-f", str(overlay_path)], pull, log_path)
    started = _now_iso()
    state.record_up(proc, log_path, ["compose", "up", "-d", f"offset={offset}"], started)

    return {
        "status": "starting",
        "mode": "remap",
        "port_offset": offset,
        "pid": proc.pid,
        "log_path": str(log_path),
        "overlay": str(overlay_path),
        "pull": pull,
        "started": started,
        "port_mapping": mapping,
        "urls": _url_hints(mapping),
        "warnings": env["warnings"],
        "next": "Poll stack_status to see when services become healthy (cold pulls take several minutes).",
    }


def _url_hints(mapping: list[dict]) -> dict:
    """Human-friendly access URLs given the remapped ports."""
    by_new = {(m["service"], m["container"]): m["new_host"] for m in mapping}
    hints = {}
    nginx = next((m["new_host"] for m in mapping if m["service"] == "nginx"), None)
    if nginx:
        hints["UIs (via nginx, Host-routed)"] = {
            "gamma console": f"http://gamma.localhost:{nginx}",
            "APIM console": f"http://apim.localhost:{nginx}",
            "APIM portal": f"http://portal.localhost:{nginx}",
            "AM webui": f"http://am.localhost:{nginx}",
            "note": "UIs/OAuth flows registered by stack_setup assume :80; if a "
                    "redirect misbehaves, run with GAMMA_PORT_KEEP=nginx to keep :80.",
        }
    backends = {
        "AM mgmt API": by_new.get(("management", 8093)),
        "AM gateway": by_new.get(("gateway", 8092)),
        "APIM rest-api": by_new.get(("apim-rest-api", 8083)),
        "APIM gateway": by_new.get(("apim-gateway", 8082)),
        "SPIRE JWKS": by_new.get(("spire-oidc", 8443)),
    }
    hints["backends (direct)"] = {k: f"http://localhost:{v}" for k, v in backends.items() if v}
    return hints


@mcp.tool()
def stack_status() -> dict:
    """Report stack readiness via two independent signals.

    (1) Is the tracked `up` process still running, or has it exited (with what code)?
    (2) Actual health from `docker compose ps` (per-service state/health).

    Returns an overall verdict — starting | healthy | partial | down | failed —
    plus per-service state, any problem services, and the tail of up.log. This is
    the tool to call after stack_up to answer "is it ready yet?".

    The tracked up-process record is kept (even after it exits) so a `failed`
    verdict and its log tail persist across polls; it is replaced by the next
    stack_up and cleared by stack_down.
    """
    return _summarize()


@mcp.tool()
def stack_setup(timeout_seconds: int = 300) -> dict:
    """Bootstrap the demo environment (`run.sh setup`), run in the foreground.

    Waits for AM + APIM + SPIRE to answer, then runs setup.sh. Assumes the stack
    is already up (run stack_up first). Captures and returns output.

    Args:
        timeout_seconds: Max wait (default 300 = 5 min). setup can take a minute+;
            if it times out, run it yourself: `cd $GAMMA_STACK_DIR/docker && bash run.sh setup`.
    """
    result = runner.run_foreground(["setup"], timeout=timeout_seconds)
    if result["timed_out"]:
        return {
            "status": "timeout",
            "message": f"setup exceeded {timeout_seconds}s and was left running/killed by the timeout. "
                       "Re-run in your terminal if needed: "
                       "cd $GAMMA_STACK_DIR/docker && bash run.sh setup",
            "stdout_tail": (result["stdout"] or "")[-4000:],
            "stderr_tail": (result["stderr"] or "")[-4000:],
        }
    return {
        "status": "ok" if result["returncode"] == 0 else "error",
        "returncode": result["returncode"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }


@mcp.tool()
def stack_down(timeout_seconds: int = 180) -> dict:
    """Stop the stack (`run.sh down` -> `docker compose down`), foreground."""
    result = runner.run_foreground(["down"], timeout=timeout_seconds)
    state.forget_up()
    if result["timed_out"]:
        return {
            "status": "timeout",
            "message": f"down exceeded {timeout_seconds}s.",
            "stdout_tail": (result["stdout"] or "")[-4000:],
            "stderr_tail": (result["stderr"] or "")[-4000:],
        }
    return {
        "status": "ok" if result["returncode"] == 0 else "error",
        "returncode": result["returncode"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }


@mcp.tool()
def stack_logs(service: str, lines: int = 100) -> dict:
    """Tail logs for one stack service (`docker compose logs --tail=<lines> <service>`).

    Args:
        service: Service name. Must be one of the stack's services (see the
            `valid_services` list returned on a bad name).
        lines: Number of trailing log lines (default 100).
    """
    valid = runner.compose_services()
    if service not in valid:
        return {
            "status": "invalid_service",
            "message": f"unknown service '{service}'.",
            "valid_services": valid,
        }
    p = runner.compose_logs(service, lines)
    return {
        "status": "ok" if p.returncode == 0 else "error",
        "service": service,
        "lines": lines,
        "returncode": p.returncode,
        "logs": p.stdout or p.stderr,
    }


@mcp.tool()
def stack_install_daemon() -> dict:
    """Return the command to install the host Edge Daemon — does NOT run it.

    `run.sh install-daemon` self-elevates with sudo and prompts interactively for a
    password (installs a LaunchDaemon, edits DNS, binds :443). That cannot run
    unattended through a tool call, so this returns the exact command for you to run
    in your own terminal instead.
    """
    cmd = f"cd {runner.docker_dir()} && bash run.sh install-daemon"
    return {
        "status": "manual_action_required",
        "command": cmd,
        "note": "Run this yourself — it self-elevates via sudo and prompts for your "
                "password (LaunchDaemon, DNS, port 443). Run it after `stack_setup`.",
    }


@mcp.tool()
def stack_uninstall_daemon() -> dict:
    """Return the command to remove the host Edge Daemon — does NOT run it.

    `run.sh uninstall-daemon` also runs under sudo (removes the LaunchDaemon,
    binary, config, CA). Returns the exact command for you to run in your terminal.
    """
    cmd = f"cd {runner.docker_dir()} && bash run.sh uninstall-daemon"
    return {
        "status": "manual_action_required",
        "command": cmd,
        "note": "Run this yourself — it self-elevates via sudo and prompts for your password.",
    }


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
