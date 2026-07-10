"""
Live re-validation of the propose_change MCP tool.

The original enforcement-eval result (100% catch rate, 0.9% false-gate rate across
talkai/melodi/expenses/spark, 114 scenarios total — see ENFORCEMENT_RESULTS.md) was computed
by run_multi_repo_enforcement.py, which duplicated the gate decision math inline rather than
calling a real MCP tool. propose_change() productizes that exact mechanism as a shipped tool.

This script proves the SHIPPED TOOL reproduces the validated result: it replays the SAME
already-captured real Claude Sonnet 4.5 responses (results/enforcement_eval/{repo}/raw.jsonl —
no new API calls, no new cost) through the actual uce.server.mcp_server.propose_change() function,
live, against a freshly (re)ingested Neo4j graph for each repo.

Usage:
  python replay_propose_change_live.py --ingest              # all 4 repos
  python replay_propose_change_live.py --only talkai --ingest
"""
from __future__ import annotations

import csv
import json
import sys
from dataclasses import replace
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
REPO_ROOT = BASE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from uce.core.config import load_config
from uce.core.graph_db import GraphDB
import uce.server.mcp_server as srv

from independent_oracle import (
    build_import_graph, governance_oracle, parse_policies, parse_requirements,
    parse_schema, scenario_seeds, reverse_reachable, normalize_repo_path,
)
from run_multi_repo_enforcement import repo_specs, build_scenarios_enforcement, _ingest, _collect_repo_files
from run_anthropic_baseline import _extract_json, REQ_ID_RE, _collect_ids
from run_rbac_complexity import parse_rules_from_text
from uce.core.rbac import evaluate_rules

OUT_ROOT = BASE_DIR / "results" / "propose_change_live_eval"


def _load_raw_responses(repo_name: str) -> list[dict]:
    path = BASE_DIR / "results" / "enforcement_eval" / repo_name / "raw.jsonl"
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def run_repo_live(spec, ingest: bool) -> tuple[list[dict], dict]:
    if ingest:
        print(f"  ingesting {spec.name}...", flush=True)
        _ingest(spec)

    config = load_config(str(spec.config_path))

    # RBAC: only talkai ships an RBAC doc in this corpus. Enable RBAC for the live test and
    # ingest its rules into the graph so authorize_change() (which reads AuthorityRule nodes
    # from the graph, unlike the original script's standalone evaluate_rules() call) has real
    # rules to evaluate against — this exercises propose_change's RBAC integration for real,
    # not just its blast-radius/governance dimensions.
    has_rbac = bool(spec.rbac_md and spec.rbac_md.exists())
    graph = GraphDB(config.neo4j.uri, config.neo4j.user, config.neo4j.password)
    rules: list = []

    if has_rbac:
        rbac_text = spec.rbac_md.read_text(encoding="utf-8")
        rules = parse_rules_from_text(rbac_text)
        rule_dicts = [
            {
                "policy_id": "RBAC_DEMO_001", "rule_id": r.rule_id, "operation": r.operation,
                "path_pattern": r.path_pattern, "min_role": r.min_role, "effect": r.effect,
                "source_priority": r.source_priority,
            }
            for r in rules
        ]
        graph.replace_authority_rules(rule_dicts)
        config = replace(
            config,
            rbac=replace(config.rbac, enabled=True, enforce_mode="enforced", deny_default=True),
        )

    # Point the live server module at this repo's graph/config for the duration of this run.
    srv._CONFIG = config
    srv._DB = graph
    srv._GATE_TOKEN_STORE = None
    srv._RBAC_CACHE = None
    srv._RBAC_CACHE_EXPIRES = 0.0

    schema = parse_schema(spec.schema_paths, kind=spec.schema_kind)
    import_graph = build_import_graph(spec.root, spec.code_dirs, spec.alias_map, spec.ignore)
    requirements_docs = (
        parse_requirements(spec.requirements_dir, schema)
        if spec.requirements_dir and spec.requirements_dir.exists() else []
    )
    policies_docs = (
        parse_policies(spec.policies_dir)
        if spec.policies_dir and spec.policies_dir.exists() else []
    )

    scenarios = build_scenarios_enforcement(schema, import_graph)
    raw_rows = _load_raw_responses(spec.name)

    if len(raw_rows) != len(scenarios):
        print(
            f"  WARNING: {spec.name} scenario count mismatch: "
            f"{len(scenarios)} regenerated vs {len(raw_rows)} captured raw rows"
        )

    results: list[dict] = []
    for sc, raw_row in zip(scenarios, raw_rows):
        if raw_row.get("scenario_id") != sc["id"]:
            print(
                f"  WARNING: scenario id mismatch at this position: "
                f"regenerated={sc['id']} captured={raw_row.get('scenario_id')}"
            )

        raw_text = raw_row.get("raw", "")
        parsed = _extract_json(raw_text) if raw_text else {}
        declared_files = sorted(_collect_repo_files(parsed.get("files_to_edit") or []))
        declared_reqs = sorted(_collect_ids(parsed.get("affected_requirements") or [], REQ_ID_RE))

        try:
            resp = srv.propose_change(
                operation="write",
                entity_type=sc["type"],
                entity_name=sc["name"],
                files_to_edit=declared_files,
                declared_requirements=declared_reqs,
                strict=True,
            )
        except Exception as exc:  # pragma: no cover - defensive, surfaced in the report
            resp = {
                "decision": "error",
                "error": str(exc),
                "rbac": {"allowed": None},
                "blast_radius": {"missed_files": [], "actual_files": []},
                "governance": {"silent_requirements": []},
            }

        # Independent oracle (does not read propose_change's own output) for false-gate scoring.
        # oracle_has_issue mirrors run_multi_repo_enforcement.py's definition EXACTLY (non-
        # subtractive: "does the oracle find real content for this entity at all", not "did the
        # agent miss something") so this false_gate_rate is directly comparable to the original
        # 0.9% pooled result rather than a differently-defined metric.
        seeds = scenario_seeds(import_graph, schema, spec.schema_rel_candidates, sc["type"], sc["name"])
        oracle_files = {normalize_repo_path(f) for f in reverse_reachable(import_graph, seeds)}
        oracle_reqs, _oracle_pols = governance_oracle(sc["type"], sc["name"], requirements_docs, policies_docs)

        if sc["type"] == "file":
            rbac_probe_path = normalize_repo_path(sc["name"])
        else:
            rbac_probe_path = spec.rbac_schema_path
        if has_rbac:
            rbac_oracle_allowed = evaluate_rules(
                operation="write", normalized_path=rbac_probe_path,
                principal_role="editor", rules=rules, deny_default=True,
            ).allowed
        else:
            rbac_oracle_allowed = True

        oracle_has_issue = bool(
            oracle_files
            or (bool(requirements_docs) and oracle_reqs)
            or (has_rbac and not rbac_oracle_allowed)
        )

        gate_fired = resp["decision"] != "allow"
        false_gate = gate_fired and not oracle_has_issue

        results.append(
            {
                "repo": spec.name,
                "scenario_id": sc["id"],
                "entity_type": sc["type"],
                "decision": resp["decision"],
                "rbac_allowed": resp.get("rbac", {}).get("allowed"),
                "missed_files_count": len(resp.get("blast_radius", {}).get("missed_files", [])),
                "silent_requirements_count": len(resp.get("governance", {}).get("silent_requirements", [])),
                "declared_files_count": len(declared_files),
                "actual_files_count": len(resp.get("blast_radius", {}).get("actual_files", [])),
                "gate_fired": gate_fired,
                "oracle_has_issue": oracle_has_issue,
                "false_gate": false_gate,
            }
        )

    graph.close()

    n = len(results) or 1
    summary = {
        "repo": spec.name,
        "n_scenarios": len(results),
        "catch_rate": round(sum(r["gate_fired"] for r in results) / n, 4),
        "false_gate_rate": round(sum(r["false_gate"] for r in results) / n, 4),
        "mean_missed_files": round(sum(r["missed_files_count"] for r in results) / n, 2),
        "has_rbac": has_rbac,
        "rbac_denies_seen": sum(1 for r in results if r["rbac_allowed"] is False),
    }
    return results, summary


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="talkai|melodi|expenses|spark")
    ap.add_argument("--ingest", action="store_true")
    args = ap.parse_args()

    import logging
    logging.getLogger("neo4j").setLevel(logging.ERROR)

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    selected = [s for s in repo_specs() if args.only is None or s.name == args.only]

    all_rows: list[dict] = []
    per_repo: list[dict] = []
    for spec in selected:
        print(f"\n=== {spec.name} (live propose_change replay) ===", flush=True)
        rows, summary = run_repo_live(spec, ingest=args.ingest)
        all_rows.extend(rows)
        per_repo.append(summary)
        print(json.dumps(summary, indent=2))

    n = len(all_rows) or 1
    pooled = {
        "n_scenarios": len(all_rows),
        "catch_rate": round(sum(r["gate_fired"] for r in all_rows) / n, 4),
        "false_gate_rate": round(sum(r["false_gate"] for r in all_rows) / n, 4),
        "mean_missed_files": round(sum(r["missed_files_count"] for r in all_rows) / n, 2),
        "repos": [s["repo"] for s in per_repo],
        "tool": "uce.server.mcp_server.propose_change (live, real MCP tool)",
        "note": "Replays the same captured Claude Sonnet 4.5 responses used for "
                "ENFORCEMENT_RESULTS.md through the shipped tool instead of inline research logic.",
    }

    (OUT_ROOT / "per_repo_summary.json").write_text(json.dumps(per_repo, indent=2), encoding="utf-8")
    (OUT_ROOT / "pooled_summary.json").write_text(json.dumps(pooled, indent=2), encoding="utf-8")
    if all_rows:
        with (OUT_ROOT / "all_scenarios.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
            writer.writeheader()
            writer.writerows(all_rows)

    print("\n=== POOLED (live propose_change tool, all repos) ===")
    print(json.dumps(pooled, indent=2))


if __name__ == "__main__":
    main()
