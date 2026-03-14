from graph import GraphDB
from uce.reasoning.impact_analysis import (
    TABLE_IMPACT_QUERY,
    COLUMN_IMPACT_QUERY,
    TABLE_REQUIREMENTS_QUERY,
    COLUMN_REQUIREMENTS_QUERY,
    REVERSE_IMPORT_QUERY,
    FILE_IMPACT_QUERY,
    TABLE_TO_REQUIREMENTS_QUERY,
    COLUMN_TO_REQUIREMENTS_QUERY,
    REQUIREMENT_TO_POLICIES_QUERY,
    COLUMN_TO_TABLE_QUERY,
    TABLE_FILES_FUNCTIONS_APIS_QUERY,
    TABLE_REQUIREMENTS_POLICIES_QUERY,
    table_impact_analysis as _table_impact_analysis,
    column_impact_analysis as _column_impact_analysis,
    file_blast_radius as _file_blast_radius,
    explain_change as _explain_change,
)


def table_impact_analysis(table_name: str):
    graph = GraphDB()
    try:
        return _table_impact_analysis(graph, table_name)
    finally:
        graph.close()


def column_impact_analysis(table_name: str, column_name: str):
    graph = GraphDB()
    try:
        return _column_impact_analysis(graph, table_name, column_name)
    finally:
        graph.close()


def file_blast_radius(file_path: str):
    graph = GraphDB()
    try:
        return _file_blast_radius(graph, file_path)
    finally:
        graph.close()


def explain_change(entity_type: str, entity_name: str):
    graph = GraphDB()
    try:
        return _explain_change(graph, entity_type, entity_name)
    finally:
        graph.close()


def validate_graph_integrity():
    graph = GraphDB()

    table_rows = graph.run("MATCH (t:Table) RETURN count(t) AS count")
    requirement_rows = graph.run("MATCH (r:Requirement) RETURN count(r) AS count")
    policy_rows = graph.run("MATCH (p:Policy) RETURN count(p) AS count")
    backend_rows = graph.run(
        "MATCH (f:File)-[:USES_TABLE]->(:Table) RETURN collect(DISTINCT f.path) AS paths"
    )
    orphan_columns = graph.run(
        "MATCH (c:Column) WHERE NOT ( (:Table)-[:HAS_COLUMN]->(c) ) RETURN count(c) AS count"
    )

    table_count = int(table_rows[0]["count"]) if table_rows else 0
    requirement_count = int(requirement_rows[0]["count"]) if requirement_rows else 0
    policy_count = int(policy_rows[0]["count"]) if policy_rows else 0

    backend_paths = []
    if backend_rows:
        backend_paths = backend_rows[0].get("paths") or []
    backend_count = len({p for p in backend_paths if p})

    orphan_count = int(orphan_columns[0]["count"]) if orphan_columns else 0

    ok = (
        table_count > 0
        and requirement_count > 0
        and policy_count > 0
        and backend_count > 0
        and orphan_count == 0
    )

    summary = (
        f"tables={table_count}, requirements={requirement_count}, "
        f"policies={policy_count}, backend_files_linked_to_tables={backend_count}, "
        f"orphan_columns={orphan_count}"
    )

    graph.close()
    return {
        "ok": ok,
        "summary": summary,
        "details": {
            "tables": table_count,
            "requirements": requirement_count,
            "policies": policy_count,
            "backend_files_linked_to_tables": backend_count,
            "orphan_columns": orphan_count,
        },
    }
