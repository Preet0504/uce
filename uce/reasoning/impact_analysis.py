from typing import Iterable

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

# The import depth is bounded to keep the reverse-import closure from becoming a
# pathological (or, on a cyclic import graph, explosive) traversal. Real import
# chains are far shallower than this bound, so results are unchanged in practice.
REVERSE_IMPORT_QUERY = """
MATCH (d:File)
WHERE d.path IN $direct
MATCH (u:File)-[:IMPORTS*1..25]->(d)
RETURN DISTINCT u.path AS path
"""

FILE_IMPACT_QUERY = """
MATCH (f:File {path: $path})
RETURN f.path AS path
"""

REQUIREMENT_TO_POLICIES_QUERY = """
MATCH (p:Policy)-[:ENFORCES]->(r:Requirement {id: $req_id})
RETURN collect(DISTINCT p.id) AS policy_ids
"""

COLUMN_TO_TABLE_QUERY = """
MATCH (t:Table)-[:HAS_COLUMN]->(c:Column {name: $column, table: $table})
RETURN t.name AS table_name
"""

TABLE_FILES_FUNCTIONS_QUERY = """
MATCH (t:Table {name: $table})<-[:USES_TABLE]-(f:File)
OPTIONAL MATCH (f)-[:DECLARES_FUNCTION]->(fn:Function)
RETURN DISTINCT f.path AS file_path,
                coalesce(fn.name, null) AS fn_name,
                coalesce(fn.file_path, null) AS fn_file_path
"""

TABLE_REQUIREMENTS_POLICIES_QUERY = """
MATCH (t:Table {name: $table})<-[:GOVERNS]-(r:Requirement)
OPTIONAL MATCH (p:Policy)-[:ENFORCES]->(r)
RETURN DISTINCT r.id AS req_id, coalesce(p.id, null) AS policy_id
"""

FILE_USES_TABLE_REQUIREMENTS_QUERY = """
MATCH (f:File {path: $path})-[:USES_TABLE]->(t:Table)<-[:GOVERNS]-(r:Requirement)
OPTIONAL MATCH (p:Policy)-[:ENFORCES]->(r)
RETURN DISTINCT r.id AS req_id, coalesce(p.id, null) AS policy_id
"""

FILE_REFERENCES_COLUMN_REQUIREMENTS_QUERY = """
MATCH (f:File {path: $path})-[:REFERENCES_COLUMN]->(c:Column)<-[:GOVERNS]-(r:Requirement)
OPTIONAL MATCH (p:Policy)-[:ENFORCES]->(r)
RETURN DISTINCT r.id AS req_id, coalesce(p.id, null) AS policy_id
"""

IMPLEMENTED_BY_QUERY = """
MATCH (r:Requirement {id: $req_id})-[:IMPLEMENTED_BY]->(artifact)
RETURN labels(artifact)[0] AS node_type,
       coalesce(artifact.path, artifact.file_path) AS path
"""

APPLIES_TO_QUERY = """
MATCH (p:Policy {id: $policy_id})-[:APPLIES_TO]->(artifact)
RETURN labels(artifact)[0] AS node_type,
       coalesce(artifact.path, artifact.file_path) AS path
"""

CALL_CHAIN_UPSTREAM_QUERY = """
MATCH (callee:Function)
WHERE callee.file_path IN $direct_files
MATCH (caller:Function)-[:CALLS*1..3]->(callee)
WHERE caller.file_path IS NOT NULL
RETURN DISTINCT caller.file_path AS file_path
"""


def _reverse_import_closure(graph: GraphDB, direct_paths: list[str]):
    if not direct_paths:
        return []

    rows = graph.run(REVERSE_IMPORT_QUERY, direct=direct_paths)
    return sorted({row["path"] for row in rows if row.get("path")})


def _call_chain_upstream(graph: GraphDB, direct_paths: list[str]) -> list[str]:
    """Return file paths that call into the given set of files via CALLS edges."""
    if not direct_paths:
        return []
    rows = graph.run(CALL_CHAIN_UPSTREAM_QUERY, direct_files=direct_paths)
    return sorted({row["file_path"] for row in rows if row.get("file_path")})


def _normalize_repo_path(path: str) -> str:
    normalized = (path or "").replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _normalize_backend_prefixes(backend_paths: Iterable[str] | None) -> tuple[str, ...]:
    if not backend_paths:
        return tuple()
    normalized: set[str] = set()
    for raw in backend_paths:
        prefix = _normalize_repo_path(str(raw))
        if prefix:
            normalized.add(prefix)
    return tuple(sorted(normalized))


def _matches_backend_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _is_backend_file_heuristic(path: str) -> bool:
    normalized = _normalize_repo_path(path).lower()
    if not normalized:
        return False
    segments = [segment for segment in normalized.split("/") if segment]
    if any(segment in {"ui", "views", "components"} for segment in segments):
        return False
    if any(segment in {"server", "api", "db", "trpc", "inngest"} for segment in segments):
        return True
    return "modules" in segments and "server" in segments


def _is_backend_file(path: str, backend_prefixes: tuple[str, ...] = tuple()) -> bool:
    normalized = _normalize_repo_path(path)
    if not normalized:
        return False
    if backend_prefixes:
        lowered = normalized.lower()
        return any(_matches_backend_prefix(lowered, prefix.lower()) for prefix in backend_prefixes)
    return _is_backend_file_heuristic(normalized)


def _filter_backend(paths: list[str], backend_paths: Iterable[str] | None = None) -> list[str]:
    backend_prefixes = _normalize_backend_prefixes(backend_paths)
    return sorted({path for path in paths if _is_backend_file(path, backend_prefixes)})


def table_impact_analysis(
    graph: GraphDB,
    table_name: str,
    backend_paths: Iterable[str] | None = None,
):
    rows = graph.run(TABLE_IMPACT_QUERY, table=table_name)

    direct_files = set()
    if rows:
        table_files = rows[0]["table_files"] or []
        column_files = rows[0]["column_files"] or []
        direct_files.update([p for p in table_files if p])
        direct_files.update([p for p in column_files if p])

    direct_list = sorted(direct_files)
    transitive_list = _reverse_import_closure(graph, direct_list)
    call_chain_list = _call_chain_upstream(graph, direct_list)

    affected = sorted(set(direct_list) | set(transitive_list) | set(call_chain_list))
    backend_affected = _filter_backend(affected, backend_paths)

    req_rows = graph.run(TABLE_REQUIREMENTS_QUERY, table=table_name)
    violated_requirements = []
    if req_rows:
        violated_requirements = sorted({r for r in req_rows[0]["req_ids"] or [] if r})

    risk = assess_risk(
        affected_files=len(backend_affected),
        affected_functions=0,
        violated_requirements=len(violated_requirements),
        enforced_policies=0,
    )

    return {
        "target_table": table_name,
        "direct_files": direct_list,
        "transitive_files": transitive_list,
        "call_chain_files": call_chain_list,
        "affected_files": backend_affected,
        "violated_requirements": violated_requirements,
        "risk_score": risk.risk_score,
        "risk_severity": risk.severity,
    }


def column_impact_analysis(
    graph: GraphDB,
    table_name: str,
    column_name: str,
    backend_paths: Iterable[str] | None = None,
):
    rows = graph.run(COLUMN_IMPACT_QUERY, table=table_name, column=column_name)

    direct_files = set()
    if rows:
        files = rows[0]["files"] or []
        direct_files.update([p for p in files if p])

    direct_list = sorted(direct_files)
    transitive_list = _reverse_import_closure(graph, direct_list)
    call_chain_list = _call_chain_upstream(graph, direct_list)

    affected = sorted(set(direct_list) | set(transitive_list) | set(call_chain_list))
    backend_affected = _filter_backend(affected, backend_paths)

    req_rows = graph.run(COLUMN_REQUIREMENTS_QUERY, table=table_name, column=column_name)
    violated_requirements = []
    if req_rows:
        violated_requirements = sorted({r for r in req_rows[0]["req_ids"] or [] if r})

    risk = assess_risk(
        affected_files=len(backend_affected),
        affected_functions=0,
        violated_requirements=len(violated_requirements),
        enforced_policies=0,
    )

    return {
        "target_table": table_name,
        "target_column": column_name,
        "direct_files": direct_list,
        "transitive_files": transitive_list,
        "call_chain_files": call_chain_list,
        "affected_files": backend_affected,
        "violated_requirements": violated_requirements,
        "risk_score": risk.risk_score,
        "risk_severity": risk.severity,
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
    call_chain_list = _call_chain_upstream(graph, direct_list)

    affected = set(direct_list) | set(transitive_list) | set(call_chain_list)
    # Score on the same weighted scale as the rest of the risk model (via the
    # shared file weight) instead of a raw file count, so the value is
    # comparable to every other tool's risk_score.
    risk = assess_risk(
        affected_files=len(affected),
        affected_functions=0,
        violated_requirements=0,
        enforced_policies=0,
    )
    return {
        "target_file": file_path,
        "direct_files": direct_list,
        "transitive_files": transitive_list,
        "call_chain_files": call_chain_list,
        "risk_score": risk.risk_score,
        "risk_severity": risk.severity,
    }


def explain_change(
    graph: GraphDB,
    entity_type: str,
    entity_name: str,
    backend_paths: Iterable[str] | None = None,
):
    affected_files: set[str] = set()
    affected_functions: set[str] = set()
    violated_requirements: set[str] = set()
    enforced_policies: set[str] = set()
    trace_paths: set[str] = set()
    backend_prefixes = _normalize_backend_prefixes(backend_paths)

    if entity_type == "table":
        table_name = entity_name

        rows = graph.run(TABLE_FILES_FUNCTIONS_QUERY, table=table_name)
        for row in rows:
            file_path = row.get("file_path")
            fn_name = row.get("fn_name")
            fn_file_path = row.get("fn_file_path")
            if file_path and _is_backend_file(file_path, backend_prefixes):
                affected_files.add(file_path)

            if fn_name and fn_file_path and file_path and _is_backend_file(file_path, backend_prefixes):
                fn_id = f"{fn_name}@{fn_file_path}"
                affected_functions.add(fn_id)

            if file_path and _is_backend_file(file_path, backend_prefixes):
                trace = f"Table({table_name}) -> File({file_path})"
                if fn_name and fn_file_path:
                    trace += f" -> Function({fn_name})"
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

            # Follow IMPLEMENTED_BY to reach code that implements this requirement
            impl_rows = graph.run(IMPLEMENTED_BY_QUERY, req_id=req_id)
            for row in impl_rows:
                path = row.get("path")
                if path and _is_backend_file(path, backend_prefixes):
                    affected_files.add(path)
                    trace_paths.add(
                        f"Table({table_name}) -> Requirement({req_id}) -> IMPLEMENTED_BY -> File({path})"
                    )

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

            req_rows = graph.run(COLUMN_REQUIREMENTS_QUERY, table=table_name, column=column_name)
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

                impl_rows = graph.run(IMPLEMENTED_BY_QUERY, req_id=req_id)
                for row in impl_rows:
                    path = row.get("path")
                    if path and _is_backend_file(path, backend_prefixes):
                        affected_files.add(path)
                        trace_paths.add(
                            f"Column({column_name}) -> Requirement({req_id}) -> IMPLEMENTED_BY -> File({path})"
                        )

    elif entity_type == "file":
        file_path = entity_name

        # Include the file itself if it's a backend file
        if _is_backend_file(file_path, backend_prefixes):
            affected_files.add(file_path)

        # Find files that import this file (reverse closure)
        transitive_rows = graph.run(REVERSE_IMPORT_QUERY, direct=[file_path])
        for row in transitive_rows:
            path = row.get("path")
            if path and _is_backend_file(path, backend_prefixes):
                affected_files.add(path)
                trace_paths.add(f"File({file_path}) <- IMPORTS <- File({path})")

        # Find requirements that govern tables this file uses
        req_rows = graph.run(FILE_USES_TABLE_REQUIREMENTS_QUERY, path=file_path)
        _collect_req_policy_rows(
            req_rows, violated_requirements, enforced_policies, trace_paths,
            prefix=f"File({file_path}) -> USES_TABLE"
        )

        # Find requirements that govern columns this file references
        col_req_rows = graph.run(FILE_REFERENCES_COLUMN_REQUIREMENTS_QUERY, path=file_path)
        _collect_req_policy_rows(
            col_req_rows, violated_requirements, enforced_policies, trace_paths,
            prefix=f"File({file_path}) -> REFERENCES_COLUMN"
        )

    elif entity_type == "requirement":
        req_id = entity_name
        policy_rows = graph.run(REQUIREMENT_TO_POLICIES_QUERY, req_id=req_id)
        policy_ids = []
        if policy_rows:
            policy_ids = sorted({p for p in policy_rows[0]["policy_ids"] or [] if p})
        for policy_id in policy_ids:
            enforced_policies.add(policy_id)
            trace_paths.add(f"Requirement({req_id}) -> Policy({policy_id})")

        # Follow IMPLEMENTED_BY to reach implementing code
        impl_rows = graph.run(IMPLEMENTED_BY_QUERY, req_id=req_id)
        for row in impl_rows:
            path = row.get("path")
            if path and _is_backend_file(path, backend_prefixes):
                affected_files.add(path)
                trace_paths.add(f"Requirement({req_id}) -> IMPLEMENTED_BY -> File({path})")

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

        # Follow APPLIES_TO to reach code this policy applies to
        applies_rows = graph.run(APPLIES_TO_QUERY, policy_id=policy_id)
        for row in applies_rows:
            path = row.get("path")
            if path and _is_backend_file(path, backend_prefixes):
                affected_files.add(path)
                trace_paths.add(f"Policy({policy_id}) -> APPLIES_TO -> File({path})")

    risk = assess_risk(
        affected_files=len(affected_files),
        affected_functions=len(affected_functions),
        violated_requirements=len(violated_requirements),
        enforced_policies=len(enforced_policies),
    )

    summary = (
        f"{entity_type} {entity_name} affects {len(affected_files)} files, "
        f"{len(affected_functions)} functions, violates {len(violated_requirements)} requirements, "
        f"and enforces {len(enforced_policies)} policies."
    )

    return {
        "entity": entity_name,
        "affected_files": sorted(affected_files),
        "affected_functions": sorted(affected_functions),
        "violated_requirements": sorted(violated_requirements),
        "enforced_policies": sorted(enforced_policies),
        "risk_breakdown": {
            "backend_files": len(affected_files),
            "violated_requirements": len(violated_requirements),
            "enforced_policies": len(enforced_policies),
            "risk_score": risk.risk_score,
        },
        "risk_score": risk.risk_score,
        "risk_severity": risk.severity,
        "trace_paths": sorted(trace_paths),
        "summary": summary,
        "chains": sorted(trace_paths),
    }


def _collect_req_policy_rows(
    rows: list,
    violated_requirements: set[str],
    enforced_policies: set[str],
    trace_paths: set[str],
    prefix: str,
) -> None:
    """Helper to add requirement/policy findings from a query result into the output sets."""
    for row in rows:
        req_id = row.get("req_id")
        policy_id = row.get("policy_id")
        if not req_id:
            continue
        violated_requirements.add(req_id)
        if policy_id:
            enforced_policies.add(policy_id)
            trace_paths.add(f"{prefix} -> Requirement({req_id}) -> Policy({policy_id})")
        else:
            trace_paths.add(f"{prefix} -> Requirement({req_id})")


def _blast_radius_files(base: dict) -> list[str]:
    """Full import/call closure (unfiltered). Used for MCP + agent planning."""
    return sorted(
        set(base.get("direct_files", []))
        | set(base.get("transitive_files", []))
        | set(base.get("call_chain_files", []))
    )


def _with_full_blast_radius(base: dict, detail: dict) -> dict:
    """Promote the complete closure to top-level affected_files (agents read this field)."""
    full = _blast_radius_files(base)
    backend_only = sorted(set(base.get("affected_files", [])))
    base_out = dict(base)
    base_out["affected_files_all"] = full
    base_out["affected_files_backend"] = backend_only
    base_out["affected_files"] = full
    detail["affected_files"] = sorted(set(full) | set(detail.get("affected_files", [])))
    detail["impact"] = base_out
    return detail


def impact_analysis(
    graph: GraphDB,
    entity_type: str,
    entity_name: str,
    backend_paths: Iterable[str] | None = None,
):
    if entity_type == "table":
        base = table_impact_analysis(graph, entity_name, backend_paths=backend_paths)
        detail = explain_change(graph, entity_type, entity_name, backend_paths=backend_paths)
        return _with_full_blast_radius(base, detail)
    if entity_type == "column":
        if "." in entity_name:
            table, column = entity_name.split(".", 1)
        else:
            raise ValueError("column impact requires table.column")
        base = column_impact_analysis(graph, table, column, backend_paths=backend_paths)
        detail = explain_change(graph, entity_type, entity_name, backend_paths=backend_paths)
        return _with_full_blast_radius(base, detail)
    if entity_type == "file":
        base = file_blast_radius(graph, entity_name)
        detail = explain_change(graph, entity_type, entity_name, backend_paths=backend_paths)
        return _with_full_blast_radius(base, detail)
    raise ValueError(f"Unknown entity_type: {entity_type}")
