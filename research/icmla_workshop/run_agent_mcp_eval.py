"""
PRIMARY evaluation: LLM coding agent with vs without UCE MCP tools.

This matches real deployment: agents call the UCE MCP server (`impact_analysis`,
`explain_change`, …) rather than humans comparing deterministic graph output to a
hand-built oracle for F1 bragging rights.

Conditions:
  prompt_only  — Agent gets task + schema + governance + file inventory in the prompt.
                 No tools. (Same as "enforcement" agent phase.)
  uce_mcp      — Same agent, but may call UCE MCP tools (in-process, identical to
                 mcp_server.impact_analysis / explain_change). Tools return the same
                 JSON the live server would return.

Ground truth for file/requirement recall: independent import-graph oracle (for
measuring whether the agent's *declared plan* is complete — not for scoring UCE itself).

Metrics (per repo + pooled):
  - file_precision, file_recall, file_f1 vs oracle (agent's final JSON plan)
  - req_*, pol_* vs oracle (governed repos)
  - tool_use_rate: % uce_mcp scenarios where agent invoked impact_analysis
  - recall_lift: file_recall(uce_mcp) - file_recall(prompt_only)
  - incomplete_plan_rate: % scenarios where agent plan misses >=1 oracle file

Usage:
  python run_agent_mcp_eval.py --ingest                    # all repos, both conditions
  python run_agent_mcp_eval.py --only talkai --condition uce_mcp --ingest
  python run_agent_mcp_eval.py --reuse-prompt-only           # skip API for prompt_only (use prior enforcement run)
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
REPO_ROOT = BASE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from uce.core.config import load_config, UceConfig
from uce.core.graph_db import GraphDB
from uce.server import mcp_server

from independent_oracle import (
    build_import_graph, governance_oracle, parse_policies, parse_requirements,
    parse_schema, scenario_seeds, reverse_reachable, normalize_repo_path,
)
from run_independent_eval import prf
from run_anthropic_baseline import (
    AnthropicClient, _extract_json, _load_env,
    REQ_ID_RE, POL_ID_RE, _collect_ids,
)
from run_multi_repo_enforcement import (
    repo_specs, RepoSpec, build_scenarios_enforcement, _collect_repo_files,
    _schema_ctx, _req_ctx, _pol_ctx, _ingest,
)

OUT_ROOT = BASE_DIR / "results" / "agent_mcp_eval"
ENFORCEMENT_ROOT = BASE_DIR / "results" / "enforcement_eval"
AGENT_ROLE = "editor"
MAX_TOOL_ROUNDS = 4

# Deployment-realistic prompts (no "copy verbatim" leakage). Use --prompt strict only for ablation.
PROMPT_PROFILES = {
    "neutral": {
        "system_tools": (
            "You are an AI coding agent with UCE MCP tools connected to a Neo4j governance graph. "
            "You may call impact_analysis and explain_change to assess blast radius before planning edits. "
            "Return valid JSON only."
        ),
        "system_no_tools": (
            "You are an AI coding agent with NO tools, databases, or graph APIs. "
            "Use ONLY the pasted context. Return valid JSON only."
        ),
        "tool_block": (
            "You have UCE MCP tools `impact_analysis` and `explain_change`. "
            "Call `impact_analysis` for this task's entity before you finalize the plan. "
            "Use the tool output to inform files_to_edit, affected_requirements, and affected_policies."
        ),
        "tool_desc": (
            "UCE MCP: graph-backed impact analysis. Returns blast_radius_files, "
            "violated_requirements, enforced_policies."
        ),
    },
    "strict": {
        "system_tools": (
            "You are an AI coding agent with UCE MCP tools. Always call impact_analysis first. "
            "Your files_to_edit MUST equal blast_radius_files exactly. Return only valid JSON."
        ),
        "system_no_tools": (
            "You are an AI coding agent with NO tools. Use only pasted context. Return valid JSON only."
        ),
        "tool_block": (
            "Call `impact_analysis` for this entity. Copy EVERY path from blast_radius_files into files_to_edit."
        ),
        "tool_desc": "Returns blast_radius_files; copy verbatim into files_to_edit.",
    },
}

def _compact_tool_payload(res: dict) -> str:
    """Agent-facing tool result: lead with the full file list agents must copy into their plan."""
    files = sorted(files_from_mcp_result(res))
    payload = {
        "blast_radius_files": files,
        "blast_radius_file_count": len(files),
        "violated_requirements": res.get("violated_requirements") or [],
        "enforced_policies": res.get("enforced_policies") or [],
        "summary": res.get("summary"),
        "entity": res.get("entity"),
    }
    return json.dumps(payload, indent=2)


def _uce_tools(profile: dict) -> list[dict]:
    return [
        {
            "name": "impact_analysis",
            "description": profile["tool_desc"],
            "input_schema": {
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string", "enum": ["table", "column", "file"]},
                    "entity_name": {"type": "string"},
                },
                "required": ["entity_type", "entity_name"],
            },
        },
        {
            "name": "explain_change",
            "description": "UCE MCP: trace-oriented explanation for an entity change.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "entity_type": {"type": "string", "enum": ["table", "column", "file"]},
                    "entity_name": {"type": "string"},
                },
                "required": ["entity_type", "entity_name"],
            },
        },
    ]


def init_mcp(config: UceConfig) -> None:
    """Wire MCP tool handlers to the ingested graph for this repo."""
    with mcp_server._DB_LOCK:
        if mcp_server._DB is not None:
            try:
                mcp_server._DB.close()
            except Exception:
                pass
        mcp_server._CONFIG = config
        mcp_server._DB = GraphDB(config.neo4j.uri, config.neo4j.user, config.neo4j.password)
        try:
            mcp_server._DB.ensure_indexes()
        except Exception:
            pass
    mcp_server.invalidate_rbac_cache()


def files_from_mcp_result(res: dict) -> set[str]:
    """Unfiltered blast-radius files from MCP `impact_analysis` (same union as enforcement eval)."""
    files: set[str] = set()
    impact = res.get("impact") or {}
    for k in ("direct_files", "transitive_files", "call_chain_files"):
        files.update(impact.get(k) or [])
    files.update(res.get("affected_files") or [])
    if not files and impact.get("affected_files"):
        files.update(impact["affected_files"])
    return {normalize_repo_path(f) for f in files if f}


def _graph_file_count() -> int:
    try:
        stats = mcp_server.graph_stats()
        nodes = stats.get("nodes") or {}
        return int(nodes.get("File") or stats.get("files") or stats.get("file_count") or 0)
    except Exception:
        return 0


def _task_line(scenario: dict) -> str:
    if scenario["type"] == "table":
        return f"Drop the `{scenario['name']}` table entirely from the codebase."
    if scenario["type"] == "column":
        tbl, col = scenario["name"].split(".", 1)
        return f"Remove the `{col}` column from the `{tbl}` table."
    return f"Delete or replace the file `{scenario['name']}`."


def _user_prompt(scenario: dict, schema_ctx: str, req_ctx: str, pol_ctx: str,
                  inventory: str, with_tools: bool, profile: dict) -> str:
    task = _task_line(scenario)
    if with_tools:
        tool_block = profile["tool_block"] + (
            f"\nEntity for impact_analysis: entity_type={scenario['type']!r}, entity_name={scenario['name']!r}."
        )
    else:
        tool_block = "You have NO tools — use only the context below."

    return f"""You are an editor-role coding agent.

Task: {task}

{tool_block}

Database schema:
{schema_ctx}

Requirements:
{req_ctx}

Policies:
{pol_ctx}

File inventory (paths only):
{inventory}

When done, respond with ONLY a JSON object:
{{
  "files_to_edit": ["path/..."],
  "affected_requirements": ["RQ-001"],
  "affected_policies": ["P-001"],
  "used_impact_analysis": true or false,
  "notes": "one sentence"
}}"""


def _system_prompt(with_tools: bool, profile: dict) -> str:
    return profile["system_tools"] if with_tools else profile["system_no_tools"]


def _execute_tool(name: str, inputs: dict) -> dict:
    if name == "impact_analysis":
        return mcp_server.impact_analysis(
            entity_type=str(inputs.get("entity_type", "")),
            entity_name=str(inputs.get("entity_name", "")),
        )
    if name == "explain_change":
        return mcp_server.explain_change(
            entity_type=str(inputs.get("entity_type", "")),
            entity_name=str(inputs.get("entity_name", "")),
        )
    return {"error": f"unknown tool {name}"}


def _run_agent_mcp(
    client: AnthropicClient, scenario: dict, schema_ctx, req_ctx, pol_ctx, inventory, profile: dict,
) -> tuple[dict, list[dict]]:
    """Anthropic tool-use loop mirroring MCP calls."""
    messages: list[dict] = [
        {"role": "user", "content": _user_prompt(scenario, schema_ctx, req_ctx, pol_ctx, inventory, True, profile)},
    ]
    tool_log: list[dict] = []
    final_text = ""

    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.client.messages.create(
            model=client.model,
            max_tokens=client.max_tokens,
            temperature=0,
            system=_system_prompt(True, profile),
            tools=_uce_tools(profile),
            messages=messages,
        )
        # Collect text and tool uses from response
        tool_uses = []
        text_parts = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        if text_parts:
            final_text = "\n".join(text_parts)

        if resp.stop_reason != "tool_use" or not tool_uses:
            break

        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for tu in tool_uses:
            result = _execute_tool(tu.name, tu.input if isinstance(tu.input, dict) else {})
            tool_log.append({"tool": tu.name, "input": tu.input, "result_keys": list(result.keys())})
            content = _compact_tool_payload(result) if tu.name == "impact_analysis" else json.dumps(result, default=str)[:12000]
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": content[:16000],
            })
        messages.append({"role": "user", "content": tool_results})

    parsed = _extract_json(final_text) if final_text else {}
    return parsed, tool_log


def _run_agent_prompt_only(client: AnthropicClient, scenario, schema_ctx, req_ctx, pol_ctx, inventory, profile: dict) -> dict:
    text = client.json_text(
        _system_prompt(False, profile),
        _user_prompt(scenario, schema_ctx, req_ctx, pol_ctx, inventory, False, profile),
    )
    return _extract_json(text) if text else {}


def _score_row(
    spec: RepoSpec,
    scenario: dict,
    condition: str,
    parsed: dict,
    oracle_files: set[str],
    oracle_reqs: set[str],
    oracle_pols: set[str],
    tool_log: list[dict],
    tool_files: set[str],
    latency_ms: float,
) -> dict:
    agent_files = _collect_repo_files(parsed.get("files_to_edit") or [])
    agent_reqs = _collect_ids(parsed.get("affected_requirements") or [], REQ_ID_RE)
    agent_pols = _collect_ids(parsed.get("affected_policies") or [], POL_ID_RE)

    f_tp, f_fp, f_fn, f_p, f_r, f_f1 = prf(agent_files, oracle_files)
    r_tp, r_fp, r_fn, r_p, r_r, r_f1 = prf(agent_reqs, oracle_reqs)
    p_tp, p_fp, p_fn, p_p, p_r, p_f1 = prf(agent_pols, oracle_pols)
    _, _, _, _, tool_r, _ = prf(tool_files, oracle_files) if tool_files else (0, 0, 0, 0.0, 0.0, 0.0)
    if tool_files:
        adhere_tp = len(agent_files & tool_files)
        tool_adherence = adhere_tp / len(tool_files)
    else:
        tool_adherence = None

    used_tool = bool(parsed.get("used_impact_analysis")) or any(t.get("tool") == "impact_analysis" for t in tool_log)
    if condition == "uce_mcp" and tool_log:
        used_tool = True

    return {
        "repo": spec.name,
        "scenario_id": scenario["id"],
        "entity_type": scenario["type"],
        "entity_name": scenario["name"],
        "condition": condition,
        "oracle_files": len(oracle_files),
        "oracle_reqs": len(oracle_reqs),
        "agent_files": len(agent_files),
        "tool_files": len(tool_files),
        "used_impact_analysis": used_tool,
        "file_tp": f_tp, "file_fp": f_fp, "file_fn": f_fn,
        "file_precision": round(f_p, 4), "file_recall": round(f_r, 4), "file_f1": round(f_f1, 4),
        "mcp_tool_file_recall": round(tool_r, 4) if condition == "uce_mcp" else None,
        "tool_adherence": round(tool_adherence, 4) if tool_adherence is not None else None,
        "req_f1": round(r_f1, 4) if oracle_reqs or agent_reqs else None,
        "pol_f1": round(p_f1, 4) if oracle_pols or agent_pols else None,
        "incomplete_plan": int(bool(oracle_files - agent_files)),
        "latency_ms": round(latency_ms, 1),
    }


def _load_reuse_prompt_only(
    spec: RepoSpec,
    scenarios: list[dict],
    g,
    schema,
    requirements,
    policies,
) -> list[dict]:
    """Re-score prior enforcement agent responses vs independent oracle (no new API calls)."""
    raw_path = ENFORCEMENT_ROOT / spec.name / "raw.jsonl"
    if not raw_path.exists():
        return []
    raw_by_id: dict[str, dict] = {}
    for line in raw_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        raw_by_id[rec["scenario_id"]] = rec

    rows: list[dict] = []
    for sc in scenarios:
        rec = raw_by_id.get(sc["id"])
        if not rec:
            continue
        parsed = _extract_json(rec.get("raw") or "")
        seeds = scenario_seeds(g, schema, spec.schema_rel_candidates, sc["type"], sc["name"])
        oracle_files = {normalize_repo_path(f) for f in reverse_reachable(g, seeds)}
        oracle_reqs, oracle_pols = governance_oracle(sc["type"], sc["name"], requirements, policies)
        rows.append(
            _score_row(
                spec, sc, "prompt_only", parsed,
                oracle_files, oracle_reqs, oracle_pols,
                [], set(), float(rec.get("latency_ms") or 0),
            )
        )
    return rows


def run_repo(
    spec: RepoSpec,
    client: AnthropicClient | None,
    conditions: set[str],
    do_ingest: bool,
    reuse_prompt: bool,
    auto_ingest: bool = True,
    profile: dict | None = None,
    raw_log=None,
) -> list[dict]:
    profile = profile or PROMPT_PROFILES["neutral"]
    if do_ingest:
        print(f"\n--- Ingesting {spec.name} ---", flush=True)
        _ingest(spec)

    config = load_config(str(spec.config_path))
    init_mcp(config)
    if _graph_file_count() < 5:
        print(
            f"  WARN: Neo4j graph for {spec.name} looks empty — MCP tools will return no blast radius. "
            "Re-run with --ingest.",
            flush=True,
        )
        if auto_ingest and not do_ingest:
            print(f"  Auto-ingesting {spec.name} because graph is empty.", flush=True)
            _ingest(spec)
            init_mcp(config)

    schema = parse_schema(spec.schema_paths, kind=spec.schema_kind)
    g = build_import_graph(spec.root, spec.code_dirs, spec.alias_map, spec.ignore)
    requirements = (
        parse_requirements(spec.requirements_dir, schema)
        if spec.requirements_dir and spec.requirements_dir.exists() else []
    )
    policies = (
        parse_policies(spec.policies_dir)
        if spec.policies_dir and spec.policies_dir.exists() else []
    )
    schema_ctx = _schema_ctx(schema)
    req_ctx = _req_ctx(requirements)
    pol_ctx = _pol_ctx(policies)
    inv = sorted(g.files)
    inventory = "\n".join(f"- {f}" for f in (inv[:80] + inv[-40:] if len(inv) > 120 else inv))

    scenarios = build_scenarios_enforcement(schema, g)
    out_dir = OUT_ROOT / spec.name
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if raw_log is None and "uce_mcp" in conditions:
        raw_log = (out_dir / "raw_uce_mcp.jsonl").open("w", encoding="utf-8")

    if reuse_prompt and "prompt_only" in conditions:
        reused = _load_reuse_prompt_only(spec, scenarios, g, schema, requirements, policies)
        if reused:
            print(f"  Reused {len(reused)} prompt_only rows from enforcement_eval", flush=True)
            rows.extend(reused)
            conditions = conditions - {"prompt_only"}

    import time
    for sc in scenarios:
        seeds = scenario_seeds(g, schema, spec.schema_rel_candidates, sc["type"], sc["name"])
        oracle_files = {normalize_repo_path(f) for f in reverse_reachable(g, seeds)}
        oracle_reqs, oracle_pols = governance_oracle(sc["type"], sc["name"], requirements, policies)

        tool_files: set[str] = set()
        if "uce_mcp" in conditions and client:
            t0 = time.perf_counter()
            parsed, tool_log = _run_agent_mcp(client, sc, schema_ctx, req_ctx, pol_ctx, inventory, profile)
            lat = (time.perf_counter() - t0) * 1000
            if raw_log:
                raw_log.write(json.dumps({
                    "scenario_id": sc["id"], "parsed": parsed, "tool_log": tool_log,
                    "latency_ms": lat,
                }, default=str) + "\n")
                raw_log.flush()
            for t in tool_log:
                if t["tool"] == "impact_analysis":
                    # Re-run once to capture files deterministically for metrics
                    inp = t.get("input") or {}
                    if inp.get("entity_type") == sc["type"] and inp.get("entity_name") == sc["name"]:
                        res = _execute_tool("impact_analysis", inp)
                        tool_files = files_from_mcp_result(res)
            if not tool_files:
                res = _execute_tool("impact_analysis", {"entity_type": sc["type"], "entity_name": sc["name"]})
                tool_files = files_from_mcp_result(res)
            row = _score_row(spec, sc, "uce_mcp", parsed, oracle_files, oracle_reqs, oracle_pols, tool_log, tool_files, lat)
            rows.append(row)
            print(f"  [uce_mcp] {sc['id']:32s} recall={row['file_recall']:.2f} tool={row['used_impact_analysis']}", flush=True)

        if "prompt_only" in conditions and client:
            t0 = time.perf_counter()
            parsed = _run_agent_prompt_only(client, sc, schema_ctx, req_ctx, pol_ctx, inventory, profile)
            lat = (time.perf_counter() - t0) * 1000
            row = _score_row(spec, sc, "prompt_only", parsed, oracle_files, oracle_reqs, oracle_pols, [], set(), lat)
            rows.append(row)
            print(f"  [prompt_only] {sc['id']:32s} recall={row['file_recall']:.2f}", flush=True)

    if rows:
        with (out_dir / "agent_mcp_results.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        summary = _summarize_agent_mcp(rows, spec.name)
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"  {spec.name} prompt_only recall={summary.get('prompt_only_file_recall')} "
              f"uce_mcp recall={summary.get('uce_mcp_file_recall')}", flush=True)
    if raw_log:
        raw_log.close()
    return rows


def _summarize_agent_mcp(rows: list[dict], repo: str) -> dict:
    out: dict[str, Any] = {"repo": repo}
    for cond in ("prompt_only", "uce_mcp"):
        sub = [r for r in rows if r["condition"] == cond]
        if not sub:
            continue
        n = len(sub)
        agg_f = [sum(r["file_tp"] for r in sub), sum(r["file_fp"] for r in sub), sum(r["file_fn"] for r in sub)]
        tp, fp, fn = agg_f
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        out[f"{cond}_file_precision"] = round(p, 4)
        out[f"{cond}_file_recall"] = round(r, 4)
        out[f"{cond}_file_f1"] = round(f1, 4)
        out[f"{cond}_incomplete_rate"] = round(sum(r["incomplete_plan"] for r in sub) / n, 4)
        if cond == "uce_mcp":
            out["uce_mcp_tool_use_rate"] = round(sum(r["used_impact_analysis"] for r in sub) / n, 4)
            tool_recalls = [r["mcp_tool_file_recall"] for r in sub if r.get("mcp_tool_file_recall") is not None]
            if tool_recalls:
                out["mcp_tool_output_file_recall"] = round(sum(tool_recalls) / len(tool_recalls), 4)
            adheres = [r["tool_adherence"] for r in sub if r.get("tool_adherence") is not None]
            if adheres:
                out["mean_tool_adherence"] = round(sum(adheres) / len(adheres), 4)
    if "prompt_only_file_recall" in out and "uce_mcp_file_recall" in out:
        out["file_recall_lift"] = round(out["uce_mcp_file_recall"] - out["prompt_only_file_recall"], 4)
    return out


def main() -> None:
    import logging
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    _load_env()
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--ingest", action="store_true", help="Wipe+ingest Neo4j per repo before MCP eval (recommended)")
    ap.add_argument("--no-auto-ingest", action="store_true", help="Do not auto-ingest when graph is empty")
    ap.add_argument("--condition", default="both", choices=["both", "prompt_only", "uce_mcp"])
    ap.add_argument("--reuse-prompt-only", action="store_true",
                    help="Load prompt_only rows from prior enforcement_eval CSV (skip duplicate API calls)")
    ap.add_argument("--prompt", default="neutral", choices=list(PROMPT_PROFILES.keys()),
                    help="Prompt profile: neutral (deployment-realistic) or strict (ablation)")
    args = ap.parse_args()
    profile = PROMPT_PROFILES[args.prompt]

    conditions = {"prompt_only", "uce_mcp"}
    if args.condition != "both":
        conditions = {args.condition}

    client = None
    if "uce_mcp" in conditions or (not args.reuse_prompt_only and "prompt_only" in conditions):
        client = AnthropicClient(max_tokens=16000)

    all_rows: list[dict] = []
    summaries: list[dict] = []
    for spec in repo_specs():
        if args.only and spec.name != args.only:
            continue
        rows = run_repo(
            spec, client, conditions, args.ingest,
            args.reuse_prompt_only, auto_ingest=not args.no_auto_ingest,
            profile=profile,
        )
        all_rows.extend(rows)
        if rows:
            summaries.append(_summarize_agent_mcp(rows, spec.name))

    if all_rows:
        pooled_summary = _pooled_agent_mcp(all_rows, prompt_profile=args.prompt)
        (OUT_ROOT / "per_repo_summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
        (OUT_ROOT / "pooled_summary.json").write_text(json.dumps(pooled_summary, indent=2), encoding="utf-8")
        with (OUT_ROOT / "all_scenarios.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
        _write_metrics_md(summaries, pooled_summary)
        print("\n=== POOLED agent+MCP eval ===")
        print(json.dumps(pooled_summary, indent=2))


def _pooled_agent_mcp(rows: list[dict], prompt_profile: str = "neutral") -> dict:
    pooled: dict[str, Any] = {
        "evaluation": "llm_agent_with_vs_without_uce_mcp_tools",
        "prompt_profile": prompt_profile,
        "n_repos": len({r["repo"] for r in rows}),
    }
    for cond in ("prompt_only", "uce_mcp"):
        sub = [r for r in rows if r["condition"] == cond]
        if not sub:
            continue
        tp = sum(r["file_tp"] for r in sub)
        fp = sum(r["file_fp"] for r in sub)
        fn = sum(r["file_fn"] for r in sub)
        p = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * rec / (p + rec) if p + rec else 0.0
        pooled[f"{cond}_n"] = len(sub)
        pooled[f"{cond}_file_precision"] = round(p, 4)
        pooled[f"{cond}_file_recall"] = round(rec, 4)
        pooled[f"{cond}_file_f1"] = round(f1, 4)
        pooled[f"{cond}_incomplete_plan_rate"] = round(sum(r["incomplete_plan"] for r in sub) / len(sub), 4)
        if cond == "uce_mcp":
            pooled["uce_mcp_tool_use_rate"] = round(
                sum(r["used_impact_analysis"] for r in sub) / len(sub), 4
            )
            tool_recalls = [r["mcp_tool_file_recall"] for r in sub if r.get("mcp_tool_file_recall") is not None]
            if tool_recalls:
                pooled["mcp_tool_output_file_recall"] = round(sum(tool_recalls) / len(tool_recalls), 4)
            adheres = [r["tool_adherence"] for r in sub if r.get("tool_adherence") is not None]
            if adheres:
                pooled["mean_tool_adherence"] = round(sum(adheres) / len(adheres), 4)
    if "prompt_only_file_recall" in pooled and "uce_mcp_file_recall" in pooled:
        pooled["file_recall_lift"] = round(pooled["uce_mcp_file_recall"] - pooled["prompt_only_file_recall"], 4)
    return pooled


def _write_metrics_md(summaries: list[dict], pooled: dict) -> None:
    lines = [
        f"# Agent + UCE MCP evaluation (prompt profile: {pooled.get('prompt_profile', 'neutral')})\n",
        "Compares the **same LLM agent** with vs without calling UCE MCP tools (`impact_analysis`).\n",
        "Scores the agent's **final declared plan** vs an independent blast-radius oracle.\n",
        "",
        "## Pooled\n",
        f"| condition | n | file P | file R | file F1 | incomplete plan rate |",
        f"|-----------|---|--------|--------|---------|----------------------|",
    ]
    for cond in ("prompt_only", "uce_mcp"):
        n = pooled.get(f"{cond}_n", 0)
        if not n:
            continue
        lines.append(
            f"| {cond} | {n} | {pooled.get(f'{cond}_file_precision', 0):.3f} | "
            f"{pooled.get(f'{cond}_file_recall', 0):.3f} | {pooled.get(f'{cond}_file_f1', 0):.3f} | "
            f"{pooled.get(f'{cond}_incomplete_plan_rate', 0):.1%} |"
        )
    if "file_recall_lift" in pooled:
        lines.append(f"\n**File recall lift (uce_mcp − prompt_only):** {pooled['file_recall_lift']:.3f}")
    if "uce_mcp_tool_use_rate" in pooled:
        lines.append(f"**Tool use rate:** {pooled['uce_mcp_tool_use_rate']:.1%}")
    if "mcp_tool_output_file_recall" in pooled:
        lines.append(
            f"**MCP tool output recall vs oracle:** {pooled['mcp_tool_output_file_recall']:.3f}"
        )
    if "mean_tool_adherence" in pooled:
        lines.append(f"**Agent adherence to tool file list:** {pooled['mean_tool_adherence']:.3f}")
    lines.append("\n## Per repo\n| repo | prompt_only R | uce_mcp R | lift | tool use |")
    lines.append("|------|---------------|-----------|------|----------|")
    for s in summaries:
        lines.append(
            f"| {s['repo']} | {s.get('prompt_only_file_recall', 'n/a')} | {s.get('uce_mcp_file_recall', 'n/a')} | "
            f"{s.get('file_recall_lift', 'n/a')} | {s.get('uce_mcp_tool_use_rate', 'n/a')} |"
        )
    (OUT_ROOT / "METRICS.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
