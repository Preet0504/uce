# Retrieval metrics (recalculated)

Impact **prediction** vs the **independent oracle** (import-resolver + governance docs).  
**114 scenarios** across 4 repos. UCE scored on **unfiltered** `direct + transitive + call_chain` files (not backend-filtered).

Reproduce: `python research/icmla_workshop/run_multi_repo_eval.py --ingest --with-uce`  
Tables: `results/multi_repo/RETRIEVAL_METRICS.md`

## Pooled micro-averaged (all scenarios)

| system | file P | file R | file F1 | req P | req R | req F1 | pol P | pol R | pol F1 |
|--------|--------|--------|---------|-------|-------|--------|-------|-------|--------|
| naive_edit | 0.458 | 0.009 | 0.018 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| lexical | 0.620 | 0.287 | 0.392 | 0.513 | 0.380 | 0.437 | 0.545 | 0.635 | 0.587 |
| static (madge) | 1.000 | 1.000 | 1.000 | — | — | — | — | — | — |
| **UCE** | **0.643** | **0.987** | **0.779** | **0.847** | **0.390** | **0.534** | **0.821** | **0.573** | **0.675** |

## Per-repo file F1 (UCE)

| repo | file F1 | file recall |
|------|---------|-------------|
| talkai | 0.867 | 0.979 |
| melodi | 0.607 | 0.948 |
| expenses | 0.714 | 1.000 |
| spark | 0.768 | 1.000 |

## What changed vs earlier (wrong) numbers

- **UCE file recall** was ~0.36 on talkai because we scored backend-filtered `affected_files`; corrected **0.979**.
- **Pooled UCE file F1** was ~0.585 macro; corrected **micro F1 = 0.779** (pooled over 114 scenarios).
- **static** matches oracle closure by design (F1 = 1.0 on files); UCE does not beat static on files but adds governance (req/pol F1).

## Macro-average of per-repo F1 (4 repos, equal weight)

| system | mean file F1 | mean req F1 | mean pol F1 |
|--------|--------------|-------------|-------------|
| naive | 0.100 | 0.000 | 0.000 |
| lexical | 0.428 | 0.560 | 0.665 |
| static | 1.000 | — | — |
| UCE | 0.739 | 0.588 | 0.684 |
