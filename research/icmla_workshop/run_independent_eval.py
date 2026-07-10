"""
Independent (non-circular) evaluation of UCE impact analysis on TalkAI.

Ground truth comes from `independent_oracle.py` (a from-scratch TS import resolver + governance
document parser), NOT from UCE's own Cypher queries. UCE and the baselines are all scored against
the same independent oracle, so UCE no longer scores 1.0 by construction.

Systems compared:
  - uce          : reasoning.impact_analysis.impact_analysis() over the Neo4j graph
  - naive_edit   : the do-nothing-tracer agent (schema file for schema changes; the file itself
                   for file changes; no governance awareness)
  - lexical      : token-frequency retrieval over the backend source corpus + lexical
                   requirement/policy matching (a non-trivial retrieval baseline)

Outputs (under results/):
  - independent_scenario_results.csv   (per-scenario, per-system: tp/fp/fn for files/reqs/policies)
  - tables/independent_overall_metrics.csv
  - independent_summary.json
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

# Make the uce package importable when run from the repo root.
import sys
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from uce.core.config import load_config
from uce.core.graph_db import GraphDB
from uce.reasoning import impact_analysis as impact_module

from independent_oracle import (
    TableSchema,
    build_import_graph,
    governance_oracle,
    independent_file_oracle,
    is_backend_file,
    normalize_repo_path,
    parse_policies,
    parse_requirements,
    parse_schema,
)

BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "results"
TABLES_DIR = RESULTS_DIR / "tables"
SYSTEMS = ("naive_edit", "lexical", "uce")


def prf(pred: set[str], truth: set[str]) -> tuple[int, int, int, float, float, float]:
    """Compute precision/recall/F1.

    If ``truth`` is empty the scenario has no ground truth and should be excluded
    from micro-averaged scoring.  We return all-zeros so the counts contribute
    zero to both numerators and denominators of the micro-average, preventing
    recall from being inflated to 1.0 on empty-truth scenarios.
    """
    if not truth:
        # No ground truth — exclude from scoring by contributing zeros everywhere.
        return 0, len(pred), 0, 0.0, 0.0, 0.0
    tp = len(pred & truth)
    fp = len(pred - truth)
    fn = len(truth - pred)
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * p * r / (p + r)) if (p + r) else 0.0
    return tp, fp, fn, p, r, f1


# ---------------------------------------------------------------------------
# Scenario generation (entities derived from the schema + import graph, not UCE)
# ---------------------------------------------------------------------------

def build_scenarios(schema: dict[str, TableSchema], import_graph, backend_prefixes):
    scenarios: list[dict] = []
    for sql_name, ts in sorted(schema.items()):
        scenarios.append({"id": f"TBL-{sql_name}", "type": "table", "name": sql_name})
        cols = sorted(ts.sql_to_prop.keys())[:2]
        for col in cols:
            scenarios.append(
                {"id": f"COL-{sql_name}-{col}", "type": "column", "name": f"{sql_name}.{col}"}
            )

    # Top backend files by independent import in-degree.
    indeg = {f: len(import_graph.imported_by.get(f, ())) for f in import_graph.files}
    backend_files = [f for f in import_graph.files if is_backend_file(f, backend_prefixes)]
    backend_files.sort(key=lambda f: (-indeg.get(f, 0), f))
    for f in backend_files[:6]:
        scenarios.append({"id": f"FIL-{f.replace('/', '_')}", "type": "file", "name": f})
    return scenarios


# ---------------------------------------------------------------------------
# System predictions
# ---------------------------------------------------------------------------

def predict_uce(graph: GraphDB, scenario: dict, backend_paths) -> dict:
    start = time.perf_counter()
    result = impact_module.impact_analysis(graph, scenario["type"], scenario["name"], backend_paths=backend_paths)
    latency_ms = (time.perf_counter() - start) * 1000.0
    files = set(result.get("affected_files") or [])
    if not files and isinstance(result.get("impact"), dict):
        files = set(result["impact"].get("affected_files") or [])
    return {
        "files": {normalize_repo_path(f) for f in files},
        "reqs": set(result.get("violated_requirements") or []),
        "policies": set(result.get("enforced_policies") or []),
        "latency_ms": latency_ms,
    }


def predict_naive(scenario: dict, schema_rel: str) -> dict:
    start = time.perf_counter()
    if scenario["type"] == "file":
        files = {normalize_repo_path(scenario["name"])}
    else:
        files = {normalize_repo_path(schema_rel)}
    latency_ms = (time.perf_counter() - start) * 1000.0
    return {"files": files, "reqs": set(), "policies": set(), "latency_ms": latency_ms}


def _tokens(scenario: dict) -> list[str]:
    if scenario["type"] == "column":
        tbl, col = scenario["name"].split(".", 1)
        raw = f"{tbl} {col}"
    elif scenario["type"] == "file":
        raw = Path(scenario["name"]).stem.replace("_", " ").replace("-", " ")
    else:
        raw = scenario["name"]
    return sorted({t.lower() for t in re.split(r"[^A-Za-z0-9]+", raw) if t})


def predict_lexical(scenario, import_graph, backend_prefixes, requirements, policies, oracle_file_count=None) -> dict:
    # oracle_file_count is intentionally ignored — see k=5 below (no oracle leakage).
    start = time.perf_counter()
    toks = _tokens(scenario)
    scored = []
    for rel in import_graph.files:
        if not is_backend_file(rel, backend_prefixes):
            continue
        text = import_graph.text(rel).lower()
        score = sum(text.count(t) for t in toks)
        if score > 0:
            scored.append((rel, score))
    scored.sort(key=lambda x: (-x[1], x[0]))
    # Use a fixed retrieval budget (k=5) so the lexical baseline cannot exploit
    # knowledge of the oracle's size.  k=oracle_file_count is oracle leakage.
    k = 5
    files = {normalize_repo_path(r) for r, _ in scored[:k]}

    req_scored = []
    for r in requirements:
        text = r.description.lower()
        score = sum(text.count(t) for t in toks)
        if score > 0:
            req_scored.append((r.req_id, score))
    req_scored.sort(key=lambda x: (-x[1], x[0]))
    reqs = {rid for rid, _ in req_scored[: max(1, min(4, len(req_scored)))]}
    pols = {p.policy_id for p in policies if p.enforces & reqs}
    latency_ms = (time.perf_counter() - start) * 1000.0
    return {"files": files, "reqs": reqs, "policies": pols, "latency_ms": latency_ms}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(config_path: str) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    TABLES_DIR.mkdir(parents=True, exist_ok=True)

    config = load_config(config_path)
    project_root = Path(config.project_root)
    backend_prefixes = tuple(
        normalize_repo_path(p).lower() for p in (config.paths.backend or ()) if normalize_repo_path(p)
    )

    schema_path = project_root / "src" / "db" / "schema.ts"
    schema = parse_schema(schema_path)
    schema_rel = schema_path.relative_to(project_root).as_posix()
    schema_rel_candidates = {schema_rel, "src/db/index.ts"}

    alias_map = dict(config.aliases) if config.aliases else {"@/": "src/"}
    if "@/" not in alias_map:
        alias_map["@/"] = "src/"

    import_graph = build_import_graph(
        project_root=project_root,
        code_dirs=config.paths.code or ("src",),
        alias_map=alias_map,
        ignore_dirs=config.ignore,
    )

    requirements = parse_requirements(project_root / "src" / "requirements", schema)
    policies = parse_policies(project_root / "src" / "policies")

    graph = GraphDB(config.neo4j.uri, config.neo4j.user, config.neo4j.password)
    try:
        scenarios = build_scenarios(schema, import_graph, backend_prefixes)
        rows: list[dict] = []

        for sc in scenarios:
            truth_files = independent_file_oracle(
                import_graph, schema, schema_rel_candidates, sc["type"], sc["name"], backend_prefixes
            )
            truth_reqs, truth_pols = governance_oracle(sc["type"], sc["name"], requirements, policies)
            oracle_file_count = len(truth_files)

            preds = {
                "uce": predict_uce(graph, sc, config.paths.backend),
                "naive_edit": predict_naive(sc, schema_rel),
                "lexical": predict_lexical(
                    sc, import_graph, backend_prefixes, requirements, policies, oracle_file_count
                ),
            }

            for system, pred in preds.items():
                f_tp, f_fp, f_fn, f_p, f_r, f_f1 = prf(pred["files"], truth_files)
                r_tp, r_fp, r_fn, r_p, r_r, r_f1 = prf(pred["reqs"], truth_reqs)
                p_tp, p_fp, p_fn, p_p, p_r, p_f1 = prf(pred["policies"], truth_pols)
                rows.append({
                    "scenario_id": sc["id"], "entity_type": sc["type"], "entity_name": sc["name"],
                    "system": system,
                    "oracle_file_count": oracle_file_count,
                    "oracle_req_count": len(truth_reqs),
                    "oracle_pol_count": len(truth_pols),
                    "file_tp": f_tp, "file_fp": f_fp, "file_fn": f_fn,
                    "file_precision": f_p, "file_recall": f_r, "file_f1": f_f1,
                    "req_tp": r_tp, "req_fp": r_fp, "req_fn": r_fn,
                    "req_precision": r_p, "req_recall": r_r, "req_f1": r_f1,
                    "pol_tp": p_tp, "pol_fp": p_fp, "pol_fn": p_fn,
                    "pol_precision": p_p, "pol_recall": p_r, "pol_f1": p_f1,
                    # paired binary outcome: did the system catch >=1 true violated requirement?
                    "req_caught_any": int(r_tp > 0) if len(truth_reqs) > 0 else -1,
                    "latency_ms": pred["latency_ms"],
                })

        import csv
        out_csv = RESULTS_DIR / "independent_scenario_results.csv"
        with out_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

        summary = _summarize(rows)
        (RESULTS_DIR / "independent_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        _write_overall_table(summary)

        print(f"Scenarios: {len(scenarios)} | rows: {len(rows)}")
        print(f"Import graph: {len(import_graph.files)} files, "
              f"{sum(len(v) for v in import_graph.imports.values())} import edges")
        print("\n=== MICRO-AVERAGED METRICS (scored vs INDEPENDENT oracle) ===")
        for s in summary["overall"]:
            print(f"  {s['system']:11s}  file_F1={s['file_f1']:.3f}  "
                  f"req_F1={s['requirement_f1']:.3f}  pol_F1={s['policy_f1']:.3f}  "
                  f"file_recall={s['file_recall']:.3f}  latency_ms={s['latency_ms_mean']:.2f}")
        print(f"\nWrote: {out_csv}")
    finally:
        graph.close()


def _micro(rows, system, prefix):
    sub = [r for r in rows if r["system"] == system]
    tp = sum(r[f"{prefix}_tp"] for r in sub)
    fp = sum(r[f"{prefix}_fp"] for r in sub)
    fn = sum(r[f"{prefix}_fn"] for r in sub)
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * p * r / (p + r)) if (p + r) else 0.0
    return p, r, f1, tp, fp, fn


def _summarize(rows) -> dict:
    overall = []
    for system in SYSTEMS:
        fp_, fr_, ff_, *_ = _micro(rows, system, "file")
        rp_, rr_, rf_, *_ = _micro(rows, system, "req")
        pp_, pr_, pf_, *_ = _micro(rows, system, "pol")
        sub = [r for r in rows if r["system"] == system]
        lat = sum(r["latency_ms"] for r in sub) / len(sub) if sub else 0.0
        overall.append({
            "system": system, "n_scenarios": len(sub),
            "file_precision": fp_, "file_recall": fr_, "file_f1": ff_,
            "requirement_precision": rp_, "requirement_recall": rr_, "requirement_f1": rf_,
            "policy_precision": pp_, "policy_recall": pr_, "policy_f1": pf_,
            "latency_ms_mean": lat,
        })
    return {"overall": overall}


def _write_overall_table(summary) -> None:
    import csv
    path = TABLES_DIR / "independent_overall_metrics.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(summary["overall"][0].keys()))
        writer.writeheader()
        writer.writerows(summary["overall"])


def main() -> None:
    ap = argparse.ArgumentParser(description="Independent (non-circular) UCE evaluation.")
    ap.add_argument("--config", default=str(Path("F:/UIC/CS540/Projects/talkai-main/config.yaml")))
    args = ap.parse_args()
    run(args.config)


if __name__ == "__main__":
    main()
