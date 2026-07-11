# Connecting an AI Assistant to UCE

UCE ships an MCP server (`uce.server.mcp_server`, launched via the `uce` CLI). Any MCP-compatible
assistant — Claude Desktop, Claude Code, Cursor, Goose, or your own agent — can connect to it.
There are two connection modes, and which one you use is decided automatically by whether RBAC is
enabled in `config.yaml`.

| | Local dev (default) | Full stack |
|---|---|---|
| RBAC | disabled | enforced |
| Transport | `stdio` (the client launches UCE itself) | `http` (UCE runs standalone, client connects over the network) |
| Setup | `pip install uce-engine`, a `config.yaml`, a reachable Neo4j | Docker Compose stack (Neo4j + Keycloak + Neo4j-MCP + UCE MCP) |
| Auth | none | `Authorization: Bearer <role token>` |
| When to use | solo/local exploration | anyone besides you needs role-scoped access, or you want RBAC enforced |

The `propose_change` gate itself (see below) works identically in both modes — RBAC is an
independent layer on top, not a prerequisite for the gate.

## Mode 1: Local dev (stdio, no RBAC)

1. Install and point a `config.yaml` at the project you want UCE to analyze (`project_root: .`
   inside that project, or an absolute path). Make sure `rbac.enabled: false` (the default).
2. Confirm Neo4j is reachable (`neo4j.uri`/`user`/`password` in `config.yaml`, or `NEO4J_URI`/
   `NEO4J_USER`/`NEO4J_PASSWORD` env vars).
3. Point your client at the `uce` command with `--config` pointing at that file. The client
   launches and owns the process — you do not run `uce` yourself in a separate terminal first.

**Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "uce": {
      "command": "uce",
      "args": ["--config", "/absolute/path/to/config.yaml"]
    }
  }
}
```

**Claude Code** (project-level `.mcp.json`, or `claude mcp add uce -- uce --config /absolute/path/to/config.yaml`):
```json
{
  "mcpServers": {
    "uce": {
      "command": "uce",
      "args": ["--config", "/absolute/path/to/config.yaml"]
    }
  }
}
```

**Cursor** (`.cursor/mcp.json`):
```json
{
  "mcpServers": {
    "uce": {
      "command": "uce",
      "args": ["--config", "/absolute/path/to/config.yaml"]
    }
  }
}
```

If `uce` isn't on your client's `PATH` (common with a venv), use the venv's absolute interpreter
path instead: `"command": "/path/to/.venv/bin/python", "args": ["-m", "uce.cli", "--config", "..."]`
(Windows: `...\.venv\Scripts\python.exe`).

Add `"--skip-refresh"` (skip full ingestion on startup, use the existing graph) or
`"--no-watcher"` to `args` if you don't want a background filesystem watcher running for the
lifetime of the client session.

## Mode 2: Full stack (HTTP, RBAC enforced)

Follow [TUTORIAL.md](TUTORIAL.md) to bring up the stack, bootstrap Keycloak, and mint a role
token. Then point your client at the HTTP endpoint instead of a local command:

**Claude Desktop / Claude Code / Cursor** (all use the same `url`/`headers` shape):
```json
{
  "mcpServers": {
    "uce": {
      "url": "http://127.0.0.1:9001/mcp/",
      "headers": {
        "Authorization": "Bearer <editorToken>"
      }
    }
  }
}
```

Use a separate `mcpServers` entry (e.g. `uce-viewer`, `uce-editor`, `uce-admin`) per role token if
you want to switch roles without editing config each time — see TUTORIAL.md step 4.

**Goose**: see [TUTORIAL.md](TUTORIAL.md) step 4 — create one extension per role token, all
pointing at `http://127.0.0.1:9001/mcp/`.

Never point any client at `http://127.0.0.1:8000/mcp/` (Neo4j-MCP) — that's the backend-only
sidecar with no RBAC of its own; it must never be reachable by an assistant directly.

## The gate: what your assistant actually has to do

As of the `propose_change` gate, **`write_file` and `delete_file` will refuse to run without a
`gate_token`** — this holds in both connection modes, independent of RBAC. There is no config
flag that makes this optional in a default deployment; skipping the gate is not something a
prompt can talk the server into.

The required sequence for any mutating change:

1. **Call `propose_change`** with the concrete entity being changed and the files you intend to
   touch:
   ```
   propose_change(
     operation="write",
     entity_type="table",           # table | column | file
     entity_name="meetings",
     files_to_edit=["src/modules/meetings/schemas.ts", "src/modules/meetings/types.ts"],
     declared_requirements=["RQ-001"],   # optional — requirements you believe apply
   )
   ```
2. **Read the `decision` field.**
   - `"allow"` → the response includes a `gate_token`. Use it for every file in `files_to_edit`.
   - `"block"` → do not proceed. `blast_radius.missed_files` lists files you didn't declare that
     the graph says are actually affected; `governance.silent_requirements` lists requirements you
     didn't mention that the graph says are violated. `remediation` restates both as next steps.
     Revise the plan (usually: add the missed files to `files_to_edit` and call `propose_change`
     again) or explain to the user why the change should proceed anyway.
   - `"warn"` → same evidence as `block`, but the server is running in non-strict mode; you may
     proceed but should surface the evidence to the user first.
3. **Call `write_file`/`delete_file` with `gate_token`** set to the token from step 2, once per
   file. A token only covers the exact files it was issued for and each file can be consumed once.

If you want the exact literal evidence behind a `block` — the requirement/policy document text
and the precise trace chain, not a summary — call `explain_violation(entity_type, entity_name)`.
It returns the same evidence `propose_change` embeds, standalone, with zero LLM involvement (every
field is a graph query result or literal stored document text).

Calling `write_file` without ever having called `propose_change` fails immediately with an error
telling you to call it first — this is enforced by the server, not by instructing the assistant to
behave a certain way.

## Sanity-check the connection

Once configured, ask your assistant to call `graph_stats` (read-only, no auth beyond the
connection itself) — a working connection returns node/edge counts. If that fails, check:

- **stdio mode**: is `uce` actually on the launching process's `PATH`? Is Neo4j reachable from
  wherever the client spawns the process (not just from your own shell)?
- **HTTP mode**: is the token still valid (tokens expire — see
  `--access-token-lifespan-seconds` in the Keycloak bootstrap step)? Is `RBAC_JWT_ISSUER` reachable
  from the UCE MCP container specifically (not just from your host machine)?

See [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md) "Recover From Common Issues" for more.
