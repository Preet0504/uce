## Risk Model

This document defines the deterministic risk scoring formula used by `explain_change`,
`impact_analysis`, and every other tool that reports a `risk_score` for an entity (they all share
the same underlying `uce.core.risk_model.assess_risk`, so the score is consistent across tools —
see `docs/TECHNICAL_REPORT.md` Section 4).

Formula (`uce/core/risk_model.py`):

```
risk_score =
  2 × affected_files
  1 × affected_functions
  4 × violated_requirements
  3 × enforced_policies
```

Definitions:
- `affected_files`: count of affected files. `explain_change`/`impact_analysis` narrow this to
  backend files (excluding UI paths like `/ui/`, `/views/`, `/components/` when no explicit
  `paths.backend` config is set); other entry points may use the full unfiltered blast radius —
  see the specific tool's docstring in `uce/server/mcp_server.py`.
- `affected_functions`: count of functions whose declaring file is in the backend-affected set.
- `violated_requirements`: count of requirement IDs linked to the target entity.
- `enforced_policies`: count of policy IDs enforcing those requirements.

Risk bands: low (0-7), moderate (8-19), high (20+).

All counts are deterministic and derived from graph traversal only.
