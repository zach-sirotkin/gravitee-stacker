# gamma-stack-mcp

An [MCP](https://modelcontextprotocol.io) server that manages two Gravitee Docker
stacks from your AI assistant (Claude Code, Cursor, Claude Desktop):

- **Standalone APIM** (`apim_*`) — a self-contained, ready-to-run API Management
  stack. **Needs only Docker.** Pulls any release you pick and stands it up.
- **Gamma demo** (`stack_*`) — a thin wrapper over the Gamma SDK's `docker/run.sh`.
  Needs the SDK repo + registry access (below).

## Quickstart

**Requirements:** Docker Desktop (running), Python 3.10+, macOS or Linux.

**1. Install**
```bash
git clone <this-repo-url> gamma-stack-mcp
cd gamma-stack-mcp
python3 -m venv .venv && ./.venv/bin/python -m pip install -e .
```

**2. Register it** with your client — Claude Code:
```bash
claude mcp add gamma-stack -- /ABSOLUTE/PATH/TO/gamma-stack-mcp/.venv/bin/python -m gamma_stack_mcp.server
```
…or add the JSON block under [Wire it into a client](#wire-it-into-a-client) (Cursor / Claude Desktop use the same block). Use an **absolute** path — clients don't expand `~`.

**3. Check readiness** — ask your assistant to run the `doctor` tool. It reports
what's ready and what's missing for each stack.

**4. Run APIM** — that's all it takes for the APIM stack:
> "Stand up the latest APIM." → `apim_up()` → poll `apim_status` → console at http://localhost:8084 (admin/admin).

### Where to put things

| Thing | Where | Needed for |
| ----- | ----- | ---------- |
| **Gravitee license** (optional) | `~/.gravitee/license.key` — `mkdir -p ~/.gravitee && cp <your-license>.key ~/.gravitee/license.key` | Enterprise features on APIM. Without it APIM runs in **OSS mode**. Auto-detected; no config needed. |
| **Gamma SDK repo** | Clone `gravitee-gamma-modules-sdk`, then set `GAMMA_STACK_DIR` to its path (default `~/gravitee-gamma-modules-sdk`) | The **Gamma** stack only (`stack_*`). APIM doesn't need it. |
| **Registry login** | `az acr login --name graviteeio` (or `REGISTRY=graviteeio` for the public hub) | The **Gamma** stack only — its images are on Gravitee's private registry. |

> APIM images are public (`graviteeio/apim-*`) — no login needed. A license is optional.
> So if you only want APIM, step 4 is the whole setup.

Nothing secret is committed: `.venv/` and `.run/` (per-stack state + logs) are gitignored,
and this project never touches the Gamma SDK repo's working tree.

## What it exposes

It manages **two independent stacks**: the Gravitee **Gamma** demo stack (`stack_*`,
a wrapper over the stack repo's `run.sh`) and a self-contained standalone **APIM**
stack (`apim_*`, shipped with the tool). Plus **`doctor`** — a one-call readiness
check (Docker, license, Gamma SDK) that tells you what's set up and what's missing.
Run it first.

### Gamma stack (`stack_*`)

| Tool                     | What it does                                                                                 |
| ------------------------ | -------------------------------------------------------------------------------------------- |
| `stack_up`               | Launches `run.sh` **in the background** (pull + `up -d` + health poll), returns immediately. |
| `stack_status`           | Two independent signals: is the tracked up-process alive, **and** `docker compose ps` health. Returns `starting`/`healthy`/`partial`/`down`/`failed`, per-service state, and the tail of `up.log`. |
| `stack_setup`            | Runs `run.sh setup` in the foreground (configurable timeout, default 5 min).                  |
| `stack_down`             | Runs `run.sh down` (`docker compose down`).                                                   |
| `stack_logs`             | `docker compose logs --tail=<lines> <service>` for one validated service.                     |
| `stack_ports`            | Shows the Gamma stack's canonical host ports + access URLs.                                   |
| `stack_install_daemon`   | **Returns the command to run yourself** — does not execute (it self-elevates via sudo).      |
| `stack_uninstall_daemon` | Same treatment as install.                                                                    |

### Standalone APIM stack (`apim_*`)

A self-contained OSS APIM stack (`apim-compose.yml`, project `gravitee-apim`:
mongodb + elasticsearch + gateway + management-api + console + portal) on ports
**8082/8083/8084/8085**. Independent of the Gamma stack.

| Tool                 | What it does                                                                                       |
| -------------------- | ------------------------------------------------------------------------------------------------- |
| `apim_up`            | Resolves + pins a release, checks ports, and starts APIM in the background. On a port conflict it does not start — returns `port_conflict`. Options: `version` (`"latest"` or e.g. `"4.12.7"`), `coexist=true` (remapped ports, run alongside another stack), `down_conflicting=true` (down the other stack first), `recreate=true` (reload), `license="/path/to/license.key"`. |
| `apim_status`        | Overall verdict + per-service health for the `gravitee-apim` project, plus the pinned version, mode, and URLs. |
| `apim_down`          | `docker compose down` (volumes preserved).                                                          |
| `apim_logs`          | Tail one APIM service.                                                                              |
| `apim_latest_version`| Resolves the newest stable APIM release tag from the repo (via `git ls-remote`).                   |

**Versions.** `version="latest"` resolves the newest stable tag from
`gravitee-io/gravitee-api-management` (e.g. `4.12.8`) → sets `APIM_VERSION` → pulls
`graviteeio/apim-*:<version>`. Pin a tag with `version="4.12.7"`; reload/recreate with
`recreate=true`. UIs are direct (no host-routing): console `http://localhost:8084`
(admin/admin), portal `:8085`, management API `:8083/management`, gateway `:8082`
(add `APIM_PORT_OFFSET`, default 20000, in coexist mode).

**License.** To run with enterprise features, drop a Gravitee license at the
conventional path **`~/.gravitee/license.key`** and `apim_up` mounts it into the
gateway + management-api automatically. Resolution order: `license="/path/..."` arg →
`APIM_LICENSE` env → `~/.gravitee/license.key` → OSS mode. The mount is applied via an
overlay (`apim-license.yml`) only when a real, non-empty license file is found (a
phantom bind-mount directory is skipped). `apim_up`'s result reports which source was used.

**Conflict flow.** `apim_up` checks its target ports first (canonical, or remapped in
coexist mode). On a conflict it identifies the exact compose project/containers and
returns `port_conflict` + a `suggest` payload — it **never auto-downs**. In canonical
mode `suggest` offers both `run_alongside` (`coexist=true`) and `down_the_other`
(`down_conflicting=true`); the latter brings the conflicting project(s) down (no `-v`,
data preserved) and resets Gamma's tracking if it was the tool-managed stack.

### The background-process design (the important part)

`stack_up` uses `subprocess.Popen` (never `subprocess.run`), redirects stdout+stderr
to `.run/up.log`, records the PID + log path, and returns `{status: "starting", pid,
log_path}` **without waiting**. That's what keeps cold image pulls from timing out
your MCP client. You then poll `stack_status` to learn when the stack is actually
ready. Only one up-process is allowed at a time.

`stack_status` reports liveness two ways and shows both: (1) `popen.poll()` /
PID-liveness of the tracked up-process, and (2) real health parsed from
`docker compose ps`. Init containers that exit 0 (`spire-perms-init`,
`spire-bootstrap`) are treated as *completed*, not failed.

> **Compose files:** this server mirrors `run.sh` exactly — the effective set is
> `-f docker-compose.yml` (which pulls in `docker-compose.apim.yml` via an
> `include:` directive) plus `-f docker-compose.esm.yml` **only when `ESM_MESH` is
> set**. The service list is read live via `docker compose config --services`, so it
> never drifts from the actual compose files.

## Configuration

| Env var          | Default                                                     | Meaning                                    |
| ---------------- | ---------------------------------------------------------- | ------------------------------------------ |
| `GAMMA_STACK_DIR`| `~/gravitee-gamma-modules-sdk` (override this) | Path to your checkout of the stack repo. All Gamma calls run in `$GAMMA_STACK_DIR/docker`. |
| `ESM_MESH`       | unset                                                      | If set, adds the ESM Kafka-mesh overlay (matches `run.sh`). |
| `REGISTRY`       | `graviteeio.azurecr.io`                                    | Passed through to `run.sh`. Set `graviteeio` for the public hub. |
| `APIM_PORT_OFFSET` | `20000`                                                 | Host-port shift for `apim_up(coexist=true)`. |
| `APIM_LICENSE`   | unset                                                      | Default license path for the APIM stack (overridden by `apim_up`'s `license` arg). |
| `APIM_COMPOSE_FILE` | shipped `apim-compose.yml`                            | Point at your own APIM compose to use instead of the bundled one (see below). |
| `GAMMA_MCP_STATE_DIR` | `<this project>/.run`                                 | Where the tracked up-process metadata + `up.log` live (per stack). |

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

### Ports — who owns which mode

- **Gamma runs on canonical ports only.** Its consoles hardcode host-routing and
  `:80` (the bootstrap `baseURL` has no port), so remapping breaks the UIs — Gamma is
  strict by design. `stack_up` pre-checks its ports and, if one is taken, returns
  `port_conflict` (`busy_ports`) without starting. Free them (or down the other stack)
  and retry. UIs: `http://gamma.localhost`, `apim.localhost`, `portal.localhost`,
  `am.localhost` (all via nginx on `:80`).
- **APIM supports coexist** (`apim_up(coexist=true)`), because it uses direct
  `localhost` ports and the compose parameterizes both the ports *and* the
  console/portal API URLs — so a remap stays self-consistent. Ports shift by
  `APIM_PORT_OFFSET` (default 20000): gateway `28082`, mgmt-api `28083`, console
  `28084`, portal `28085`. This lets APIM run **alongside** Gamma (or any stack on
  8082/8083) with fully-working consoles.

Runtime state (tracked up-process metadata + `up.log`, per stack) lives in **this
project's** `.run/` directory (gitignored here) — kept out of the stack repo so it
never shows up as untracked noise. `run.sh` itself still runs with
`cwd = $GAMMA_STACK_DIR/docker`.

### Prerequisites (surfaced by `stack_up`'s pre-flight)

- **Docker Desktop running** (`docker info` must succeed).
- **ACR login** (`az acr login --name graviteeio`) — or set `REGISTRY=graviteeio` in
  `docker/.env` to use the public hub.
- A **license** at `docker/license/license.key`.

`stack_up` checks these first and returns a clear message instead of a cryptic
mid-startup failure.

## Install / run

```bash
git clone <this-repo-url> gamma-stack-mcp
cd gamma-stack-mcp
python3 -m venv .venv          # Python 3.10+
./.venv/bin/python -m pip install -e .
```

Run the server directly (stdio transport):

```bash
GAMMA_STACK_DIR=/path/to/gravitee-gamma-modules-sdk \
  ./.venv/bin/python -m gamma_stack_mcp.server
# or, via the installed console script:
GAMMA_STACK_DIR=/path/to/gravitee-gamma-modules-sdk ./.venv/bin/gamma-stack-mcp
```

## Wire it into a client

Both use stdio transport and point at the venv's Python so no activation is needed.

### Claude Code

Add to `~/.claude.json` (or a project `.mcp.json`) under `mcpServers`:

```json
{
  "mcpServers": {
    "gamma-stack": {
      "command": "/ABSOLUTE/PATH/TO/gamma-stack-mcp/.venv/bin/python",
      "args": ["-m", "gamma_stack_mcp.server"],
      "env": {
        "GAMMA_STACK_DIR": "/ABSOLUTE/PATH/TO/gravitee-gamma-modules-sdk"
      }
    }
  }
}
```

Or with the CLI:

```bash
claude mcp add gamma-stack \
  --env GAMMA_STACK_DIR=/ABSOLUTE/PATH/TO/gravitee-gamma-modules-sdk \
  -- /ABSOLUTE/PATH/TO/gamma-stack-mcp/.venv/bin/python -m gamma_stack_mcp.server
```

> **Paths in the JSON must be absolute** — MCP clients don't expand `~` or `$HOME`.
> Point `command` at this project's `.venv/bin/python` and `GAMMA_STACK_DIR` at your
> local checkout of the stack repo. (If you `pipx install` this project, `command`
> can just be `gamma-stack-mcp`.)

### Cursor

Add to `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project):

```json
{
  "mcpServers": {
    "gamma-stack": {
      "command": "/ABSOLUTE/PATH/TO/gamma-stack-mcp/.venv/bin/python",
      "args": ["-m", "gamma_stack_mcp.server"],
      "env": {
        "GAMMA_STACK_DIR": "/ABSOLUTE/PATH/TO/gravitee-gamma-modules-sdk"
      }
    }
  }
}
```

## Typical flow

Gamma demo stack:
```
stack_up                       → { status: "starting", pid, log_path }   (canonical ports)
stack_status   (repeat)        → overall: starting → … → healthy
stack_setup                    → bootstraps the demo (after healthy)
stack_logs("apim-rest-api")    → tail a service
stack_down                     → tears the stack down
```

Standalone APIM stack (can run alongside Gamma):
```
apim_up(version="latest")               → starting; on port_conflict, either:
   ├─ apim_up(coexist=true)             → run alongside on 28082-28085
   └─ apim_up(down_conflicting=true)    → down the other stack first
apim_status  (repeat)                   → overall: healthy  (console http://localhost:8084)
apim_up(version="4.12.7", recreate=true)→ pin a different release + recreate
apim_down                               → tears the APIM stack down
```
