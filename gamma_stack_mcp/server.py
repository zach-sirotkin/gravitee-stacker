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
def stack_up(pull: bool = True, coexist: bool = False) -> dict:
    """Start the Gamma demo stack in the background (non-blocking).

    Launches the up in a detached subprocess, redirecting output to .run/up.log,
    and returns immediately — it never waits, so cold image pulls won't time out
    your MCP client. Poll `stack_status` to learn when the stack is actually ready.

    Two port modes:
      * default (coexist=False): the stack's canonical ports via `run.sh` — the
        fully-wired demo (stack_setup, OAuth, edge config all correct). If a
        canonical port is already taken (e.g. another local stack), this does NOT
        launch; it returns status "port_conflict" and suggests coexist mode.
      * coexist=True: every published host port is shifted by GAMMA_PORT_OFFSET
        (default 20000) via a generated compose overlay, so the stack runs
        alongside others. Drives `docker compose` directly (the stack's ports are
        hardcoded and run.sh takes no overlay). Caveat: stack_setup hardcodes a few
        canonical ports (edge gatewayUrl :8082, :80 OAuth redirect URIs) that can't
        be fully remapped, so the demo bootstrap is best-effort in this mode.
        GAMMA_PORT_KEEP (comma-separated services) leaves those on original ports.

    Args:
        pull: Pull images first (default). False uses whatever images are cached.
        coexist: Run on remapped ports so the stack won't collide with others.

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

    log_path = runner.up_log_path()

    # ── Default: canonical ports via run.sh (with a conflict preflight) ─────
    if not coexist:
        try:
            busy = runner.ports_in_use(runner.published_host_ports())
        except (RuntimeError, ValueError):
            busy = []  # couldn't read config; let run.sh's own preflight handle it
        if busy:
            return {
                "status": "port_conflict",
                "message": (
                    f"canonical port(s) already in use: {busy} — likely another local "
                    "stack (e.g. apim-latest on 8082/8083). The stack was NOT started. "
                    "Re-run stack_up with coexist=true to bring it up on remapped "
                    f"ports (shifted by {runner.port_offset()}), or free these ports."
                ),
                "busy_ports": busy,
                "suggest": {"tool": "stack_up", "args": {"coexist": True}},
            }
        args = [] if pull else ["--no-pull"]
        log_path.write_bytes(b"")
        proc = runner.launch_background(args, log_path)
        started = _now_iso()
        state.record_up(proc, log_path, args, started)
        state.record_mode(coexist=False, offset=0, keep=[])
        return {
            "status": "starting", "mode": "canonical", "pid": proc.pid,
            "log_path": str(log_path), "pull": pull, "started": started,
            "warnings": env["warnings"],
            "next": "Poll stack_status to see when services become healthy.",
        }

    # ── Coexist: generate overlay + drive docker compose directly ───────────
    offset = runner.port_offset() or runner.DEFAULT_PORT_OFFSET
    keep = runner.port_keep()
    try:
        cfg = runner.compose_config_json()
    except (RuntimeError, ValueError) as e:
        return {"status": "blocked", "message": f"could not read compose config: {e}"}

    acr_err = runner.acr_probe(cfg)
    if acr_err:
        return {"status": "blocked", "message": acr_err, "warnings": env["warnings"]}

    try:
        overlay_path, mapping = runner.generate_ports_override(offset, keep)
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
    state.record_mode(coexist=True, offset=offset, keep=sorted(keep))

    return {
        "status": "starting",
        "mode": "coexist",
        "port_offset": offset,
        "pid": proc.pid,
        "log_path": str(log_path),
        "overlay": str(overlay_path),
        "pull": pull,
        "started": started,
        "port_mapping": mapping,
        "urls": _url_hints(mapping),
        "caveats": [
            "stack_setup will remap AM_URL/APIM_URL/EDGE_REACTOR_PORT, but its "
            "hardcoded edge gatewayUrl (:8082) and :80 OAuth redirect URIs stay "
            "canonical — edge/OAuth wiring is best-effort in coexist mode.",
        ],
        "warnings": env["warnings"],
        "next": "Poll stack_status to see when services become healthy (cold pulls take several minutes).",
    }


def _sfx(port: int) -> str:
    return "" if port == 80 else f":{port}"


def _url_hints(mapping: list[dict]) -> dict:
    """Human-friendly access URLs given the (possibly remapped) ports."""
    by_new = {(m["service"], m["container"]): m["new_host"] for m in mapping}
    hints = {}
    nginx = next((m["new_host"] for m in mapping if m["service"] == "nginx"), None)
    if nginx:
        hints["UIs (via nginx, Host-routed)"] = {
            "gamma console": f"http://gamma.localhost{_sfx(nginx)}",
            "APIM console": f"http://apim.localhost{_sfx(nginx)}",
            "APIM portal": f"http://portal.localhost{_sfx(nginx)}",
            "AM webui": f"http://am.localhost{_sfx(nginx)}",
        }
        if nginx != 80:
            hints["UIs (via nginx, Host-routed)"]["note"] = (
                "OAuth flows registered by stack_setup assume :80; if a redirect "
                "misbehaves, run with GAMMA_PORT_KEEP=nginx to keep :80."
            )
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


def _coexist_setup_env(offset: int, keep: list[str]) -> dict:
    """Point setup.sh at the remapped host ports it CAN be told about.

    setup.sh parameterizes AM_URL / APIM_URL / EDGE_REACTOR_PORT (canonical 8093 /
    8083 / 18072). A service in `keep` stays on its canonical port.
    """
    keep_set = set(keep)

    def host(port: int, service: str) -> int:
        return port if service in keep_set else port + offset

    return {
        "AM_URL": f"http://localhost:{host(8093, 'management')}",
        "APIM_URL": f"http://localhost:{host(8083, 'apim-rest-api')}",
        "EDGE_REACTOR_PORT": str(host(18072, 'apim-gateway')),
    }


@mcp.tool()
def stack_setup(timeout_seconds: int = 300, coexist: "bool | None" = None) -> dict:
    """Bootstrap the demo environment (`run.sh setup`), run in the foreground.

    Waits for AM + APIM + SPIRE to answer, then runs setup.sh. Assumes the stack
    is already up (run stack_up first). Captures and returns output.

    Args:
        timeout_seconds: Max wait (default 300 = 5 min). setup can take a minute+;
            if it times out, run it yourself: `cd $GAMMA_STACK_DIR/docker && bash run.sh setup`.
        coexist: Whether the stack is on remapped ports. Default (None) auto-detects
            from the last stack_up. When true, AM_URL/APIM_URL/EDGE_REACTOR_PORT are
            pointed at the remapped ports — but setup.sh's hardcoded edge gatewayUrl
            (:8082) and :80 OAuth redirect URIs stay canonical (a documented gap).
    """
    mode = state.read_mode()
    active = mode.get("coexist", False) if coexist is None else coexist

    caveats = []
    if active:
        # Coexist: run setup.sh DIRECTLY with remapped AM_URL/APIM_URL. We can't use
        # `run.sh setup` here — its wait_healthy gate targets hardcoded canonical
        # ports (8093/8083/18443) that aren't bound in coexist mode, so it would hang.
        offset = mode.get("offset") or runner.DEFAULT_PORT_OFFSET
        keep = mode.get("keep", [])
        extra_env = _coexist_setup_env(offset, keep)
        caveats = [
            f"coexist mode: ran setup.sh directly, pointed at remapped ports {extra_env} "
            "(run.sh's setup health-gate targets canonical ports and would hang here).",
            "setup.sh's hardcoded edge gatewayUrl (http://localhost:8082) and :80 "
            "OAuth redirect URIs are NOT remapped — edge/OAuth wiring is best-effort. "
            "For a fully-wired demo use canonical ports (stack_up without coexist).",
            "Run this only after stack_status reports healthy (no health-gate here).",
        ]
        result = runner.run_setup_script_direct(timeout_seconds, extra_env)
    else:
        result = runner.run_foreground(["setup"], timeout=timeout_seconds)
    if result["timed_out"]:
        return {
            "status": "timeout",
            "coexist": active,
            "caveats": caveats,
            "message": f"setup exceeded {timeout_seconds}s and was left running/killed by the timeout. "
                       "Re-run in your terminal if needed: "
                       "cd $GAMMA_STACK_DIR/docker && bash run.sh setup",
            "stdout_tail": (result["stdout"] or "")[-4000:],
            "stderr_tail": (result["stderr"] or "")[-4000:],
        }
    return {
        "status": "ok" if result["returncode"] == 0 else "error",
        "coexist": active,
        "caveats": caveats,
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
def stack_ports(coexist: "bool | None" = None) -> dict:
    """Show the active host-port mapping and access URLs for the stack.

    Reflects how the stack was last brought up (canonical vs coexist / offset /
    keep-list). Handy after a coexist `stack_up` without re-reading its payload.

    Args:
        coexist: Override the mode to preview. Default (None) uses the last
            stack_up mode. Pass true/false to see what either mode's ports would be.
    """
    mode = state.read_mode()
    is_coexist = mode.get("coexist", False) if coexist is None else coexist
    try:
        cfg = runner.compose_config_json()
    except (RuntimeError, ValueError) as e:
        return {"status": "error", "message": f"could not read compose config: {e}"}

    if is_coexist:
        offset = mode.get("offset") or runner.DEFAULT_PORT_OFFSET
        keep = set(mode.get("keep", []))
    else:
        offset, keep = 0, set()

    try:
        mapping = runner.compute_port_mapping(offset, keep, cfg)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    return {
        "status": "ok",
        "mode": "coexist" if is_coexist else "canonical",
        "offset": offset,
        "keep": sorted(keep),
        "source": "last stack_up" if coexist is None else "preview",
        "ports": [
            {"service": m["service"], "container": m["container"],
             "host": m["new_host"], "original": m["old_host"],
             "remapped": m["new_host"] != m["old_host"]}
            for m in mapping
        ],
        "urls": _url_hints(mapping),
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
def _apim_urls() -> dict:
    return {
        "console": "http://localhost:8084 (admin/admin)",
        "portal": "http://localhost:8085",
        "management API": "http://localhost:8083/management",
        "gateway": "http://localhost:8082",
    }


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

    return {
        "overall": overall,
        "version": up.get("version") or apim.current_version(),
        "up_process": up,
        "services": [{"service": s, **{k: by.get(s, {}).get(k) for k in ("state", "health", "exit_code")},
                      "label": labels[s]} for s in expected],
        "problems": [{"service": s, "label": labels[s]} for s in expected
                     if labels[s] in _BAD or labels[s] == "missing"],
        "urls": _apim_urls(),
        "up_log_tail": runner.tail_file(apim.up_log_path(), 40),
        "checked_at": _now_iso(),
    }


@mcp.tool()
def apim_up(version: str = "latest", pull: bool = True,
            down_conflicting: bool = False, recreate: bool = False) -> dict:
    """Stand up a standalone Gravitee APIM stack (background, non-blocking).

    Self-contained OSS stack (mongo + elasticsearch + gateway + management-api +
    console + portal) on ports 8082/8083/8084/8085. Pulls the pinned image version
    and `docker compose up -d`, returning immediately; poll `apim_status`.

    Port safety: it checks 8082-8085 first. If another compose project holds any of
    them (e.g. the Gamma stack), it does NOT start — it returns status
    "port_conflict" listing the offending project(s). Re-run with
    down_conflicting=true to bring those projects down first (preserving their data),
    then start APIM.

    Args:
        version: Image tag to pin. "latest" (default) resolves the newest stable
            release from the APIM repo (e.g. 4.12.8). Pass e.g. "4.12.3" to pin.
        pull: Pull images before up (default).
        down_conflicting: Down any conflicting compose projects first (no -v; data kept).
        recreate: `up -d --force-recreate` — reload/recreate containers (e.g. after
            changing the pinned version).
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

    ports = apim.compose_config_ports()
    conflicts = apim.detect_conflicts(ports)
    if conflicts:
        if not down_conflicting:
            projects = sorted({c["project"] for c in conflicts})
            return {
                "status": "port_conflict",
                "version": resolved,
                "message": (
                    f"port(s) needed by APIM are held by other stack(s): "
                    f"{[(c['port'], c['project']) for c in conflicts]}. "
                    "The APIM stack was NOT started. Re-run apim_up with "
                    "down_conflicting=true to bring those projects down first "
                    "(their data volumes are preserved), or free the ports yourself."
                ),
                "conflicts": conflicts,
                "conflicting_projects": projects,
                "suggest": {"tool": "apim_up",
                            "args": {"version": version, "down_conflicting": True}},
            }
        # Down each conflicting project (data preserved). Reset Gamma tracking if it was ours.
        downed = []
        gamma_project = runner.docker_dir().name
        for proj in sorted({c["project"] for c in conflicts}):
            res = apim.down_project(proj)
            downed.append({"project": proj, "returncode": res["returncode"]})
            if proj == gamma_project:
                state.forget_up()
    else:
        downed = []

    log_path = apim.up_log_path()
    log_path.write_bytes(b"")
    proc = apim.launch_up_background(resolved, pull, recreate, log_path)
    started = _now_iso()
    apim.record_up(proc, resolved, log_path, started)

    return {
        "status": "starting",
        "version": resolved,
        "pid": proc.pid,
        "log_path": str(log_path),
        "pull": pull,
        "recreate": recreate,
        "downed_conflicts": downed,
        "ports": ports,
        "urls": _apim_urls(),
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


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
