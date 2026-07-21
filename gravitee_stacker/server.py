"""FastMCP server exposing the Gravitee Gamma demo-stack tools.

Thin wrapper over docker/run.sh. Tools:
  stack_up, stack_status, stack_setup, stack_down, stack_logs,
  stack_install_daemon, stack_uninstall_daemon
"""

from __future__ import annotations

from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from . import am, apim, quicksetup, runner, state

mcp = FastMCP("gravitee-stacker")


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
    if role_ports.get("prometheus"):
        out["prometheus"] = f"http://localhost:{role_ports['prometheus']}"
    return out


def _apim_summarize(instance: str = "default") -> dict:
    up = apim.up_process_status(instance)
    variant = up.get("variant") or apim.current_variant(instance)
    features = up.get("features") or apim.current_features(instance)
    rows = apim.compose_ps(variant, instance, features)
    expected = apim.service_names(variant, instance, features)
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

    offset = up.get("offset") if up.get("offset") is not None else apim.current_offset(instance)
    role_ports = up.get("urls")
    if not role_ports:
        try:
            role_ports = apim.plan_ports(offset, variant, features)["urls"]
        except (RuntimeError, ValueError):
            role_ports = {}
    return {
        "overall": overall,
        "instance": instance,
        "version": up.get("version") or apim.current_version(instance),
        "variant": variant,
        "features": features,
        "project": apim.project_for(variant, instance),
        "mode": "coexist" if offset else "canonical",
        "up_process": up,
        "services": [{"service": s, **{k: by.get(s, {}).get(k) for k in ("state", "health", "exit_code")},
                      "label": labels[s]} for s in expected],
        "problems": [{"service": s, "label": labels[s]} for s in expected
                     if labels[s] in _BAD or labels[s] == "missing"],
        "urls": _apim_urls(role_ports),
        "up_log_tail": runner.tail_file(apim.up_log_path(instance), 40),
        "checked_at": _now_iso(),
    }


@mcp.tool()
def apim_up(version: str = "latest", variant: str = "default", instance: str = "default",
            features: list = None, pull: bool = True, down_conflicting: bool = False,
            recreate: bool = False, license: str = "") -> dict:
    """Stand up a standalone Gravitee APIM stack (background, non-blocking).

    Variants (the gateway BASE):
      * "default" (OSS): mongo + es + gateway + management-api + console + portal.
      * "kafka": the native-Kafka gateway stack (adds a KRaft broker + kafka-client;
        gateway binds a Kafka listener on :9092 TLS). REQUIRES an EE license. Single
        instance / fixed ports.

    Features (composable add-ons): pass `features` to layer capabilities onto EITHER
    base — each is a curated compose overlay merged with `-f`. Available:
      * "prometheus"       — adds a Prometheus that scrapes the gateway's metrics
                             (Prometheus UI on host port 9090).
      * "redis-rate-limit" — points the gateway's rate-limit store at a bundled Redis
                             (Redis stays internal — no host port).
    They combine freely, e.g. features=["prometheus","redis-rate-limit"], and on the
    kafka base too — so `variant="kafka", features=[...]` is a Kafka stack with those
    add-ons. (These are the curated, coexist-safe equivalent of the one-shot
    `quicksetup_*` configs — prefer these when you want to MIX capabilities.)

    Instances (generalized coexist): pass a unique `instance` name to run MULTIPLE
    APIM stacks at once. instance="default" uses the canonical ports/project
    (gravitee-apim). A named instance gets its own project (gravitee-apim-<name>),
    its own data volumes, and an auto-allocated host-port band (+20000, +40000, …) —
    feature ports (e.g. prometheus :9090) shift by the same offset, so composed stacks
    coexist cleanly.

    Pulls the pinned version and `docker compose -p <project> up -d`, returning
    immediately; poll `apim_status(instance)`. On a port conflict it does NOT start —
    returns "port_conflict".

    IMPORTANT — do NOT decide coexist-vs-down on the user's behalf. When the user asks
    to bring up a stack, leave `instance="default"` unless they explicitly asked for a
    second/named one. If the canonical ports are taken, DON'T silently start a named
    coexist instance to dodge the conflict — instead surface the choice and let the user
    pick: down the conflicting stack (`down_conflicting=true` / `apim_down`) OR run
    alongside as a named `instance`. Run `stack_preflight` first to get that choice as a
    structured payload, and confirm version + variant with the user before launching.

    Args:
        version: Image tag to pin ("latest" resolves the newest stable APIM release).
        variant: gateway base — "default" or "kafka".
        instance: unique name to run several stacks at once (default "default").
        features: list of overlays to layer on, e.g. ["prometheus","redis-rate-limit"].
        pull: Pull images before up (default).
        down_conflicting: down conflicting projects first (no -v; data kept).
        recreate: `up -d --force-recreate`.
        license: Path to a license.key. Empty = APIM_LICENSE env, else ~/.gravitee/license.key.
    """
    if variant not in apim.VARIANTS:
        return {"status": "blocked", "message": f"unknown variant '{variant}'; use one of {list(apim.VARIANTS)}."}
    features = apim.normalize_features(features)
    bad = apim.unknown_features(features)
    if bad:
        return {"status": "blocked",
                "message": f"unknown feature(s) {bad}; available: {list(apim.FEATURES)}."}
    if not apim.supports_instances(variant) and instance != "default":
        return {"status": "blocked",
                "message": f"the {variant} variant is single-instance (fixed ports); use instance='default'."}
    if apim.is_up_running(instance):
        return {"status": "already_running",
                "message": f"APIM instance '{instance}' is already running. Use apim_status(instance='{instance}')."}

    docker_err = runner.docker_running_error()
    if docker_err:
        return {"status": "blocked", "message": docker_err}

    resolved, err = apim.resolve_version(version)
    if err:
        return {"status": "blocked", "message": err}

    license_path, license_src = apim.resolve_license(license)
    if apim.requires_license(variant) and not license_path:
        return {"status": "blocked",
                "message": ("the kafka variant needs an EE license with the Kafka Gateway "
                            "feature — none found. Drop it at ~/.gravitee/license.key, set "
                            "APIM_LICENSE, or pass license=/path/to/license.key.")}

    warnings = []
    mem = runner.docker_total_memory_gib()
    if variant == "kafka" and mem is not None and mem < 15.5:
        warnings.append(f"Docker has ~{mem:.1f} GiB; the Kafka stack wants >= 16 GiB.")

    offset = apim.allocate_offset(variant, instance, features)
    if offset is None:
        return {"status": "blocked",
                "message": f"no free host-port band for a new APIM instance (tried offsets up to {apim.MAX_OFFSET}). "
                           "Down an existing instance first (apim_list to see them)."}
    try:
        plan = apim.plan_ports(offset, variant, features)
    except (RuntimeError, ValueError) as e:
        return {"status": "blocked", "message": f"could not read compose config: {e}"}
    ports, port_env, role_ports = plan["ports"], plan["port_env"], plan["urls"]

    conflicts = apim.detect_conflicts(ports, variant, instance)
    downed = []
    if conflicts:
        if not down_conflicting:
            suggest = {"down_the_other": {"tool": "apim_up",
                                          "args": {"version": version, "instance": instance, "down_conflicting": True}}}
            if instance == "default":
                suggest["run_another_instance"] = {"tool": "apim_up", "args": {"version": version, "instance": "b"}}
            return {
                "status": "port_conflict",
                "version": resolved, "instance": instance, "offset": offset,
                "message": (
                    f"port(s) needed by APIM instance '{instance}' are held by other stack(s): "
                    f"{[(c['port'], c['project']) for c in conflicts]}. APIM was NOT started. "
                    "down_conflicting=true to free them"
                    + (", or run a named instance to coexist (e.g. instance='b')." if instance == "default" else ".")
                ),
                "conflicts": conflicts,
                "conflicting_projects": sorted({c["project"] for c in conflicts}),
                "suggest": suggest,
            }
        gamma_project = runner.docker_dir().name
        for proj in sorted({c["project"] for c in conflicts}):
            res = apim.down_project(proj)
            downed.append({"project": proj, "returncode": res["returncode"]})
            if proj == gamma_project:
                state.forget_up()

    log_path = apim.up_log_path(instance)
    log_path.write_bytes(b"")
    proc = apim.launch_up_background(resolved, pull, recreate, port_env, license_path,
                                    log_path, variant, instance, features)
    started = _now_iso()
    apim.record_up(proc, resolved, variant, instance, offset, license_path, role_ports, ports,
                   log_path, started, features)

    result = {
        "status": "starting",
        "version": resolved,
        "variant": variant,
        "features": features,
        "instance": instance,
        "project": apim.project_for(variant, instance),
        "mode": "coexist" if offset else "canonical",
        "port_offset": offset,
        "pid": proc.pid,
        "log_path": str(log_path),
        "compose_file": str(apim.compose_file(variant)),
        "pull": pull,
        "recreate": recreate,
        "license": {"mounted": bool(license_path), "path": license_path, "source": license_src},
        "downed_conflicts": downed,
        "ports": ports,
        "urls": _apim_urls(role_ports),
        "warnings": warnings,
        "started": started,
        "next": f"Poll apim_status(instance='{instance}') until overall: healthy.",
    }
    if variant == "kafka":
        result["kafka"] = _kafka_demo(apim.project_for("kafka", instance), role_ports)
    return result


def _kafka_demo(project: str, role_ports: dict) -> dict:
    """Ready-to-run guidance for the Kafka variant (accurate container names)."""
    client = f"{project}-kafka-client-1"
    broker = f"{project}-kafka-1"
    console = role_ports.get("console", 8084)
    return {
        "bootstrap": "foo.kafka.local:9092 (or bar.; TLS/SNI)",
        "verify_listener": "apim_logs('apim-gateway') → 'Kafka server ready to accept connections on port 9092'",
        "one_time_console_setup": [
            f"Open http://localhost:{console} (admin/admin). This config lives in Mongo, so do it once per fresh build.",
            "Organization → Entrypoints & Sharding Tags → Entrypoint Configuration → "
            "Default Kafka Bootstrap Domain Pattern = {apiHost}.kafka.local  "
            "(the field defaults to just {apiHost} — the .kafka.local suffix MUST be appended so "
            "SNI/DNS line up with the *.kafka.local cert + foo.kafka.local alias). Default Kafka port = 9092.",
            "Create a Kafka API: Protocol Kafka, host prefix 'foo', endpoint PLAINTEXT to kafka:9091, "
            "Keyless plan, Save & Deploy.",
        ],
        "commands": {
            # single-line (zsh breaks on multi-line pastes with # comments or \ continuations)
            "produce": (f"docker exec -it {client} bash -c \"/opt/kafka/bin/kafka-console-producer.sh "
                        "--bootstrap-server foo.kafka.local:9092 "
                        "--producer.config /app/config/kafka-keyless-plan-ssl.properties --topic client-topic-1\""),
            "consume_via_gateway": (f"docker exec -it {client} bash -c \"/opt/kafka/bin/kafka-console-consumer.sh "
                        "--bootstrap-server foo.kafka.local:9092 "
                        "--consumer.config /app/config/kafka-keyless-plan-ssl.properties --topic client-topic-1 "
                        "--from-beginning --group demo-grp --timeout-ms 20000\""),
            "verify_broker_direct": (f"docker exec {broker} /opt/kafka/bin/kafka-console-consumer.sh "
                        "--bootstrap-server localhost:9091 --topic client-topic-1 "
                        "--partition 0 --offset earliest --timeout-ms 15000"),
        },
        "notes": [
            "First produce to a new topic logs a one-time UNKNOWN_TOPIC_OR_PARTITION warning — benign "
            "(auto.create.topics.enable=true).",
            "Consumer-group reads now work: the broker sets offsets.topic.replication.factor=1 (single-broker), "
            "so __consumer_offsets is created and group coordination succeeds. For a coordination-free sanity "
            "check use verify_broker_direct (--partition 0 --offset earliest).",
            "kafka + kafka-client have restart: unless-stopped, so they survive a Docker/VM restart; after an "
            "explicit apim_down they're removed — apim_up(variant='kafka') to bring them back.",
        ],
    }


@mcp.tool()
def apim_status(instance: str = "default") -> dict:
    """Status of an APIM instance: overall verdict + per-service health, version,
    variant, project, and access URLs. Pass `instance` to target a named stack."""
    return _apim_summarize(instance)


@mcp.tool()
def apim_list() -> dict:
    """List all tracked APIM instances (for generalized coexist) with their status."""
    instances = []
    for name in apim.known_instances():
        s = _apim_summarize(name)
        instances.append({"instance": name, "overall": s["overall"], "variant": s["variant"],
                          "features": s.get("features"), "version": s["version"],
                          "project": s["project"], "mode": s["mode"], "urls": s["urls"]})
    return {"status": "ok", "count": len(instances), "instances": instances}


@mcp.tool()
def apim_down(instance: str = "default", timeout_seconds: int = 180) -> dict:
    """Stop an APIM instance (`docker compose down`, volumes preserved)."""
    variant = apim.current_variant(instance)
    result = apim.run_down(timeout_seconds, variant, instance, apim.current_features(instance))
    apim.forget_up(instance)
    if result["timed_out"]:
        return {"status": "timeout", "instance": instance, "message": f"down exceeded {timeout_seconds}s.",
                "stdout_tail": (result["stdout"] or "")[-2000:],
                "stderr_tail": (result["stderr"] or "")[-2000:]}
    return {"status": "ok" if result["returncode"] == 0 else "error", "instance": instance,
            "returncode": result["returncode"],
            "stdout": result["stdout"], "stderr": result["stderr"]}


@mcp.tool()
def apim_logs(service: str, lines: int = 100, instance: str = "default") -> dict:
    """Tail logs for one service of an APIM instance (incl. feature services like
    apim-prometheus / apim-redis)."""
    variant = apim.current_variant(instance)
    features = apim.current_features(instance)
    valid = apim.service_names(variant, instance, features)
    if service not in valid:
        return {"status": "invalid_service", "message": f"unknown service '{service}'.",
                "valid_services": valid}
    p = apim.compose_logs(service, lines, variant, instance, features)
    return {"status": "ok" if p.returncode == 0 else "error", "service": service,
            "instance": instance, "lines": lines, "returncode": p.returncode,
            "logs": p.stdout or p.stderr}


@mcp.tool()
def apim_latest_version() -> dict:
    """Resolve the newest stable Gravitee APIM release tag (from the APIM repo)."""
    resolved, err = apim.resolve_version("latest")
    if err:
        return {"status": "error", "message": err}
    return {"status": "ok", "latest_version": resolved,
            "repo": "https://github.com/gravitee-io/gravitee-api-management"}


# ── standalone AM (Access Management) stack tools ─────────────────────────────
def _am_summarize(instance: str = "default") -> dict:
    up = am.up_process_status(instance)
    rows = am.compose_ps(instance)
    expected = am.service_names(instance)
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

    port = up.get("port") or am.current_port(instance)
    return {
        "overall": overall,
        "instance": instance,
        "version": up.get("version") or am.current_version(instance),
        "port": port,
        "project": am.project_for(instance),
        "up_process": up,
        "services": [{"service": s, **{k: by.get(s, {}).get(k) for k in ("state", "health", "exit_code")},
                      "label": labels[s]} for s in expected],
        "problems": [{"service": s, "label": labels[s]} for s in expected
                     if labels[s] in _BAD or labels[s] == "missing"],
        "urls": am.urls_for(port),
        "up_log_tail": runner.tail_file(am.up_log_path(instance), 40),
        "checked_at": _now_iso(),
    }


@mcp.tool()
def am_up(version: str = "latest", instance: str = "default", port: int = 0,
          pull: bool = True, recreate: bool = False, down_conflicting: bool = False) -> dict:
    """Stand up a standalone Gravitee Access Management (AM) stack (background).

    Self-contained stack (nginx + mongo + gateway + management-api + console). Only
    nginx is published to the host. Poll `am_status(instance)` (the management API is
    slow to become ready, so wait for overall: healthy).

    Instances (generalized coexist): pass a unique `instance` to run MULTIPLE AM
    stacks at once. instance="default" uses project gravitee-am on AM_NGINX_PORT
    (8086); a named instance gets project gravitee-am-<name> on an auto-allocated
    free port (or the explicit `port`).

    IMPORTANT — do NOT decide coexist-vs-down for the user. Leave `instance="default"`
    unless they explicitly asked for a second one; if the port is taken, surface the
    choice (down the conflicting stack vs run a named `instance`) rather than silently
    coexisting. `stack_preflight` returns that choice as a structured payload.

    Args:
        version: Image tag to pin (GIO_AM_VERSION). "latest" resolves the newest stable.
        instance: unique name to run several stacks at once (default "default").
        port: Host port for nginx. 0 = auto (default instance → 8086; named → next free).
        pull: Pull images before up (default).
        recreate: `up -d --force-recreate`.
        down_conflicting: Down the project holding the port first (no -v; data kept).
    """
    if am.is_up_running(instance):
        return {"status": "already_running",
                "message": f"AM instance '{instance}' is already running. Use am_status(instance='{instance}')."}

    docker_err = runner.docker_running_error()
    if docker_err:
        return {"status": "blocked", "message": docker_err}

    resolved, err = am.resolve_version(version)
    if err:
        return {"status": "blocked", "message": err}

    nginx_port = am.allocate_port(instance, port)
    if nginx_port is None:
        return {"status": "blocked", "message": "no free host port for a new AM instance."}

    conflict = am.conflict_on(nginx_port, instance)
    downed = []
    if conflict:
        if not down_conflicting:
            suggest = {
                "different_port": {"tool": "am_up", "args": {"version": version, "instance": instance, "port": nginx_port + 1}},
                "down_the_other": {"tool": "am_up", "args": {"version": version, "instance": instance, "down_conflicting": True}},
            }
            if instance == "default":
                suggest["run_another_instance"] = {"tool": "am_up", "args": {"version": version, "instance": "b"}}
            return {
                "status": "port_conflict", "version": resolved, "instance": instance,
                "message": (
                    f"port {nginx_port} is held by project '{conflict['project']}' "
                    f"(container {conflict['container']}). AM instance '{instance}' was NOT started. "
                    "Pass a different port=..., run a named instance, or down_conflicting=true."
                ),
                "conflict": conflict, "suggest": suggest,
            }
        res = am.down_project(conflict["project"])
        downed.append({"project": conflict["project"], "returncode": res["returncode"]})
        if conflict["project"] == runner.docker_dir().name:
            state.forget_up()

    log_path = am.up_log_path(instance)
    log_path.write_bytes(b"")
    proc = am.launch_up_background(resolved, nginx_port, pull, recreate, log_path, instance)
    started = _now_iso()
    am.record_up(proc, resolved, nginx_port, instance, log_path, started)

    return {
        "status": "starting",
        "version": resolved,
        "instance": instance,
        "project": am.project_for(instance),
        "port": nginx_port,
        "pid": proc.pid,
        "log_path": str(log_path),
        "compose_file": str(am.compose_file()),
        "pull": pull,
        "recreate": recreate,
        "downed_conflicts": downed,
        "urls": am.urls_for(nginx_port),
        "started": started,
        "next": f"Poll am_status(instance='{instance}') until overall: healthy (mgmt API takes a while).",
    }


@mcp.tool()
def am_status(instance: str = "default") -> dict:
    """Status of an AM instance: overall verdict + per-service health, version, port,
    project, and access URLs. Pass `instance` to target a named stack."""
    return _am_summarize(instance)


@mcp.tool()
def am_list() -> dict:
    """List all tracked AM instances (for generalized coexist) with their status."""
    instances = []
    for name in am.known_instances():
        s = _am_summarize(name)
        instances.append({"instance": name, "overall": s["overall"], "version": s["version"],
                          "port": s["port"], "project": s["project"], "urls": s["urls"]})
    return {"status": "ok", "count": len(instances), "instances": instances}


@mcp.tool()
def am_down(instance: str = "default", timeout_seconds: int = 180) -> dict:
    """Stop an AM instance (`docker compose down`, volumes preserved)."""
    result = am.run_down(timeout_seconds, instance)
    am.forget_up(instance)
    if result["timed_out"]:
        return {"status": "timeout", "instance": instance, "message": f"down exceeded {timeout_seconds}s.",
                "stdout_tail": (result["stdout"] or "")[-2000:],
                "stderr_tail": (result["stderr"] or "")[-2000:]}
    return {"status": "ok" if result["returncode"] == 0 else "error", "instance": instance,
            "returncode": result["returncode"],
            "stdout": result["stdout"], "stderr": result["stderr"]}


@mcp.tool()
def am_logs(service: str, lines: int = 100, instance: str = "default") -> dict:
    """Tail logs for one service of an AM instance."""
    valid = am.service_names(instance)
    if service not in valid:
        return {"status": "invalid_service", "message": f"unknown service '{service}'.",
                "valid_services": valid}
    p = am.compose_logs(service, lines, instance)
    return {"status": "ok" if p.returncode == 0 else "error", "service": service,
            "instance": instance, "lines": lines, "returncode": p.returncode,
            "logs": p.stdout or p.stderr}


@mcp.tool()
def am_latest_version() -> dict:
    """Resolve the newest stable Gravitee AM release tag (from the AM repo)."""
    resolved, err = am.resolve_version("latest")
    if err:
        return {"status": "error", "message": err}
    return {"status": "ok", "latest_version": resolved,
            "repo": "https://github.com/gravitee-io/gravitee-access-management"}


# ── generic quick-setup runner (any docker/quick-setup/* config) ──────────────
def _quicksetup_summarize(name: str) -> dict:
    up = quicksetup.up_process_status(name)
    rows = quicksetup.compose_ps(name)
    expected = quicksetup.service_names(name)
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
        "name": name,
        "version": up.get("version") or quicksetup.current_version(name),
        "project": quicksetup.project_for(name),
        "up_process": up,
        "ports": up.get("ports"),
        "services": [{"service": s, **{k: by.get(s, {}).get(k) for k in ("state", "health", "exit_code")},
                      "label": labels[s]} for s in expected],
        "problems": [{"service": s, "label": labels[s]} for s in expected
                     if labels[s] in _BAD or labels[s] == "missing"],
        # A known gotcha here means overall= may lie (e.g. 'healthy' but non-functional,
        # or 'partial' from a false-unhealthy healthcheck). Surface it alongside.
        "gotcha": quicksetup.gotcha_for(name),
        "up_log_tail": runner.tail_file(quicksetup.up_log_path(name), 40),
        "checked_at": _now_iso(),
    }


@mcp.tool()
def quicksetup_list(version: str = "latest") -> dict:
    """List every official APIM `docker/quick-setup/*` config at the given version.

    These are the upstream ready-made compose configs (mongodb, postgresql,
    redis-rate-limit, keycloak, native-kafka, opensearch, prometheus, https-*,
    distributed-sync*, ee-with-alert-engine, …). `quicksetup_up(name)` fetches and
    runs one as-is. For the polished happy-path OSS/Kafka stacks prefer apim_up.

    Also flags which configs already have a tracked up-record locally.
    """
    resolved, err = quicksetup.resolve_version(version)
    if err:
        return {"status": "error", "message": err}
    names, err = quicksetup.list_configs(resolved)
    if err:
        return {"status": "error", "version": resolved, "message": err}
    gotchas = {n: {"severity": quicksetup.GOTCHAS[n]["severity"],
                   "summary": quicksetup.GOTCHAS[n]["summary"]}
               for n in names if n in quicksetup.GOTCHAS}
    return {"status": "ok", "version": resolved, "count": len(names),
            "configs": names, "running_locally": quicksetup.known_configs(),
            "known_gotchas": gotchas,
            "note": "Run one with quicksetup_up(name). One at a time — these composes "
                    "hardcode ports/container names, so they can't coexist. `known_gotchas` "
                    "flags configs that are broken/misleading as shipped (from a functional "
                    "sweep); quicksetup_up auto-applies the safe fixes and warns on the rest."}


@mcp.tool()
def quicksetup_up(name: str, version: str = "latest", pull: bool = True,
                  recreate: bool = False, down_conflicting: bool = False) -> dict:
    """Fetch an official APIM quick-setup config and stand it up (background, non-blocking).

    Fetches `docker/quick-setup/<name>` from the APIM repo at the pinned version, copies
    it into a local workdir, drops ~/.gravitee/license.key in if the config mounts one,
    then `docker compose -p gravitee-qs-<name> up -d`. Returns immediately — poll
    `quicksetup_status(name)`.

    IMPORTANT — this runs the UPSTREAM config verbatim, so it inherits that config's
    gotchas and any MANUAL steps (keycloak realm import, native-kafka console setup,
    mssql/postgres backends, …). The fetched README is returned here — read it and relay
    the manual steps to the user. For the curated, fully-automated OSS or native-Kafka
    stacks, prefer `apim_up` instead.

    NO coexist: these composes hardcode host ports (mostly 8082–8085) and container
    names, so only ONE quick-setup runs at a time. On a port conflict it does NOT start
    (status "port_conflict"); ask the user to down the other stack (down_conflicting=true)
    — there is no port-shift option here.

    Args:
        name: config name from quicksetup_list (e.g. "redis-rate-limit", "keycloak").
        version: APIM tag to pin ("latest" resolves the newest stable release).
        pull: pull images before up (default).
        recreate: `up -d --force-recreate`.
        down_conflicting: down any project holding the needed ports first (no -v; data kept).
    """
    if quicksetup.is_up_running(name):
        return {"status": "already_running",
                "message": f"quick-setup '{name}' is already running. Use quicksetup_status(name='{name}')."}

    docker_err = runner.docker_running_error()
    if docker_err:
        return {"status": "blocked", "message": docker_err}

    resolved, err = quicksetup.resolve_version(version)
    if err:
        return {"status": "blocked", "message": err}

    fetched, err = quicksetup.fetch(name, resolved)
    if err:
        return {"status": "blocked", "message": err}

    warnings = []
    if fetched.needs_license and not fetched.license_mounted:
        warnings.append(
            f"'{name}' mounts ./.license but no license was found (checked ~/.gravitee/license.key "
            "and APIM_LICENSE). Enterprise features in this config won't start; OSS parts still will.")
    if fetched.gotcha and fetched.gotcha["severity"] == "broken":
        applied = [a["fix"] for a in (fetched.autofixes or []) if a.get("applied")]
        prefix = (f"auto-applied {len(applied)} fix(es) {applied}; " if applied else "")
        warnings.append(
            f"KNOWN GOTCHA ({name}): {prefix}{fetched.gotcha['summary']} FIX: {fetched.gotcha['fix']}")

    try:
        ports = quicksetup.published_ports(name)
    except (RuntimeError, ValueError) as e:
        return {"status": "blocked", "message": f"could not read compose config: {e}"}

    conflicts = quicksetup.detect_conflicts(name, ports)
    downed = []
    if conflicts:
        if not down_conflicting:
            return {
                "status": "port_conflict", "name": name, "version": resolved, "ports": ports,
                "message": (
                    f"port(s) needed by quick-setup '{name}' are held by other stack(s): "
                    f"{[(c['port'], c['project']) for c in conflicts]}. It was NOT started. "
                    "These configs can't run on shifted ports — down the conflicting stack "
                    "(down_conflicting=true) or stop whatever holds the ports, then retry."),
                "conflicts": conflicts,
                "conflicting_projects": sorted({c["project"] for c in conflicts}),
                "suggest": {"down_conflicting": {"tool": "quicksetup_up",
                                                 "args": {"name": name, "version": version, "down_conflicting": True}}},
            }
        gamma_project = runner.docker_dir().name
        for proj in sorted({c["project"] for c in conflicts}):
            res = apim.down_project(proj)
            downed.append({"project": proj, "returncode": res["returncode"]})
            if proj == gamma_project:
                state.forget_up()

    log_path = quicksetup.up_log_path(name)
    log_path.write_bytes(b"")
    proc = quicksetup.launch_up_background(name, resolved, pull, recreate, log_path)
    started = _now_iso()
    license_path = fetched.license_source and str(quicksetup.workdir(name) / ".license" / "license.key")
    quicksetup.record_up(proc, name, resolved, ports, license_path, log_path, started)

    return {
        "status": "starting",
        "name": name,
        "version": resolved,
        "project": quicksetup.project_for(name),
        "workdir": fetched.workdir,
        "pid": proc.pid,
        "log_path": str(log_path),
        "pull": pull,
        "recreate": recreate,
        "license": {"needed": fetched.needs_license, "mounted": fetched.license_mounted,
                    "source": fetched.license_source},
        "downed_conflicts": downed,
        "ports": ports,
        "warnings": warnings,
        "gotcha": fetched.gotcha,
        "autofixes": fetched.autofixes,
        "started": started,
        "readme": quicksetup.readme(name),
        "next": f"Poll quicksetup_status(name='{name}') until overall: healthy. "
                + ("NOTE: this config has a known gotcha (see `gotcha`) — "
                   "'healthy' may not mean functional. " if fetched.gotcha else "")
                + "Read `readme` above for any manual steps this config needs.",
    }


@mcp.tool()
def quicksetup_status(name: str) -> dict:
    """Status of a running quick-setup config: overall verdict + per-service health,
    version, project, ports, and the up-log tail."""
    return _quicksetup_summarize(name)


@mcp.tool()
def quicksetup_down(name: str, timeout_seconds: int = 180, volumes: bool = False) -> dict:
    """Stop a quick-setup config (`docker compose down`). Pass volumes=true to also drop
    its data volumes (`down -v`); default preserves them."""
    result = quicksetup.run_down(name, timeout_seconds, volumes)
    quicksetup.forget_up(name)
    if result["timed_out"]:
        return {"status": "timeout", "name": name, "message": f"down exceeded {timeout_seconds}s.",
                "stdout_tail": (result["stdout"] or "")[-2000:],
                "stderr_tail": (result["stderr"] or "")[-2000:]}
    return {"status": "ok" if result["returncode"] == 0 else "error", "name": name,
            "volumes_removed": volumes, "returncode": result["returncode"],
            "stdout": result["stdout"], "stderr": result["stderr"]}


@mcp.tool()
def quicksetup_logs(name: str, service: str, lines: int = 100) -> dict:
    """Tail logs for one service of a running quick-setup config."""
    valid = quicksetup.service_names(name)
    if service not in valid:
        return {"status": "invalid_service", "message": f"unknown service '{service}'.",
                "valid_services": valid}
    p = quicksetup.compose_logs(name, service, lines)
    return {"status": "ok" if p.returncode == 0 else "error", "service": service,
            "name": name, "lines": lines, "returncode": p.returncode,
            "logs": p.stdout or p.stderr}


@mcp.tool()
def stack_preflight(kind: str = "apim", version: str = "latest", variant: str = "default") -> dict:
    """Preview a stack bring-up WITHOUT starting it — the guided-launch helper.

    Resolves the version, computes the stack's canonical host ports, and checks whether
    anything already holds them. Returns a structured recommendation so you can ask the
    user which path they want BEFORE launching:
      * status "clear"    → ports free; use the returned `start` option.
      * status "conflict" → ports held; present the user with `down_conflicting` (free
        the ports by downing the other stack) OR `coexist` (run the new stack alongside
        as a named instance on shifted ports), and let them choose.

    RECOMMENDED FLOW when a user asks to bring up a stack:
      1. Ask which VERSION (latest, or a specific tag) and — for APIM — which VARIANT
         (default | kafka).
      2. Call stack_preflight with those.
      3. If status is "conflict", ask the user: down the conflicting stack, or run in
         coexist mode? Then call apim_up/am_up with the chosen option (down_conflicting
         or a named `instance`). If "clear", just call the `start` option.

    Args:
        kind: "apim" or "am".
        version: "latest" or a specific tag (e.g. "4.12.7").
        variant: APIM only — "default" or "kafka".
    """
    kind = kind.lower()
    if kind not in ("apim", "am"):
        return {"status": "error", "message": "kind must be 'apim' or 'am'."}

    if kind == "apim":
        if variant not in apim.VARIANTS:
            return {"status": "error", "message": f"unknown variant '{variant}'; use {list(apim.VARIANTS)}."}
        resolved, err = apim.resolve_version(version)
        if err:
            return {"status": "error", "message": err}
        try:
            ports = apim.plan_ports(0, variant)["ports"]
        except (RuntimeError, ValueError) as e:
            return {"status": "error", "message": f"could not read compose config: {e}"}
        conflicts = apim.detect_conflicts(ports, variant, "default")
        can_coexist = apim.supports_instances(variant)
        up_tool, up_base = "apim_up", {"version": version, "variant": variant}
        coexist_ports = apim.plan_ports(apim.DEFAULT_PORT_OFFSET, variant)["ports"] if can_coexist else None
        license_note = ("the kafka variant needs an EE license with the Kafka feature"
                        if variant == "kafka" else None)
    else:
        resolved, err = am.resolve_version(version)
        if err:
            return {"status": "error", "message": err}
        port = am.default_port()
        ports = [port]
        holder = am.conflict_on(port, "default")
        conflicts = [holder] if holder else []
        can_coexist, up_tool, up_base = True, "am_up", {"version": version}
        coexist_ports, license_note = None, None

    if not conflicts:
        return {
            "status": "clear", "kind": kind, "resolved_version": resolved,
            "variant": variant if kind == "apim" else None, "ports": ports,
            "license_note": license_note,
            "message": f"ports {ports} are free — ready to start {kind} {resolved}.",
            "options": {"start": {"tool": up_tool, "args": up_base}},
        }

    options = {"down_conflicting": {"tool": up_tool, "args": {**up_base, "down_conflicting": True}}}
    if can_coexist:
        options["coexist"] = {"tool": up_tool, "args": {**up_base, "instance": "b"},
                              "ports": coexist_ports or "next free port"}
    return {
        "status": "conflict", "kind": kind, "resolved_version": resolved,
        "variant": variant if kind == "apim" else None, "ports": ports,
        "license_note": license_note,
        "conflicts": conflicts,
        "conflicting_projects": sorted({c["project"] for c in conflicts}),
        "message": (
            f"{kind} {resolved} needs port(s) {sorted({c['port'] for c in conflicts})}, held by "
            f"{sorted({c['project'] for c in conflicts})}. Ask the user: down the conflicting "
            "stack (down_conflicting), or run the new one in coexist mode (a named instance on "
            "shifted ports)?"),
        "options": options,
    }


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
