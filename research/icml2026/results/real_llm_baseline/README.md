# Real no-tool LLM baseline

This directory contains the corrected baseline run. The previous `vanilla`
numbers in `research/icml2026/results/scenario_results.csv` were a lexical
proxy from `vanilla_prediction()` in `run_benchmark.py`; they are not real LLM
responses and should not be described as such.

## Provider used

- OpenAI was attempted first during this correction work, but the configured
  key returned `429 insufficient_quota`.
- The completed run therefore used the configured local OpenAI-compatible model:
  `llama3:instruct`.
- The baseline is "vanilla" in the sense that the model received pasted static
  context only. It had no MCP, Neo4j, filesystem, grep, or graph-query access.

## How scoring works

- The model was asked to return structured JSON with exact requirement IDs
  (`RQ-###`), policy IDs (`P-###`), file paths, and RBAC allow/deny decisions.
- The evaluator extracts IDs and file paths from the model's actual response.
  This extraction is only response parsing; it is not the prediction method.
- Requirement/policy truth labels come from a graphless deterministic oracle
  that mirrors the current ingestion logic over schema, requirement, policy,
  code, and import data. This avoids using the broken Neo4j service while still
  keeping the oracle reproducible.
- RBAC truth labels come from `core.rbac.evaluate_rules()` over the RBAC
  Markdown policy.

## Main outputs

- `raw_scenario_responses.jsonl`: prompts, raw model responses, parse errors,
  and oracle payloads for each scenario batch.
- `scenario_predictions.csv`: parsed model predictions per scenario.
- `scenario_eval.csv`: exact-overlap scoring per scenario.
- `scenario_comparison_summary.csv`: corrected no-tool LLM vs existing MCP-UCE
  graph-run summary.
- `raw_rbac_responses.jsonl`: prompts and raw RBAC model responses.
- `rbac_eval.csv`: scored RBAC probe decisions.
- `rbac_comparison_summary.csv`: corrected RBAC comparison summary.

## Current headline results

- Requirement caught-any rate: `0.550` for the real no-tool LLM vs `0.773` for
  the existing MCP-UCE graph run.
- Policy caught-any rate: `0.368` for the real no-tool LLM vs `0.714` for the
  existing MCP-UCE graph run.
- RBAC breach rate on oracle-denied probes: `0.647` for the real no-tool LLM vs
  `0.000` for the existing MCP-UCE graph run.

## Important caveat

The MCP-UCE side in this comparison is still the existing graph-run artifact,
not a fresh rerun, because the local Neo4j/Bolt service was timing out during
this correction. For a final paper, rerun MCP-UCE after Neo4j is healthy and
replace the `mcp_uce_existing_graph_run` rows with fresh same-day results.
