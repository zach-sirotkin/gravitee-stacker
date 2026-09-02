# gravitee-stacker

An [MCP](https://modelcontextprotocol.io) server that stands up and manages local
Gravitee Docker stacks from your AI assistant (Claude Code, Cursor, Claude Desktop) —
launch any release on command, run several at once, no hand-rolled compose files.

- **Standalone APIM** (`apim_*`) — a self-contained API Management stack; **needs only
  Docker**. Bases: `default` (OSS) and `kafka` (native-Kafka gateway), plus **composable
  features** — layer `prometheus`, `redis-rate-limit`, `debug-logging`, … onto either base
  and mix them freely (e.g. a Kafka stack *with* Prometheus scraping *and* Redis
  rate-limiting), all coexist-safe.
- **Verbose logging out of the box** — the `debug-logging` feature (gateway +
  management-api at `DEBUG`) is applied to **every** deploy by default, since local stacks
  exist to be debugged. Opt out per-deploy with `features=["-debug-logging"]`, or
  machine-wide with `APIM_DEFAULT_FEATURES=""`.
- **Plugin management** (`apim_plugin_*`) — search the Gravitee plugin **catalog**
  (download.gravitee.io), check **which APIM version a plugin was built for**, list what's
  already **bundled** in an image, and **add/remove** plugins on a running stack
  (downloaded into `plugins-ext`, gateway reloaded — Gravitee's documented approach).
- **Standalone AM** (`am_*`) — a self-contained Access Management stack; needs only Docker.
- **Any official quick-setup** (`quicksetup_*`) — fetch and run any of the ~two dozen
  upstream `docker/quick-setup/*` configs (mongodb, postgresql, keycloak, native-kafka,
  opensearch, prometheus, …) on demand, with known-gotcha auto-fixes.
- **Gamma platform** (`gamma_*`) — the full Gamma platform (Agent / Authorization /
  Event / Edge Management on top of APIM) from a self-contained compose.
- **See + tweak config** (`*_config`) — surface the rendered `gravitee_*` overrides on a
  running stack as an editable file and apply changes on the fly (apim / am / gamma).

APIM, AM, and Gamma support **named instances** so you can run several stacks of the same
kind at once — ports shift with the instance so they coexist. A guided-launch helper
(`stack_preflight`) resolves the version, checks ports, and offers down-vs-coexist on a
conflict.

## Quickstart

**Requirements:** Docker Desktop (running), Python 3.10+, macOS or Linux.

**1. Install**
```bash
git clone https://github.com/zach-sirotkin/gravitee-stacker.git
cd gravitee-stacker
python3 -m venv .venv && ./.venv/bin/python -m pip install -e .
```

**2. Register it** with your client — Claude Code:
```bash
claude mcp add gravitee-stacker -s user -- /ABSOLUTE/PATH/TO/gravitee-stacker/.venv/bin/gravitee-stacker
```
…or add the JSON block under [Wire it into a client](#wire-it-into-a-client) (Cursor / Claude Desktop use the same block). Use an **absolute** path — clients don't expand `~`.

**3. Check readiness** — ask your assistant to run the `doctor` tool. It reports
what's ready and what's missing for each stack.

**4. Run APIM** — that's all it takes for the APIM stack:
> "Stand up the latest APIM." → `apim_up()` → `apim_wait()` (returns when healthy) → console at http://localhost:8084 (admin/admin).

### Where to put things

| Thing | Where | Needed for |
| ----- | ----- | ---------- |
| **Gravitee license** (optional) | `~/.gravitee/license.key` — `mkdir -p ~/.gravitee && cp <your-license>.key ~/.gravitee/license.key` | Enterprise features on any stack. Without it stacks run in **OSS mode**. Auto-detected + auto-mounted; no config needed. |

> A license is optional; images are pulled from Docker Hub. So step 4 is the whole setup.

## What it exposes

The stack families listed above, tool by tool. Two meta tools first:

| Tool              | What it does                                                                                          |
| ----------------- | ---------------------------------------------------------------------------------------------------- |
| `doctor`          | One-call readiness check (Docker, license) — what's set up and what's missing. Run it first. |
| `stack_preflight` | Guided-launch preview (no side effects): resolves the version, computes the ports, checks conflicts, and returns the options — `start`, or on conflict `down_conflicting` vs `coexist`. Use it to ask the user which path before launching. |

**Recommended launch flow:** when asked to bring up a stack, (1) confirm the **version**
(latest or a tag) and — for APIM — the **variant** (default/kafka); (2) `stack_preflight`;
(3) on a conflict, offer **down the other stack** vs **coexist** (a named instance); then
`apim_up`/`am_up`.

### Gamma platform (`gamma_*`)

The full Gamma platform (`gamma-compose.yml`: gateway + management-api + APIM console +
portal + Gamma console + mongo + es), from the [Gravitee docs](https://documentation.gravitee.io/gravitee-gamma/platform-management/install/self-hosted-installation-guides/docker/docker-compose).
Canonical ports **8082–8086** (Gamma console `:8086`, APIM console `:8084`, portal
`:8085`); named instances coexist on shifted bands (see below). A license is optional —
only Agent Management needs one.

| Tool           | What it does |
| -------------- | ------------ |
| `gamma_up`     | Pull public images + `docker compose up -d` in the background. Options: `version` (`latest`→4.12, or a pin), `instance` (run several at once), `features` (e.g. `["mailpit"]` — local SMTP capture), `pull`, `recreate`, `down_conflicting`, `license` (optional — Agent Management only). |
| `gamma_wait`   | Block until healthy, then return at once (fails fast). Use after `gamma_up` instead of a sleep loop. Takes `instance`. |
| `gamma_status` | Overall verdict + per-service health, version, project, URLs. Takes `instance`. |
| `gamma_config` | View + tweak the rendered `gravitee_*` config values (`show`/`apply`/`reset`; `full=True` dumps the hidden gravitee.yml defaults). Takes `instance`. See [Config](#config-see--tweak-the-rendered-values). |
| `gamma_list`   | List tracked Gamma instances with status/version/ports/URLs (for coexist). |
| `gamma_license` | Show the license entitlements (tier/packs/features/expiry) loaded on the running stack — read from the mgmt-api node endpoint. A greyed-out Gamma module means its pack isn't in `packs`. Takes `instance`. |
| `gamma_down`   | `docker compose down` (volumes preserved; `volumes=true` wipes data). Takes `instance`. |
| `gamma_logs`   | Tail one service (`gateway`, `management_api`, `gamma_console`, …). Takes `instance`. |

**Coexist:** a named instance gets its own project + volumes + auto-allocated port band
(+20000, +40000) — all five ports and the console URLs shift together, so a named Gamma
runs alongside a standalone `apim_*`/`am_*` or a second Gamma **version**. (Gamma bundles
APIM, so same-band stacks collide.) Coexisting consoles share the `localhost` cookie jar
— open the second in **Incognito** (`gamma_up` warns).

### Standalone APIM stack (`apim_*`)

A self-contained OSS APIM stack (`apim-compose.yml`, project `gravitee-apim`:
mongodb + elasticsearch + gateway + management-api + console + portal) on ports
**8082/8083/8084/8085**. Independent of the Gamma stack.

| Tool                 | What it does                                                                                       |
| -------------------- | ------------------------------------------------------------------------------------------------- |
| `apim_up`            | Resolves + pins a release, checks ports, starts APIM in the background. On a port conflict it does not start — returns `port_conflict`. Options: `version` (`"latest"` or e.g. `"4.12.7"`), `variant` (`default`/`kafka`), `features` (see below), `instance` (run several at once), `down_conflicting=true` (down the other stack first), `recreate=true`, `license="/path/…"`. |
| `apim_status`        | Overall verdict + per-service health, pinned version, variant, features, project, and URLs. Takes `instance`. |
| `apim_config`        | View + tweak the rendered `gravitee_*` config overrides on a running instance — `show` writes an editable `~/.gravitee/stacker-config/<project>.override.yml`, `apply` recreates to make edits live, `reset` reverts; `full=True` also dumps the hidden gravitee.yml defaults. See [Config](#config-see--tweak-the-rendered-values). |
| `apim_license`       | Show the enterprise license entitlements (tier/packs/features/expiry) loaded on a running instance — read from the mgmt-api node endpoint. A disabled EE feature usually means its pack isn't in `packs` (entitlement gap, not a mount problem). |
| `apim_wait`          | **Block until the stack is actually healthy, then return at once** — the readiness-aware alternative to sleeping in a loop after `apim_up`. Returns the moment `overall` flips to healthy; fails fast on a crashed launcher (`failed`) or nothing running (`down`). `timeout_seconds` is a safety ceiling (raise it for a first-ever pull), not a fixed wait. |
| `apim_list`          | Tracked APIM instances + status/version/features/URLs — **plus** `other_stacks_on_apim_ports` (quick-setups or untracked stacks actually holding 8082–8085; run-records alone are unreliable, so prefer `stack_preflight` for real occupancy). |
| `apim_down`          | `docker compose down` (volumes preserved). `volumes=true` → `down -v` (wipe data, e.g. for a clean version downgrade). Takes `instance`.                                        |
| `apim_logs`          | Tail one APIM service (incl. feature services `apim-prometheus` / `apim-redis`). Takes `instance`. |
| `apim_latest_version`| Resolves the newest stable APIM release tag from the repo (via `git ls-remote`).                   |
| `apim_plugin_search` | Search the plugin **catalog** (download.gravitee.io) by name/type → latest version.                |
| `apim_plugin_info`   | Inspect a catalog plugin's manifest + the **APIM version it was built for** (from its embedded pom). |
| `apim_plugin_bundled`| List plugins **bundled** in an image (`ls plugins/` in `apim-<component>:<version>`).               |
| `apim_plugin_add`    | Download a plugin (name+version or a download.gravitee.io URL) into `plugins-ext` + reload the gateway. Takes `instance`. |
| `apim_plugin_list` / `apim_plugin_remove` | list added (+ bundled) / remove an added plugin. Takes `instance`.            |

**Composable features.** Pass `features` to layer curated capability overlays onto either
base (`default` or `kafka`) — each is a small compose fragment merged with `-f`, so they
combine cleanly and mix freely:

| Feature | Adds | Access |
| ------- | ---- | ------ |
| `prometheus` | a Prometheus that scrapes the gateway's metrics endpoint | Prometheus UI on **:9090** |
| `redis-rate-limit` | points the gateway's rate-limit store at a bundled Redis | internal (no host port) |
| `debug-logging` **(on by default)** | verbose `DEBUG` logs from the gateway + management-api (`io.gravitee` at DEBUG, via a bundled `logback-debug.xml`) | via `apim_logs` |
| `alert-engine` **(EE license)** | Gravitee Alert Engine wired to the gateway **container-to-container** on the shared network (`ws_discovery=false`), so it receives gateway events. | node API on **:18072** (`/_node/*`); create alerts in the console |
| `mailpit` | Local SMTP capture (Mailpit) + `email.enabled=true` on the management-api — so registration / password-reset / subscription emails are exercisable locally instead of silently no-oping. | Mailpit web UI on **:8025**; SMTP `:1025` internal |

**Alert Engine — one gotcha to know.** After a **fresh-volume** start, restart the gateway
once the stack is healthy — **`apim_alert_engine_fix(instance)`** — or alerts *silently*
never fire. On a cold Mongo the gateway caches `installation=null` (the mgmt-api hasn't
written the installation record yet), and every console alert's auto-injected `installation
EQUALS <uuid>` filter then drops every event, with no error anywhere. `apim_up` warns about
this when `alert-engine` is layered on fresh volumes. For debugging: **`ae_log_level`**
(flip an AE logger to DEBUG via `/_node/logging`) and **`ae_trigger_dump`** (`/_node/triggers`
— diff a trigger's `filters` against the event `properties` to catch a silent rejection).
Note: `Events successfully sent.` in the gateway log is just node-heartbeat traffic — it
does **not** prove request events are flowing.

**Custom / experimental features** live **outside** the package: drop an
`apim-feature-<name>.yml` in `~/.gravitee/stacker-features/` (override with
`APIM_FEATURES_DIR`) and use it via `apim_up(features=["<name>"])`. A user overlay wins
over a bundled one of the same name — so you never have to edit the installed package to
try a feature.

**Defaults.** `debug-logging` is layered onto **every** deploy (`apim.DEFAULT_FEATURES`) —
local stacks are for debugging, so verbose logs are the useful default. Opt out per-deploy
by prefixing with `-`, or machine-wide via the env var:

```
apim_up(features=["-debug-logging"])          # this deploy: normal INFO/WARN logging
APIM_DEFAULT_FEATURES=""                       # machine-wide: no default features
```
`apim_up`/`apim_status` report the **resolved** `features`, so you can always see what
actually got layered on. (This is verbose *logging* — distinct from APIM **Debug mode**,
the console's policy tracer, which is a bundled plugin, enabled by default, and needs an
EE license.)

```
apim_up(variant="kafka", features=["prometheus", "redis-rate-limit"])
```
…gives a native-Kafka gateway **with** Prometheus scraping **and** Redis rate-limiting in
one stack. Features are **coexist-safe**: a named `instance` shifts the feature ports too
(prometheus → 29090 at +20000), so composed stacks run side by side. Reach for `features`
to *combine* capabilities and coexist; reach for `quicksetup_*` to run one specific
upstream config verbatim.

**Plugins.** Add any Gravitee plugin to a running APIM instance, following Gravitee's
documented approach (drop the zip into a `plugins-ext` dir + restart the node — the
curated stack bind-mounts one per instance into the gateway + management-api). Three views:

- **Catalog** — `apim_plugin_search("keycloak")` lists what's downloadable from
  download.gravitee.io (all APIM plugins, OSS **and** EE — EE ones need a license at
  runtime), with latest versions.
- **Compatibility** — `apim_plugin_info(name)` downloads the plugin and reads the APIM
  version it was **built for** from its embedded `pom.xml` (`gravitee-apim.version`). A
  plugin's version is its *own* line, not the APIM version — check before adding.
- **Bundled** — `apim_plugin_bundled(version)` lists what already ships in an image, so
  you don't re-add something that's built in.

`apim_plugin_add("gravitee-resource-oauth2-provider-keycloak")` resolves the latest
version, downloads it into the instance's `plugins-ext`, and recreates the gateway to
load it (`apim_plugin_list` / `apim_plugin_remove` manage what's added). You can also pass
an explicit `version`, `type`, or a full download.gravitee.io URL.

> Not covered: plugins that live **only** in private GitHub repos (e.g. the AM EE
> IdP/MFA packs) — those ship no release binary and would need a build-from-source step.

### Standalone AM stack (`am_*`)

A self-contained AM stack (`am-compose.yml`, project `gravitee-am`: nginx + mongo +
gateway + management-api + console), derived from the official
`gravitee-access-management` compose with the automation gotchas fixed (no host
log-file bind-mounts — logs via `am_logs`; nginx routing shipped as a stable file;
env-driven mongo URIs; no obsolete `version:` key). **Only nginx is published to the
host** (one port); everything else is internal.

| Tool               | What it does                                                                                     |
| ------------------ | ----------------------------------------------------------------------------------------------- |
| `am_up`            | Resolves + pins a release (`GIO_AM_VERSION`), checks the nginx port, starts AM in the background. On a port conflict returns `port_conflict`. Options: `version` (`"latest"` or e.g. `"4.11.10"`), `instance` (run several at once), `port` (default `AM_NGINX_PORT` or 8086), `features` (e.g. `["mailpit"]` — local SMTP for AM's registration/forgot-password/MFA-by-email flows), `recreate=true`, `down_conflicting=true`. |
| `am_status`        | Overall verdict + per-service health, version, port, project, URLs. Takes `instance`. The management API is slow — status stays `partial` until its healthcheck passes, so wait for `healthy`. |
| `am_config`        | View + tweak the rendered `gravitee_*` config values (`show`/`apply`/`reset`; `full=True` dumps the hidden gravitee.yml defaults). Takes `instance`. See [Config](#config-see--tweak-the-rendered-values). |
| `am_wait`          | Block until the AM stack is healthy, then return at once (fails fast on error). Use it instead of a sleep loop after `am_up` — the slow mgmt-API healthcheck is exactly what it handles. |
| `am_list`          | List all tracked AM instances with their status/version/port/URLs.                                |
| `am_down`          | `docker compose down` (volumes preserved). Takes `instance`.                                        |
| `am_logs`          | Tail one AM service (e.g. `gateway`, `management`). Takes `instance`.                               |
| `am_latest_version`| Resolves the newest stable AM release tag from the repo (via `git ls-remote`).                    |

**Ports & access.** AM needs a single host port (`port`, default 8086). Since it
overlaps nothing else (Gamma/APIM use 80–8085), it usually just runs; if the port is
taken it reports the conflict. UIs are path-routed through nginx: console
`http://localhost:{port}/am/ui/` (admin/adminadmin), management API `…/am/management/`,
gateway `…/am/`.

### Config: see + tweak the rendered values

Hard to know what's *actually* configured on a running stack? `apim_config` / `am_config` /
`gamma_config` surface the **rendered `gravitee_*` overrides** — the values stacker + your
overlays actually set, fully interpolated — as an editable file you can change on the fly:

```
apim_config(instance="default")                   # writes ~/.gravitee/stacker-config/gravitee-apim.override.yml
# …edit any value in that file…
apim_config(instance="default", action="apply")   # recreate gateway + mgmt-api → edits live (~20s), then apim_wait
apim_config(instance="default", action="reset")   # delete override + recreate → back to base+features
```

- **`show`** (default) writes/refreshes the editable override; **`apply`** recreates the config
  services so edits take effect (volumes kept); **`reset`** reverts.
- The override file is **auto-layered** onto every future up/recreate for that project, so
  edits persist until you `reset`.
- These are the top **overrides** layer only. The image's `gravitee.yml` ships hundreds of
  **hidden defaults** you never set (e.g. `email.enabled: false` — the reason the [`mailpit`
  feature](#composable-features) has to flip it). Pass **`full=True`** to also dump each
  service's in-image `gravitee.yml` as a read-only reference so those defaults are visible.
- The **full rendered compose** (`<project>.rendered-compose.yml`) — every service / image /
  port / volume / network + all env, fully interpolated (like `docker compose config`) — is
  **written automatically on every `*_up`**, refreshed on config `apply`/`reset`, and **deleted
  on `*_down`**. So if the file is there it matches the live stack (never stale); it's the whole
  stack the thin override layer doesn't repeat. Pass **`compose=True`** to force-refresh it on
  demand, or set `STACKER_COMPOSE_DUMP=0` to turn the auto-dump off.
- Gravitee resolves config at **startup**, so "on the fly" means edit → recreate (fast), not a
  live hot-reload.

### Quick-setup configs (`quicksetup_*`)

The APIM repo ships ~two dozen ready-made compose configs under `docker/quick-setup/`
(mongodb, postgresql, redis-rate-limit, keycloak, native-kafka, opensearch, prometheus,
opentelemetry-jaeger, https-\*, distributed-sync\*, ee-with-alert-engine, …). Rather than
vendor and maintain 26 copies, this runner **fetches one on demand** at the pinned APIM
version (a sparse + blobless depth-1 clone of just that subdir, ~1–2 s) and runs it
**as-is** under project `gravitee-qs-<name>`.

| Tool                | What it does                                                                                       |
| ------------------- | ------------------------------------------------------------------------------------------------- |
| `quicksetup_list`   | List every `docker/quick-setup/*` config at a version (`"latest"` or a tag), plus which are running locally. |
| `quicksetup_up`     | Fetch `<name>` + `docker compose up -d` in the background. Drops `~/.gravitee/license.key` in if the config mounts one. On a port conflict returns `port_conflict`. Returns the fetched **README** so its manual steps are at hand. Options: `version`, `pull`, `recreate`, `down_conflicting`, `fetch`. **`fetch=False` reuses your EDITED workdir instead of re-cloning** — the default `fetch=True` overwrites local edits (and now warns when it does). Loop: fetch once → edit `docker-compose.yml` in the workdir → re-run `fetch=False, recreate=True`. |
| `quicksetup_status` | Overall verdict + per-service health, version, project, ports, up-log tail.                        |
| `quicksetup_down`   | `docker compose down` (add `volumes=true` for `down -v`).                                          |
| `quicksetup_logs`   | Tail one service of a running config.                                                              |

**Boundaries (by design).** These are the "everything else" escape hatch; the curated
`apim_*` / `am_*` stacks stay the polished, fully-automated happy-path.
1. Runs the upstream config **verbatim** → it inherits that config's gotchas and any
   **manual steps** (keycloak realm import, native-kafka console setup, mssql/postgres
   backends). Read the returned README.
2. **One at a time (raw runner only)** — the upstream composes hardcode host ports
   (mostly 8082–8085) and container names (`gio_apim_*`), so only **one quick-setup runs
   at a time**, and there's no port-shift. Conflict detection and down-the-other work.
   This limit is specific to running a config *verbatim* — **to coexist or to combine
   capabilities** (e.g. Kafka + Prometheus + Redis in one stack, or two stacks at once),
   use `apim_up` with [composable features](#standalone-apim-stack-apim_) instead, which
   are port-parameterized and coexist-safe.
3. **EE configs** (`ee-with-alert-engine`, `native-kafka`, …) need a license — dropped in
   automatically from `~/.gravitee/license.key`/`APIM_LICENSE` when present.

**Known gotchas + auto-fixes.** Several upstream configs are broken or misleading as
shipped (found in a deep-functional sweep at 4.12.9). The runner carries a curated
`GOTCHAS` map — `quicksetup_list` flags them, and `quicksetup_up` returns the relevant
one plus **auto-applies the safe, download-free fixes** at fetch time:

| Config | Gotcha | Handling |
| ------ | ------ | -------- |
| `redis-rate-limit` | `gravitee_ratelimit_redis_host=redis-rate-limit` ≠ service `redis_rate_limit` → gateway `UnknownHostException`, rate-limit silently fails **open** | **auto-fixed** (host → `redis_rate_limit`) |
| `keycloak` | KC26 image but legacy `KEYCLOAK_IMPORT` + realm mounted to `/tmp` → the `gio` realm never imports, no tokens | **auto-fixed** (realm → `/opt/keycloak/data/import/`). Gateway token validation still needs `download-plugins-ext.sh` (a download) |
| `postgresql` | needs a JDBC driver in `./.driver`; without it management-api crash-loops `Unable to load repository repository-jdbc` — while reporting **healthy** | **warn-only** (driver is a download) |
| `mssql` | (1) same JDBC driver requirement as postgres; **(2)** bundled `init-db.sh` calls the old `/opt/mssql-tools/bin/sqlcmd` path (image now ships `mssql-tools18`) and omits `-C`, so the `gravitee` DB is never created | **(2) auto-fixed** (path + `-C`); **(1) warn-only** (driver is a download) |
| `ee-with-alert-engine` | works, but a false healthcheck marks `alert_engine` **unhealthy** so status stays `partial` | **warn-only** (cosmetic) |

The through-line: for these configs docker **`healthy`/`running` ≠ functional** — the
`gotcha` field on `quicksetup_up`/`quicksetup_status` says when to distrust the verdict.

Once the download-gated requirements are satisfied, all of these were verified working
end-to-end: **postgresql** / **mssql** (drop the JDBC driver into `.driver` → an API
persists via JDBC and serves through the gateway), and **keycloak** (run
`download-plugins-ext.sh` → the OAuth2-secured API rejects requests with no/invalid token
`401` and validates real Keycloak tokens).

### Running multiple stacks at once (generalized coexist)

`apim_*`, `am_*`, and `gamma_*` take an **`instance`** name so you can run several stacks
of the same kind concurrently. Each instance gets its own compose **project**, its own
data volumes, and an **auto-allocated host-port band** — no manual port math:

- `instance="default"` → canonical ports / project `gravitee-apim` (or `gravitee-am`).
- a named instance → project `gravitee-apim-<name>` / `gravitee-am-<name>`, ports
  auto-shifted (APIM: +20000, +40000; AM: next free port). Allocation avoids ports
  already bound *and* already claimed by another tracked instance (no start-up race).

```
apim_up(instance="a")            # canonical 8082–8085  (a == first/default band)
apim_up(instance="b")            # auto → 28082–28085
am_up(instance="a")              # → 8086 (or next free)
am_up(instance="b")              # → 8087
apim_list  /  am_list            # every tracked instance + its status/urls
apim_status(instance="b")        # target a specific instance
apim_down(instance="b")          # down just that instance (volumes preserved)
```

The **kafka** variant is single-instance (its `*.kafka.local` cert + advertised
listeners assume fixed ports). Up to ~3 concurrent APIM instances fit before the
port bands run out (offset cap 40000).

> **⚠️ Two consoles in one browser share a login.** Browser cookies are scoped by
> **host**, not by host **and port**, so every stack on `localhost` (`localhost:8084`,
> `localhost:28084`, …) writes to the **same cookie jar**. The console's auth cookie
> (`Auth-Graviteeio-APIM`) is overwritten each time you sign into a different stack, so
> bouncing between two consoles keeps logging you out of the one you left. This is a
> browser rule, not a stacker bug. **Open the second stack's console in an Incognito /
> private window** (its own cookie jar) — or use a separate browser profile per stack —
> to keep both sessions alive at once. `apim_up` also emits this reminder in its
> `warnings` whenever it starts a coexist instance.

**Versions.** `version="latest"` resolves the newest stable tag from
`gravitee-io/gravitee-api-management` → sets `APIM_VERSION` → pulls
`graviteeio/apim-*:<version>`. Pin a tag with `version="4.12.7"`; reload/recreate with
`recreate=true`. UIs are direct (no host-routing): console `http://localhost:8084`
(admin/admin), portal `:8085`, management API `:8083/management`, gateway `:8082`
(a named instance shifts these by `APIM_PORT_OFFSET`, default 20000 — see coexist below).

**License.** To run with enterprise features, drop a Gravitee license at the
conventional path **`~/.gravitee/license.key`** and `apim_up` mounts it into the
gateway + management-api automatically. Resolution order: `license="/path/..."` arg →
`APIM_LICENSE` env → `~/.gravitee/license.key` → OSS mode. The mount is applied via an
overlay (`apim-license.yml`) only when a real, non-empty license file is found (a
phantom bind-mount directory is skipped). `apim_up`'s result reports which source was used.

**Conflict flow.** `apim_up` checks its target ports first. On a conflict it identifies
the exact compose project/containers and returns `port_conflict` + a `suggest` payload —
it **never auto-downs**. `suggest` offers `down_the_other` (`down_conflicting=true`,
which downs the conflicting project without `-v` so data is preserved) and, for the
default instance, `run_another_instance` (a named `instance` on a shifted port band).
Prefer `stack_preflight` up front so you can present these choices before launching.

**Kafka variant** — `apim_up(variant="kafka")` brings up the **native-Kafka gateway**
stack (project `gravitee-apim-kafka`: adds a KRaft broker + kafka-client; the gateway
also binds a Kafka listener on **:9092** TLS/SNI). Vendored from Gravitee's official
`native-kafka` quickstart (demo `*.kafka.local` certs + KRaft config) with the known
gotchas designed out (no `gio_apim_*` name collisions, a real kafka healthcheck, restart
policies, trimmed kibana/mailhog/kafka-ui, no fragile host mounts). **Requires an EE
license with the Kafka feature** (blocks otherwise — the gateway won't bind :9092
without it). Any recent release works (verified on 4.12.x). Runs on default ports;
coexist isn't supported for this variant (single-instance). After healthy, verify with
`apim_logs("apim-gateway")` for `Kafka server ready to accept connections on port 9092`.

One-time console config (per fresh build — it lives in Mongo, not the files): at the
console (admin/admin), Organization → Entrypoints & Sharding Tags → **Default Kafka
Bootstrap Domain Pattern = `{apiHost}.kafka.local`** (the field defaults to just
`{apiHost}` — the `.kafka.local` suffix must be appended so SNI/DNS match the
`*.kafka.local` cert and `foo.kafka.local` alias), then create a Kafka API (Protocol
Kafka, host prefix `foo`, endpoint `kafka:9091`, Keyless). Clients bootstrap at
`foo.kafka.local:9092`. `apim_up`'s return payload includes ready-to-run single-line
produce/consume commands with this project's exact container names.

Consumer-group reads work out of the box: the vendored broker config pins
`offsets.topic.replication.factor=1` (+ the transaction-state topics), so
`__consumer_offsets` is creatable on the single broker — without it, group reads
(incl. consume-through-gateway) time out at 0 messages while partition-direct reads
work. For a coordination-free sanity check, read the broker directly with
`--partition 0 --offset earliest`. Wants Docker >= 16 GiB.

### The background-process design (the important part)

Every `*_up` uses `subprocess.Popen` (never `subprocess.run`), redirects stdout+stderr
to `.run/<stack>/up.log`, records the PID + log path, and returns `{status: "starting",
pid, log_path}` **without waiting**. That's what keeps cold image pulls from timing out
your MCP client. You then call `*_wait` (or poll `*_status`) to learn when the stack is
actually ready.

`*_status` reports liveness two ways and shows both: (1) PID-liveness of the tracked
up-process, and (2) real health parsed from `docker compose ps` (the authoritative
signal — the launcher exits the moment `up -d` returns). The service list is read live
via `docker compose config --services`, so it never drifts from the actual compose files.

## Configuration

| Env var          | Default                                                     | Meaning                                    |
| ---------------- | ---------------------------------------------------------- | ------------------------------------------ |
| `GAMMA_VERSION`  | `4.12`                                                     | Image minor tag for the public Gamma stack (overridden by `gamma_up`'s `version` arg). |
| `GAMMA_COMPOSE_FILE` | shipped `gamma-compose.yml`                            | Point at your own Gamma compose instead of the bundled one. |
| `APIM_PORT_OFFSET` | `20000`                                                 | Host-port band size for named APIM instances (coexist). |
| `APIM_LICENSE`   | unset                                                      | Default license path for the APIM stack (overridden by `apim_up`'s `license` arg). |
| `APIM_COMPOSE_FILE` | shipped `apim-compose.yml`                            | Point at your own APIM compose to use instead of the bundled one (see below). |
| `AM_NGINX_PORT`  | `8086`                                                     | Default host port for the AM stack's nginx (overridden by `am_up`'s `port` arg). |
| `AM_COMPOSE_FILE` | shipped `am-compose.yml`                                  | Point at your own AM compose instead of the bundled one. |
| `APIM_FEATURES_DIR` | `~/.gravitee/stacker-features`                          | Where custom feature overlays (`<stack>-feature-<name>.yml`) live — your overlays win over bundled ones. |
| `STACKER_CONFIG_DIR` | `~/.gravitee/stacker-config`                           | Where `*_config` writes the editable per-project override files + the auto rendered-compose dump. |
| `STACKER_COMPOSE_DUMP` | `1` (on)                                              | Set to `0`/`false` to stop auto-writing `<project>.rendered-compose.yml` on every up. |
| `GAMMA_MCP_STATE_DIR` | `<this project>/.run`                                 | Where the tracked up-process metadata + `up.log` live (per stack). The daily-use install points this at `~/.gravitee/stacker-run`. |

**Customizing the APIM stack.** The shipped `apim-compose.yml` is a plain compose
file. Two ways to change it safely:

- **Edit it in place** — the tool reads the published ports back from `docker compose
  config`, so changing ports there is reflected in conflict detection and the reported
  URLs (no desync). Caveat: on a non-editable (wheel/pipx) install the file lives in
  `site-packages` and a reinstall overwrites it — prefer `APIM_COMPOSE_FILE` for durable
  edits.
- **Bring your own** — set `APIM_COMPOSE_FILE=/path/to/your-compose.yml`. The tool uses
  it for up/down/logs/status and reads the project name + ports from it. For coexist mode
  to remap ports, your compose should parameterize them as `${APIM_GATEWAY_PORT:-8082}`
  etc. (as the shipped one does); otherwise it still runs fine on canonical ports.

### Ports

- **Gamma** uses canonical `8082–8086` by default; named instances shift to +20000/+40000.
  Gamma bundles APIM, so two stacks on the *same* band collide.
- **APIM, AM & Gamma coexist** via named `instance`s — see [Running multiple stacks](#running-multiple-stacks-at-once-generalized-coexist).

Runtime state (up-process metadata + `up.log`, per stack) lives in this project's `.run/`
(gitignored).

### Prerequisites

- **Docker Desktop running** (`docker info` must succeed) — that's it. All images are
  public; a Gravitee license at `~/.gravitee/license.key` is optional (enterprise features).

Each `*_up` checks Docker first and returns a clear message instead of a cryptic
mid-startup failure.

## Install / run

> **Public, but not on PyPI.** Install a pinned version straight from git (no clone
> needed), or grab the wheel from a [GitHub Release](https://github.com/zach-sirotkin/gravitee-stacker/releases):
> ```bash
> pipx install "git+https://github.com/zach-sirotkin/gravitee-stacker@v0.7.2"
> ```
> Use **≥ v0.7.2** — earlier wheels don't cap the `mcp` dependency and break on a fresh
> install now that `mcp` 2.0 is out. Everything runs on public images; no Gravitee access needed.

For local development, clone and install editable:

```bash
git clone https://github.com/zach-sirotkin/gravitee-stacker.git
cd gravitee-stacker
python3 -m venv .venv          # Python 3.10+
./.venv/bin/python -m pip install -e .
```

Run the server directly (stdio transport):

```bash
./.venv/bin/python -m gravitee_stacker.server
# or, via the installed console script:
./.venv/bin/gravitee-stacker
```

## Wire it into a client

Both use stdio transport and point at the venv's Python so no activation is needed.

### Claude Code

The one-liner (user scope → available in every project). Point at the venv's console
script — no env vars needed:

```bash
claude mcp add gravitee-stacker -s user \
  -- /ABSOLUTE/PATH/TO/gravitee-stacker/.venv/bin/gravitee-stacker
```

Verify, and later remove, with:

```bash
claude mcp get gravitee-stacker      # → Status: ✓ Connected
claude mcp remove gravitee-stacker -s user
```

Equivalently, add to `~/.claude.json` (user scope) or a project `.mcp.json` by hand:

```json
{
  "mcpServers": {
    "gravitee-stacker": {
      "command": "/ABSOLUTE/PATH/TO/gravitee-stacker/.venv/bin/gravitee-stacker"
    }
  }
}
```

Notes:

- **Restart to pick up the tools.** MCP tools load at session start, so the
  `mcp__gravitee-stacker__*` tools appear in a **new** Claude Code session, not the one
  you ran `claude mcp add` in. Sanity-check with `doctor` or `quicksetup_list`.
- **Editable install ⇒ no reinstall on edits.** If you installed with `pip install -e .`,
  the console script runs the source tree in place — after changing the code, just
  restart the session (or toggle the server) to load it.
- **Paths must be absolute** — MCP clients don't expand `~`/`$HOME`. No env vars are
  required; every stack needs only Docker.
- Prefer a PATH-stable install decoupled from the dev venv? `pipx install
  /path/to/gravitee-stacker`, then set `command` to just `gravitee-stacker` (rebuild on
  changes with `pipx reinstall`).

### Cursor

Add to `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project) — same block as above:

```json
{
  "mcpServers": {
    "gravitee-stacker": {
      "command": "/ABSOLUTE/PATH/TO/gravitee-stacker/.venv/bin/gravitee-stacker"
    }
  }
}
```

### Claude Desktop

The desktop app has **its own MCP config**, separate from Claude Code — so registering
in Claude Code does **not** make it appear in Desktop. Edit
`~/Library/Application Support/Claude/claude_desktop_config.json` (Settings → Developer →
Edit Config), add the block below, then **fully quit and reopen** the app (⌘Q — it reads
the config at launch):

```json
{
  "mcpServers": {
    "gravitee-stacker": {
      "command": "/ABSOLUTE/PATH/TO/gravitee-stacker/.venv/bin/gravitee-stacker",
      "env": {
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
      }
    }
  }
}
```

Two macOS gotchas hit **GUI apps only** (Claude Code, launched from a terminal, avoids
both — so if it works there but not in Desktop, it's one of these):

- **Pin `PATH`.** A GUI-launched process gets a minimal `PATH`, so `docker` (usually in
  `/usr/local/bin` or `/opt/homebrew/bin`) isn't found and `doctor` reports *"docker not
  on PATH"*. The `PATH` above covers `docker`/`git`/`lsof`.
- **Keep the install out of a protected folder.** macOS TCC blocks GUI apps from reading
  `~/Documents`, `~/Desktop`, and `~/Downloads`, so a clone there crashes the server with
  `PermissionError: Operation not permitted` on its own `.venv`. Put the clone somewhere
  like `~/gravitee/` — or grant Claude **Full Disk Access** (System Settings → Privacy &
  Security). Claude Code isn't affected.

## Typical flow

Public Gamma platform:
```
gamma_up                       → { status: "starting", pid, log_path }   (canonical 8082–8086)
gamma_wait                     → returns the moment it's healthy
gamma_license                  → tier / packs / features loaded on the stack
gamma_logs("gamma_console")    → tail a service
gamma_down                     → tears the stack down
```

Standalone APIM stack (guided launch):
```
stack_preflight(kind="apim", version="latest")   → resolves version, checks ports:
   status "clear"    → apim_up(version="latest")
   status "conflict" → ask the user, then either:
      ├─ apim_up(down_conflicting=true)           → down the other stack first
      └─ apim_up(instance="b")                    → run alongside on 28082-28085
apim_status  (repeat)                    → overall: healthy  (console http://localhost:8084)
apim_up(version="4.12.7", recreate=true) → pin a different release + recreate
apim_list                                → see every running instance
apim_down(instance="b")                  → tear down one instance (volumes preserved)
```

Kafka variant + features + multiple instances:
```
apim_up(variant="kafka", features=["prometheus","redis-rate-limit"])  → Kafka gateway + Prometheus + Redis (EE license), :9092
am_up(instance="a")   /   am_up(instance="b")  → two AM stacks at once (8086 / 8087)
```

See + tweak a running stack's config:
```
apim_config                                → editable ~/.gravitee/stacker-config/gravitee-apim.override.yml
apim_config(action="apply")   → apim_wait  → your edits are live (recreate, volumes kept)
apim_config(full=True)                     → also dump the hidden gravitee.yml defaults for reference
```
