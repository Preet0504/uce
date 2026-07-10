"""
Ablation study for UCE impact analysis, scored against the INDEPENDENT oracle.

Impact-propagation ablation (file dimension):
  - direct        : only files that directly reference the entity (USES_TABLE / REFERENCES_COLUMN,
                    or the file itself for file scenarios). No propagation.
  - +transitive   : direct + reverse import closure (files that transitively import the direct set).
  - +callchain    : direct + transitive + CALLS-edge upstream propagation (full UCE).

This isolates the contribution of each graph-traversal mechanism to precision/recall and shows the
precision/recall trade-off that drives the design.

Reads the live Neo4j graph (must be ingested) + independent oracle.
Writes: results/tables/ablation_impact.csv and results/ablation.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from uce.core.config import load_config
from uce.core.graph_db import GraphDB
from uce.reasoning import impact_analysis as im

from independent_oracle import (
    build_import_graph,
    independent_file_oracle,
    is_backend_file,
    normalize_repo_path,
    parse_schema,
)
from run_independent_eval import build_scenarios, prf

BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "results"
TABLES_DIR = RESULTS_DIR / "tables"

VARIANTS = ("direct", "+transitive", "+callchain")


def _direct_files(graph: GraphDB, scenario: dict, backend_prefixes) -> list[str]:
    et, name = scenario["type"], scenario["name"]
    direct: set[str] = set()
    if et == "table":
        rows = graph.run(im.TABLE_IMPACT_QUERY, table=name)
        if rows:
            direct.update(p for p in (rows[0].get("table_files") or []) if p)
            direct.update(p for p in (rows[0].get("column_files") or []) if p)
    elif et == "column":
        tbl, col = name.split(".", 1)
        rows = graph.run(im.COLUMN_IMPACT_QUERY, table=tbl, column=col)
        if rows:
            direct.update(p for p in (rows[0].get("files") or []) if p)
    elif et == "file":
        direct.add(normalize_repo_path(name))
    return sorted(direct)


def _filter_backend(paths, backend_prefixes) -> set[str]:
    return {normalize_repo_path(p) for p in paths if is_backend_file(p, backend_prefixes)}


def compute_variant(graph, scenario, variant, backend_prefixes) -> set[str]:
    direct = _direct_files(graph, scenario, backend_prefixes)
    files = set(direct)
    if variant in ("+transitive", "+callchain"):
        files |= set(im._reverse_import_closure(graph, direct))
    if variant == "+callchain":
        files |= set(im._call_chain_upstream(graph, sorted(files)))
    return _filter_backend(files, backend_prefixes)


def run(config_path: str) -> None:
    config = load_config(config_path)
    project_root = Path(config.project_root)
    backend_prefixes = tuple(
        normalize_repo_path(p).lower() for p in (config.paths.backend or ()) if normalize_repo_path(p)
    )

    schema = parse_schema(project_root / "src" / "db" / "schema.ts")
    schema_rel_candidates = {"src/db/schema.ts", "src/db/index.ts"}
    alias_map = dict(config.aliases) if config.aliases else {"@/": "src/"}
    alias_map.setdefault("@/", "src/")
    import_graph = build_import_graph(project_root, config.paths.code or ("src",), alias_map, config.ignore)

    graph = GraphDB(config.neo4j.uri, config.neo4j.user, config.neo4j.password)
    try:
        scenarios = build_scenarios(schema, import_graph, backend_prefixes)
        agg = {v: {"tp": 0, "fp": 0, "fn": 0} for v in VARIANTS}
        per_rows = []
        for sc in scenarios:
            truth = independent_file_oracle(
                import_graph, schema, schema_rel_candidates, sc["type"], sc["name"], backend_prefixes
            )
            for v in VARIANTS:
                pred = compute_variant(graph, sc, v, backend_prefixes)
                tp, fp, fn, p, r, f1 = prf(pred, truth)
                agg[v]["tp"] += tp; agg[v]["fp"] += fp; agg[v]["fn"] += fn
                per_rows.append({"scenario_id": sc["id"], "variant": v,
                                 "tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f1})

        summary = []
        for v in VARIANTS:
            tp, fp, fn = agg[v]["tp"], agg[v]["fp"], agg[v]["fn"]
            p = tp / (tp + fp) if (tp + fp) else 0.0
            r = tp / (tp + fn) if (tp + fn) else 0.0
            f1 = (2 * p * r / (p + r)) if (p + r) else 0.0
            summary.append({"variant": v, "precision": round(p, 4), "recall": round(r, 4),
                            "f1": round(f1, 4), "tp": tp, "fp": fp, "fn": fn})

        TABLES_DIR.mkdir(parents=True, exist_ok=True)
        import csv
        with (TABLES_DIR / "ablation_impact.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(summary[0].keys())); w.writeheader(); w.writerows(summary)
        (RESULTS_DIR / "ablation.json").write_text(json.dumps({"impact_ablation": summary}, indent=2), encoding="utf-8")

        print("=== Impact-propagation ablation (file dimension, vs independent oracle) ===")
        print(f"{'variant':14s} {'precision':>10s} {'recall':>8s} {'f1':>8s}   (micro-averaged)")
        for s in summary:
            print(f"{s['variant']:14s} {s['precision']:>10.3f} {s['recall']:>8.3f} {s['f1']:>8.3f}")
    finally:
        graph.close()


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "F:/UIC/CS540/Projects/talkai-main/config.yaml"
    run(cfg)
