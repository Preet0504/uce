# Operator Runbook

## Start Stack

```bash
# 1) Prepare env file
copy docker\configs\client.env.example docker\configs\client.env
# Linux/macOS: cp docker/configs/client.env.example docker/configs/client.env
# Then edit docker/configs/client.env: set UCE_TARGET_REPO and an LLM provider key.

# 2) Start services
docker compose -f docker/compose/client/docker-compose.client.yml --env-file docker/configs/client.env up -d --build

# 3) Bootstrap Keycloak RBAC clients and secrets
python scripts/bootstrap_keycloak.py \
  --base-url http://localhost:8080 \
  --public-base-url http://localhost:8080 \
  --realm uce-realm \
  --audience uce-mcp \
  --access-token-lifespan-seconds 3600 \
  --output-env-file .keycloak-secrets.env
```

## Health Checks

```bash
docker compose -f docker/compose/client/docker-compose.client.yml ps
docker compose -f docker/compose/client/docker-compose.client.yml logs -f uce
docker compose -f docker/compose/client/docker-compose.client.yml logs -f neo4j-mcp
docker compose -f docker/compose/client/docker-compose.client.yml logs -f keycloak
```

Expected listeners:
- Neo4j Bolt: `localhost:7687`
- Neo4j-MCP (backend-only): `localhost:8000/mcp/`
- Keycloak: `localhost:8080`
- UCE MCP (client target — Goose, Claude Desktop, Claude Code, Cursor): `localhost:9001/mcp/`

## Stop and Restart

```bash
docker compose -f docker/compose/client/docker-compose.client.yml --env-file docker/configs/client.env stop
docker compose -f docker/compose/client/docker-compose.client.yml --env-file docker/configs/client.env start
```

## Clean Reset (destructive)

```bash
docker compose -f docker/compose/client/docker-compose.client.yml --env-file docker/configs/client.env down -v
```

Then rerun start + bootstrap commands.

## Recover From Common Issues

1. `401/invalid token` from UCE:
- Re-run `python scripts/bootstrap_keycloak.py` to rotate secrets.
- Regenerate tokens using new client secrets.
- Confirm `RBAC_JWT_ISSUER`, `RBAC_JWT_AUDIENCE`, and `RBAC_JWKS_URI` match your stack.

2. UCE running but no policy/requirement links:
- Check LLM settings in `docker/configs/client.env`.
- Verify requirement/policy/rbac docs exist in mounted repo paths.

3. Client can query the graph but every `write_file`/`delete_file` call fails with "Missing
   gate_token" or "Gate token rejected":
- This is expected if the client never called `propose_change` first, or called `write_file` for
  a path not in that call's `files_to_edit`, or reused an already-consumed token — the gate is
  mandatory by design (`UCE_GATE_ENFORCEMENT=enforced`), not a bug. See
  [CONNECTING_AI_ASSISTANTS.md](CONNECTING_AI_ASSISTANTS.md#the-gate-what-your-assistant-actually-has-to-do)
  for the required call sequence.
- If you deliberately want the old advisory-only behavior for local debugging, set
  `UCE_GATE_ENFORCEMENT=advisory` — never in a shared deployment.

4. Client can query graph but cannot mutate at all (even with a token):
- Confirm role token (`viewer`/`editor`/`admin`) and `RBAC_ENFORCE_MODE`.
- Use `authorize_change` first to inspect the RBAC rule decision, then `propose_change` to see the
  full gate evaluation (RBAC + blast radius + governance) together.

5. Client can bypass RBAC:
- Ensure the client connects only to UCE (`9001`) and not Neo4j-MCP (`8000`).
