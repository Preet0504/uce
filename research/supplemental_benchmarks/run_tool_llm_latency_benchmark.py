from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"
REAL_DIR = RESULTS_DIR / "real_llm_baseline"
TOOL_DIR = RESULTS_DIR / "tool_llm_latency"

from core.config import load_config
from ingestion.schema_parser import parse_schema_file
from run_real_llm_baseline import (
    ChatJsonClient,
    GraphlessOracle,
    POL_ID_RE,
    REQ_ID_RE,
    _build_scenarios_from_csv,
    _collect_files,
    _collect_ids,
    _ensure_dirs,
    _extract_json,
    _load_dotenv,
    _policies_context,
    _read_policies,
    _read_requirements,
    _requirements_context,
    _schema_context,
)


def _tool_planner_prompt(batch) -> str:
    scenario_lines = "\n".join(
        f"- {scenario.scenario_id}: entity_type={scenario.entity_type}, "
        f"entity_name={scenario.entity_name}, task={scenario.prompt}"
        for scenario in batch
    )
    return f"""
You are a tool-capable local LLM. You can call exactly one tool:

Tool name: impact_analysis
Arguments:
- entity_type: one of table, column, file
- entity_name: exact entity name from the scenario

For each scenario below, decide which tool call should be made. Return JSON only.

Scenarios:
{scenario_lines}

Return this shape:
{{
  "tool_calls": [
    {{
      "scenario_id": "TBL-example",
      "tool": "impact_analysis",
      "arguments": {{"entity_type": "table", "entity_name": "example"}}
    }}
  ]
}}
""".strip()


def _tool_final_prompt(batch, tool_outputs) -> str:
    scenario_lines = "\n".join(
        f"- {scenario.scenario_id}: entity_type={scenario.entity_type}, "
        f"entity_name={scenario.entity_name}, task={scenario.prompt}"
        for scenario in batch
    )
    return f"""
You are a tool-capable local LLM. The impact_analysis tool has already been called.
Use the tool outputs as authoritative. Do not invent extra requirement IDs, policy IDs, or file paths.
Return compact/minified JSON only. Do not include rationale text. Do not pretty-print.

Scenarios:
{scenario_lines}

Tool outputs:
{json.dumps(tool_outputs, indent=2)}

Return JSON only:
{{
  "predictions": [
    {{
      "scenario_id": "TBL-example",
      "affected_files": ["src/..."],
      "violated_requirements": ["RQ-001"],
      "enforced_policies": ["P-001"]
    }}
  ]
}}
""".strip()


def _prediction_for_scenario(parsed: dict, scenario_id: str) -> object:
    predictions = parsed.get("predictions")
    if isinstance(predictions, list):
        for item in predictions:
            if isinstance(item, dict) and str(item.get("scenario_id") or "").strip() == scenario_id:
                return item
    return {}


def _score_sets(predicted, truth) -> dict[str, float | int]:
    pred = set(predicted)
    gold = set(truth)
    tp = len(pred & gold)
    fp = len(pred - gold)
    fn = len(gold - pred)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def _scenario_latency_from_existing_no_tool() -> dict[str, float]:
    path = REAL_DIR / "scenario_eval.csv"
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    if "latency_ms" not in df:
        return {}
    return {
        "no_tool_total_ms": float(df["latency_ms"].sum()),
        "no_tool_mean_per_scenario_ms": float(df["latency_ms"].mean()),
        "no_tool_scenarios": int(len(df)),
    }


def run_tool_latency(args) -> None:
    _ensure_dirs()
    TOOL_DIR.mkdir(parents=True, exist_ok=True)
    _load_dotenv(ROOT_DIR / ".env")

    config = load_config(args.config)
    project_root = Path(config.project_root).resolve()
    tables = parse_schema_file(str(project_root / "src" / "db" / "schema.ts"))
    requirements = _read_requirements(project_root / "src" / "requirements")
    policies = _read_policies(project_root / "src" / "policies")
    oracle = GraphlessOracle(config, tables, requirements, policies)
    scenarios = _build_scenarios_from_csv(RESULTS_DIR / "scenario_results.csv")
    if args.limit:
        scenarios = scenarios[: args.limit]

    client = ChatJsonClient(args.provider, max_tokens=args.max_tokens)

    raw_path = TOOL_DIR / "raw_tool_llm_scenario_responses.jsonl"
    rows = []
    batch_rows = []
    total_start = time.perf_counter()

    with raw_path.open("w", encoding="utf-8") as raw_handle:
        for batch_start in range(0, len(scenarios), args.batch_size):
            batch = scenarios[batch_start : batch_start + args.batch_size]

            planner_prompt = ""
            planner_raw = ""
            planner_json = {}
            planner_ms = 0.0
            if args.mode == "planner_and_final":
                planner_prompt = _tool_planner_prompt(batch)
                planner_start = time.perf_counter()
                planner_raw = client.generate_json_text(planner_prompt)
                planner_ms = (time.perf_counter() - planner_start) * 1000.0
                planner_json = _extract_json(planner_raw)

            tool_outputs = []
            tool_start = time.perf_counter()
            for scenario in batch:
                output = oracle.prediction_for(scenario)
                tool_outputs.append(
                    {
                        "scenario_id": scenario.scenario_id,
                        "tool": "impact_analysis",
                        "arguments": {
                            "entity_type": scenario.entity_type,
                            "entity_name": scenario.entity_name,
                        },
                        "output": output,
                    }
                )
            tool_ms = (time.perf_counter() - tool_start) * 1000.0

            final_prompt = _tool_final_prompt(
                batch=batch,
                tool_outputs=tool_outputs,
            )
            final_start = time.perf_counter()
            final_raw = client.generate_json_text(final_prompt)
            final_ms = (time.perf_counter() - final_start) * 1000.0
            final_json = _extract_json(final_raw)
            batch_total_ms = planner_ms + tool_ms + final_ms

            raw_handle.write(
                json.dumps(
                    {
                        "batch_start": batch_start,
                        "batch_size": len(batch),
                        "provider": args.provider,
                        "model": client.model,
                        "mode": args.mode,
                        "planner_prompt": planner_prompt,
                        "planner_raw_response": planner_raw,
                        "planner_parsed": planner_json,
                        "tool_outputs": tool_outputs,
                        "final_prompt": final_prompt,
                        "final_raw_response": final_raw,
                        "planner_ms": planner_ms,
                        "tool_ms": tool_ms,
                        "final_ms": final_ms,
                        "batch_total_ms": batch_total_ms,
                    },
                    ensure_ascii=True,
                )
                + "\n"
            )

            batch_rows.append(
                {
                    "batch_start": batch_start,
                    "batch_size": len(batch),
                    "planner_ms": planner_ms,
                    "tool_ms": tool_ms,
                    "final_ms": final_ms,
                    "batch_total_ms": batch_total_ms,
                }
            )

            for scenario in batch:
                truth = oracle.prediction_for(scenario)
                prediction = _prediction_for_scenario(final_json, scenario.scenario_id)
                predicted_files = _collect_files(prediction)
                predicted_requirements = _collect_ids(prediction, REQ_ID_RE)
                predicted_policies = _collect_ids(prediction, POL_ID_RE)
                file_score = _score_sets(predicted_files, truth["affected_files"])
                req_score = _score_sets(predicted_requirements, truth["violated_requirements"])
                pol_score = _score_sets(predicted_policies, truth["enforced_policies"])
                rows.append(
                    {
                        "scenario_id": scenario.scenario_id,
                        "entity_type": scenario.entity_type,
                        "entity_name": scenario.entity_name,
                        "provider": args.provider,
                        "model": client.model,
                        "planner_ms_batch": planner_ms,
                        "tool_ms_batch": tool_ms,
                        "final_ms_batch": final_ms,
                        "batch_total_ms": batch_total_ms,
                        "per_scenario_end_to_end_ms": batch_total_ms / max(1, len(batch)),
                        "predicted_files": json.dumps(predicted_files),
                        "predicted_requirements": json.dumps(predicted_requirements),
                        "predicted_policies": json.dumps(predicted_policies),
                        "oracle_files": json.dumps(truth["affected_files"]),
                        "oracle_requirements": json.dumps(truth["violated_requirements"]),
                        "oracle_policies": json.dumps(truth["enforced_policies"]),
                        "file_f1": file_score["f1"],
                        "requirement_f1": req_score["f1"],
                        "policy_f1": pol_score["f1"],
                    }
                )

            print(
                f"[tool-llm batch {batch_start // args.batch_size + 1}] "
                f"planner={planner_ms:.1f}ms tool={tool_ms:.1f}ms "
                f"final={final_ms:.1f}ms total={batch_total_ms:.1f}ms",
                flush=True,
            )

    total_wall_ms = (time.perf_counter() - total_start) * 1000.0
    pred_df = pd.DataFrame(rows)
    pred_df.to_csv(TOOL_DIR / "tool_llm_scenario_eval.csv", index=False)
    batch_df = pd.DataFrame(batch_rows)
    batch_df.to_csv(TOOL_DIR / "tool_llm_batch_latency.csv", index=False)

    no_tool = _scenario_latency_from_existing_no_tool()
    tool_total_ms = float(batch_df["batch_total_ms"].sum()) if len(batch_df) else 0.0
    summary = {
        "provider": args.provider,
        "model": client.model,
        "scenario_count": len(scenarios),
        "tool_llm_total_ms_sum_batches": tool_total_ms,
        "tool_llm_total_wall_ms": total_wall_ms,
        "tool_llm_mean_per_scenario_ms": float(pred_df["per_scenario_end_to_end_ms"].mean())
        if len(pred_df)
        else 0.0,
        "planner_total_ms": float(batch_df["planner_ms"].sum()) if len(batch_df) else 0.0,
        "tool_execution_total_ms": float(batch_df["tool_ms"].sum()) if len(batch_df) else 0.0,
        "final_generation_total_ms": float(batch_df["final_ms"].sum()) if len(batch_df) else 0.0,
        **no_tool,
    }
    if no_tool.get("no_tool_total_ms") and no_tool.get("no_tool_scenarios") == len(scenarios):
        summary["tool_vs_no_tool_delta_ms"] = tool_total_ms - no_tool["no_tool_total_ms"]
        summary["tool_vs_no_tool_speedup_ratio"] = (
            no_tool["no_tool_total_ms"] / tool_total_ms if tool_total_ms else None
        )
    elif no_tool.get("no_tool_mean_per_scenario_ms"):
        summary["no_tool_comparison_note"] = (
            "Tool run scenario count differs from existing no-tool run; totals are not directly comparable."
        )
        summary["tool_extrapolated_24_scenario_ms"] = (
            summary["tool_llm_mean_per_scenario_ms"] * float(no_tool.get("no_tool_scenarios", 24))
        )
    (TOOL_DIR / "tool_llm_latency_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    pd.DataFrame([summary]).to_csv(TOOL_DIR / "tool_llm_latency_summary.csv", index=False)
    print(json.dumps(summary, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure end-to-end local LLM latency with UCE-style tool calls."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--provider", choices=["local"], default="local")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-tokens", type=int, default=900)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--mode",
        choices=["routed_final_only", "planner_and_final"],
        default="routed_final_only",
    )
    args = parser.parse_args()
    run_tool_latency(args)


if __name__ == "__main__":
    main()
