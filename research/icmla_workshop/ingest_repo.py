"""
Deterministic per-repo ingestion into the live Neo4j graph for multi-repo UCE evaluation.

Because the evaluation uses a single Neo4j instance, we evaluate one repo at a time:
  reset graph -> ensure indexes -> deterministic full_refresh (code + schema) ->
  deterministic governance ingest (Requirement/Policy + GOVERNS/ENFORCES) from config paths.

No LLM is used (deterministic lane only), so ingestion is reproducible.

Usage: python ingest_repo.py <config.yaml> [--keep]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from uce.core.config import load_config
from uce.core.graph_db import GraphDB
from uce.ingestion.graph_builder import load_columns, load_tables, upsert_policies, upsert_requirements
from uce.runtime.updater import GraphUpdater

from ingest_governance import read_policies, read_requirements


def _stat(graph, query):
    try:
        return graph.run(query)[0]["c"]
    except Exception:
        return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--keep", action="store_true", help="do not wipe the graph first")
    args = ap.parse_args()

    config = load_config(args.config)
    root = Path(config.project_root)
    graph = GraphDB(config.neo4j.uri, config.neo4j.user, config.neo4j.password)
    try:
        if not args.keep:
            graph.run("MATCH (n) DETACH DELETE n")
            print("graph reset")
        try:
            graph.ensure_indexes()
        except Exception as exc:
            print("ensure_indexes warning:", exc)

        updater = GraphUpdater(config, graph)
        updater.full_refresh()
        print("full_refresh complete")

        # Deterministic governance from config-resolved paths.
        req_dir = (root / config.paths.requirements[0]) if config.paths.requirements else None
        pol_dir = (root / config.paths.policies[0]) if config.paths.policies else None
        if req_dir and req_dir.exists():
            tables = load_tables(graph)
            columns = load_columns(graph)
            reqs = read_requirements(req_dir)
            pols = read_policies(pol_dir) if pol_dir and pol_dir.exists() else []
            upsert_requirements(graph, reqs, tables, columns)
            upsert_policies(graph, pols)

        print("--- graph stats ---")
        print("files     :", _stat(graph, "MATCH (f:File) RETURN count(f) AS c"))
        print("functions :", _stat(graph, "MATCH (fn:Function) RETURN count(fn) AS c"))
        print("tables    :", _stat(graph, "MATCH (t:Table) RETURN count(t) AS c"))
        print("columns   :", _stat(graph, "MATCH (c:Column) RETURN count(c) AS c"))
        print("IMPORTS   :", _stat(graph, "MATCH ()-[r:IMPORTS]->() RETURN count(r) AS c"))
        print("USES_TABLE:", _stat(graph, "MATCH ()-[r:USES_TABLE]->() RETURN count(r) AS c"))
        print("reqs      :", _stat(graph, "MATCH (r:Requirement) RETURN count(r) AS c"))
        print("policies  :", _stat(graph, "MATCH (p:Policy) RETURN count(p) AS c"))
        print("GOVERNS   :", _stat(graph, "MATCH (:Requirement)-[g:GOVERNS]->() RETURN count(g) AS c"))
        print("ENFORCES  :", _stat(graph, "MATCH (:Policy)-[e:ENFORCES]->() RETURN count(e) AS c"))
    finally:
        graph.close()


if __name__ == "__main__":
    main()
