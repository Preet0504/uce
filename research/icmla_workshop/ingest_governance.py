"""
Deterministic governance ingestion (no LLM) for the evaluation graph.

Populates Requirement/Policy nodes and GOVERNS/ENFORCES edges using UCE's own deterministic
graph_builder, so that UCE's reasoning tools have governance data to traverse. This mirrors what
`uce`'s production deterministic lane does; it does NOT use the independent oracle.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from uce.core.config import load_config
from uce.core.graph_db import GraphDB
from uce.ingestion.graph_builder import (
    load_columns,
    load_tables,
    upsert_policies,
    upsert_requirements,
)


@dataclass(frozen=True)
class RequirementRecord:
    req_id: str
    title: str
    description: str


@dataclass(frozen=True)
class PolicyRecord:
    policy_id: str
    description: str
    requirement_ids: list[str]


def _frontmatter(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def read_requirements(req_dir: Path) -> list[RequirementRecord]:
    records = []
    for path in sorted(req_dir.glob("*.md")):
        fm = _frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        rid = fm.get("id", path.stem)
        records.append(RequirementRecord(rid, fm.get("title", rid), fm.get("description", "")))
    return records


def read_policies(pol_dir: Path) -> list[PolicyRecord]:
    records = []
    for path in sorted(pol_dir.glob("*.md")):
        fm = _frontmatter(path.read_text(encoding="utf-8", errors="ignore"))
        pid = fm.get("id", path.stem)
        enforces = [t.strip() for t in fm.get("enforces", "").split(",") if t.strip()]
        records.append(PolicyRecord(pid, fm.get("description", ""), enforces))
    return records


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "F:/UIC/CS540/Projects/talkai-main/config.yaml"
    config = load_config(config_path)
    root = Path(config.project_root)
    graph = GraphDB(config.neo4j.uri, config.neo4j.user, config.neo4j.password)
    try:
        tables = load_tables(graph)
        columns = load_columns(graph)
        reqs = read_requirements(root / "src" / "requirements")
        pols = read_policies(root / "src" / "policies")
        upsert_requirements(graph, reqs, tables, columns)
        upsert_policies(graph, pols)

        gov = graph.run("MATCH (:Requirement)-[g:GOVERNS]->() RETURN count(g) AS c")[0]["c"]
        enf = graph.run("MATCH (:Policy)-[e:ENFORCES]->() RETURN count(e) AS c")[0]["c"]
        nr = graph.run("MATCH (r:Requirement) RETURN count(r) AS c")[0]["c"]
        npol = graph.run("MATCH (p:Policy) RETURN count(p) AS c")[0]["c"]
        print(f"Requirements: {nr}, Policies: {npol}, GOVERNS edges: {gov}, ENFORCES edges: {enf}")
    finally:
        graph.close()


if __name__ == "__main__":
    main()
