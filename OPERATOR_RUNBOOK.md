# Operator Runbook

## Start Stack

```bash
# 1) Prepare env file
copy .env.docker.example .env.docker
# Linux/macOS: cp .env.docker.example .env.docker

# 2) Start services
docker compose --env-file .env.docker up -d --build

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
docker compose ps
docker compose logs -f uce
docker compose logs -f neo4j-mcp
docker compose logs -f keycloak
```

Expected listeners:
- Neo4j Bolt: `localhost:7687`
- Neo4j-MCP (backend-only): `localhost:8000/mcp/`
- Keycloak: `localhost:8080`
- UCE MCP (Goose target): `localhost:9001/mcp/`

## Stop and Restart

```bash
docker compose --env-file .env.docker stop
docker compose --env-file .env.docker start
```

## Clean Reset (destructive)

```bash
docker compose --env-file .env.docker down -v
```

Then rerun start + bootstrap commands.

## Recover From Common Issues

1. `401/invalid token` from UCE:
- Re-run `python scripts/bootstrap_keycloak.py` to rotate secrets.
- Regenerate tokens using new client secrets.
- Confirm `RBAC_JWT_ISSUER`, `RBAC_JWT_AUDIENCE`, and `RBAC_JWKS_URI` match your stack.

2. UCE running but no policy/requirement links:
- Check LLM settings in `.env.docker`.
- Verify requirement/policy/rbac docs exist in mounted repo paths.

3. Goose can query graph but cannot mutate:
- Confirm role token (`viewer`/`editor`/`admin`) and `RBAC_ENFORCE_MODE`.
- Use `authorize_change` first to inspect rule decision.

4. Goose can bypass RBAC:
- Ensure Goose connects only to UCE (`9001`) and not Neo4j-MCP (`8000`).
