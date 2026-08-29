"""FastMCP server exposing the Gravitee stacker tools.

Self-contained, public-image Docker stacks: APIM (apim_*, incl. native-Kafka + composable
features), Access Management (am_*), the full Gamma platform (gamma_*), and any official
quick-setup config (quicksetup_*), plus plugin management (apim_plugin_*), license inspection
(apim_license / gamma_license), and the guided-launch helper (stack_preflight) + doctor.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from . import am, apim, gamma, plugins, quicksetup, runner

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


def _wait_ready(summarize, instance: str, timeout_seconds: int, poll_seconds: int, kind: str) -> dict:
    """Block until `summarize(instance)['overall']` is healthy, then return at once.

    The server-side, readiness-aware replacement for an agent's `sleep`-loop. Polls actual
    container health at a SHORT interval and returns the instant the stack is healthy;
    fails FAST on a non-zero launcher exit ("failed") or nothing running ("down") instead
    of burning the whole cap. `timeout_seconds` is a safety ceiling, not a fixed sleep.
    """
    poll = max(1, min(int(poll_seconds or 4), 30))
    start = time.monotonic()
    deadline = start + max(1, int(timeout_seconds or 1))
    saw_ok = False
    while True:
        waited = round(time.monotonic() - start, 1)
        try:
            s = summarize(instance)
        except Exception as e:
            # First-ever probe failing = a setup/config problem the wait can't outlast (e.g.
            # the stack was never started, or a repo dir is missing). Return at once. A failure
            # AFTER a prior good read is treated as a transient hiccup and retried to the cap.
            if not saw_ok:
                return {"status": "error", "ready": False, "instance": instance, "waited_seconds": waited,
                        "error": str(e),
                        "hint": f"couldn't read {kind} status — is the stack started? call {kind}_up first."}
            if time.monotonic() >= deadline:
                return {"status": "timeout", "ready": False, "instance": instance, "waited_seconds": waited,
                        "error": str(e), "hint": "status probe kept failing near the deadline."}
            time.sleep(poll)
            continue
        saw_ok = True
        overall = s.get("overall")
        if overall == "healthy":
            return {"status": "ready", "ready": True, "overall": overall, "instance": instance,
                    "waited_seconds": waited, "services": s.get("services"), "urls": s.get("urls")}
        if overall == "failed":
            return {"status": "failed", "ready": False, "overall": overall, "instance": instance,
                    "waited_seconds": waited, "problems": s.get("problems"),
                    "up_log_tail": s.get("up_log_tail"),
                    "hint": f"the launcher exited non-zero — inspect {kind}_logs / the up_log_tail; "
                            "waiting longer won't help."}
        if overall == "down":
            return {"status": "down", "ready": False, "overall": overall, "instance": instance,
                    "waited_seconds": waited,
                    "hint": f"nothing is running for instance '{instance}' — call {kind}_up first."}
        # overall is "starting" (launcher/pull in progress) or "partial" (some services not yet
        # healthy) — both are normal boot states; keep polling until healthy or the cap.
        if time.monotonic() >= deadline:
            return {"status": "timeout", "ready": False, "overall": overall, "instance": instance,
                    "waited_seconds": waited, "problems": s.get("problems"), "services": s.get("services"),
                    "hint": ("not healthy within the cap — it may still be pulling images on a first-ever "
                             f"run (raise timeout_seconds), or a service is stuck (check {kind}_logs). "
                             "This is a ceiling, not a fixed wait.")}
        time.sleep(poll)


@mcp.tool()
def apim_wait(instance: str = "default", timeout_seconds: int = 420, poll_seconds: int = 4) -> dict:
    """Block until an APIM instance is actually healthy, then return immediately.

    The readiness-aware alternative to sleeping in a loop after apim_up. Polls real
    container health every `poll_seconds` and returns the MOMENT `overall` flips to
    "healthy" — so the common case is fast, not a fixed wait. Fails FAST if the launcher
    exited non-zero ("failed") or nothing is running ("down") rather than burning the
    whole cap. `timeout_seconds` is a SAFETY CEILING, not a sleep: raise it for a
    first-ever image pull (a cold native-Kafka stack can exceed the default); the call
    still returns early the instant it's healthy. Do NOT wrap this in your own sleep loop
    — that's the exact pattern it replaces.
    """
    return _wait_ready(_apim_summarize, instance, timeout_seconds, poll_seconds, "apim")


@mcp.tool()
def apim_up(version: str = "latest", variant: str = "default", instance: str = "default",
            features: list = None, pull: bool = True, down_conflicting: bool = False,
            recreate: bool = False, license: str = "") -> dict:
    """Stand up a standalone Gravitee APIM stack (background, non-blocking).

    Variants (the gateway BASE):
      * "default" (OSS): mongo + es + gateway + management-api + console + portal.
      * "kafka": the native-Kafka gateway stack (adds a KRaft broker + kafka-client;
        gateway binds a Kafka listener on :9092 TLS). REQUIRES an EE license. Single
        instance — fixed *.kafka.local certs and broker ports (9091, 9093–9096 are
        literals; a port offset can't resolve the cert/hostname collision, so coexist
        is unavailable for kafka — unlike the default variant).

    Features (composable add-ons): pass `features` to layer capabilities onto EITHER
    base — each is a curated compose overlay merged with `-f`. Available:
      * "prometheus"       — adds a Prometheus that scrapes the gateway's metrics
                             (Prometheus UI on host port 9090).
      * "redis-rate-limit" — points the gateway's rate-limit store at a bundled Redis
                             (Redis stays internal — no host port).
      * "debug-logging"    — verbose DEBUG logs from the gateway + management-api
                             (io.gravitee at DEBUG). **ON BY DEFAULT** — see below.
      * "alert-engine"     — Gravitee Alert Engine wired to the gateway container-to-
                             container (fixes what the ee-with-alert-engine quick-setup
                             can't). **Requires an EE license.** Alerts are still created
                             manually in the console.
    They combine freely, e.g. features=["prometheus","redis-rate-limit"], and on the
    kafka base too — so `variant="kafka", features=[...]` is a Kafka stack with those
    add-ons. (These are the curated, coexist-safe equivalent of the one-shot
    `quicksetup_*` configs — prefer these when you want to MIX capabilities.)

    Custom overlays (the list above is NOT closed): drop an `apim-feature-<name>.yml`
    into `~/.gravitee/stacker-features/` (override the dir with `APIM_FEATURES_DIR`) and
    pass `<name>` in `features` exactly like a built-in — it's merged with `-f` the same
    way. A user overlay of the same name as a bundled one TAKES PRECEDENCE, so you can
    shadow a built-in. Because this dir lives OUTSIDE the installed package, your overlays
    survive tool upgrades (they're never wiped) and never ship with the tool. This is also
    the answer to "how do I mount an extra file / env into the gateway" — author a small
    overlay here rather than editing site-packages. Your overlay can reference the same
    env vars the built-in overlays use — they're exported even for read-only validation:
    `${APIM_LICENSE}` (resolved license path, mirrors apim-license.yml), `${HOME}`,
    `${APIM_PLUGINS_DIR}`, `${APIM_VERSION}` — so overlays stay portable (no hardcoded
    absolute host paths needed).

    Instances (generalized coexist): pass a unique `instance` name to run MULTIPLE
    APIM stacks at once. instance="default" uses the canonical ports/project
    (gravitee-apim). A named instance gets its own project (gravitee-apim-<name>),
    its own data volumes, and an auto-allocated host-port band (+20000, +40000, …) —
    feature ports (e.g. prometheus :9090) shift by the same offset, so composed stacks
    coexist cleanly.

    Pulls the pinned version and `docker compose -p <project> up -d`, returning
    immediately. Then call `apim_wait(instance)` — it blocks only until the stack is
    actually healthy and returns at once (fails fast on error), so you never sleep-loop;
    `apim_status(instance)` remains for a one-shot check. On a port conflict it does NOT
    start — returns "port_conflict".

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
        features: overlays to layer on, e.g. ["prometheus","redis-rate-limit"]. DEFAULTS
            ("debug-logging") are applied to EVERY deploy on top of whatever you pass —
            opt out per-deploy by prefixing with "-" (features=["-debug-logging"]), or
            machine-wide via APIM_DEFAULT_FEATURES="". The result is reported back as
            `features`, so you can always see what actually got layered on.
        pull: Pull images before up (default).
        down_conflicting: down conflicting projects first (no -v; data kept).
        recreate: `up -d --force-recreate`. ALSO: on an already-running instance whose
            requested version/variant/features DIFFER, recreate=True reconfigures it in
            place (down+up, volumes kept) in one call — the fast path for config iteration.
        license: Path to a license.key. Empty = APIM_LICENSE env, else ~/.gravitee/license.key.
    """
    if variant not in apim.VARIANTS:
        return {"status": "blocked", "message": f"unknown variant '{variant}'; use one of {list(apim.VARIANTS)}."}
    # Defaults-on: DEFAULT_FEATURES are layered into every deploy; "-name" opts out.
    features = apim.resolve_features(features)
    bad = apim.unknown_features(features)
    if bad:
        return {"status": "blocked",
                "message": f"unknown feature(s) {bad}; built-in: {list(apim.FEATURES)} "
                           f"(prefix with '-' to opt out of a default: {list(apim.default_features())}). "
                           f"For a CUSTOM feature, drop apim-feature-<name>.yml into "
                           f"{apim.features_dir()} (or set APIM_FEATURES_DIR) and pass <name> here.",
                "features_dir": str(apim.features_dir())}
    if not apim.supports_instances(variant) and instance != "default":
        return {"status": "blocked",
                "message": (f"the {variant} variant is single-instance — fixed *.kafka.local certs and "
                            "broker ports (9091, 9093–9096 are literals; a port offset can't resolve the "
                            "cert/hostname collision). Use instance='default'.")}
    docker_err = runner.docker_running_error()
    if docker_err:
        return {"status": "blocked", "message": docker_err}

    resolved, err = apim.resolve_version(version)
    if err:
        return {"status": "blocked", "message": err}

    # "Already up" = the tracked launcher is alive (mid-start) OR the project has running
    # containers (authoritative: the launcher exits once `up -d` returns). When the requested
    # version/variant/features DIFFER from what's running, recreate=True reconfigures IN PLACE
    # (down+up, volumes kept) so a config-iteration is ONE call, not down-then-up.
    if apim.is_up_running(instance) or apim.stack_running(variant, instance):
        cur_variant = apim.current_variant(instance) or variant
        cur_version = apim.current_version(instance)
        cur_features = list(apim.current_features(instance) or [])
        same_config = (cur_variant == variant and cur_version in (None, resolved)
                       and set(cur_features) == set(features))
        project = apim.project_for(variant, instance)
        if same_config or not recreate:
            running_config = {"variant": cur_variant, "version": cur_version, "features": sorted(cur_features)}
            requested_config = {"variant": variant, "version": resolved, "features": sorted(features)}
            if same_config:
                msg = (f"APIM instance '{instance}' is already running with the SAME config "
                       f"(project {project}); nothing to do — inspect apim_status(instance='{instance}').")
            else:
                msg = (f"APIM instance '{instance}' is already running with a DIFFERENT config "
                       "(see running_config vs requested_config). To APPLY the change in ONE call, "
                       f"re-run apim_up(instance='{instance}', recreate=True, …) — it reconfigures in "
                       f"place (down+up, volumes kept). Or apim_down(instance='{instance}') first.")
            return {"status": "already_running", "instance": instance, "project": project,
                    "same_config": same_config, "running_config": running_config,
                    "requested_config": requested_config, "message": msg}
        # Different config + recreate=True → reconfigure IN PLACE: down the running project
        # (keep volumes) using its CURRENT variant/features, then fall through to the normal
        # up path below with the new config.
        apim.run_down(180, variant=cur_variant, instance=instance, features=cur_features)

    license_path, license_src = apim.resolve_license(license)
    if apim.requires_license(variant) and not license_path:
        return {"status": "blocked",
                "message": ("the kafka variant needs an EE license with the Kafka Gateway "
                            "feature — none found. Drop it at ~/.gravitee/license.key, set "
                            "APIM_LICENSE, or pass license=/path/to/license.key.")}
    if apim.features_require_license(features) and not license_path:
        return {"status": "blocked",
                "message": (f"feature(s) {[f for f in features if f in apim.FEATURES_REQUIRING_LICENSE]} "
                            "need an EE license — none found. Drop it at ~/.gravitee/license.key, set "
                            "APIM_LICENSE, or pass license=/path/to/license.key.")}

    warnings = []
    mem = runner.docker_total_memory_gib()
    if variant == "kafka" and mem is not None and mem < 15.5:
        warnings.append(f"Docker has ~{mem:.1f} GiB; the Kafka stack wants >= 16 GiB.")

    # Feature gotchas (surfaced so an assistant sees them). For alert-engine on a FRESH
    # volume set specifically, the gateway must be restarted after healthy or alerts never
    # fire — check volume freshness BEFORE `up` creates them.
    feature_gotchas = [g for f in features if (g := apim.feature_gotcha(f))]
    fresh_volumes = apim.volumes_fresh(variant, instance)
    if "alert-engine" in features and fresh_volumes:
        warnings.append(
            "ALERT-ENGINE + FRESH VOLUMES: once the stack is healthy you MUST restart the "
            "gateway — apim_alert_engine_fix(instance) — or alerts SILENTLY never fire "
            "(the gateway caches installation=null on a cold Mongo, and every alert's "
            "auto-injected 'installation EQUALS' filter then drops every event).")

    # Taking over an EXISTING (non-fresh) instance with a DIFFERENT version reuses its
    # volumes — warn (esp. downgrades: an older mgmt-api against newer Mongo data).
    prev_version = apim.current_version(instance)
    version_change = None
    if prev_version and not fresh_volumes and prev_version != resolved:
        version_change = {"was": prev_version, "now": resolved,
                          "downgrade": apim.is_downgrade(resolved, prev_version)}
        if version_change["downgrade"]:
            warnings.append(
                f"DOWNGRADE ON EXISTING VOLUMES: instance '{instance}' last ran {prev_version}; "
                f"starting {resolved} runs an OLDER management-api against Mongo data written by "
                f"{prev_version} — it may mishandle newer documents (upgraders only run forward). "
                "For a clean downgrade, apim_down(instance, volumes=True) first (WIPES data).")
        else:
            warnings.append(f"instance '{instance}' last ran {prev_version}; now starting "
                            f"{resolved} on the SAME project + volumes (data reused).")

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

    # A coexist instance (offset > 0) means a SECOND console on localhost. Browser cookies
    # are host-scoped, not port-scoped, so localhost:<a> and localhost:<b> share one jar —
    # logging into one console silently logs you out of the other. Advise Incognito.
    if offset:
        warnings.append(
            "MULTIPLE CONSOLES SHARE BROWSER COOKIES: this is a second APIM console on "
            "localhost, and browser cookies are scoped by host (not port), so both consoles "
            "share one cookie jar — signing into this one logs you out of the other. Open "
            "THIS console in an Incognito/private window (or a separate browser profile) to "
            "keep the two sessions independent.")

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
        for proj in sorted({c["project"] for c in conflicts}):
            res = apim.down_project(proj)
            downed.append({"project": proj, "returncode": res["returncode"]})

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
        "feature_gotchas": feature_gotchas or None,
        "fresh_volumes": fresh_volumes,
        "version_change": version_change,
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
        "next": f"Call apim_wait(instance='{instance}') — it returns the moment the stack is "
                "healthy (or fails fast). Do NOT write your own sleep loop.",
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
    """List tracked APIM instances (for coexist) with their status — PLUS what's actually
    running on Docker.

    IMPORTANT: the `instances` list reflects stacker's own run-records, NOT reality — it's
    blind to quick-setups and can report a stale record as authoritative (e.g. show a
    tracked instance "down" while its ports are actually held by something else). So this
    also returns `other_stacks_on_apim_ports`: any Docker project (quick-setups
    `gravitee-qs-*`, gamma, or an untracked stack) holding the canonical APIM ports
    8082–8085 that ISN'T a tracked instance. Before assuming ports are free, prefer
    `stack_preflight` (it probes actual port occupancy)."""
    instances = []
    for name in apim.known_instances():
        s = _apim_summarize(name)
        instances.append({"instance": name, "overall": s["overall"], "variant": s["variant"],
                          "features": s.get("features"), "version": s["version"],
                          "project": s["project"], "mode": s["mode"], "urls": s["urls"]})
    foreign = apim.foreign_apim_port_holders()
    out = {"status": "ok", "count": len(instances), "instances": instances,
           "other_stacks_on_apim_ports": foreign or None}
    if foreign:
        out["note"] = ("canonical APIM ports 8082–8085 are held by non-tracked project(s) "
                       f"{sorted({h['project'] for h in foreign})} (e.g. a quick-setup). "
                       "apim_up on the default instance would port-conflict; use stack_preflight.")
    return out


@mcp.tool()
def apim_down(instance: str = "default", timeout_seconds: int = 180, volumes: bool = False) -> dict:
    """Stop an APIM instance (`docker compose down`). Volumes are PRESERVED by default;
    pass volumes=True (`down -v`) to also wipe its data — needed for a clean version
    downgrade (an older management-api mishandles Mongo data written by a newer one)."""
    variant = apim.current_variant(instance)
    result = apim.run_down(timeout_seconds, variant, instance, apim.current_features(instance), volumes)
    apim.forget_up(instance)
    if result["timed_out"]:
        return {"status": "timeout", "instance": instance, "message": f"down exceeded {timeout_seconds}s.",
                "stdout_tail": (result["stdout"] or "")[-2000:],
                "stderr_tail": (result["stderr"] or "")[-2000:]}
    return {"status": "ok" if result["returncode"] == 0 else "error", "instance": instance,
            "volumes_removed": volumes, "returncode": result["returncode"],
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


# ── Alert Engine helpers (for the alert-engine feature) ───────────────────────
def _ae_node(instance: str, path: str, method: str = "GET", body: dict = None) -> dict:
    """Call the AE node/management API (all endpoints under /_node, basic auth
    admin/adminadmin). Returns {status, http_status, body}."""
    base = apim.ae_mgmt_url(instance)
    if not base:
        return {"status": "error",
                "message": f"instance '{instance}' has no running alert-engine feature "
                           "(bring it up with apim_up(features=['alert-engine']))."}
    url = f"{base}/_node/{path.lstrip('/')}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(b"admin:adminadmin").decode())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode(errors="replace")
            code = r.status
    except urllib.error.HTTPError as e:
        text, code = e.read().decode(errors="replace"), e.code
    except (urllib.error.URLError, OSError) as e:
        return {"status": "error",
                "message": f"could not reach AE node API at {url}: {e}. If the stack was "
                           "just started, give it a moment; if it persists, recreate the "
                           "alert-engine service so it picks up gravitee_services_core_http_host=0.0.0.0."}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = text
    return {"status": "ok" if code < 400 else "error", "http_status": code, "url": url, "body": parsed}


@mcp.tool()
def apim_alert_engine_fix(instance: str = "default") -> dict:
    """Restart the gateway of an alert-engine instance — the fix for the fresh-volume bug
    where alerts silently never fire.

    On a cold boot against an empty Mongo the gateway caches installation=null (the mgmt-api
    hadn't written the installation record yet); every console alert's auto-injected
    `installation EQUALS <uuid>` filter then drops every REQUEST event. Restarting the
    gateway re-resolves the (now-written) installation id. Run this ONCE after the stack is
    healthy on a fresh-volume alert-engine deploy."""
    res = apim.restart_gateway(instance)
    if not res.get("ok") and res.get("error"):
        return {"status": "error", "instance": instance, "message": res["error"]}
    return {"status": "ok" if res.get("ok") else "error", "instance": instance,
            "restarted": "apim-gateway", "returncode": res.get("returncode"),
            "next": "Give the gateway ~30s, then confirm: apim_logs('apim-gateway', instance) "
                    "should show NODE_HEARTBEAT with a non-null installation, and a matching "
                    "alert now fires (AE log 'Received alert event ... type=REQUEST' → 'Webhook sent!')."}


@mcp.tool()
def ae_log_level(instance: str = "default", level: str = "DEBUG",
                 logger: str = "com.graviteesource.ae") -> dict:
    """Set an Alert Engine logger's level at runtime (no restart) via /_node/logging.

    Handy for diagnosing why an alert doesn't fire. Default flips the whole AE package to
    DEBUG. WARNING (per the AE node API): an UNPARSEABLE level string resolves to null and
    silently RESETS that logger rather than erroring — pass a valid level (DEBUG/INFO/WARN…).
    Returns the full current logger→level map."""
    return _ae_node(instance, "logging", method="POST", body={logger: level})


@mcp.tool()
def ae_trigger_dump(instance: str = "default", trigger_id: str = "") -> dict:
    """Dump the alert triggers AS THE ENGINE HOLDS THEM (/_node/triggers[/<id>]).

    Diffing a trigger's `filters` against a REQUEST event's `properties` is the ONLY way to
    catch a silent filter rejection (e.g. the `installation EQUALS <uuid>` filter dropping
    every event when the gateway cached installation=null — see apim_alert_engine_fix).
    Pass `trigger_id` for one trigger, or omit for all."""
    path = f"triggers/{trigger_id}" if trigger_id else "triggers"
    return _ae_node(instance, path)


# ── APIM plugin management (catalog + bundled + install) ──────────────────────
@mcp.tool()
def apim_plugin_search(query: str = "", type: str = "") -> dict:
    """Search the Gravitee plugin CATALOG (download.gravitee.io) — everything you can add.

    Lists plugin artifacts by name + type + latest version. `query` filters by substring
    (e.g. "keycloak", "cache", "jwt"); `type` narrows to one of connectors, endpoints,
    entrypoints, fetchers, notifiers, policies, reporters, repositories, resources,
    service-discovery, services, tracers. With no query it lists everything under `type`.

    Covers OSS *and* EE plugins (EE ones need an APIM license at runtime). To see what's
    already BUNDLED in a version (no need to add), use apim_plugin_bundled. To check
    compatibility of a specific plugin, use apim_plugin_info.
    """
    if type and type not in plugins.PLUGIN_TYPES:
        return {"status": "error", "message": f"unknown type '{type}'; use one of {list(plugins.PLUGIN_TYPES)}."}
    if not query and not type:
        return {"status": "error",
                "message": "provide a `query` (e.g. 'keycloak') or a `type` — listing ALL plugins across all "
                           f"types is slow. Types: {list(plugins.PLUGIN_TYPES)}."}
    try:
        results = plugins.search(query, type)
    except Exception as e:  # network/XML
        return {"status": "error", "message": f"catalog query failed: {e}"}
    out = {"status": "ok", "query": query, "type": type or "all", "count": len(results),
           "plugins": results,
           "note": "Add one with apim_plugin_add(name). Check compatibility first with "
                   "apim_plugin_info(name). Version numbers are the plugin's own line — NOT the APIM version."}
    if not results:
        # A zero-result read must NOT be mistaken for "this plugin doesn't exist". The catalog
        # only covers the artifact families below; NODE-level plugins are shipped INSIDE the
        # images, not published to it — so `apim_plugin_bundled` is where to look for those.
        out["no_results_note"] = (
            f"No catalog match for query={query!r} type={type or 'any'}. This does NOT mean the "
            "plugin doesn't exist — the download.gravitee.io catalog only covers these families: "
            f"{list(plugins.PLUGIN_TYPES)}. NODE-level plugins (gravitee-node-*: cluster/cache "
            "standalone e.g. hazelcast, the DSP distributed-sync bits) are bundled inside the APIM "
            "images and are NOT in this catalog — list them with apim_plugin_bundled(component=…) "
            "instead. Also try a broader query or drop the type filter.")
    return out


@mcp.tool()
def apim_plugin_info(name: str, version: str = "latest", type: str = "") -> dict:
    """Inspect a catalog plugin: its manifest + which APIM version it was BUILT FOR.

    Downloads the plugin and reads the build metadata embedded in its jar (pom.xml) →
    the APIM baseline it targets (`gravitee-apim.version`, or the older
    `gravitee-gateway-api.version`). Use this to sanity-check compatibility before adding,
    since a plugin's version is its own line, not the APIM version.

    Args:
        name: plugin artifact (e.g. "gravitee-resource-oauth2-provider-keycloak").
        version: "latest" (default) or an explicit version.
        type: optional plugin type; auto-detected if omitted.
    """
    ptype = type or plugins.find_type(name)
    if not ptype:
        return {"status": "error", "message": f"could not find plugin '{name}' in the catalog; check the name via apim_plugin_search."}
    resolved = plugins.latest_version(ptype, name) if version in ("", "latest") else version
    if not resolved:
        return {"status": "error", "message": f"no versions found for '{name}' ({ptype})."}
    info = plugins.plugin_info(ptype, name, resolved)
    info["status"] = "error" if info.get("error") else "ok"
    return info


@mcp.tool()
def apim_plugin_bundled(version: str = "latest", component: str = "gateway") -> dict:
    """List the plugins BUNDLED in an APIM image (already loaded — no need to add).

    Runs `ls plugins/` inside graviteeio/apim-<component>:<version> (pulls the image if
    not cached — that can take a while the first time). component: "gateway" or
    "management-api". Compare against apim_plugin_search to see what's extra vs. bundled.
    """
    if component not in ("gateway", "management-api"):
        return {"status": "error", "message": "component must be 'gateway' or 'management-api'."}
    resolved, err = apim.resolve_version(version)
    if err:
        return {"status": "error", "message": err}
    docker_err = runner.docker_running_error()
    if docker_err:
        return {"status": "blocked", "message": docker_err}
    rows, err = plugins.bundled_plugins(resolved, component)
    if err:
        return {"status": "error", "version": resolved, "message": err}
    return {"status": "ok", "version": resolved, "component": component,
            "count": len(rows), "bundled": rows}


@mcp.tool()
def apim_plugin_add(source: str, version: str = "latest", type: str = "", instance: str = "default") -> dict:
    """Add a plugin to a running APIM instance (download → plugins-ext → reload).

    Follows Gravitee's documented approach: drop the plugin zip into the gateway's
    `plugins-ext` dir and restart the node. This tool downloads the plugin, then recreates
    the instance's gateway + management-api to load it.

    `source` is EITHER a plugin artifact name (e.g. "gravitee-resource-oauth2-provider-
    keycloak") — with the type auto-detected and `version` resolved (latest by default) —
    OR a full download.gravitee.io URL to a plugin .zip (only that host is allowed).

    Note: a plugin's version is its OWN line, not the APIM version — check compatibility
    with apim_plugin_info first. EE plugins additionally need an APIM license on the stack.
    If the instance isn't running, the plugin is staged and loads on the next apim_up.

    This tool is for CATALOG plugins. To mount an arbitrary EXTRA file or env into the
    gateway/management-api (a config file, a hand-built jar, a custom logback), that's a
    feature OVERLAY, not a plugin: drop an `apim-feature-<name>.yml` into
    `~/.gravitee/stacker-features/` (or set APIM_FEATURES_DIR) and pass `<name>` to
    apim_up(features=[...]). See apim_up's docstring.
    """
    if source.startswith("http://") or source.startswith("https://"):
        url = source
    else:
        ptype = type or plugins.find_type(source)
        if not ptype:
            return {"status": "error", "message": f"could not find plugin '{source}' in the catalog (apim_plugin_search to find the name)."}
        resolved = plugins.latest_version(ptype, source) if version in ("", "latest") else version
        if not resolved:
            return {"status": "error", "message": f"no versions found for '{source}' ({ptype})."}
        url = plugins.plugin_url(ptype, source, resolved)

    fname, err = plugins.install(instance, url)
    if err:
        return {"status": "error", "message": err}

    if not apim.is_tracked(instance):
        return {"status": "staged", "instance": instance, "plugin": fname,
                "plugins_dir": str(apim.plugins_dir(instance)),
                "message": f"'{fname}' staged in plugins-ext but instance '{instance}' isn't running; "
                           "it will load on the next apim_up."}
    res = apim.recreate_gateway(instance)
    return {"status": "ok" if res.get("ok") else "error", "instance": instance, "plugin": fname,
            "url": url, "reloaded": res.get("ok"),
            "next": f"Poll apim_status(instance='{instance}'); check apim_logs('apim-gateway', instance='{instance}') "
                    f"for the plugin loading (and any license error if it's an EE plugin).",
            "recreate_output": res.get("output")}


@mcp.tool()
def apim_plugin_list(instance: str = "default") -> dict:
    """List an instance's plugins: user-ADDED (via apim_plugin_add) and, if running, the
    BUNDLED set loaded from the image."""
    added = plugins.installed(instance)
    out = {"status": "ok", "instance": instance, "added": added,
           "plugins_dir": str(apim.plugins_dir(instance))}
    if apim.is_tracked(instance):
        version = apim.current_version(instance) or "latest"
        rows, err = plugins.bundled_plugins(version, "gateway")
        out["bundled_gateway"] = rows if not err else f"(unavailable: {err})"
    return out


@mcp.tool()
def apim_plugin_remove(name: str, instance: str = "default") -> dict:
    """Remove a user-added plugin from an instance (deletes the zip + reloads if running).
    `name` is the zip filename or the artifact-name prefix."""
    if not plugins.remove(instance, name):
        return {"status": "not_found", "instance": instance,
                "message": f"no added plugin matching '{name}'; see apim_plugin_list.",
                "added": plugins.installed(instance)}
    reloaded = apim.recreate_gateway(instance).get("ok") if apim.is_tracked(instance) else None
    return {"status": "ok", "instance": instance, "removed": name, "reloaded": reloaded,
            "remaining": plugins.installed(instance)}


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
def am_wait(instance: str = "default", timeout_seconds: int = 420, poll_seconds: int = 4) -> dict:
    """Block until an AM instance is actually healthy, then return immediately.

    Same readiness-aware wait as apim_wait, for the AM stack — returns the moment
    `overall` is "healthy", fails fast on "failed"/"down". The AM management API is slow
    to pass its healthcheck, so this is exactly what you want instead of a sleep loop.
    `timeout_seconds` is a safety ceiling, not a fixed wait.
    """
    return _wait_ready(_am_summarize, instance, timeout_seconds, poll_seconds, "am")


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
    if am.is_up_running(instance) or am.stack_running(instance):
        return {"status": "already_running", "instance": instance,
                "project": am.project_for(instance),
                "message": f"AM instance '{instance}' is already running (project {am.project_for(instance)}). "
                           f"Starting it again would RECREATE its containers. Inspect with "
                           f"am_status(instance='{instance}'), stop with am_down(instance='{instance}'), "
                           "or run a SECOND stack as a named instance."}

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
        "next": f"Call am_wait(instance='{instance}') — it returns the moment the stack is healthy "
                "(the mgmt API is slow, so don't sleep-loop; the wait handles it).",
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


# ── Public Gamma stack (gamma_*) — self-contained, public images, no ACR ───────
def _gamma_summarize() -> dict:
    up = gamma.up_process_status()
    rows = gamma.compose_ps()
    expected = gamma.service_names()
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
        "version": up.get("version") or gamma.current_version(),
        "project": gamma.project_for(),
        "up_process": up,
        "services": [{"service": s, **{k: by.get(s, {}).get(k) for k in ("state", "health", "exit_code")},
                      "label": labels[s]} for s in expected],
        "problems": [{"service": s, "label": labels[s]} for s in expected
                     if labels[s] in _BAD or labels[s] == "missing"],
        "urls": gamma.urls(),
        "up_log_tail": runner.tail_file(gamma.up_log_path(), 40),
        "checked_at": _now_iso(),
    }


@mcp.tool()
def gamma_up(version: str = "latest", pull: bool = True, recreate: bool = False,
             down_conflicting: bool = False, license: str = "") -> dict:
    """Stand up the PUBLIC, self-contained Gravitee Gamma platform (background, non-blocking).

    Runs the customer-facing docker-compose from the Gravitee docs — PUBLIC Docker Hub images
    (graviteeio/*, graviteeio/gamma-ui), NO ACR login, and NO license required (Agent Management
    is the only module that wants one; API/AuthZ/Platform Management run without it). This is the
    public counterpart to the internal SDK-repo `stack_*` path. Single instance / canonical
    ports 8082-8086 — Gamma console :8086, APIM console :8084, portal :8085 (all admin/admin).

    Returns immediately — then call `gamma_wait()` (or poll `gamma_status()`). On a port conflict
    it does NOT start (status "port_conflict"); `down_conflicting=true` frees the ports.

    Args:
        version: image tag — "latest"/"" → 4.12 (the published minor gamma-ui + apim share);
            or pin e.g. "4.12".
        pull: pull images before up (default).
        recreate: `up -d --force-recreate`.
        down_conflicting: down any project holding 8082-8086 first (no -v; data kept).
        license: OPTIONAL path to a license.key (Agent Management only); empty =
            GAMMA_LICENSE/APIM_LICENSE env, else ~/.gravitee/license.key if present.
    """
    docker_err = runner.docker_running_error()
    if docker_err:
        return {"status": "blocked", "message": docker_err}
    resolved, err = gamma.resolve_version(version)
    if err:
        return {"status": "blocked", "message": err}
    if gamma.is_up_running() or gamma.stack_running():
        return {"status": "already_running", "project": gamma.project_for(),
                "message": "the public Gamma stack is already running. Inspect gamma_status(), "
                           "stop with gamma_down(), or gamma_up(recreate=True) to force-recreate."}
    conflicts = gamma.detect_conflicts()
    downed = []
    if conflicts:
        if not down_conflicting:
            return {"status": "port_conflict", "version": resolved, "ports": gamma.CANONICAL_PORTS,
                    "conflicts": conflicts, "conflicting_projects": sorted({c["project"] for c in conflicts}),
                    "message": (f"port(s) {sorted({c['port'] for c in conflicts})} held by "
                                f"{sorted({c['project'] for c in conflicts})}. Gamma uses FIXED ports "
                                "8082-8086 (single instance) — down_conflicting=true to free them. NOTE: "
                                "Gamma BUNDLES APIM, so a separate apim/am stack on those ports always collides.")}
        for proj in sorted({c["project"] for c in conflicts}):
            downed.append(gamma.down_project(proj))
    license_path, license_src = gamma.resolve_license(license)
    log_path = gamma.up_log_path()
    log_path.write_bytes(b"")
    proc = gamma.launch_up_background(resolved, pull, recreate, license_path, log_path)
    started = _now_iso()
    gamma.record_up(proc, resolved, license_path, log_path, started)
    return {
        "status": "starting", "version": resolved, "project": gamma.project_for(),
        "pid": proc.pid, "log_path": str(log_path), "pull": pull, "recreate": recreate,
        "license": {"mounted": bool(license_path), "path": license_path, "source": license_src,
                    "note": "optional — only Agent Management needs it; the rest run without."},
        "downed_conflicts": downed, "ports": gamma.CANONICAL_PORTS, "urls": gamma.urls(),
        "started": started,
        "next": "Call gamma_wait() — it returns the moment the stack is healthy (or fails fast). "
                "Do NOT write your own sleep loop.",
    }


@mcp.tool()
def gamma_status() -> dict:
    """Overall verdict + per-service health for the public Gamma stack (version, project, URLs)."""
    return _gamma_summarize()


@mcp.tool()
def gamma_license() -> dict:
    """Show the enterprise license entitlements loaded on the running public Gamma stack.

    Reads tier / packs / features / expiry from the management-api's node license endpoint.
    A Gamma console module shown as 'Upgrade to access' means its required PACK isn't in the
    license's `packs` (an entitlement gap, not a mount problem). Returns `no_license` for an
    OSS/unlicensed stack, `not_running` if it isn't up.
    """
    return runner.read_stack_license(gamma.project_for())


@mcp.tool()
def gamma_wait(timeout_seconds: int = 600, poll_seconds: int = 4) -> dict:
    """Block until the public Gamma stack is healthy, then return immediately (fails fast on error).

    Same readiness-aware wait as apim_wait/am_wait. Defaults to a higher ceiling since a
    first-ever pull of the full platform (7 services) is slow. timeout_seconds is a safety
    ceiling, not a fixed wait.
    """
    return _wait_ready(lambda _inst: _gamma_summarize(), "gamma", timeout_seconds, poll_seconds, "gamma")


@mcp.tool()
def gamma_down(volumes: bool = False, timeout_seconds: int = 180) -> dict:
    """Stop the public Gamma stack (`docker compose down`; volumes preserved). volumes=True → `down -v`."""
    res = gamma.run_down(timeout_seconds, volumes=volumes)
    gamma.forget_up()
    status = "ok" if res.get("returncode") == 0 else ("timeout" if res.get("timed_out") else "error")
    return {"status": status, "volumes_removed": volumes, "returncode": res.get("returncode"),
            "stdout": res.get("stdout", ""), "stderr": res.get("stderr", "")}


@mcp.tool()
def gamma_logs(service: str, lines: int = 100) -> dict:
    """Tail logs for one service of the public Gamma stack (gateway, management_api, gamma_console, …)."""
    valid = gamma.service_names()
    if service not in valid:
        return {"status": "invalid_service", "message": f"unknown service '{service}'.",
                "valid_services": valid}
    p = gamma.compose_logs(service, lines)
    return {"status": "ok" if p.returncode == 0 else "error", "service": service, "lines": lines,
            "returncode": p.returncode, "logs": p.stdout or p.stderr}


@mcp.tool()
def apim_license(instance: str = "default") -> dict:
    """Show the enterprise license entitlements loaded on a RUNNING APIM instance.

    Reads tier / packs / features / expiry from the management-api's node license endpoint
    (auto-resolves the instance's variant). A disabled EE feature or a console module shown as
    'Upgrade to access' usually means the required PACK isn't in the license's `packs` — an
    entitlement gap, not a mount/load problem. Returns `no_license` for an OSS/unlicensed stack,
    `not_running` if the instance isn't up. Also covers the kafka variant.
    """
    variant = apim.current_variant(instance) or "default"
    return runner.read_stack_license(apim.project_for(variant, instance))


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
    out = {"status": "ok", "version": resolved, "count": len(names),
           "configs": names, "running_locally": quicksetup.known_configs(),
           "known_gotchas": gotchas}
    if "ee-with-alert-engine" in names:
        # The curated alert-engine feature is the working alternative; carry its gotcha too.
        out["alert_engine_feature"] = {
            "use_instead": "apim_up(features=['alert-engine'])  # works end-to-end vs the broken quick-setup",
            "gotcha": apim.feature_gotcha("alert-engine"),
        }
    return {**out,
            "note": "Run one with quicksetup_up(name). One at a time — the raw upstream "
                    "composes hardcode ports/container names, so they can't coexist; to "
                    "coexist or COMBINE capabilities (e.g. Kafka + Prometheus + Redis) use "
                    "apim_up(features=[…]) instead. `known_gotchas` flags configs that are "
                    "broken/misleading as shipped (from a functional sweep); quicksetup_up "
                    "auto-applies the safe fixes and warns on the rest."}


@mcp.tool()
def quicksetup_up(name: str, version: str = "latest", pull: bool = True,
                  recreate: bool = False, down_conflicting: bool = False,
                  fetch: bool = True) -> dict:
    """Fetch an official APIM quick-setup config and stand it up (background, non-blocking).

    Fetches `docker/quick-setup/<name>` from the APIM repo at the pinned version, copies
    it into a local workdir, drops ~/.gravitee/license.key in if the config mounts one,
    then `docker compose -p gravitee-qs-<name> up -d`. Returns immediately — poll
    `quicksetup_status(name)`.

    LOCAL EDITS: fetch=True (default) RE-CLONES the upstream config into the workdir, which
    OVERWRITES any manual edits you made there (e.g. instrumentation) — the result warns you
    when it overwrote an existing workdir. To iterate on an EDITED workdir (add logging,
    tweak a service) WITHOUT losing it, pass **fetch=False**: it reuses the on-disk workdir
    as-is (no clone, no autofix re-apply). Typical loop: fetch once (fetch=True) → edit the
    workdir's docker-compose.yml → re-run with fetch=False, recreate=True.

    IMPORTANT — this runs the UPSTREAM config verbatim, so it inherits that config's
    gotchas and any MANUAL steps (keycloak realm import, native-kafka console setup,
    mssql/postgres backends, …). The fetched README is returned here — read it and relay
    the manual steps to the user. For the curated, fully-automated OSS or native-Kafka
    stacks, prefer `apim_up` instead.

    NO coexist FOR THE RAW RUNNER: the upstream composes hardcode host ports (mostly
    8082–8085) and container names, so only ONE quick-setup runs at a time. On a port
    conflict it does NOT start (status "port_conflict"); ask the user to down the other
    stack (down_conflicting=true) — there is no port-shift option for a raw quick-setup.
    BUT if the user actually wants to COEXIST or COMBINE capabilities (e.g. Kafka +
    Prometheus + Redis together, or two stacks at once), that IS supported — via
    `apim_up(variant=…, features=[…], instance=…)`, which is port-parameterized and
    coexist-safe. Prefer that whenever the goal is mixing/coexisting rather than fidelity
    to one specific upstream config.

    Args:
        name: config name from quicksetup_list (e.g. "redis-rate-limit", "keycloak").
        version: APIM tag to pin ("latest" resolves the newest stable release).
        pull: pull images before up (default).
        recreate: `up -d --force-recreate`.
        down_conflicting: down any project holding the needed ports first (no -v; data kept).
        fetch: True (default) re-clones upstream into the workdir, OVERWRITING local edits;
            False reuses the on-disk workdir as-is (preserves your edits — see LOCAL EDITS).
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

    warnings = []
    had_workdir = quicksetup.workdir_present(name)
    if fetch:
        fetched, err = quicksetup.fetch(name, resolved)
        if err:
            return {"status": "blocked", "message": err}
        if had_workdir:
            warnings.append(
                f"RE-FETCHED upstream '{name}' into {fetched.workdir} — any LOCAL EDITS to that "
                "workdir were overwritten. To iterate on an edited workdir, re-run with fetch=False.")
    else:
        fetched, err = quicksetup.reuse(name, resolved)
        if err:
            return {"status": "blocked", "message": err}
        warnings.append(
            f"REUSING the on-disk workdir {fetched.workdir} as-is (fetch=False) — upstream was NOT "
            "re-fetched and autofixes were NOT re-applied, so your local edits are preserved.")
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
                    "(down_conflicting=true STOPS the running stack there; data volumes are kept "
                    "but it's no longer running) or stop whatever holds the ports, then retry."),
                "conflicts": conflicts,
                "conflicting_projects": sorted({c["project"] for c in conflicts}),
                "suggest": {"down_conflicting": {"tool": "quicksetup_up",
                                                 "args": {"name": name, "version": version, "down_conflicting": True}}},
            }
        for proj in sorted({c["project"] for c in conflicts}):
            res = apim.down_project(proj)
            downed.append({"project": proj, "returncode": res["returncode"]})

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
      * status "running"  → the target 'default' stack is ALREADY UP (its own containers
        hold the ports). Do NOT start on canonical ports — it would recreate them. Offer
        `inspect` / `down_first` / `coexist`. (Detected by running containers, so a dead
        launcher process can't mask a live stack.)
      * status "conflict" → ports held by ANOTHER stack; present `down_conflicting` (free
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

    # Is the TARGET 'default' stack ALREADY RUNNING? detect_conflicts/conflict_on skip the
    # target project's own ports (to allow an idempotent re-up), so a genuinely-running
    # default stack would otherwise read as "clear" — and starting on canonical ports would
    # RECREATE its containers out from under the user. Container-based check, so a dead
    # launcher PID can't mask a live stack.
    target_running = apim.stack_running(variant, "default") if kind == "apim" else am.stack_running("default")
    if target_running:
        project = apim.project_for(variant, "default") if kind == "apim" else am.project_for("default")
        opts = {"inspect": {"tool": f"{kind}_status", "args": {}},
                "down_first": {"tool": f"{kind}_down", "args": {}}}
        if can_coexist:
            opts["coexist"] = {"tool": up_tool, "args": {**up_base, "instance": "b"},
                               "ports": coexist_ports or "next free port"}
        coexist_or_not = (" or run a SECOND stack as a named instance (coexist)" if can_coexist
                          else f" — the {variant} variant can't coexist (fixed *.kafka.local certs + "
                               "literal broker ports), so down it first if you need a different one")
        return {
            "status": "running", "kind": kind, "resolved_version": resolved,
            "variant": variant if kind == "apim" else None, "ports": ports, "project": project,
            "can_coexist": can_coexist,
            "message": (
                f"the {kind} 'default' stack is ALREADY RUNNING on the canonical ports (project "
                f"{project}). Do NOT start on canonical ports — it would RECREATE its containers. "
                f"Inspect it ({kind}_status), stop it first ({kind}_down){coexist_or_not}."),
            "options": opts,
        }

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
    # Keep the prose in lock-step with `options`: only offer coexist when it actually works.
    # For kafka it does NOT (fixed *.kafka.local certs + literal broker ports), so say why
    # rather than dangling a path an agent would then try and fail on.
    if can_coexist:
        choice = ("Ask the user: down the conflicting stack (down_conflicting), or run the new "
                  "one in coexist mode (a named instance on shifted ports)?")
    else:
        choice = ("The only option is to down the conflicting stack (down_conflicting): the "
                  f"{variant} variant can't coexist — its *.kafka.local certs and broker ports "
                  "(9091, 9093–9096 are literals) are fixed, so a port offset can't resolve the "
                  "cert/hostname collision.")
    return {
        "status": "conflict", "kind": kind, "resolved_version": resolved,
        "variant": variant if kind == "apim" else None, "ports": ports,
        "license_note": license_note,
        "can_coexist": can_coexist,
        "conflicts": conflicts,
        "conflicting_projects": sorted({c["project"] for c in conflicts}),
        "message": (
            f"{kind} {resolved} needs port(s) {sorted({c['port'] for c in conflicts})}, held by "
            f"{sorted({c['project'] for c in conflicts})}. {choice}"),
        "options": options,
    }


@mcp.tool()
def doctor() -> dict:
    """Check environment readiness and report what's missing.

    Call this first when setting up. Everything the tool runs — APIM, AM, the public Gamma
    platform, and quick-setups — needs only Docker (public images). A Gravitee license is
    optional and only unlocks enterprise features.
    """
    docker_err = runner.docker_running_error()
    docker_ok = docker_err is None

    lic_path, lic_src = apim.resolve_license("")
    next_steps = []
    if not docker_ok:
        next_steps.append(f"Start Docker: {docker_err}")
    if not lic_path:
        next_steps.append(f"(optional) drop a Gravitee license at {apim.DEFAULT_LICENSE_PATH} "
                          "for enterprise features — OSS works without it.")

    return {
        "docker": {"ok": docker_ok, "detail": docker_err or "running"},
        "license": {"found": bool(lic_path),
                    "path": lic_path or str(apim.DEFAULT_LICENSE_PATH), "source": lic_src},
        "apim_stack": {"ready": docker_ok, "needs": "Docker only (public images).",
                       "latest_version": apim.resolve_version("latest")[0],
                       "next_steps": next_steps or ["ready — call apim_up()"]},
        "gamma_stack": {"ready": docker_ok,
                        "needs": "Docker only — public images, no ACR, no license required.",
                        "next_steps": next_steps or ["ready — call gamma_up()"]},
        "am_stack": {"ready": docker_ok, "needs": "Docker only.",
                     "next_steps": next_steps or ["ready — call am_up()"]},
    }


def main() -> None:
    """Console-script entry point: run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
