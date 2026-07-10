# ICMLA workshop evaluation hierarchy

UCE is meant to be used by **LLM agents calling the MCP server** (`impact_analysis`, `explain_change`, …), not as a batch graph-vs-oracle scorer. The harness reflects that.

## Primary (paper-facing)

| Script | What it measures |
|--------|------------------|
| **`run_agent_mcp_eval.py`** | Same Claude agent **with vs without UCE MCP tools**. Agent must call `impact_analysis` in the tool condition; we score the **final declared plan** vs independent blast-radius oracle. Headline: **file recall lift** when tools are available. |
| **`run_multi_repo_enforcement.py`** | Agent proposes a change (prompt only); **UCE gate** compares agent plan to graph impact. Headline: **catch rate**, **incomplete-plan rate**, mean missed files. |
| **`run_rbac_complexity.py`** | Hard RBAC: LLM breach rate vs UCE **0%** deterministic enforcement. |
| **`run_context_comparison.py`** | Context ladder (bare → +gov → +inventory → RAG) vs UCE — shows pasted context does not substitute for tools. |

Run agent+MCP (reuse prior enforcement for `prompt_only` to save API cost):

```bash
python research/icmla_workshop/ingest_repo.py <path/to/config.yaml>
python research/icmla_workshop/run_agent_mcp_eval.py --reuse-prompt-only --ingest
```

## Supplementary (engineering / mechanism only)

| Script | Note |
|--------|------|
| `run_independent_eval.py` | TalkAI-only deterministic UCE vs oracle F1 — **not** the user-facing workflow. |
| `run_multi_repo_eval.py` | Multi-repo retrieval F1 (naive / lexical / madge / UCE). Useful for graph QA; **do not** lead the paper with this. |
| `run_ablation.py` | Propagation ablation on TalkAI graph. |

## Results directories

- `results/agent_mcp_eval/` — **primary** agent + MCP comparison
- `results/enforcement_eval/` — gate / catch-rate study
- `results/multi_repo/` — supplementary retrieval metrics only
