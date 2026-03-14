from __future__ import annotations

import re
from fastmcp import FastMCP

from uce.core.config import UceConfig
from uce.core.graph_db import GraphDB
from uce.reasoning import impact_analysis as impact_module
from uce.reasoning.trace_engine import preflight_assessment


mcp = FastMCP(name="UnifiedContextEngine", version="0.2")
_CONFIG: UceConfig | None = None


def _graph_from_config(config: UceConfig | None) -> GraphDB:
    if config is None:
        raise RuntimeError("UCE server config not initialized")
    return GraphDB(config.neo4j.uri, config.neo4j.user, config.neo4j.password)


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


def _detect_entity(text: str, tables: list[str], columns_by_table: dict[str, list[str]], files: list[str]):
    table_hits = []
    for table in tables:
        if _word_pattern(table).search(text):
            table_hits.append(table)
    table_hits = sorted(table_hits)

    for table in table_hits:
        for column in sorted(set(columns_by_table.get(table, []))):
            if _word_pattern(column).search(text):
                return "column", table, column

    if table_hits:
        return "table", table_hits[0], None

    column_to_tables = {}
    for table, columns in columns_by_table.items():
        for column in columns:
            column_to_tables.setdefault(column, set()).add(table)

    for column in sorted(column_to_tables):
        if _word_pattern(column).search(text):
            tables_for_column = sorted(column_to_tables[column])
            if len(tables_for_column) == 1:
                return "column", tables_for_column[0], column
            return "unknown", None, None

    for path in files:
        if path in text:
            return "file", path, None

    return "unknown", None, None


def _collect_affected(result: dict):
    affected_files = result.get("affected_files")
    if affected_files:
        return sorted(set(affected_files))
    direct_files = result.get("direct_files") or []
    transitive_files = result.get("transitive_files") or []
    return sorted(set(direct_files) | set(transitive_files))


@mcp.tool
def impact_analysis(entity_type: str, entity_name: str) -> dict:
    graph = _graph_from_config(_CONFIG)
    try:
        result = impact_module.impact_analysis(graph, entity_type, entity_name)
    finally:
        graph.close()
    return result


@mcp.tool
def explain_change(entity_type: str, entity_name: str) -> dict:
    graph = _graph_from_config(_CONFIG)
    try:
        result = impact_module.explain_change(graph, entity_type, entity_name)
    finally:
        graph.close()
    return result


@mcp.tool
def risk_assessment(proposed_change: str) -> dict:
    graph = _graph_from_config(_CONFIG)
    try:
        result = preflight_assessment(graph, proposed_change)
    finally:
        graph.close()
    return result


# Backwards-compatible tools

@mcp.tool
def impact_table(table_name: str) -> dict:
    graph = _graph_from_config(_CONFIG)
    try:
        return impact_module.table_impact_analysis(graph, table_name)
    finally:
        graph.close()


@mcp.tool
def impact_column(table_name: str, column_name: str) -> dict:
    graph = _graph_from_config(_CONFIG)
    try:
        return impact_module.column_impact_analysis(graph, table_name, column_name)
    finally:
        graph.close()


@mcp.tool
def preflight_check(proposed_change: str) -> dict:
    graph = _graph_from_config(_CONFIG)
    try:
        tables = _load_tables(graph)
        columns_by_table = _load_columns(graph)
        files = _load_files(graph)
    finally:
        graph.close()

    entity_type, table_name, column_name = _detect_entity(
        proposed_change, tables, columns_by_table, files
    )

    if entity_type == "column" and table_name and column_name:
        graph = _graph_from_config(_CONFIG)
        try:
            result = impact_module.column_impact_analysis(graph, table_name, column_name)
        finally:
            graph.close()
        detected = f"{table_name}.{column_name}"
    elif entity_type == "table" and table_name:
        graph = _graph_from_config(_CONFIG)
        try:
            result = impact_module.table_impact_analysis(graph, table_name)
        finally:
            graph.close()
        detected = table_name
    elif entity_type == "file" and table_name:
        graph = _graph_from_config(_CONFIG)
        try:
            result = impact_module.file_blast_radius(graph, table_name)
        finally:
            graph.close()
        detected = table_name
    else:
        result = {
            "direct_files": [],
            "transitive_files": [],
            "risk_score": 0,
        }
        detected = "unknown"
        entity_type = "unknown"

    risk_score = int(result.get("risk_score") or 0)
    affected_files = _collect_affected(result)
    violated_requirements = result.get("violated_requirements") or []

    if violated_requirements:
        recommendation = "High risk - violates requirements"
    elif risk_score >= 10:
        recommendation = "High risk"
    elif risk_score >= 5:
        recommendation = "Moderate risk"
    else:
        recommendation = "Low risk"

    return {
        "entity": detected,
        "entity_type": entity_type,
        "risk_score": risk_score,
        "violated_requirements": violated_requirements,
        "affected_files": affected_files,
        "recommendation": recommendation,
    }


@mcp.tool
def validate_change(proposed_change: str) -> dict:
    return preflight_check(proposed_change)


@mcp.tool
def preflight_validation(payload: dict) -> dict:
    tool_name = payload.get("tool")
    tool_input = payload.get("input") or {}

    if tool_name != "preflight_validation":
        return {
            "tool": "preflight_validation",
            "error": "Invalid tool name",
        }

    proposed_change = tool_input.get("proposed_change", "")
    result = preflight_check(proposed_change)
    return {
        "tool": "preflight_validation",
        "input": {"proposed_change": proposed_change},
        "output": result,
    }


@mcp.tool
def explain_change_rpc(payload: dict) -> dict:
    tool_name = payload.get("tool")
    tool_input = payload.get("input") or {}

    if tool_name != "explain_change":
        return {
            "tool": "explain_change",
            "error": "Invalid tool name",
        }

    entity_type = tool_input.get("entity_type", "")
    entity_name = tool_input.get("entity_name", "")
    result = explain_change(entity_type, entity_name)
    return {
        "tool": "explain_change",
        "input": {"entity_type": entity_type, "entity_name": entity_name},
        "output": result,
    }


@mcp.tool
def logic_trace(entity: str) -> dict:
    graph = _graph_from_config(_CONFIG)
    try:
        tables = _load_tables(graph)
        columns_by_table = _load_columns(graph)
        files = _load_files(graph)

        entity_type, table_name, column_name = _detect_entity(
            entity, tables, columns_by_table, files
        )

        if entity_type == "table":
            queries = [
                impact_module.TABLE_IMPACT_QUERY,
                impact_module.TABLE_REQUIREMENTS_QUERY,
                impact_module.REVERSE_IMPORT_QUERY,
            ]
        elif entity_type == "column":
            queries = [
                impact_module.COLUMN_IMPACT_QUERY,
                impact_module.COLUMN_REQUIREMENTS_QUERY,
                impact_module.REVERSE_IMPORT_QUERY,
            ]
        elif entity_type == "file":
            queries = [impact_module.FILE_IMPACT_QUERY, impact_module.REVERSE_IMPORT_QUERY]
        else:
            queries = []

        node_rows = graph.run("MATCH (n) RETURN count(n) AS count")
        edge_rows = graph.run("MATCH ()-[r]->() RETURN count(r) AS count")
    finally:
        graph.close()

    node_count = int(node_rows[0]["count"]) if node_rows else 0
    edge_count = int(edge_rows[0]["count"]) if edge_rows else 0

    return {
        "cypher_queries_executed": queries,
        "node_count": node_count,
        "edge_count": edge_count,
    }


def run_server(config: UceConfig):
    global _CONFIG
    _CONFIG = config
    mcp.run()
