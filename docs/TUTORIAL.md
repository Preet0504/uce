# UCE Tutorial

This tutorial shows how to run UCE with reproducible Docker, generate Keycloak RBAC tokens, and verify behavior in Goose.

## 1) Start the Stack

```bash
copy .env.docker.example .env.docker
# Linux/macOS: cp .env.docker.example .env.docker

docker compose --env-file .env.docker up -d --build
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

1. Viewer: `write_file` should be denied.
2. Editor: write in allowed scope should succeed.
3. Editor: write in protected RBAC/policy path should be denied.
4. Admin: write/delete in admin scope should succeed.

Use `authorize_change` to inspect why a path was allowed/denied.

## 6) Compliance/Impact Queries in Goose

Ask Goose to call:
- `risk_assessment`
- `explain_change`
- `impact_analysis`

These read graph relationships and report signals including `violated_requirements` and policy links when available.

## 7) Shutdown and Cleanup

```bash
docker compose --env-file .env.docker down
# destructive reset:
# docker compose --env-file .env.docker down -v
```
