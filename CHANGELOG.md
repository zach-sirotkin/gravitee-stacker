# Changelog

All notable changes to **gravitee-stacker** are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.5] — 2026-08-19

### Fixed
- **`stack_preflight` no longer offers an impossible coexist path for kafka (bug).** On a
  port conflict (and in the already-running branch), the message template always suggested
  "coexist mode (a named instance on shifted ports)" even though the `options` dict
  correctly omitted it for kafka — so an agent trusting the prose over the structure would
  offer a path that can't work. Both messages now branch on `can_coexist` and, for kafka,
  state *why* coexist is unavailable. Added `can_coexist` to the payloads.

### Changed
- **Custom feature overlays are now discoverable at the MCP surface.** The
  `~/.gravitee/stacker-features/` mechanism (drop an `apim-feature-<name>.yml`, override
  the dir with `APIM_FEATURES_DIR`, a user overlay shadows a bundled one, overlays survive
  upgrades because they live outside the package) was documented only in a Python docstring
  that never reached the client. Added a "Custom overlays" paragraph to `apim_up`, enriched
  the unknown-feature error to point at `features_dir()` (and return it as a field), and
  cross-referenced it from `apim_plugin_add` as the answer to "how do I mount an extra file
  into the gateway".
- **The kafka single-instance restriction now states its mechanism, not just "fixed
  ports".** "Fixed ports" invited the wrong inference that a port offset would help; the
  `apim_up` block message and variant docstring now cite the real blocker — fixed
  `*.kafka.local` certs + literal broker ports (9091, 9093–9096) that a port offset can't
  reconcile.

## [0.8.4] — 2026-08-18

### Added
- **Coexist cookie-jar disclaimer.** Running two APIM consoles in one browser silently
  shares a login: browser cookies are host-scoped, not port-scoped, so `localhost:8084`
  and `localhost:28084` share one jar and the console auth cookie (`Auth-Graviteeio-APIM`)
  is overwritten on each sign-in. `apim_up` now emits a `warnings` note whenever it starts
  a coexist instance (offset > 0), advising Incognito / a separate browser profile for the
  second console. Documented in the README's multiple-stacks section.

### Changed
- **Packaging: wheel is robustly clean.** Removed `artifacts = ["*.yml", …]` from the
  wheel target — Hatchling already ships tracked non-`.py` assets under `packages` and
  respects `.gitignore`, so the extension glob was the only thing force-including
  git-ignored scratch overlays into local builds. Released wheels (built from a clean tag
  checkout) were already unaffected. Added a `.gitignore` guard against scratch overlays
  (`apim-feature-<ticket>`/`mcd*`, `otel-collector-*`) landing in the package dir.

## [0.8.3] — 2026-08-13

### Changed
- Docs/notes only. Completed the `ee-with-alert-engine` gotcha note with the remaining
  reference details from the end-to-end writeup: config quirks (`frontend`/`storage`
  external:true must pre-exist; AE healthcheck baked into the image, not compose-overridable;
  `./.plugins` inert for AE; AE has no Mongo/ES dependency by design; alerts are manual
  console work) and diagnostic signals (`Register trigger` = reached engine; alert HISTORY
  populated = evaluated; `Channel is ready`/`Events successfully sent.` = bootstrap/heartbeat
  only, not event flow). `quicksetup_up`'s `port_conflict` message now notes that
  `down_conflicting` STOPS the running stack (data volumes kept, but no longer running).

## [0.8.2] — 2026-08-03

### Fixed
- **`apim_list` now reflects reality, not just run-records.** It was blind to quick-setups
  and reported stale records as authoritative (e.g. a tracked instance shown "down" while
  a `gravitee-qs-*` quick-setup actually held 8082–8085 — trusting it and running `apim_up`
  would port-conflict with no explanation). `apim_list` now also returns
  `other_stacks_on_apim_ports`: any Docker project holding the canonical APIM ports that
  isn't a tracked instance, with a note to use `stack_preflight`.
- **`apim_up` warns before taking over an existing instance with a different version.** When
  the requested version differs from the instance's last-tracked one on EXISTING volumes it
  now reports `version_change` and warns — and for a **downgrade** (an older management-api
  against Mongo data written by a newer one), it warns loudly to `apim_down(volumes=True)`
  first. Previously it silently reused the volumes across a downgrade.

### Added
- **`apim_down(instance, volumes=True)`** (`down -v`) — wipe an instance's data, e.g. for a
  clean version downgrade. Volumes are still preserved by default.

## [0.8.1] — 2026-08-03

### Added
- **Alert Engine ops tooling** (from live end-to-end testing that confirmed the feature
  works — evaluation + notification + webhook delivery — on APIM 4.12.13 + AE 3.0.2):
  - `apim_alert_engine_fix(instance)` — restarts the gateway to fix the **fresh-volume
    bug**: on a cold Mongo the gateway caches `installation=null` (the mgmt-api hasn't
    written the installation record yet), so every console alert's auto-injected
    `installation EQUALS <uuid>` filter silently drops every REQUEST event — no error, alert
    history just stays empty. `apim_up` now detects fresh volumes + alert-engine and warns;
    a `FEATURE_GOTCHAS` entry surfaces on `apim_up`/`quicksetup_list`.
  - `ae_log_level` / `ae_trigger_dump` — drive the AE node API (`/_node/logging`,
    `/_node/triggers`) so you can flip AE to DEBUG and diff a trigger's `filters` vs an
    event's `properties` (how the fresh-volume bug was found). The overlay now publishes the
    AE node API (18072, offset-aware) and binds it to `0.0.0.0` (AE defaults to 127.0.0.1).

### Fixed
- **False readiness signal corrected.** `Events successfully sent.` in the gateway log is
  only 5s node-heartbeat/monitor traffic — it does NOT prove request events reach AE (the
  earlier v0.8.0 note claimed it did). Real signals documented: gateway `processor-alert in
  processor chain post-platform`; AE `Received alert event ... type=REQUEST` →
  `DampeningState` → `Fire a new notification` → `Webhook sent!`.

## [0.8.0] — 2026-08-03

### Added
- **`alert-engine` feature** — Gravitee Alert Engine on ANY curated APIM stack (not just
  the quick-setup). Adds an `apim-alert-engine` service on the shared `storage` network and
  wires the gateway + management-api AE connector to it **container-to-container**
  (`ws_discovery=false`, endpoint `http://apim-alert-engine:8072/`). This is exactly what
  the upstream `ee-with-alert-engine` quick-setup can't do — there AE has no `networks:`
  key (isolated on the default net) and `ws_discovery=true` makes the gateway chase AE's
  announced, unroutable container IP, so events never arrive. **Requires an EE license**
  (gated). AE image version auto-tracks the gateway's bundled `alert-engine-connectors-ws`
  (APIM 4.12→AE 3, 4.11→2.3, ≤4.10→2; override with `AE_VERSION`). The AE image's false-401
  healthcheck is disabled so status isn't stuck `partial`. **Verified live:** the gateway's
  AE connector logs `Events successfully sent.` continuously with a stable channel — the
  event stream the quick-setup never established.
- **External feature-overlay dir** — custom/experimental features now live OUTSIDE the
  package: an `apim-feature-<name>.yml` in `~/.gravitee/stacker-features/` (or
  `APIM_FEATURES_DIR`) is usable via `apim_up(features=["<name>"])`, and takes precedence
  over a bundled overlay of the same name. So you never author experiments in the installed
  package (which is what let scratch files accumulate there).

### Changed
- **Package hygiene** — removed accumulated experimental scratch (ticket-numbered configs,
  custom overlays, otel-collector configs) from the `gravitee_stacker/` package dir; the
  wheel now ships only real tool assets. (Moved to `~/gravitee/stacker-scratch/`, not
  deleted.)
- **`ee-with-alert-engine` gotcha note rewritten** — it wrongly claimed the gateway
  "streams events" to AE; in fact AE never receives events in that config (root-caused to
  the missing network + `ws_discovery=true`). Now marked `broken` for AE testing, pointing
  at `apim_up(features=["alert-engine"])` instead.

## [0.7.3] — 2026-07-28

### Changed
- Docs/workflow only, for the repo going **public**. An audit found no leaked secrets
  (the bundled kafka `.jks` are public demo certs; nothing sensitive in the tree or git
  history). Marked the **Gamma stack (`stack_*`) clearly as Gravitee-internal** — it wraps
  the private `gravitee-gamma-modules-sdk` repo + private registry and can't be repointed
  at the public `gravitee-api-management/gravitee-gamma` source, so it won't work outside
  Gravitee; everything else (APIM/AM/quick-setups/plugins/features) runs on public
  resources. Dropped the now-false "this repo is private" wording from the README and the
  release workflow.

## [0.7.2] — 2026-07-28

### Fixed
- **Fresh installs broke on `mcp` 2.0.** The dependency was unpinned (`mcp>=1.28.0`); mcp
  2.0.0 removed `mcp.server.fastmcp` (which this server imports), so a clean
  `pip install` / `pipx install` (and every CI `test` job) resolved 2.0 and failed at
  import with `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. Capped at
  `mcp>=1.28.0,<2`. (Existing editable dev installs already on mcp 1.x were unaffected —
  only fresh resolutions hit it.)

## [0.7.1] — 2026-07-23

### Fixed
- **Data-loss-class bug: `stack_preflight` could report "clear" for ports held by an
  already-running stack**, and `apim_up`/`am_up` could then recreate its gateway +
  management-api out from under the user. Two root causes, both "is it up?" answered by
  the wrong signal:
  - `detect_conflicts`/`conflict_on` deliberately skip ports held by the *target's own*
    project (to allow idempotent re-up), so a running `default` stack read as no-conflict
    → "clear".
  - the up-guards used `is_up_running`, which tracks the detached `up -d` launcher process
    — dead the moment launch completes — so a genuinely-running stack read as absent.
  Fix: a container-based `runner.project_running_containers()` (docker label + running
  status) is now the authoritative check. `stack_preflight` returns a new **`running`**
  status (with `inspect` / `down_first` / `coexist` options) when the target stack is up,
  and the `apim_up`/`am_up` guards refuse with `already_running` (never recreate).

## [0.7.0] — 2026-07-22

### Added
- **`debug-logging` feature** — mounts a bundled `logback-debug.xml` over the gateway's and
  management-api's `config/logback.xml`, putting `io.gravitee` at `DEBUG` (third-party
  loggers stay at WARN so the output is readable). The images hardcode their log levels
  with no env var to flip them, so replacing the file is the supported route (per the
  Gravitee "Debug Logging" docs). Deliberately a minimal STDOUT-only config rather than a
  version-pinned copy of each component's file — this tool reads logs via
  `docker compose logs`, so the upstream rolling *file* appender adds nothing.
- **Default features** — `apim.DEFAULT_FEATURES` is layered onto **every** deploy, with
  `debug-logging` as the first member. Opt out per-deploy with a `-` prefix
  (`features=["-debug-logging"]`) or machine-wide with `APIM_DEFAULT_FEATURES=""`.
  `apim_up`/`apim_status` report the resolved feature list.

### Changed
- **Behaviour change:** new APIM deploys now log at `DEBUG` by default (see opt-out above).

## [0.6.0] — 2026-07-21

### Added
- **APIM plugin management** (`apim_plugin_*`) — add any Gravitee plugin to the curated
  APIM stack, per Gravitee's documented approach (a per-instance `plugins-ext` dir
  bind-mounted into the gateway + management-api, `gravitee_plugins_path_1`, recreate to
  load). New `plugins.py` + six tools:
  - `apim_plugin_search` — the **catalog** from download.gravitee.io (the S3 bucket is
    listable with `Accept: text/xml`, as the site's own browser does); covers OSS **and**
    APIM EE plugins, with latest versions.
  - `apim_plugin_info` — downloads a plugin and reads the APIM version it was **built
    for** from the embedded `pom.xml` (`gravitee-apim.version`, else the older
    `gravitee-gateway-api.version`) — a real compatibility signal, since a plugin's
    version is its own line.
  - `apim_plugin_bundled` — lists what's **bundled** in an image (`ls plugins/`).
  - `apim_plugin_add` (name+version or a download.gravitee.io URL → `plugins-ext` +
    gateway reload), `apim_plugin_list`, `apim_plugin_remove`.
  Verified end-to-end: `apim_plugin_add` of the keycloak OAuth2 resource → the gateway
  logs `oauth2-keycloak-resource [4.0.0] has been loaded`. Not covered: plugins that live
  only in private GitHub repos (AM EE IdP/MFA) — no release binary → build-from-source.

### Fixed
- `recreate_gateway` (used by plugin add/remove) now reconstructs a coexist instance's
  shifted host-port band, so reloading a named instance no longer rebinds the canonical
  ports and collides with the default stack.

## [0.5.2] — 2026-07-21

### Changed
- Docs only. Added a **Claude Desktop** wiring section (the desktop app uses a separate
  MCP config from Claude Code) documenting the two macOS GUI-app gotchas — a minimal
  `PATH` (so `docker` isn't found) and TCC blocking `~/Documents`/`~/Desktop`/`~/Downloads`
  (`PermissionError` on the venv) — with fixes: pin `PATH`, and keep the install out of
  protected folders (or grant Full Disk Access).

## [0.5.1] — 2026-07-21

### Changed
- Docs/clarity only (no functional change). Clarified the quick-setup "no coexist"
  boundary in the tool docstrings (`quicksetup_up`, `quicksetup_list`) + module doc: it's
  the *raw runner* that's one-at-a-time; to coexist or **combine** capabilities use
  `apim_up(features=[…])`. README accuracy pass — corrected a stale install pin and the
  Kafka version guidance (verified on 4.12.x), aligned the Cursor snippet on the console
  script, and trimmed duplicated coexist / "what it exposes" prose.

## [0.5.0] — 2026-07-21

### Added
- **Composable APIM features** — `apim_up(features=[…])` layers curated capability
  overlays onto either base (`default` or `kafka`), merged via `docker compose -f`:
  - `prometheus` — a Prometheus that scrapes the gateway's metrics endpoint (UI on :9090).
  - `redis-rate-limit` — points the gateway's rate-limit store at a bundled Redis (internal).
  They combine freely and on either base, so `apim_up(variant="kafka",
  features=["prometheus","redis-rate-limit"])` is a native-Kafka gateway with Prometheus
  scraping and Redis rate-limiting in one stack (verified live end-to-end). This is the
  curated, coexist-safe counterpart to the one-shot `quicksetup_*` configs — reach for
  `features` to *combine* capabilities, `quicksetup_*` for fidelity to a single upstream config.
- **Composed stacks coexist** — feature host ports shift with the instance offset
  (prometheus → 29090 at +20000), so two composed stacks run side by side. `apim_status`
  / `apim_list` now report the active `features`; `apim_logs` tails feature services
  (`apim-prometheus`, `apim-redis`).

### Changed
- The OSS base (`apim-compose.yml`) now declares explicit `storage`/`frontend` networks
  (mirroring the kafka base) so feature overlays attach uniformly on any base. No change
  to its published ports or behaviour.

## [0.4.1] — 2026-07-20

### Added
- **mssql `init-db.sh` auto-fix.** Verifying the download-gated configs surfaced a second
  mssql defect: the bundled `init-db.sh` (which creates the `gravitee` database) calls the
  old `/opt/mssql-tools/bin/sqlcmd` path — gone in the `2019-latest` image (now
  `mssql-tools18`) — and omits `-C`, so with tools18/ODBC18's mandatory encryption the DB
  is never created and management-api fails with `Cannot open database "gravitee"`. This is
  a deterministic, download-free fix, so it's now **auto-applied at fetch** (path + `-C`);
  the JDBC driver stays warn-only (a download). The auto-fix engine now patches any bundled
  file (init scripts, …), not just `docker-compose.yml`.

### Verified
- All download-gated configs work once their requirement is satisfied: **postgresql** and
  **mssql** (JDBC driver in `.driver` → an API persists via JDBC and serves through the
  gateway; mssql's DB auto-creates via the fixed `init-db.sh`), and **keycloak** (the
  `download-plugins-ext.sh` OAuth2 resource plugin → secured API returns `401` with no/bad
  token and validates real Keycloak tokens).

## [0.4.0] — 2026-07-20

### Added
- **Quick-setup gotchas layer + auto-fixes.** A deep-functional sweep of the common
  configs (prometheus, mongodb, opensearch, postgresql, redis-rate-limit, keycloak,
  ee-with-alert-engine) at 4.12.9 found 3 of 7 broken as shipped. The runner now carries
  a curated `GOTCHAS` map: `quicksetup_list` returns `known_gotchas`, and
  `quicksetup_up` / `quicksetup_status` return the relevant `gotcha` (root cause + fix).
  For the two broken configs whose fix is a deterministic, download-free compose edit,
  `quicksetup_up` **auto-applies** it at fetch time and reports it under `autofixes`:
  - `redis-rate-limit`: `gravitee_ratelimit_redis_host` `redis-rate-limit` → `redis_rate_limit`
    (else the gateway throws `UnknownHostException` and rate-limit fails open).
  - `keycloak`: realm mount `/tmp/realm-gio.json` → `/opt/keycloak/data/import/realm-gio.json`
    (KC26 `--import-realm` dir; the legacy `KEYCLOAK_IMPORT` env is ignored, so the realm
    never imported).
  Warn-only for fixes that need a download (postgresql/mssql JDBC driver, keycloak oauth2
  plugin) or are cosmetic (ee-with-alert-engine's false-`unhealthy` healthcheck). Theme:
  for these configs docker `healthy`/`running` ≠ functional.

## [0.3.0] — 2026-07-17

### Added
- **Generic quick-setup runner** (`quicksetup_*`) — fetch and run any of the ~two dozen
  official `docker/quick-setup/*` configs from the APIM repo on demand, instead of
  vendoring them. `quicksetup_list` enumerates the configs at a version; `quicksetup_up`
  does a sparse + blobless depth-1 clone of just the one subdir at the pinned tag
  (~1–2 s), copies it into a local workdir, drops `~/.gravitee/license.key` in when the
  config mounts one, and runs it in the background under project `gravitee-qs-<name>`;
  `quicksetup_status` / `quicksetup_logs` / `quicksetup_down` (with optional `-v`) round
  it out. The fetched README is returned by `quicksetup_up` so any manual steps are at
  hand. Reuses the existing background-process + two-signal-status + port-conflict
  machinery. Runs upstream configs **as-is** (inherits their gotchas); one at a time,
  since they hardcode ports and container names.

## [0.2.0] — 2026-07-17

The tool grew from a single Gamma-stack wrapper into a multi-stack launcher and was
renamed **gamma-stack-mcp → gravitee-stacker** (package `gravitee_stacker`, MCP server
name `gravitee-stacker`, console script `gravitee-stacker`).

### Added
- **Standalone APIM stack** (`apim_*`) — self-contained compose shipped in the tool;
  version pinning (`"latest"` resolves the newest release via `git ls-remote`),
  non-blocking background bring-up, two-signal status, per-service health.
- **APIM `kafka` variant** (`apim_up(variant="kafka")`) — the native-Kafka gateway
  stack, vendored from Gravitee's official `native-kafka` quickstart (demo
  `*.kafka.local` certs + KRaft config), with the documented gotchas designed out
  (no `gio_apim_*` name collisions, a real kafka healthcheck, restart policies,
  trimmed kibana/mailhog/kafka-ui, no fragile host mounts). Requires an EE license.
  `apim_up`'s result includes ready-to-run produce/consume/verify commands.
- **Standalone AM stack** (`am_*`) — self-contained, derived from the official
  `gravitee-access-management` compose with the automation gotchas fixed.
- **Generalized coexist** — named `instance`s let multiple APIM/AM stacks run at once,
  each with its own compose project, data volumes, and auto-allocated port band.
  New `apim_list` / `am_list` enumerate tracked instances.
- **`stack_preflight`** — guided-launch preview: resolves the version, checks ports,
  and returns `start` / `down_conflicting` / `coexist` options without side effects.
- **`doctor`** — one-call readiness check (Docker, license, Gamma SDK).
- **License auto-detection** at `~/.gravitee/license.key` (arg → `APIM_LICENSE` env →
  conventional path → OSS).
- **`APIM_COMPOSE_FILE` / `AM_COMPOSE_FILE`** overrides to bring your own compose.
- Distribution: MIT → **Apache-2.0** (Gravitee copyright); wheel bundles all data
  files; installs + runs from a fresh venv (pipx-ready).

### Changed
- Gamma coexist (host-port remap overlay) was removed — Gamma is canonical-ports-only
  (its consoles hardcode host-routing/`:80`). Coexist is now the APIM/AM instance model.
- Ports are read live from `docker compose config`, so editing a compose's ports is
  reflected in conflict detection and reported URLs (no desync).

### Fixed
- **APIM console "Management API unreachable"** — the console's `MGMT_API_URL` was
  org/env-scoped; corrected to the base `/management/` path (matches Gravitee's own
  reference compose). The related bootstrap-500 was confirmed to be stock APIM behavior,
  not a generator bug.
- **Kafka consume-through-gateway returned 0 messages** — root-caused to the single
  broker: `offsets.topic.replication.factor` defaulted to 3, so `__consumer_offsets`
  could never be created and all consumer-group reads timed out. Pinned it (and the
  transaction-state topics) to 1; group reads now work.
- **Instance port-allocation race** — two instances started in quick succession could
  grab the same port; allocation now also avoids ports/offsets already claimed by
  another tracked instance.
- **Kafka broker crash-loop on the OAuth JWKS placeholder** — the vendored broker
  config now defaults to `sasl.enabled.mechanisms=PLAIN` only (keyless + API-key demo
  flows), with OAUTHBEARER commented out. Kafka eagerly initializes the OAuth validator
  at startup and fatals if `sasl.oauthbearer.jwks.endpoint.url` is a placeholder /
  unreachable host; re-enable it once you have a real JWKS.

## [0.1.0] — initial

- **Gamma demo stack** wrapper (`stack_*`) over the stack repo's `docker/run.sh`:
  non-blocking background bring-up (`Popen`, never `run`), two-signal status
  (up-process liveness + `docker compose ps` health), `stack_setup` / `stack_down` /
  `stack_logs` / `stack_ports`, and `stack_install_daemon` / `stack_uninstall_daemon`
  (which return the sudo command rather than executing it).
