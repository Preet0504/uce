from __future__ import annotations

import re
from typing import Iterable

from core.graph_db import GraphDB
from core.risk_model import assess_risk
from reasoning.impact_analysis import (
    impact_analysis,
    explain_change,
)


def _word_pattern(term: str):
    return re.compile(rf"(?<!\\w){re.escape(term)}(?!\\w)")


def _load_tables(graph: GraphDB):
    rows = graph.run("MATCH (t:Table) RETURN t.name AS name")
    return sorted({row["name"] for row in rows if row.get("name")})


def _load_columns(graph: GraphDB):
    rows = graph.run("MATCH (c:Column) RETURN c.name AS name, c.table AS table")
    columns = {}
    for row in rows:
        name = row.get("name")
        table = row.get("table")
        if not name or not table:
            continue
        columns.setdefault(table, []).append(name)
    return columns


def _load_files(graph: GraphDB):
    rows = graph.run("MATCH (f:File) RETURN f.path AS path")
    return sorted({row["path"] for row in rows if row.get("path")})


def detect_entity(text: str, tables: list[str], columns_by_table: dict[str, list[str]], files: list[str]):
    table_hits = []
    for table in tables:
        if _word_pattern(table).search(text):
            table_hits.append(table)
    table_hits = sorted(table_hits)

    for table in table_hits:
        for column in sorted(set(columns_by_table.get(table, []))):
            if _word_pattern(column).search(text):
                return "column", f"{table}.{column}"

    if table_hits:
        return "table", table_hits[0]

    column_to_tables = {}
    for table, columns in columns_by_table.items():
        for column in columns:
            column_to_tables.setdefault(column, set()).add(table)

    for column in sorted(column_to_tables):
        if _word_pattern(column).search(text):
            tables_for_column = sorted(column_to_tables[column])
            if len(tables_for_column) == 1:
                return "column", f"{tables_for_column[0]}.{column}"
            return "unknown", ""

    for path in files:
        if path in text:
            return "file", path

    return "unknown", ""


def preflight_assessment(
    graph: GraphDB,
    proposed_change: str,
    backend_paths: Iterable[str] | None = None,
):
    tables = _load_tables(graph)
    columns_by_table = _load_columns(graph)
    files = _load_files(graph)

    entity_type, entity_name = detect_entity(proposed_change, tables, columns_by_table, files)

    if entity_type == "unknown":
        return {
            "entity": "unknown",
            "entity_type": "unknown",
            "risk_score": 0,
            "affected_files": [],
            "affected_functions": [],
            "violated_requirements": [],
            "enforced_policies": [],
            "trace_paths": [],
            "summary": "No matching entity detected.",
        }

    impact = impact_analysis(graph, entity_type, entity_name, backend_paths=backend_paths)
    change = explain_change(graph, entity_type, entity_name, backend_paths=backend_paths)

    affected_files = change.get("affected_files", [])
    affected_functions = change.get("affected_functions", [])
    violated_requirements = change.get("violated_requirements", [])
    enforced_policies = change.get("enforced_policies", [])
    trace_paths = change.get("trace_paths", [])

    risk = assess_risk(
        affected_files=len(affected_files),
        affected_functions=len(affected_functions),
        violated_requirements=len(violated_requirements),
        enforced_policies=len(enforced_policies),
    )

    return {
        "entity": entity_name,
        "entity_type": entity_type,
        "impact": impact,
        "analysis": change,
        "risk_score": risk.risk_score,
        "risk_severity": risk.severity,
        "risk_rationale": risk.rationale,
        "affected_files": affected_files,
        "affected_functions": affected_functions,
        "violated_requirements": violated_requirements,
        "enforced_policies": enforced_policies,
        "trace_paths": trace_paths,
    }
