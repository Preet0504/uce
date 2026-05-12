# Unified Context Engine (UCE)

UCE is a policy-aware context and governance engine for software-changing AI assistants.

It builds a deterministic graph over code, schema, requirements, policies, and RBAC rules, then exposes reasoning and guarded mutation tools through MCP.

## What Problem UCE Solves

Most assistants can write code, but they cannot reliably answer:

- Which requirement will this break?
- Which policy is affected?
- Who is authorized to edit this file/path?
- What is the blast radius across imports, schema, and backend paths?

UCE was built to close this trust gap with deterministic graph reasoning and RBAC enforcement.

## Background and Motivation

This project started from an on-premise assistant goal: keep private engineering context local while still getting useful AI support.

The project evolved into UCE because "better prompts" alone were not enough for governance-critical workflows. Teams need auditable, reproducible evidence for change impact and authorization decisions.

Canonical final report:

- `research/final_report/CS540_Final_Project_Report_UCE_Preet_Patel.docx`

Supplemental benchmark artifacts:

- `research/supplemental_benchmarks/`

## How UCE Works

1. Ingest deterministic context into Neo4j:
- code structure (files/functions/classes/imports)
- schema (tables/columns)
- requirements and policies
- RBAC authority rules

2. Expose graph-backed MCP tools:
- impact/risk/explain tools
- introspection tools
- authorization + gated write/delete tools

3. Enforce RBAC at mutation time:
- viewer/editor/admin token claims
- deny-by-default mode support
- path-specific allow/deny logic

## Architecture

High-level architecture:

![UCE Architecture](research/final_report/assets/00_Simple_Architecture_Overview_UCE.png)

Deterministic vs optional LLM ingestion lanes:

![Ingestion Architecture](research/final_report/assets/09_Ingestion_Architecture_Deterministic_vs_LLM.png)

## Core Capabilities

- Deterministic graph ingestion for code/schema/governance artifacts.
- Optional LLM-assisted extraction for underspecified docs.
- Graph reasoning tools for impact, explainability, and preflight risk.
- JWT-backed RBAC gate with deny-by-default support.
- Safe mutation tools (`write_file`, `delete_file`) behind authorization checks.
- Full local stack via Docker Compose (Neo4j + Keycloak + Neo4j-MCP + UCE MCP).

## Results Snapshot (From Stored Artifacts)

Using the corrected real no-tool baseline (`llama3:instruct`) versus MCP-UCE graph run:

- Requirement caught-any rate: `0.550` (no-tool) vs `0.773` (MCP-UCE)
- Policy caught-any rate: `0.368` (no-tool) vs `0.714` (MCP-UCE)
- RBAC breach rate on oracle-denied probes: `0.647` (no-tool) vs `0.000` (MCP-UCE)

Result visuals:

![Requirement and Policy Capture](research/supplemental_benchmarks/results/figures/real_llm_requirement_policy_violation.png)

![RBAC Breach Rate](research/supplemental_benchmarks/results/figures/real_llm_rbac_breach_rate.png)

Detailed baseline explanation:

- `research/supplemental_benchmarks/results/real_llm_baseline/README.md`

## Quick Start (Spoon-Fed Path)

### Step 1: Prepare env file

```bash
copy .env.docker.example .env.docker
# Linux/macOS: cp .env.docker.example .env.docker
```

### Step 2: Bring up the full stack

```bash
docker compose --env-file .env.docker up -d --build
```

Expected services:

- Neo4j: `localhost:7687`, Browser `localhost:7474`
- Keycloak: `localhost:8080`
- Neo4j-MCP (backend-only): `localhost:8000/mcp/`
- UCE MCP (client target): `localhost:9001/mcp/`

### Step 3: Bootstrap Keycloak roles/clients/secrets

```bash
python scripts/bootstrap_keycloak.py \
  --base-url http://localhost:8080 \
  --public-base-url http://localhost:8080 \
  --realm uce-realm \
  --audience uce-mcp \
  --access-token-lifespan-seconds 3600 \
  --output-env-file .keycloak-secrets.env
```

### Step 4: Mint role tokens (PowerShell)

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

### Step 5: Connect your MCP client to UCE

Use endpoint:

- `http://127.0.0.1:9001/mcp/`

Use header:

- `Authorization: Bearer <token>`

Create role-specific sessions for viewer/editor/admin tokens.

### Step 6: Validate RBAC behavior

1. Viewer tries `write_file`: should be denied.
2. Editor writes allowed app path: should succeed.
3. Editor writes protected policy/RBAC path: should be denied.
4. Admin writes/deletes allowed admin scope: should succeed.

## Local Install (Without Docker)

### Prerequisites

- Python `>=3.10,<3.13`
- Neo4j reachable from host
- Keycloak reachable if RBAC enabled

### Install

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
- `--skip-ingestion`
- `--no-watcher`
- `--neo4j-uri`, `--neo4j-user`, `--neo4j-password`

## Configuration Model

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

RBAC env defaults for strict mode:

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

## Testing

```bash
python -m unittest discover -s tests -p "test_*.py"
```

## Regenerate Benchmarks and Final Report

Follow:

- `research/supplemental_benchmarks/README.md`

This includes deterministic benchmark reruns, real baseline reruns, and final report DOCX regeneration.

## PyPI

- Package: `uce-engine`
- Project: <https://pypi.org/project/uce-engine/>
- Maintainer: <https://pypi.org/user/preetpatel/>

## Documentation Map

- `DOCUMENTATION.md`
- `TUTORIAL.md`
- `OPERATOR_RUNBOOK.md`
- `RELEASE_CHECKLIST.md`
- `TECHNICAL_REPORT.md`
- `graph_schema.md`
- `research/report_draft.md`
- `research/final_report/CS540_Final_Project_Report_UCE_Preet_Patel.docx`
- `research/supplemental_benchmarks/README.md`

## Security Notes

- Keep `.env`, `.env.docker`, and `.keycloak-secrets.env` out of git.
- Keep Neo4j-MCP backend-only; expose UCE MCP to clients.
- Use deny-by-default RBAC in production-like environments.

## License

MIT
