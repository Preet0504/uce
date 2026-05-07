# UCE Documentation Index

This file is the single navigation point for all UCE documentation.

## Start Here

1. [README.md](README.md)
2. [TUTORIAL.md](TUTORIAL.md)
3. [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md)
4. [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)

## Technical References

- [graph_schema.md](graph_schema.md): graph entities and relationship model.
- [TECHNICAL_REPORT.md](TECHNICAL_REPORT.md): implementation rationale and system details.
- [config.yaml](config.yaml): concrete config example.
- [pyproject.toml](pyproject.toml): packaging metadata and console entry points.

## Runtime Components

- `run.py`: primary CLI entry point (`uce`).
- `run_uce.py`: compatibility entry point wrapper.
- `server/mcp_server.py`: MCP tool definitions and RBAC enforcement.
- `runtime/updater.py`: graph refresh + LLM ingestion orchestration.

## Research Artifacts

- [research/report_draft.md](research/report_draft.md)
- `research/icml2026/`
- `research/final_report/`

## Packaging and Distribution

- PyPI package: <https://pypi.org/project/uce-engine/>
- Maintainer profile: <https://pypi.org/user/preetpatel/>

## Security and Operations

- Keep secrets only in local env files (`.env`, `.env.docker`, `.keycloak-secrets.env`).
- Never connect external clients directly to Neo4j-MCP (`8000`).
- Route all user-facing tool calls through UCE MCP (`9001`).
