# UCE Documentation Index

This file is the single navigation point for all UCE documentation. Paths below are relative to
the repository root (this file lives in `docs/`).

## Start Here

1. [README.md](../README.md) — quick start, MCP tool catalog, data privacy, results.
2. [CONNECTING_AI_ASSISTANTS.md](CONNECTING_AI_ASSISTANTS.md) — wire Claude Desktop, Claude Code,
   Cursor, or Goose up to the UCE MCP server, and the `propose_change` → `gate_token` →
   `write_file` sequence an agent must follow.
3. [TUTORIAL.md](TUTORIAL.md) — full walkthrough: stack, Keycloak, role tokens, RBAC validation.
4. [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md) — start/stop/health-check/recovery for a running stack.
5. [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)
6. [research/final_report/CS540_Final_Project_Report_UCE_Preet_Patel.docx](../research/final_report/CS540_Final_Project_Report_UCE_Preet_Patel.docx)

## Technical References

- [graph_schema.md](graph_schema.md): graph entities and relationship model.
- [TECHNICAL_REPORT.md](TECHNICAL_REPORT.md): implementation rationale, risk model, and the
  `propose_change` enforcement gate.
- [../config.yaml](../config.yaml): concrete config example, including the `gate:` section.
- [../pyproject.toml](../pyproject.toml): packaging metadata and console entry points.

## Runtime Components

- `run_uce.py` / `run.py`: primary CLI entry points (`uce`).
- `uce/server/mcp_server.py`: MCP tool definitions, RBAC enforcement, and the `propose_change`
  gate (`gate_token` issuance/validation).
- `uce/reasoning/gate.py`: the deterministic gate decision logic and token store.
- `uce/runtime/updater.py`: graph refresh + LLM ingestion orchestration.

## Evaluation and Research Artifacts

- [research/icmla_workshop/EVALUATION.md](../research/icmla_workshop/EVALUATION.md): which script
  measures what, and how to reproduce — start here for real numbers.
- [research/icmla_workshop/LIVE_TOOL_VALIDATION.md](../research/icmla_workshop/LIVE_TOOL_VALIDATION.md):
  confirms the shipped `propose_change` tool reproduces the enforcement-gate research result.
- [research/icmla_workshop/FINDINGS.md](../research/icmla_workshop/FINDINGS.md): full methodology,
  ablations, and significance tests across 4 real repos.
- [research/supplemental_benchmarks/README.md](../research/supplemental_benchmarks/README.md):
  commands to regenerate the earlier single-domain benchmark and report assets.
- [research/final_report/CS540_Final_Project_Report_UCE_Preet_Patel.docx](../research/final_report/CS540_Final_Project_Report_UCE_Preet_Patel.docx):
  the course project report.

## Packaging and Distribution

- PyPI package: <https://pypi.org/project/uce-engine/>
- Maintainer profile: <https://pypi.org/user/preetpatel/>

## Security and Operations

- Keep secrets only in local env files (`.env`, `docker/configs/client.env`, `.keycloak-secrets.env`)
  — never commit them.
- Never connect external clients directly to Neo4j-MCP (`8000`).
- Route all user-facing tool calls through UCE MCP (`9001`).
- Keep `UCE_GATE_ENFORCEMENT=enforced` (the default) in any shared deployment; see the README's
  "Data Privacy" section for what stays local vs. opt-in remote.
