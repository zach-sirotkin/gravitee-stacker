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
| `GAMMA_MCP_STATE_DIR` | `<this project>/.run`                                 | Where the tracked up-process metadata + `up.log` are kept.  |

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
stack_up                     → { status: "starting", pid, log_path }
stack_status  (repeat)       → overall: starting → … → healthy
stack_setup                  → bootstraps the demo (after healthy)
stack_logs("apim-rest-api")  → tail a service
stack_install_daemon         → returns the sudo command to run in your own terminal
stack_down                   → tears the stack down
```
