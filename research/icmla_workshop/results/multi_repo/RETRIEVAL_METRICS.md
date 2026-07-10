# Retrieval metrics (impact prediction vs independent oracle)

Micro-averaged precision / recall / F1 over all scenarios. UCE uses unfiltered `direct + transitive + call_chain` files from Neo4j.


## Per repository

| repo | system | file P | file R | file F1 | req P | req R | req F1 | pol P | pol R | pol F1 |
|------|--------|--------|--------|---------|-------|-------|--------|-------|-------|--------|
| talkai | naive_edit | 0.250 | 0.005 | 0.009 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| talkai | lexical | 0.543 | 0.198 | 0.290 | 0.448 | 0.317 | 0.371 | 0.450 | 0.486 | 0.468 |
| talkai | static | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| talkai | uce | 0.778 | 0.979 | 0.867 | 1.000 | 0.293 | 0.453 | 1.000 | 0.460 | 0.630 |
| melodi | naive_edit | 0.455 | 0.041 | 0.076 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| melodi | lexical | 0.377 | 0.343 | 0.359 | 0.527 | 0.395 | 0.452 | 0.574 | 0.722 | 0.639 |
| melodi | static | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| melodi | uce | 0.447 | 0.948 | 0.607 | 0.815 | 0.427 | 0.561 | 0.773 | 0.630 | 0.694 |
| expenses | naive_edit | 0.667 | 0.200 | 0.308 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| expenses | lexical | 0.750 | 0.500 | 0.600 | 0.857 | 0.857 | 0.857 | 1.000 | 0.800 | 0.889 |
| expenses | static | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| expenses | uce | 0.556 | 1.000 | 0.714 | 0.667 | 0.857 | 0.750 | 0.667 | 0.800 | 0.727 |
| spark | naive_edit | 1.000 | 0.003 | 0.006 | — | — | — | — | — | — |
| spark | lexical | 0.760 | 0.334 | 0.464 | — | — | — | — | — | — |
| spark | static | 1.000 | 1.000 | 1.000 | — | — | — | — | — | — |
| spark | uce | 0.623 | 1.000 | 0.768 | — | — | — | — | — | — |

## Pooled (micro over all scenarios, all repos)

| system | file P | file R | file F1 | req P | req R | req F1 | pol P | pol R | pol F1 |
|--------|--------|--------|---------|-------|-------|--------|-------|-------|--------|
| naive_edit | 0.458 | 0.009 | 0.018 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| lexical | 0.620 | 0.287 | 0.392 | 0.513 | 0.380 | 0.437 | 0.545 | 0.635 | 0.587 |
| static | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 |
| uce | 0.643 | 0.987 | 0.779 | 0.847 | 0.390 | 0.534 | 0.821 | 0.573 | 0.675 |

## Macro-average of per-repo F1 (unweighted by scenario count)

| system | mean file F1 | mean file R | mean req F1 | mean pol F1 |
|--------|--------------|-------------|-------------|-------------|
| naive_edit | 0.100 | 0.062 | 0.000 | 0.000 |
| lexical | 0.428 | 0.344 | 0.560 | 0.665 |
| static | 1.000 | 1.000 | 0.000 | 0.000 |
| uce | 0.739 | 0.982 | 0.588 | 0.684 |