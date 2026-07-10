# Canonical evaluation results (enforcement model only)

Retrieval/prediction metrics (file F1 vs independent oracle) are **deprecated for claims**. Use this document only.

## What we measure

| Metric | Definition |
|--------|------------|
| **catch_rate** | % scenarios where UCE gate fires (agent missed files UCE flagged, or silent requirement, or RBAC breach) |
| **agent_self_catch_rate** | % scenarios where agent alone matched oracle blast radius + requirements + RBAC |
| **false_gate_rate** | % scenarios UCE blocked when oracle says no real issue |
| **mean_missed_files** | Average \|UCE affected files \ agent declared files\| per scenario |
| **silent_requirement_scenario_rate** | % scenarios where UCE flagged a requirement the agent did not mention |

## Setup

- **4 repos**: talkai, melodi, expenses, spark (Postgres Drizzle, SQLite Drizzle, tiny Vite app, Supabase SQL)
- **114 scenarios** total (table/column/file destructive tasks)
- **Agent**: Claude Sonnet 4.5, editor role, full context in prompt (schema, governance where present, file inventory), **no tools**
- **UCE**: deterministic Neo4j ingest + `impact_analysis()` (unfiltered import closure)
- **Script**: `run_multi_repo_enforcement.py --ingest`

## Per repository

| repo | n | catch_rate | agent_self_catch | false_gate | mean_missed | agent files (avg) | UCE files (avg) |
|------|---|------------|------------------|------------|-------------|-------------------|-----------------|
| talkai | 24 | **100%** | 8.3% | 0% | 64.5 | 3.3 | 66.7 |
| melodi | 33 | **100%** | 6.1% | 0% | 19.8 | 4.5 | 23.3 |
| expenses | 9 | **100%** | 33.3% | 0% | 3.4 | 2.6 | 6.0 |
| spark | 48 | **100%** | 2.1% | 2.1% | 57.8 | 3.7 | 61.3 |

## Pooled (all 114 scenarios)

| Metric | Value |
|--------|-------|
| UCE catch_rate | **100%** (114/114) |
| Agent self-catch (no UCE needed) | **7.0%** (8/114) |
| False gate rate | **0.9%** (1/114, spark only) |
| Mean missed files per scenario | **43.9** |
| Median missed files | **55** |
| Agent declares (avg files) | **3.7** |
| UCE flags (avg files) | **47.1** |
| Silent requirement scenarios | **14.9%** (governed repos) |
| RBAC permission breaches (agent allowed, denied) | **0** |

## Interpretation (one paragraph)

On every destructive change task, the agent names a handful of files it would touch; UCE's graph names the full transitive blast radius (typically 20–70 more files). The gate fires in **100%** of scenarios because the agent systematically under-declares impact. The agent fully self-governs in only **7%** of cases. This holds across four codebases and three DB stacks. UCE is not being scored on "prediction accuracy" — it is scored on **whether it would have stopped an under-informed agent change**.

## Files

- `results/enforcement_eval/pooled_summary.json`
- `results/enforcement_eval/per_repo_summary.json`
- `results/enforcement_eval/{repo}/enforcement_results.csv`
- `results/enforcement_eval/METRICS.md`
