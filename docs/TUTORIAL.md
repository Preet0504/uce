# UCE Tutorial

This tutorial shows how to run UCE with reproducible Docker, generate Keycloak RBAC tokens, and verify behavior in Goose.

For Claude Desktop, Claude Code, or Cursor instead of Goose, see
[CONNECTING_AI_ASSISTANTS.md](CONNECTING_AI_ASSISTANTS.md) — the stack/Keycloak/token steps below
are identical, only the client config at the end differs.

## 1) Start the Stack

```bash
copy docker\configs\client.env.example docker\configs\client.env
# Linux/macOS: cp docker/configs/client.env.example docker/configs/client.env
```

Edit `docker/configs/client.env` and set `UCE_TARGET_REPO` to the project you want UCE to
analyze, plus your `ANTHROPIC_API_KEY` (or another LLM provider — see the README's "Data Privacy"
section for a fully local Ollama profile).

```bash
docker compose \
  -f docker/compose/client/docker-compose.client.yml \
  --env-file docker/configs/client.env \
  up -d --build
```

Services:
- Neo4j: `localhost:7687`
- Keycloak: `localhost:8080`
- Neo4j-MCP (backend-only): `localhost:8000/mcp/`
- UCE MCP (Goose target): `localhost:9001/mcp/`

## 2) Bootstrap Keycloak Clients and Secrets

```bash
python scripts/bootstrap_keycloak.py \
  --base-url http://localhost:8080 \
  --public-base-url http://localhost:8080 \
  --realm uce-realm \
  --audience uce-mcp \
  --access-token-lifespan-seconds 3600 \
  --output-env-file .keycloak-secrets.env
```

This configures:
- realm roles (`viewer`, `editor`, `admin`)
- clients (`uce-viewer`, `uce-editor`, `uce-admin`)
- role and audience protocol mappers
- regenerated client secrets

## 3) Mint Viewer/Editor/Admin Tokens

Use each client's generated secret from `.keycloak-secrets.env`.

```powershell
$realm = "uce-realm"
$base = "http://localhost:8080"

function Get-ClientToken($clientId, $clientSecret) {
  (Invoke-RestMethod -Method Post `
    -Uri "$base/realms/$realm/protocol/openid-connect/token" `
    -ContentType "application/x-www-form-urlencoded" `
    -Body "grant_type=client_credentials&client_id=$clientId&client_secret=$clientSecret").access_token
}

$viewerToken = Get-ClientToken "uce-viewer" "<VIEWER_SECRET>"
$editorToken = Get-ClientToken "uce-editor" "<EDITOR_SECRET>"
$adminToken  = Get-ClientToken "uce-admin" "<ADMIN_SECRET>"
```

## 4) Configure Goose Extensions

Create three extensions (or three profiles/sessions):

- `UCE Viewer` -> `Authorization: Bearer <viewerToken>`
- `UCE Editor` -> `Authorization: Bearer <editorToken>`
- `UCE Admin` -> `Authorization: Bearer <adminToken>`

All point to the same endpoint:

- `http://127.0.0.1:9001/mcp/`

Do not expose `http://127.0.0.1:8000/mcp/` to end users.

## 5) Validate RBAC Behavior

Run the same mutation request in each role profile:

Every mutation now goes through the gate: `propose_change` first (mints a `gate_token` only on
`decision: "allow"`), then `write_file`/`delete_file` with that token — see
[CONNECTING_AI_ASSISTANTS.md](CONNECTING_AI_ASSISTANTS.md#the-gate-what-your-assistant-actually-has-to-do)
for the exact call shape. Validate:

1. Viewer: `propose_change` for any write should come back `decision: "block"` with
   `rbac.allowed: false` — no token is issued, so `write_file` cannot proceed at all.
2. Editor: `propose_change` for a write in an allowed scope, with a `files_to_edit` list that
   actually covers the real blast radius, should come back `decision: "allow"` with a
   `gate_token`; `write_file` with that token should succeed.
3. Editor: `propose_change` for a write in a protected RBAC/policy path should come back
   `decision: "block"`, `rbac.allowed: false`.
4. Admin: `propose_change` for a write/delete in admin scope should come back `decision: "allow"`.

Use `authorize_change` to inspect an RBAC decision on its own, without running the full gate.

## 6) Compliance/Impact and Gate Queries in Goose

Ask Goose to call:
- `propose_change` — the gate: declare a plan, get allow/warn/block plus a `gate_token`.
- `explain_violation` — literal requirement/policy text and exact trace chains for a `block`.
- `ci_impact_report` — the same gate applied to a whole changeset (multiple files) at once.
- `risk_assessment` / `explain_change` / `impact_analysis` — free-text or structured impact
  lookups (the first two guess the target entity from text; not authoritative for a decision).

These read graph relationships and report signals including `violated_requirements` and policy links when available.

## 7) Shutdown and Cleanup

```bash
docker compose -f docker/compose/client/docker-compose.client.yml --env-file docker/configs/client.env down
# destructive reset:
# docker compose -f docker/compose/client/docker-compose.client.yml --env-file docker/configs/client.env down -v
```
