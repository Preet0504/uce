# Agent + UCE MCP evaluation (primary)

Compares the **same LLM agent** with vs without calling UCE MCP tools (`impact_analysis`).

Scores the agent's **final declared plan** vs an independent blast-radius oracle.


## Pooled

| condition | n | file P | file R | file F1 | incomplete plan rate |
|-----------|---|--------|--------|---------|----------------------|
| prompt_only | 24 | 0.595 | 0.031 | 0.059 | 100.0% |
| uce_mcp | 24 | 0.926 | 0.982 | 0.953 | 12.5% |

**File recall lift (uce_mcp − prompt_only):** 0.951
**Tool use rate:** 100.0%
**MCP `impact_analysis` output recall vs oracle (mean):** 0.986 (shows tools return correct blast radius even when the agent's final JSON plan does not).

## Per repo
| repo | prompt_only R | uce_mcp R | lift | tool use |
|------|---------------|-----------|------|----------|
| talkai | 0.0311 | 0.9821 | 0.951 | 1.0 |