# Unified Context Engine (UCE)

UCE is a policy-aware change intelligence platform for software teams.

It builds a graph-native context model (code, schema, requirements, policy, RBAC authority), runs impact and risk reasoning over that graph, and exposes safe MCP tools for both read analysis and controlled file mutation.

## Why UCE Exists

Most code assistants can propose edits quickly but cannot reliably answer:

- Which requirement might this break?
- Which policy or control is impacted?
- Is this write operation authorized for this caller and path?
- What is the transitive blast radius across services and schemas?

UCE closes that gap with deterministic ingestion, governance-aware reasoning, and RBAC-enforced mutation tooling.

## Core Capabilities

- Deterministic graph ingestion for code, schema, identifiers, and structural relationships.
- LLM-assisted ingestion for requirements, policies, and RBAC authority rules.
- Graph reasoning tools for impact, explainability, and preflight risk scoring.
- JWT-backed RBAC gate with deny-by-default and path-specific enforcement.
- MCP mutation tools (`write_file`, `delete_file`) that enforce authorization before change.
- End-to-end local stack via Docker Compose (Neo4j + Keycloak + Neo4j-MCP + UCE MCP).

## High-Level Architecture

```text
Goose / MCP Client
        |
        |  Bearer JWT (viewer/editor/admin)
        v
 UCE MCP Server (port 9001)
        |
        |-- reasoning tools (impact/risk/explain)
        |-- authorization tool (authorize_change)
        |-- guarded mutation tools (write/delete)
        |
        v
     Neo4j Graph (port 7687)
        ^
        |
 Neo4j-MCP bridge (port 8000, backend-only)
        ^
        |
 LLM ingestion pipeline (requirements/policies/rbac)
```

Critical rule:

- Connect external agents to UCE MCP (`9001`) only.
- Do not expose Neo4j-MCP (`8000`) to end users.

## Repository Layout

```text
core/                 # config, graph DB adapter, RBAC engine, risk model
ingestion/            # parser + graph builders + LLM ingestion pipeline
reasoning/            # impact analysis and trace logic
runtime/              # watcher + graph updater orchestration
server/               # FastMCP server + tool definitions
neo4j_mcp/            # backend Neo4j MCP bridge
scripts/              # operational scripts (e.g., Keycloak bootstrap)
tests/                # RBAC and ingestion-focused tests
artifacts/            # requirements and policy source documents
docker/               # Docker runtime config
research/             # reports, benchmark scripts, experiment outputs
```

## Quickstart (Docker, Recommended)

### 1) Prepare environment file

```bash
copy .env.docker.example .env.docker
# Linux/macOS: cp .env.docker.example .env.docker
```

### 2) Start full stack

```bash
docker compose --env-file .env.docker up -d --build
```

Services:

- Neo4j: `localhost:7687` and Browser `localhost:7474`
- Keycloak: `localhost:8080`
- Neo4j-MCP (backend-only): `localhost:8000/mcp/`
- UCE MCP (external client target): `localhost:9001/mcp/`

### 3) Bootstrap Keycloak realm roles, clients, and secrets

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

- roles: `viewer`, `editor`, `admin`
- clients: `uce-viewer`, `uce-editor`, `uce-admin`
- audience mapper: `aud=uce-mcp`
- rotated client secrets for all three clients

### 4) Mint role tokens (PowerShell example)

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

### 5) Point Goose profiles to UCE MCP

Endpoint:

- `http://127.0.0.1:9001/mcp/`

Headers:

- viewer: `Authorization: Bearer <viewerToken>`
- editor: `Authorization: Bearer <editorToken>`
- admin: `Authorization: Bearer <adminToken>`

## Local Installation (Without Docker)

### Prerequisites

- Python `>=3.10,<3.13`
- Neo4j reachable from host
- Keycloak reachable if RBAC enabled

### Install from PyPI

```bash
pip install uce-engine
```

### Run

```bash
uce --config config.yaml
```

CLI options:

- `--skip-refresh`
- `--skip-llm-ingestion`
- `--skip-ingestion` (skip both refresh + llm ingestion)
- `--no-watcher`
- `--neo4j-uri`, `--neo4j-user`, `--neo4j-password`

## Configuration Model

Main config is `config.yaml`.

```yaml
project_root: .
languages: [python, typescript, javascript, go, java, c, cpp]
paths:
  code: [.]
  schema: [db, src/db]
  requirements: [artifacts/requirements]
  policies: [artifacts/policies]
  rbac: [artifacts/rbac]
  backend: [src, server, app]
  identifiers: []
ignore: [.git, node_modules, venv, .venv, dist, build, __pycache__]
aliases: {}
neo4j:
  uri: bolt://localhost:7687
  user: neo4j
  password: testpassword
```

RBAC environment variables:

```env
RBAC_ENABLED=true
RBAC_ENFORCE_MODE=enforced
RBAC_DENY_DEFAULT=true
RBAC_JWT_ISSUER=http://localhost:8080/realms/uce-realm
RBAC_JWT_AUDIENCE=uce-mcp
RBAC_JWKS_URI=http://localhost:8080/realms/uce-realm/protocol/openid-connect/certs
RBAC_CLOCK_SKEW_SECONDS=60
UCE_MCP_TRANSPORT=http
```

## MCP Tool Catalog

Reasoning:

- `impact_analysis`
- `explain_change`
- `risk_assessment`
- `preflight_check`
- `validate_change`
- `preflight_validation`
- `logic_trace`

Graph introspection:

- `count_functions_in_file`
- `find_identifier_usage`
- `impact_table` (compat)
- `impact_column` (compat)

Governance and mutation:

- `authorize_change`
- `write_file`
- `delete_file`

## RBAC Validation Scenarios

Expected behavior in `enforced` mode:

1. Viewer calling `write_file` gets denied.
2. Editor can write allowed app paths.
3. Editor is blocked from policy/RBAC-protected paths.
4. Admin can write/delete paths granted by admin rules.

Use `authorize_change` before mutations to get explicit per-path decision trace.

## Testing

Run unit tests:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

## PyPI and Release Status

- Package: `uce-engine`
- PyPI project page: <https://pypi.org/project/uce-engine/>
- Maintainer profile: <https://pypi.org/user/preetpatel/>

Current repository version in `pyproject.toml` is `0.2.1`.
Published PyPI versions may lag repository head, which is expected during active development.

## Documentation Map

- Full docs index: [DOCUMENTATION.md](DOCUMENTATION.md)
- Tutorial walkthrough: [TUTORIAL.md](TUTORIAL.md)
- Operator procedures: [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md)
- Release process: [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)
- Technical report: [TECHNICAL_REPORT.md](TECHNICAL_REPORT.md)
- Graph schema reference: [graph_schema.md](graph_schema.md)
- Research notes: [research/report_draft.md](research/report_draft.md)

## Security Notes

- Keep `.env`, `.env.docker`, and `.keycloak-secrets.env` out of git.
- Use short token lifetimes in non-local deployments.
- Keep Neo4j-MCP backend-only and route all agent traffic through UCE MCP.
- Keep `RBAC_DENY_DEFAULT=true` in production-like environments.

## License

MIT
