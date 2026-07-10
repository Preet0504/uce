# Multi-repo external validity (file impact over full import graph; governance where present)

| repo | system | file P | file R | file F1 | req F1 | pol F1 |
|------|--------|--------|--------|---------|--------|--------|
| talkai | naive_edit | 0.250 | 0.005 | 0.009 | 0.000 | 0.000 |
| talkai | lexical | 0.543 | 0.198 | 0.290 | 0.371 | 0.468 |
| talkai | static | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 |
| talkai | uce | 0.778 | 0.979 | 0.867 | 0.453 | 0.630 |
| melodi | naive_edit | 0.455 | 0.041 | 0.076 | 0.000 | 0.000 |
| melodi | lexical | 0.377 | 0.343 | 0.359 | 0.452 | 0.639 |
| melodi | static | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 |
| melodi | uce | 0.447 | 0.948 | 0.607 | 0.561 | 0.694 |
| expenses | naive_edit | 0.667 | 0.200 | 0.308 | 0.000 | 0.000 |
| expenses | lexical | 0.750 | 0.500 | 0.600 | 0.857 | 0.889 |
| expenses | static | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 |
| expenses | uce | 0.556 | 1.000 | 0.714 | 0.750 | 0.727 |
| spark | naive_edit | 1.000 | 0.003 | 0.006 |   -   |   -   |
| spark | lexical | 0.760 | 0.334 | 0.464 |   -   |   -   |
| spark | static | 1.000 | 1.000 | 1.000 |   -   |   -   |
| spark | uce | 0.623 | 1.000 | 0.768 |   -   |   -   |

## Macro-average across repos

| system | mean file F1 | mean file recall | mean req F1 (gov) | mean pol F1 (gov) |
|--------|--------------|------------------|-------------------|-------------------|
| naive_edit | 0.100 | 0.062 | 0.000 | 0.000 |
| lexical | 0.428 | 0.344 | 0.560 | 0.665 |
| static | 1.000 | 1.000 | 0.000 | 0.000 |
| uce | 0.739 | 0.982 | 0.588 | 0.684 |