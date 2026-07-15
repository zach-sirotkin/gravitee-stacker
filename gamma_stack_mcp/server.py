"""FastMCP server exposing the Gravitee Gamma demo-stack tools.

Thin wrapper over docker/run.sh. Tools:
  stack_up, stack_status, stack_setup, stack_down, stack_logs,
  stack_install_daemon, stack_uninstall_daemon
"""

from __future__ import annotations

from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from . import apim, runner, state

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

    Launches `run.sh` in a detached subprocess, redirecting output to .run/up.log,
    and returns immediately — it never waits, so cold image pulls won't time out
    your MCP client. Poll `stack_status` to learn when the stack is ready.

    The Gamma stack runs on its canonical ports (it's strict about them — its
    consoles hardcode host-routing and :80, so remapping isn't supported). Before
    launching it pre-checks those ports; if any is already taken (e.g. another local
    stack), it does NOT launch and returns status "port_conflict" naming the busy
    ports. Free them (or down the other stack) and retry.

    Args:
        pull: Pull images first (default). False uses whatever images are cached.

    Only one up at a time: a running tracked up-process is refused.
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

    try:
        busy = runner.ports_in_use(runner.published_host_ports())
    except (RuntimeError, ValueError):
        busy = []  # couldn't read config; let run.sh's own preflight handle it
    if busy:
        return {
            "status": "port_conflict",
            "message": (
                f"canonical port(s) already in use: {busy} — likely another local "
                "stack. The Gamma stack was NOT started. Free these ports (or down the "
                "conflicting stack — e.g. apim_up's down_conflicting, or apim_down) and retry."
            ),
            "busy_ports": busy,
        }

    args = [] if pull else ["--no-pull"]
    log_path = runner.up_log_path()
    log_path.write_bytes(b"")
    proc = runner.launch_background(args, log_path)
    started = _now_iso()
    state.record_up(proc, log_path, args, started)
    return {
        "status": "starting", "mode": "canonical", "pid": proc.pid,
        "log_path": str(log_path), "pull": pull, "started": started,
        "warnings": env["warnings"],
        "next": "Poll stack_status to see when services become healthy.",
    }


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
def stack_ports() -> dict:
    """Show the Gamma stack's host ports and access URLs (canonical)."""
    try:
        mapping = runner.compute_port_mapping(0, set())
    except (RuntimeError, ValueError) as e:
        return {"status": "error", "message": f"could not read compose config: {e}"}

    by = {(m["service"], m["container"]): m["new_host"] for m in mapping}
    backends = {
        "AM mgmt API": by.get(("management", 8093)),
        "AM gateway": by.get(("gateway", 8092)),
        "APIM rest-api": by.get(("apim-rest-api", 8083)),
        "APIM gateway": by.get(("apim-gateway", 8082)),
        "SPIRE JWKS": by.get(("spire-oidc", 8443)),
    }
    return {
        "status": "ok",
        "mode": "canonical",
        "ports": [{"service": m["service"], "container": m["container"], "host": m["new_host"]}
                  for m in mapping],
        "urls": {
            "UIs (via nginx, Host-routed)": {
                "gamma console": "http://gamma.localhost",
                "APIM console": "http://apim.localhost",
                "APIM portal": "http://portal.localhost",
                "AM webui": "http://am.localhost",
            },
            "backends (direct)": {k: f"http://localhost:{v}" for k, v in backends.items() if v},
        },
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


# ── standalone APIM stack tools ───────────────────────────────────────────────
def _apim_urls(role_ports: dict) -> dict:
    """Build access URLs from a {role -> host port} map (resolved from the compose)."""
    role_ports = role_ports or {}
    out = {}
    if role_ports.get("console"):
        out["console"] = f"http://localhost:{role_ports['console']} (admin/admin)"
    if role_ports.get("portal"):
        out["portal"] = f"http://localhost:{role_ports['portal']}"
    if role_ports.get("management API"):
        out["management API"] = f"http://localhost:{role_ports['management API']}/management"
    if role_ports.get("gateway"):
        out["gateway"] = f"http://localhost:{role_ports['gateway']}"
    return out


def _apim_summarize() -> dict:
    up = apim.up_process_status()
    rows = apim.compose_ps()
    expected = apim.service_names()
    by = {}
    for r in rows:
        svc = r.get("Service") or r.get("Name", "?")
        by[svc] = {
            "service": svc,
            "state": r.get("State", r.get("Status", "")),
            "health": r.get("Health") or None,
            "exit_code": r.get("ExitCode"),
            "label": _classify(r.get("State", r.get("Status", "")), r.get("Health", ""), r.get("ExitCode")),
        }
    labels = {s: (by[s]["label"] if s in by else "missing") for s in expected}

    all_ok = bool(labels) and all(l in _OK for l in labels.values())
    nothing = bool(labels) and all(l == "missing" for l in labels.values())

    if up.get("tracked") and up.get("running"):
        overall = "starting"
    elif up.get("tracked") and up.get("exit_code") not in (None, 0):
        overall = "failed"
    elif not expected or nothing:
        overall = "down"
    elif all_ok:
        overall = "healthy"
    else:
        overall = "partial"

    mode = apim.current_mode()
    # URLs from the tracked up (resolved from the compose at up time); if there's no
    # record (e.g. down), resolve canonical ports from the compose for display.
    role_ports = up.get("urls")
    if not role_ports:
        try:
            role_ports = apim.plan_ports(mode["coexist"], mode["offset"])["urls"]
        except (RuntimeError, ValueError):
            role_ports = {}
    return {
        "overall": overall,
        "version": up.get("version") or apim.current_version(),
        "mode": "coexist" if mode["coexist"] else "canonical",
        "up_process": up,
        "services": [{"service": s, **{k: by.get(s, {}).get(k) for k in ("state", "health", "exit_code")},
                      "label": labels[s]} for s in expected],
        "problems": [{"service": s, "label": labels[s]} for s in expected
                     if labels[s] in _BAD or labels[s] == "missing"],
        "urls": _apim_urls(role_ports),
        "up_log_tail": runner.tail_file(apim.up_log_path(), 40),
        "checked_at": _now_iso(),
    }


@mcp.tool()
def apim_up(version: str = "latest", pull: bool = True, coexist: bool = False,
            down_conflicting: bool = False, recreate: bool = False,
            license: str = "") -> dict:
    """Stand up a standalone Gravitee APIM stack (background, non-blocking).

    Self-contained stack (mongo + elasticsearch + gateway + management-api + console
    + portal). Pulls the pinned image version and `docker compose up -d`, returning
    immediately; poll `apim_status`.

    Ports: default (coexist=False) uses canonical 8082/8083/8084/8085. It checks
    those first; if another compose project holds any of them (e.g. the Gamma
    stack), it does NOT start — it returns status "port_conflict" and suggests
    EITHER down_conflicting=true (down the other stack first, data preserved) OR
    coexist=true. In coexist mode every host port is shifted by APIM_PORT_OFFSET
    (default 20000) — and the console/portal are told about the remapped management
    port — so APIM runs cleanly alongside another stack.

    Args:
        version: Image tag to pin. "latest" (default) resolves the newest stable
            release from the APIM repo (e.g. 4.12.8). Pass e.g. "4.12.3" to pin.
        pull: Pull images before up (default).
        coexist: Run on remapped ports (offset APIM_PORT_OFFSET) alongside another stack.
        down_conflicting: (canonical only) down conflicting projects first (no -v; data kept).
        recreate: `up -d --force-recreate` — reload/recreate (e.g. after a version change).
        license: Path to a Gravitee license.key to mount (enterprise features). Empty =
            resolve from APIM_LICENSE env, else the Gamma stack's license, else OSS mode.
    """
    if apim.is_up_running():
        return {"status": "already_running",
                "message": "An APIM up-process is already running. Use apim_status."}

    docker_err = runner.docker_running_error()
    if docker_err:
        return {"status": "blocked", "message": docker_err}

    resolved, err = apim.resolve_version(version)
    if err:
        return {"status": "blocked", "message": err}

    offset = apim.port_offset()
    license_path, license_src = apim.resolve_license(license)
    try:
        plan = apim.plan_ports(coexist, offset)
    except (RuntimeError, ValueError) as e:
        return {"status": "blocked", "message": f"could not read compose config: {e}"}
    ports, port_env, role_ports = plan["ports"], plan["port_env"], plan["urls"]

    conflicts = apim.detect_conflicts(ports)
    downed = []
    if conflicts:
        if not down_conflicting:
            return {
                "status": "port_conflict",
                "version": resolved,
                "coexist": coexist,
                "message": (
                    f"port(s) needed by APIM ({'coexist' if coexist else 'canonical'}) "
                    f"are held by other stack(s): {[(c['port'], c['project']) for c in conflicts]}. "
                    "APIM was NOT started. Either re-run with down_conflicting=true to "
                    "down the other stack(s) (data preserved)"
                    + ("" if coexist else ", or run with coexist=true to start APIM on "
                       f"remapped ports (shifted by {offset})") + "."
                ),
                "conflicts": conflicts,
                "conflicting_projects": sorted({c["project"] for c in conflicts}),
                "suggest": (
                    {"tool": "apim_up", "args": {"version": version, "down_conflicting": True}}
                    if coexist else
                    {"down_the_other": {"tool": "apim_up", "args": {"version": version, "down_conflicting": True}},
                     "run_alongside": {"tool": "apim_up", "args": {"version": version, "coexist": True}}}
                ),
            }
        gamma_project = runner.docker_dir().name
        for proj in sorted({c["project"] for c in conflicts}):
            res = apim.down_project(proj)
            downed.append({"project": proj, "returncode": res["returncode"]})
            if proj == gamma_project:
                state.forget_up()

    log_path = apim.up_log_path()
    log_path.write_bytes(b"")
    proc = apim.launch_up_background(resolved, pull, recreate, port_env, license_path, log_path)
    started = _now_iso()
    apim.record_up(proc, resolved, coexist, offset, license_path, role_ports, ports, log_path, started)

    return {
        "status": "starting",
        "version": resolved,
        "mode": "coexist" if coexist else "canonical",
        "port_offset": offset if coexist else 0,
        "pid": proc.pid,
        "log_path": str(log_path),
        "compose_file": str(apim.compose_file()),
        "pull": pull,
        "recreate": recreate,
        "license": {"mounted": bool(license_path), "path": license_path, "source": license_src},
        "downed_conflicts": downed,
        "ports": ports,
        "urls": _apim_urls(role_ports),
        "started": started,
        "next": "Poll apim_status until overall: healthy (cold pulls take several minutes).",
    }


@mcp.tool()
def apim_status() -> dict:
    """Status of the standalone APIM stack: overall verdict + per-service health.

    Two signals like stack_status: the tracked up-process, and `docker compose ps`
    for the gravitee-apim project. Reports the pinned version and access URLs.
    """
    return _apim_summarize()


@mcp.tool()
def apim_down(timeout_seconds: int = 180) -> dict:
    """Stop the standalone APIM stack (`docker compose down`, volumes preserved)."""
    result = apim.run_down(timeout_seconds)
    apim.forget_up()
    if result["timed_out"]:
        return {"status": "timeout", "message": f"down exceeded {timeout_seconds}s.",
                "stdout_tail": (result["stdout"] or "")[-2000:],
                "stderr_tail": (result["stderr"] or "")[-2000:]}
    return {"status": "ok" if result["returncode"] == 0 else "error",
            "returncode": result["returncode"],
            "stdout": result["stdout"], "stderr": result["stderr"]}


@mcp.tool()
def apim_logs(service: str, lines: int = 100) -> dict:
    """Tail logs for one APIM service (`docker compose logs --tail=<lines> <service>`)."""
    valid = apim.service_names()
    if service not in valid:
        return {"status": "invalid_service", "message": f"unknown service '{service}'.",
                "valid_services": valid}
    p = apim.compose_logs(service, lines)
    return {"status": "ok" if p.returncode == 0 else "error", "service": service,
            "lines": lines, "returncode": p.returncode, "logs": p.stdout or p.stderr}


@mcp.tool()
def apim_latest_version() -> dict:
    """Resolve the newest stable Gravitee APIM release tag (from the APIM repo)."""
    resolved, err = apim.resolve_version("latest")
    if err:
        return {"status": "error", "message": err}
    return {"status": "ok", "latest_version": resolved,
            "repo": "https://github.com/gravitee-io/gravitee-api-management"}


@mcp.tool()
def doctor() -> dict:
    """Check environment readiness for both stacks and report what's missing.

    Call this first when setting up. The APIM stack needs only Docker (public
    images; a license is optional). The Gamma stack additionally needs the stack
    repo (GAMMA_STACK_DIR), ACR login, and a license.
    """
    docker_err = runner.docker_running_error()
    docker_ok = docker_err is None

    lic_path, lic_src = apim.resolve_license("")
    apim_next = []
    if not docker_ok:
        apim_next.append(f"Start Docker: {docker_err}")
    if not lic_path:
        apim_next.append(f"(optional) drop a Gravitee license at {apim.DEFAULT_LICENSE_PATH} "
                         "for enterprise features — OSS works without it.")

    gamma_env = runner.check_environment()
    gamma_next = list(gamma_env["problems"]) + list(gamma_env["warnings"])

    return {
        "docker": {"ok": docker_ok, "detail": docker_err or "running"},
        "apim_stack": {
            "ready": docker_ok,
            "needs": "Docker only (public images).",
            "license": {"found": bool(lic_path),
                        "path": lic_path or str(apim.DEFAULT_LICENSE_PATH),
                        "source": lic_src},
            "latest_version": apim.resolve_version("latest")[0],
            "next_steps": apim_next or ["ready — call apim_up()"],
        },
        "gamma_stack": {
            "ready": gamma_env["ok"],
            "stack_dir": str(runner.stack_dir()),
            "stack_dir_found": runner.docker_dir().is_dir(),
            "needs": "the stack repo at GAMMA_STACK_DIR + ACR login + a license.",
            "next_steps": gamma_next or ["ready — call stack_up()"],
        },
    }


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
