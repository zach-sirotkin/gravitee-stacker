# gamma-stack-mcp

An [MCP](https://modelcontextprotocol.io) server that manages the **Gravitee Gamma
demo stack**. It is a thin wrapper over the stack's `docker/run.sh` — it invokes
`run.sh` / `docker compose` and surfaces status; it does **not** reimplement any
orchestration.

It lives as a **sibling** of the stack repo and has its own (optional) git history —
nothing here touches the `gravitee-gamma-modules-sdk` repo's working tree.

```
Documents/
  gravitee-gamma-modules-sdk/   ← the stack (run.sh, compose files) — left untouched
  gamma-stack-mcp/              ← this MCP server
```

## What it exposes

| Tool                     | What it does                                                                                 |
| ------------------------ | -------------------------------------------------------------------------------------------- |
| `stack_up`               | Launches `run.sh` **in the background** (pull + `up -d` + health poll), returns immediately. |
| `stack_status`           | Two independent signals: is the tracked up-process alive, **and** `docker compose ps` health. Returns `starting`/`healthy`/`partial`/`down`/`failed`, per-service state, and the tail of `up.log`. |
| `stack_setup`            | Runs `run.sh setup` in the foreground (configurable timeout, default 5 min).                  |
| `stack_down`             | Runs `run.sh down` (`docker compose down`).                                                   |
| `stack_logs`             | `docker compose logs --tail=<lines> <service>` for one validated service.                     |
| `stack_install_daemon`   | **Returns the command to run yourself** — does not execute (it self-elevates via sudo).      |
| `stack_uninstall_daemon` | Same treatment as install.                                                                    |

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
| `GAMMA_STACK_DIR`| `/Users/zachary.sirotkin/Documents/gravitee-gamma-modules-sdk` | Stack repo root. All calls run in `$GAMMA_STACK_DIR/docker`. |
| `ESM_MESH`       | unset                                                      | If set, adds the ESM Kafka-mesh overlay (matches `run.sh`). |
| `REGISTRY`       | `graviteeio.azurecr.io`                                    | Passed through to `run.sh`. Set `graviteeio` for the public hub. |
| `GAMMA_PORT_OFFSET` | `20000`                                                | Host-port shift used in **coexist** mode (see **Ports** below). |
| `GAMMA_PORT_KEEP`   | unset                                                  | Comma-separated services to leave on their original host ports in coexist mode (e.g. `nginx`). |
| `GAMMA_MCP_STATE_DIR` | `<this project>/.run`                                 | Where the tracked up-process metadata + `up.log` + generated overlay + last-mode are kept. |

### Ports — two modes

The Gamma stack's published host ports are **hardcoded** in the stack repo's compose
files (`'8082:8082'`, `'8083:8083'`, `'80:80'`, …) and `run.sh` takes no overlay, so
they can't be remapped as a pure wrapper. `stack_up` therefore offers two modes:

**Default — canonical ports (`stack_up`)**
Brings the stack up on its real ports via `run.sh` — the **fully-wired demo**
(`stack_setup`, OAuth, edge config all correct). Before launching it pre-checks the
canonical ports; if one is already taken (e.g. `apim-latest` on 8082/8083) it does
**not** start, and returns `status: "port_conflict"` **suggesting coexist mode**:

```json
{ "status": "port_conflict", "busy_ports": [8082, 8083],
  "suggest": { "tool": "stack_up", "args": { "coexist": true } } }
```

**Coexist — remapped ports (`stack_up` with `coexist=true`)**
Shifts every published host port by `GAMMA_PORT_OFFSET` (default 20000) via a
generated compose overlay (`.run/ports.override.yml`, built from `docker compose
config` so it tracks whatever the stack actually publishes) applied with the
`!override` tag, and drives `docker compose up` directly (still non-blocking). Lets
Gamma run **alongside** your other stacks.

| Service        | Original | Remapped | | Service       | Original | Remapped |
| -------------- | -------- | -------- |-| ------------- | -------- | -------- |
| nginx (UIs)    | 80       | 20080    | | gateway (AM)  | 8092     | 28092    |
| apim-gateway   | 8082     | 28082    | | management(AM)| 8093     | 28093    |
| apim-rest-api  | 8083     | 28083    | | apim-es       | 9200     | 29200    |
| apim-mongo     | 27017    | 47017    | | AM mongo      | 27018    | 47018    |

…and the 18xxx debug/reactor/SPIRE ports shift into the 38xxx band. None collide
with `apim-latest` (8082–8085) or `gravitee-am-local` (8086). UIs move with nginx:
`http://gamma.localhost:20080`, `apim.localhost:20080`, etc.

**Coexist caveats (why canonical is the default):** `setup.sh` bakes canonical ports
into a few places. `stack_setup` handles what it can and is transparent about the rest:

- It runs `setup.sh` **directly** with `AM_URL`/`APIM_URL`/`EDGE_REACTOR_PORT` pointed
  at the remapped ports (it can't use `run.sh setup`, whose health-gate targets
  hardcoded canonical ports and would hang). Run it only after `stack_status` is healthy.
- Still canonical (hardcoded literals it can't fix without editing the stack repo):
  the saved edge `gatewayUrl: http://localhost:8082` and the `:80` OAuth redirect URIs.
  So edge/OAuth wiring is **best-effort** in coexist mode.
- `GAMMA_PORT_KEEP=nginx` keeps the UIs on `:80` (clean URLs; accepts `:80` collision risk).
- **Edge Daemon:** installed with `EDGE_REACTOR_PORT` pointed at the remapped reactor
  (18072 → 38072).

For a fully-wired demo, use canonical mode (stop the conflicting stack, or free
8082/8083). Use coexist to run Gamma next to other stacks.

Runtime state (the tracked up-process metadata + `up.log`) lives in **this
project's** `.run/` directory (gitignored here) — deliberately kept out of the
stack repo so it never shows up as untracked noise there. `run.sh` itself still
runs with `cwd = $GAMMA_STACK_DIR/docker`.

### Prerequisites (surfaced by `stack_up`'s pre-flight)

- **Docker Desktop running** (`docker info` must succeed).
- **ACR login** (`az acr login --name graviteeio`) — or set `REGISTRY=graviteeio` in
  `docker/.env` to use the public hub.
- A **license** at `docker/license/license.key`.

`stack_up` checks these first and returns a clear message instead of a cryptic
mid-startup failure.

## Install / run

```bash
cd /Users/zachary.sirotkin/Documents/gamma-stack-mcp
python3 -m venv .venv
./.venv/bin/python -m pip install -e .
```

Run the server directly (stdio transport):

```bash
GAMMA_STACK_DIR=/Users/zachary.sirotkin/Documents/gravitee-gamma-modules-sdk \
  ./.venv/bin/python -m gamma_stack_mcp.server
# or, via the installed console script:
./.venv/bin/gamma-stack-mcp
```

## Wire it into a client

Both use stdio transport and point at the venv's Python so no activation is needed.

### Claude Code

Add to `~/.claude.json` (or a project `.mcp.json`) under `mcpServers`:

```json
{
  "mcpServers": {
    "gamma-stack": {
      "command": "/Users/zachary.sirotkin/Documents/gamma-stack-mcp/.venv/bin/python",
      "args": ["-m", "gamma_stack_mcp.server"],
      "env": {
        "GAMMA_STACK_DIR": "/Users/zachary.sirotkin/Documents/gravitee-gamma-modules-sdk"
      }
    }
  }
}
```

Or with the CLI:

```bash
claude mcp add gamma-stack \
  --env GAMMA_STACK_DIR=/Users/zachary.sirotkin/Documents/gravitee-gamma-modules-sdk \
  -- /Users/zachary.sirotkin/Documents/gamma-stack-mcp/.venv/bin/python -m gamma_stack_mcp.server
```

### Cursor

Add to `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project):

```json
{
  "mcpServers": {
    "gamma-stack": {
      "command": "/Users/zachary.sirotkin/Documents/gamma-stack-mcp/.venv/bin/python",
      "args": ["-m", "gamma_stack_mcp.server"],
      "env": {
        "GAMMA_STACK_DIR": "/Users/zachary.sirotkin/Documents/gravitee-gamma-modules-sdk"
      }
    }
  }
}
```

## Typical flow

```
stack_up                       → { status: "starting", pid, log_path }        (canonical ports)
   └─ if status: "port_conflict" → stack_up(coexist=true)                      (remapped ports)
stack_status   (repeat)        → overall: starting → … → healthy
stack_setup                    → bootstraps the demo (after healthy; auto-matches the up mode)
stack_logs("apim-rest-api")    → tail a service
stack_install_daemon           → returns the sudo command to run in your own terminal
stack_down                     → tears the stack down
```
