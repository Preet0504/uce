from __future__ import annotations

from uce.core.graph_db import GraphDB
from uce.core.risk_model import assess_risk

TABLE_IMPACT_QUERY = """
MATCH (t:Table {name: $table})
OPTIONAL MATCH (f:File)-[:USES_TABLE]->(t)
OPTIONAL MATCH (c:Column {table: $table})<-[:REFERENCES_COLUMN]-(f2:File)
RETURN collect(DISTINCT f.path) AS table_files,
       collect(DISTINCT f2.path) AS column_files
"""

COLUMN_IMPACT_QUERY = """
MATCH (c:Column {name: $column, table: $table})
OPTIONAL MATCH (c)<-[:REFERENCES_COLUMN]-(f:File)
RETURN collect(DISTINCT f.path) AS files
"""

TABLE_REQUIREMENTS_QUERY = """
MATCH (r:Requirement)-[:GOVERNS]->(t:Table {name: $table})
RETURN collect(DISTINCT r.id) AS req_ids
"""

COLUMN_REQUIREMENTS_QUERY = """
MATCH (r:Requirement)-[:GOVERNS]->(c:Column {name: $column, table: $table})
RETURN collect(DISTINCT r.id) AS req_ids
"""

REVERSE_IMPORT_QUERY = """
MATCH (d:File)
WHERE d.path IN $direct
MATCH (u:File)-[:IMPORTS*1..]->(d)
RETURN DISTINCT u.path AS path
"""

FILE_IMPACT_QUERY = """
MATCH (f:File {path: $path})
RETURN f.path AS path
"""

TABLE_TO_REQUIREMENTS_QUERY = """
MATCH (r:Requirement)-[:GOVERNS]->(t:Table {name: $table})
RETURN collect(DISTINCT r.id) AS req_ids
"""

COLUMN_TO_REQUIREMENTS_QUERY = """
MATCH (r:Requirement)-[:GOVERNS]->(c:Column {name: $column, table: $table})
RETURN collect(DISTINCT r.id) AS req_ids
"""

REQUIREMENT_TO_POLICIES_QUERY = """
MATCH (p:Policy)-[:ENFORCES]->(r:Requirement {id: $req_id})
RETURN collect(DISTINCT p.id) AS policy_ids
"""

COLUMN_TO_TABLE_QUERY = """
MATCH (t:Table)-[:HAS_COLUMN]->(c:Column {name: $column, table: $table})
RETURN t.name AS table_name
"""

TABLE_FILES_FUNCTIONS_APIS_QUERY = """
MATCH (t:Table {name: $table})<-[:USES_TABLE]-(f:File)
OPTIONAL MATCH (f)-[:DECLARES_FUNCTION]->(fn:Function)
OPTIONAL MATCH (fn)-[:EXPOSED_AS]->(api:API)
OPTIONAL MATCH (api)-[:BELONGS_TO]->(s:Service)
RETURN DISTINCT f.path AS file_path,
                coalesce(fn.name, null) AS fn_name,
                coalesce(fn.file_path, null) AS fn_file_path,
                coalesce(api.route, null) AS api_route,
                coalesce(api.method, null) AS api_method,
                coalesce(s.name, null) AS service_name
"""

TABLE_REQUIREMENTS_POLICIES_QUERY = """
MATCH (t:Table {name: $table})<-[:GOVERNS]-(r:Requirement)
OPTIONAL MATCH (p:Policy)-[:ENFORCES]->(r)
RETURN DISTINCT r.id AS req_id, coalesce(p.id, null) AS policy_id
"""


def _reverse_import_closure(graph: GraphDB, direct_paths: list[str]):
    if not direct_paths:
        return []

    rows = graph.run(REVERSE_IMPORT_QUERY, direct=direct_paths)
    return sorted({row["path"] for row in rows if row.get("path")})


def _is_backend_file(path: str) -> bool:
    if not path:
        return False
    lowered = path.replace("\\", "/")
    ui_markers = ["/ui/", "/views/", "/components/"]
    if any(marker in lowered for marker in ui_markers):
        return False
    backend_markers = ["/server/", "/api/", "/db/", "/trpc/", "/inngest/"]
    if any(marker in lowered for marker in backend_markers):
        return True
    if "/modules/" in lowered and "/server/" in lowered:
        return True
    return False


def _filter_backend(paths: list[str]) -> list[str]:
    return sorted({p for p in paths if _is_backend_file(p)})


def table_impact_analysis(graph: GraphDB, table_name: str):
    rows = graph.run(TABLE_IMPACT_QUERY, table=table_name)

    direct_files = set()
    if rows:
        table_files = rows[0]["table_files"] or []
        column_files = rows[0]["column_files"] or []
        direct_files.update([p for p in table_files if p])
        direct_files.update([p for p in column_files if p])

    direct_list = sorted(direct_files)
    transitive_list = _reverse_import_closure(graph, direct_list)

    affected = sorted(set(direct_list) | set(transitive_list))
    backend_affected = _filter_backend(affected)

    req_rows = graph.run(TABLE_REQUIREMENTS_QUERY, table=table_name)
    violated_requirements = []
    if req_rows:
        violated_requirements = sorted({r for r in req_rows[0]["req_ids"] or [] if r})

    return {
        "target_table": table_name,
        "direct_files": direct_list,
        "transitive_files": transitive_list,
        "affected_files": backend_affected,
        "violated_requirements": violated_requirements,
        "risk_score": len(backend_affected) + (5 * len(violated_requirements)),
    }


def column_impact_analysis(graph: GraphDB, table_name: str, column_name: str):
    rows = graph.run(COLUMN_IMPACT_QUERY, table=table_name, column=column_name)

    direct_files = set()
    if rows:
        files = rows[0]["files"] or []
        direct_files.update([p for p in files if p])

    direct_list = sorted(direct_files)
    transitive_list = _reverse_import_closure(graph, direct_list)

    affected = sorted(set(direct_list) | set(transitive_list))
    backend_affected = _filter_backend(affected)

    req_rows = graph.run(COLUMN_REQUIREMENTS_QUERY, table=table_name, column=column_name)
    violated_requirements = []
    if req_rows:
        violated_requirements = sorted({r for r in req_rows[0]["req_ids"] or [] if r})

    return {
        "target_table": table_name,
        "target_column": column_name,
        "direct_files": direct_list,
        "transitive_files": transitive_list,
        "affected_files": backend_affected,
        "violated_requirements": violated_requirements,
        "risk_score": len(backend_affected) + (5 * len(violated_requirements)),
    }


def file_blast_radius(graph: GraphDB, file_path: str):
    rows = graph.run(FILE_IMPACT_QUERY, path=file_path)

    direct_files = set()
    for row in rows:
        path = row.get("path")
        if path:
            direct_files.add(path)

    direct_list = sorted(direct_files)
    transitive_list = _reverse_import_closure(graph, direct_list)

    affected = set(direct_list) | set(transitive_list)
    return {
        "target_file": file_path,
        "direct_files": direct_list,
        "transitive_files": transitive_list,
        "risk_score": len(affected),
    }


def explain_change(graph: GraphDB, entity_type: str, entity_name: str):
    affected_files: set[str] = set()
    affected_functions: set[str] = set()
    affected_apis: set[str] = set()
    affected_services: set[str] = set()
    violated_requirements: set[str] = set()
    enforced_policies: set[str] = set()
    trace_paths: set[str] = set()

    if entity_type == "table":
        table_name = entity_name

        rows = graph.run(TABLE_FILES_FUNCTIONS_APIS_QUERY, table=table_name)
        for row in rows:
            file_path = row.get("file_path")
            fn_name = row.get("fn_name")
            fn_file_path = row.get("fn_file_path")
            api_route = row.get("api_route")
            api_method = row.get("api_method")
            service_name = row.get("service_name")

            if file_path and _is_backend_file(file_path):
                affected_files.add(file_path)

            if fn_name and fn_file_path and file_path and _is_backend_file(file_path):
                fn_id = f"{fn_name}@{fn_file_path}"
                affected_functions.add(fn_id)

            if api_route and api_method and fn_name and file_path and _is_backend_file(file_path):
                api_id = f"{api_method} {api_route}"
                affected_apis.add(api_id)

            if service_name and api_route and api_method and file_path and _is_backend_file(file_path):
                affected_services.add(service_name)

            if file_path and _is_backend_file(file_path):
                trace = f"Table({table_name}) -> File({file_path})"
                if fn_name and fn_file_path:
                    trace += f" -> Function({fn_name})"
                if api_route and api_method:
                    trace += f" -> API({api_method} {api_route})"
                if service_name:
                    trace += f" -> Service({service_name})"
                trace_paths.add(trace)

        req_rows = graph.run(TABLE_REQUIREMENTS_POLICIES_QUERY, table=table_name)
        req_to_policies: dict[str, set[str]] = {}
        for row in req_rows:
            req_id = row.get("req_id")
            policy_id = row.get("policy_id")
            if not req_id:
                continue
            violated_requirements.add(req_id)
            if policy_id:
                req_to_policies.setdefault(req_id, set()).add(policy_id)

        for req_id in sorted(violated_requirements):
            policy_ids = sorted(req_to_policies.get(req_id, set()))
            if policy_ids:
                for policy_id in policy_ids:
                    enforced_policies.add(policy_id)
                    trace_paths.add(
                        f"Table({table_name}) -> Requirement({req_id}) -> Policy({policy_id})"
                    )
            else:
                trace_paths.add(f"Table({table_name}) -> Requirement({req_id})")

    elif entity_type == "column":
        if "." in entity_name:
            table_name, column_name = entity_name.split(".", 1)
            table_targets = [table_name]
        else:
            column_name = entity_name
            rows = graph.run(
                "MATCH (c:Column {name: $column}) RETURN DISTINCT c.table AS table",
                column=column_name,
            )
            table_targets = sorted({r["table"] for r in rows if r.get("table")})

        for table_name in table_targets:
            table_rows = graph.run(COLUMN_TO_TABLE_QUERY, table=table_name, column=column_name)
            if table_rows:
                trace_paths.add(f"Column({column_name}) -> Table({table_name})")

            req_rows = graph.run(COLUMN_TO_REQUIREMENTS_QUERY, table=table_name, column=column_name)
            req_ids = []
            if req_rows:
                req_ids = sorted({r for r in req_rows[0]["req_ids"] or [] if r})

            req_to_policies: dict[str, set[str]] = {}
            for req_id in req_ids:
                violated_requirements.add(req_id)
                policy_rows = graph.run(REQUIREMENT_TO_POLICIES_QUERY, req_id=req_id)
                policy_ids = []
                if policy_rows:
                    policy_ids = sorted({p for p in policy_rows[0]["policy_ids"] or [] if p})
                if policy_ids:
                    req_to_policies.setdefault(req_id, set()).update(policy_ids)

            for req_id in sorted(req_ids):
                policy_ids = sorted(req_to_policies.get(req_id, set()))
                if policy_ids:
                    for policy_id in policy_ids:
                        enforced_policies.add(policy_id)
                        trace_paths.add(
                            f"Column({column_name}) -> Requirement({req_id}) -> Policy({policy_id})"
                        )
                else:
                    trace_paths.add(f"Column({column_name}) -> Requirement({req_id})")

    elif entity_type == "requirement":
        req_id = entity_name
        policy_rows = graph.run(REQUIREMENT_TO_POLICIES_QUERY, req_id=req_id)
        policy_ids = []
        if policy_rows:
            policy_ids = sorted({p for p in policy_rows[0]["policy_ids"] or [] if p})
        for policy_id in policy_ids:
            enforced_policies.add(policy_id)
            trace_paths.add(f"Requirement({req_id}) -> Policy({policy_id})")

    elif entity_type == "policy":
        policy_id = entity_name
        rows = graph.run(
            "MATCH (p:Policy {id: $id})-[:ENFORCES]->(r:Requirement) "
            "RETURN collect(DISTINCT r.id) AS req_ids",
            id=policy_id,
        )
        req_ids = []
        if rows:
            req_ids = sorted({r for r in rows[0]["req_ids"] or [] if r})
        for req_id in req_ids:
            trace_paths.add(f"Policy({policy_id}) -> Requirement({req_id})")

    risk = assess_risk(
        affected_files=len(affected_files),
        affected_functions=len(affected_functions),
        affected_apis=len(affected_apis),
        violated_requirements=len(violated_requirements),
        enforced_policies=len(enforced_policies),
    )

    summary = (
        f"{entity_type} {entity_name} affects {len(affected_files)} files, "
        f"{len(affected_functions)} functions, {len(affected_apis)} APIs, "
        f"{len(affected_services)} services, violates {len(violated_requirements)} requirements, "
        f"and enforces {len(enforced_policies)} policies."
    )

    return {
        "entity": entity_name,
        "affected_files": sorted(affected_files),
        "affected_functions": sorted(affected_functions),
        "affected_apis": sorted(affected_apis),
        "affected_services": sorted(affected_services),
        "violated_requirements": sorted(violated_requirements),
        "enforced_policies": sorted(enforced_policies),
        "risk_breakdown": {
            "backend_files": len(affected_files),
            "violated_requirements": len(violated_requirements),
            "enforced_policies": len(enforced_policies),
            "affected_apis": len(affected_apis),
            "risk_score": risk.risk_score,
        },
        "risk_score": risk.risk_score,
        "trace_paths": sorted(trace_paths),
        "summary": summary,
        "chains": sorted(trace_paths),
    }


def impact_analysis(graph: GraphDB, entity_type: str, entity_name: str):
    if entity_type == "table":
        base = table_impact_analysis(graph, entity_name)
        detail = explain_change(graph, entity_type, entity_name)
        detail["impact"] = base
        return detail
    if entity_type == "column":
        if "." in entity_name:
            table, column = entity_name.split(".", 1)
        else:
            raise ValueError("column impact requires table.column")
        base = column_impact_analysis(graph, table, column)
        detail = explain_change(graph, entity_type, entity_name)
        detail["impact"] = base
        return detail
    if entity_type == "file":
        base = file_blast_radius(graph, entity_name)
        affected_files = sorted(set(base.get("direct_files", [])) | set(base.get("transitive_files", [])))
        return {
            "entity": entity_name,
            "affected_files": affected_files,
            "affected_functions": [],
            "affected_apis": [],
            "affected_services": [],
            "violated_requirements": [],
            "enforced_policies": [],
            "risk_score": base.get("risk_score", 0),
            "trace_paths": [f"File({entity_name})"],
            "impact": base,
        }
    raise ValueError(f"Unknown entity_type: {entity_type}")
