## Risk Model

This document defines the deterministic risk scoring formula used by `explain_change`.

Formula:

```
risk_score =
  2 × backend_files
  4 × violated_requirements
  6 × enforced_policies
  3 × affected_apis
```

Definitions:
- `backend_files`: count of affected files excluding UI paths (`/ui/`, `/views/`, `/components/`).
- `violated_requirements`: count of requirement IDs linked to the target entity.
- `enforced_policies`: count of policy IDs enforcing those requirements.
- `affected_apis`: count of APIs exposed by affected functions.

All counts are deterministic and derived from graph traversal only.
