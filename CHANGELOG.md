# Changelog

All notable changes to **gravitee-stacker** are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
