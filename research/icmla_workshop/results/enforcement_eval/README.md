# Enforcement evaluation (canonical)

**This is the primary evaluation.** Retrieval/prediction F1 numbers under `results/multi_repo/` and `independent_summary.json` are **not** used for paper claims.

## Protocol

1. **Agent** (Claude Sonnet 4.5, editor role, no tools): destructive task + schema + governance + file inventory in prompt.
2. **UCE gate**: `impact_analysis()` on same entity (unfiltered `direct+transitive+call_chain` files from Neo4j).
3. **Oracle** (independent import graph): reference for `agent_self_caught` only.

**Gate fires** if: `|UCE files \ agent files| > 0` OR silent requirement OR RBAC permission breach.

## Reproduce

```bash
python research/icmla_workshop/run_multi_repo_enforcement.py --ingest
python research/icmla_workshop/consolidate_enforcement.py
```

Outputs:
- `per_repo_summary.json` — per-repo aggregates
- `pooled_summary.json` — all scenarios pooled
- `{repo}/enforcement_results.csv` — per-scenario rows
- `METRICS.md` — human-readable table (after consolidate)
