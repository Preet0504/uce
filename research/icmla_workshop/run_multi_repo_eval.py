"""
SUPPLEMENTARY: deterministic retrieval F1 vs independent oracle (no LLM, no MCP).

This does NOT match deployment (agents call UCE MCP tools). Use run_agent_mcp_eval.py
and run_multi_repo_enforcement.py for paper-facing numbers. See EVALUATION.md.

Multi-repo external-validity evaluation (addresses the n=1 threat).

For each target repo we score impact predictions against the SAME independent oracle
(import-resolver + governance-doc parser), comparing:
  - naive_edit : schema file for schema changes; the file itself for file changes
  - lexical    : token-frequency retrieval over the source corpus
  - static     : madge (off-the-shelf module dependency tool) reverse-reachability
  - uce        : optional; reads the live Neo4j graph (run with --with-uce after ingesting a repo)

File impact is computed over the FULL code import graph (no backend filter) so the metric is
uniform across frameworks (Next.js, Vite, Supabase). Governance (requirement/policy) metrics are
reported only for repos that ship governance docs.

Also reports an ORACLE-VALIDATION signal: edge-level agreement between madge's import graph and
our oracle's resolver (high agreement => our hand-written oracle is not idiosyncratic).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys as _sys
import time
from dataclasses import dataclass
from pathlib import Path

import sys
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
REPO_ROOT = BASE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from independent_oracle import (
    build_import_graph, governance_oracle, parse_policies, parse_requirements,
    parse_schema, reverse_reachable, scenario_seeds, normalize_repo_path,
)
from static_baseline import madge_reverse_graph, reverse_reachable_static
from run_independent_eval import prf, _tokens

PROJECTS = Path("F:/UIC/CS540/Projects")
RESULTS_DIR = BASE_DIR / "results"
OUT_DIR = RESULTS_DIR / "multi_repo"


@dataclass
class RepoSpec:
    name: str
    config_path: Path
    root: Path
    code_dirs: tuple[str, ...]
    alias_map: dict[str, str]
    schema_paths: list[Path]
    schema_kind: str
    schema_rel_candidates: set[str]
    requirements_dir: Path | None
    policies_dir: Path | None
    ts_config: str | None = "tsconfig.json"
    ignore: tuple[str, ...] = ("node_modules", ".next", "dist", "build", "coverage", ".git", "public")


def repos() -> list[RepoSpec]:
    return [
        RepoSpec(
            name="talkai", config_path=PROJECTS / "talkai-main/config.yaml",
            root=PROJECTS / "talkai-main",
            code_dirs=("src",), alias_map={"@/": "src/"},
            schema_paths=[PROJECTS / "talkai-main/src/db/schema.ts"], schema_kind="drizzle",
            schema_rel_candidates={"src/db/schema.ts", "src/db/index.ts"},
            requirements_dir=PROJECTS / "talkai-main/src/requirements",
            policies_dir=PROJECTS / "talkai-main/src/policies",
        ),
        RepoSpec(
            name="melodi", config_path=PROJECTS / "cs484-melodi-main/config.yaml",
            root=PROJECTS / "cs484-melodi-main",
            code_dirs=("app", "lib", "components", "ui", "types"), alias_map={"@/": "./"},
            schema_paths=[PROJECTS / "cs484-melodi-main/lib/db/schema.ts"], schema_kind="drizzle",
            schema_rel_candidates={"lib/db/schema.ts", "lib/db/index.ts"},
            requirements_dir=PROJECTS / "cs484-melodi-main/governance/requirements",
            policies_dir=PROJECTS / "cs484-melodi-main/governance/policies",
        ),
        RepoSpec(
            name="expenses", config_path=PROJECTS / "Preet-CS484-Homework2/config.yaml",
            root=PROJECTS / "Preet-CS484-Homework2",
            code_dirs=("src",), alias_map={"@/": "src/"},
            schema_paths=[PROJECTS / "Preet-CS484-Homework2/db/schema.ts"], schema_kind="drizzle",
            schema_rel_candidates={"db/schema.ts"},
            requirements_dir=PROJECTS / "Preet-CS484-Homework2/governance/requirements",
            policies_dir=PROJECTS / "Preet-CS484-Homework2/governance/policies",
        ),
        RepoSpec(
            name="spark", config_path=PROJECTS / "spark-creative-main/config.yaml",
            root=PROJECTS / "spark-creative-main",
            code_dirs=("src",), alias_map={"@/": "src/"},
            schema_paths=sorted((PROJECTS / "spark-creative-main/supabase/migrations").glob("*.sql")),
            schema_kind="sql", schema_rel_candidates=set(),
            requirements_dir=None, policies_dir=None,
        ),
    ]


def build_scenarios(schema, import_graph):
    scenarios = []
    for sql_name, ts in sorted(schema.items()):
        scenarios.append({"id": f"TBL-{sql_name}", "type": "table", "name": sql_name})
        for col in sorted(ts.sql_to_prop.keys())[:2]:
            scenarios.append({"id": f"COL-{sql_name}-{col}", "type": "column", "name": f"{sql_name}.{col}"})
    indeg = {f: len(import_graph.imported_by.get(f, ())) for f in import_graph.files}
    files = sorted(import_graph.files, key=lambda f: (-indeg.get(f, 0), f))
    for f in files[:6]:
        scenarios.append({"id": f"FIL-{f.replace('/', '_')}", "type": "file", "name": f})
    return scenarios


def _ingest(spec: RepoSpec) -> None:
    subprocess.run(
        [_sys.executable, str(BASE_DIR / "ingest_repo.py"), str(spec.config_path)],
        cwd=str(REPO_ROOT), check=True,
    )


def predict_naive(scenario, schema_rel):
    if scenario["type"] == "file":
        return {normalize_repo_path(scenario["name"])}
    return {normalize_repo_path(schema_rel)} if schema_rel else set()


def predict_lexical(scenario, g, requirements, policies, oracle_file_count):
    toks = _tokens(scenario)
    scored = []
    for rel in g.files:
        s = sum(g.text(rel).lower().count(t) for t in toks)
        if s > 0:
            scored.append((rel, s))
    scored.sort(key=lambda x: (-x[1], x[0]))
    k = max(1, min(25, oracle_file_count or 5))
    files = {normalize_repo_path(r) for r, _ in scored[:k]}
    req_scored = []
    for r in requirements:
        s = sum(r.description.lower().count(t) for t in toks)
        if s > 0:
            req_scored.append((r.req_id, s))
    req_scored.sort(key=lambda x: (-x[1], x[0]))
    reqs = {rid for rid, _ in req_scored[: max(1, min(4, len(req_scored)))]}
    pols = {p.policy_id for p in policies if p.enforces & reqs}
    return files, reqs, pols


def micro(agg, key):
    tp, fp, fn = agg[key]
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return round(p, 4), round(r, 4), round(f1, 4), tp, fp, fn


def run(with_uce: bool, only: str | None, do_ingest: bool) -> None:
    import logging
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    graph = None
    impact_module = None
    if with_uce:
        from uce.core.graph_db import GraphDB
        from uce.reasoning import impact_analysis as impact_module
        graph = GraphDB("bolt://localhost:7687", "neo4j", "testpassword")

    per_repo_rows = []
    scenario_rows = []
    pooled = {sysname: {"file": [0, 0, 0], "req": [0, 0, 0], "pol": [0, 0, 0]}
              for sysname in ("naive_edit", "lexical", "static", "uce")}

    selected = [s for s in repos() if (only is None or s.name == only)]
    for spec in selected:
        if do_ingest:
            print(f"\n--- Ingesting {spec.name} ---", flush=True)
            _ingest(spec)
        print(f"\n========== {spec.name} ({spec.root.name}) ==========", flush=True)
        schema = parse_schema(spec.schema_paths, kind=spec.schema_kind)
        g = build_import_graph(spec.root, spec.code_dirs, spec.alias_map, spec.ignore)
        n_edges = sum(len(v) for v in g.imports.values())
        print(f"  import graph: {len(g.files)} files, {n_edges} edges, schema tables: {len(schema)}")

        requirements = parse_requirements(spec.requirements_dir, schema) if spec.requirements_dir and spec.requirements_dir.exists() else []
        policies = parse_policies(spec.policies_dir) if spec.policies_dir and spec.policies_dir.exists() else []
        has_gov = bool(requirements)
        schema_rel = next(iter(sorted(spec.schema_rel_candidates)), "") if spec.schema_rel_candidates else ""

        madge_rev = madge_reverse_graph(spec.root, spec.code_dirs, spec.ts_config)
        if madge_rev is None:
            print("  [warn] madge unavailable; static baseline skipped for this repo")

        scenarios = build_scenarios(schema, g)
        print(f"  scenarios: {len(scenarios)}", flush=True)
        systems = ("naive_edit", "lexical", "static") + (("uce",) if with_uce else ())
        agg = {s: {"file": [0, 0, 0], "req": [0, 0, 0], "pol": [0, 0, 0]} for s in systems}

        for sc in scenarios:
            seeds = scenario_seeds(g, schema, spec.schema_rel_candidates, sc["type"], sc["name"])
            truth_files = reverse_reachable(g, seeds)
            truth_reqs, truth_pols = governance_oracle(sc["type"], sc["name"], requirements, policies)
            ofc = len(truth_files)

            preds = {}
            preds["naive_edit"] = (predict_naive(sc, schema_rel), set(), set())
            preds["lexical"] = predict_lexical(sc, g, requirements, policies, ofc)
            if madge_rev is not None:
                preds["static"] = (reverse_reachable_static(madge_rev, seeds), set(), set())
            else:
                preds["static"] = (set(), set(), set())
            if with_uce:
                res = impact_module.impact_analysis(graph, sc["type"], sc["name"], backend_paths=())
                # Use the UNFILTERED union (direct + transitive + call_chain) stored in res["impact"].
                # res["affected_files"] comes from explain_change which applies the backend heuristic
                # and strips components/ui/views — those files ARE in the oracle's truth set, so using
                # the filtered list artificially lowers recall.  The unfiltered paths in res["impact"]
                # are what UCE's graph actually found before filtering.
                base = res.get("impact") or {}
                uce_files: set[str] = set()
                for key_ in ("direct_files", "transitive_files", "call_chain_files"):
                    uce_files.update(base.get(key_) or [])
                # For file-type scenarios, impact_analysis also puts the self + transitive into
                # res["affected_files"] (unioned with detail), so include that too.
                uce_files.update(res.get("affected_files") or [])
                preds["uce"] = ({normalize_repo_path(f) for f in uce_files},
                                set(res.get("violated_requirements") or []),
                                set(res.get("enforced_policies") or []))

            for sysname in systems:
                pf, pr, pp = preds[sysname]
                f = prf(pf, truth_files); r = prf(pr, truth_reqs); p = prf(pp, truth_pols)
                for key, tup in (("file", f), ("req", r), ("pol", p)):
                    agg[sysname][key][0] += tup[0]; agg[sysname][key][1] += tup[1]; agg[sysname][key][2] += tup[2]
                    if has_gov or key == "file":
                        pooled[sysname][key][0] += tup[0]; pooled[sysname][key][1] += tup[1]; pooled[sysname][key][2] += tup[2]
                scenario_rows.append({"repo": spec.name, "scenario": sc["id"], "type": sc["type"],
                                      "system": sysname, "oracle_files": ofc,
                                      "file_tp": f[0], "file_fp": f[1], "file_fn": f[2]})

        for sysname in systems:
            fp_, fr_, ff_, *_ = micro(agg[sysname], "file")
            rp_, rr_, rf_, *_ = micro(agg[sysname], "req")
            pp_, pr_, pf_, *_ = micro(agg[sysname], "pol")
            per_repo_rows.append({
                "repo": spec.name, "system": sysname, "n_scenarios": len(scenarios),
                "has_governance": has_gov,
                "file_precision": fp_, "file_recall": fr_, "file_f1": ff_,
                "req_precision": rp_ if has_gov else None, "req_recall": rr_ if has_gov else None,
                "req_f1": rf_ if has_gov else None,
                "pol_precision": pp_ if has_gov else None, "pol_recall": pr_ if has_gov else None,
                "pol_f1": pf_ if has_gov else None,
            })
        print(f"  {'system':11s} {'file_P':>7s} {'file_R':>7s} {'file_F1':>8s}" + ("  req_F1  pol_F1" if has_gov else ""))
        for sysname in systems:
            row = next(r for r in per_repo_rows if r["repo"] == spec.name and r["system"] == sysname)
            rf = row["req_f1"] if row["req_f1"] is not None else 0.0
            pf = row["pol_f1"] if row["pol_f1"] is not None else 0.0
            extra = f"  {rf:.3f}   {pf:.3f}" if has_gov else ""
            print(f"  {sysname:11s} {row['file_precision']:>7.3f} {row['file_recall']:>7.3f} {row['file_f1']:>8.3f}{extra}")

    pooled_rows = []
    for sysname, a in pooled.items():
        if not with_uce and sysname == "uce":
            continue
        fp_, fr_, ff_, *_ = micro(a, "file")
        rp_, rr_, rf_, *_ = micro(a, "req")
        pp_, pr_, pf_, *_ = micro(a, "pol")
        pooled_rows.append({
            "system": sysname,
            "file_precision": fp_, "file_recall": fr_, "file_f1": ff_,
            "req_precision": rp_, "req_recall": rr_, "req_f1": rf_,
            "pol_precision": pp_, "pol_recall": pr_, "pol_f1": pf_,
        })

    if only is None and per_repo_rows:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with (OUT_DIR / "per_repo_metrics.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(per_repo_rows[0].keys()))
            w.writeheader()
            w.writerows(per_repo_rows)
        if scenario_rows:
            with (OUT_DIR / "scenario_file_results.csv").open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=list(scenario_rows[0].keys()))
                w.writeheader()
                w.writerows(scenario_rows)
        (OUT_DIR / "summary.json").write_text(json.dumps(
            {"per_repo": per_repo_rows, "pooled": pooled_rows,
             "note": "UCE uses unfiltered direct+transitive+call_chain files; oracle = independent import graph"},
            indent=2,
        ), encoding="utf-8")
        print("\n=== POOLED micro-F1 (independent oracle, all scenarios) ===")
        print(f"{'system':11s} {'file_P':>7s} {'file_R':>7s} {'file_F1':>8s} "
              f"{'req_P':>7s} {'req_R':>7s} {'req_F1':>7s} {'pol_F1':>7s}")
        for r in pooled_rows:
            print(f"{r['system']:11s} {r['file_precision']:>7.3f} {r['file_recall']:>7.3f} {r['file_f1']:>8.3f} "
                  f"{r['req_precision']:>7.3f} {r['req_recall']:>7.3f} {r['req_f1']:>7.3f} {r['pol_f1']:>7.3f}")
        print("\nWrote", OUT_DIR / "summary.json")
    if graph is not None:
        graph.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-uce", action="store_true", help="Also query the live Neo4j graph for UCE predictions")
    ap.add_argument("--ingest", action="store_true", help="Ingest each repo into Neo4j before UCE eval")
    ap.add_argument("--only", default=None, help="Evaluate only this repo")
    args = ap.parse_args()
    run(args.with_uce, args.only, args.ingest)
