# Live validation: does the shipped `propose_change` tool reproduce the research result?

`ENFORCEMENT_RESULTS.md` / `FINDINGS.md` (UPDATE 7) report a 100% gate-catch-rate, 0.9%
false-gate-rate result computed by `run_multi_repo_enforcement.py`, which duplicated the gate
decision math **inline** in a research script rather than calling a real MCP tool.
`uce.server.mcp_server.propose_change()` productizes that exact mechanism as a shipped, callable
tool with a mandatory `gate_token` enforcement path (see the main README).

This document reports the result of `replay_propose_change_live.py`, which replays the **same
already-captured real Claude Sonnet 4.5 responses** (`results/enforcement_eval/{repo}/raw.jsonl`
— no new API calls, no new cost) through the actual live `propose_change()` function, against a
freshly re-ingested Neo4j graph, for all 4 real external repos (talkai, melodi, expenses, spark;
3 different DB stacks). This directly tests whether the *shipped tool* reproduces the *validated
research mechanism*, rather than re-deriving the mechanism from scratch.

## Method

- For each repo: re-ingest deterministically via `ingest_repo.py` (code + schema + requirements +
  policies, no LLM). For talkai (the only repo with an RBAC doc), additionally ingest its
  authority rules into the graph via `graph.replace_authority_rules(...)` and enable RBAC on the
  config used for the test, so `propose_change`'s RBAC integration is exercised for real (the
  original script evaluated RBAC standalone from the parsed markdown, bypassing the graph).
- For each of the 114 scenarios, parse the same captured raw agent JSON the original run used
  (`files_to_edit`, `affected_requirements`) and call the live `propose_change(operation="write",
  entity_type=..., entity_name=..., files_to_edit=..., declared_requirements=..., strict=True)`.
- Score `catch_rate` (`decision != "allow"`), `false_gate_rate`, and `mean_missed_files` against
  the same independent-oracle ground truth used by the original script, using the **identical**
  `oracle_has_issue` definition (`oracle_files or (has_gov and oracle_reqs) or (has_rbac and not
  rbac_oracle_allowed)`) so the numbers are directly comparable, not a redefined metric.

## Result

| Repo | n | catch_rate (live tool) | catch_rate (original) | mean_missed_files (live) | mean_missed_files (original) |
|------|---|------------------------|------------------------|---------------------------|-------------------------------|
| talkai | 24 | 100% | 100% | 64.50 | 64.5 |
| melodi | 33 | 100% | 100% | 19.79 | 19.8 |
| expenses | 9 | 100% | 100% | 3.44 | 3.4 |
| spark | 48 | 100% | 100% | 57.75 | 57.8 |
| **Pooled** | **114** | **100%** | **100%** | **43.89** | **43.9** |

`mean_missed_files` matches to within rounding on every repo — the shipped tool's blast-radius
computation is faithful to the original mechanism, not an approximation of it.

**False-gate rate**: the live run measured **0.0%** (0/114) versus the original's **0.9%**
(1/114, on `spark: COL-user_roles-created_at`). Investigated directly: `propose_change`'s
`missed_files_count` for that exact scenario is **62 in both runs** — identical. The divergence
is entirely in the *independent oracle's* ground truth for that one column (0 files in the
original capture vs. >0 in this run), most likely explained by source drift in the external
`spark-creative-main` repo over the weeks between the two runs (the oracle's fallback heuristic
for ORM-less/Supabase-raw-SQL table linking matches quoted string literals in source text, which
is sensitive to exact file contents). This is not a difference in `propose_change`'s own
behavior — it reproduces the original gate decision and missed-file count exactly for this
scenario; only the oracle's classification of whether that decision was a "false" gate shifted.

RBAC (talkai only): `propose_change`'s live RBAC check (over the agent's actual declared
`files_to_edit`, evaluated against real `AuthorityRule` nodes in the graph) independently found a
deny in 14/24 scenarios — evidence the RBAC integration is doing real work on real data, not a
mocked path. This is a distinct measurement from "breach" (which requires comparing against the
agent's own permission claim); see `ENFORCEMENT_RESULTS.md` for the original breach metric
(0/24).

## Reproduce

```powershell
python research\icmla_workshop\replay_propose_change_live.py --ingest
```

Outputs: `results/propose_change_live_eval/{per_repo_summary.json, pooled_summary.json,
all_scenarios.csv}`.

## Conclusion

The shipped `propose_change` MCP tool reproduces the validated enforcement-gate mechanism exactly
(catch rate, missed-file counts) across all 4 real repos and 3 DB stacks. The one metric that
moved (false-gate rate) is explained by a change in independent-oracle ground truth for a single
scenario over time, not by a change in the tool's own decision logic.
