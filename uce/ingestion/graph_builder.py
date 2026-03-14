from __future__ import annotations

import os
import re
from typing import Iterable

from uce.core.graph_db import GraphDB
from uce.ingestion.code_parser import ParsedCode
from uce.ingestion.requirement_parser import RequirementDoc
from uce.ingestion.policy_parser import PolicyDoc


def normalize_path(path: str) -> str:
    return path.replace("\\", "/")


def is_ignored(path: str, ignore: Iterable[str]) -> bool:
    normalized = normalize_path(path)
    for token in ignore:
        token_norm = normalize_path(token).strip("/")
        if not token_norm:
            continue
        if f"/{token_norm}/" in f"/{normalized}/" or normalized.startswith(f"{token_norm}/"):
            return True
    return False


def ensure_relative(path: str, root: str) -> str:
    rel = os.path.relpath(path, root)
    return normalize_path(rel)


def resolve_import(
    source_rel: str,
    import_path: str,
    project_root: str,
    aliases: dict[str, str],
    extensions: tuple[str, ...],
) -> str | None:
    if not import_path:
        return None

    normalized = import_path.strip()

    for alias, target in aliases.items():
        if normalized.startswith(alias):
            normalized = os.path.join(target, normalized[len(alias) :])
            break

    if normalized.startswith("/"):
        candidate_base = os.path.join(project_root, normalized.lstrip("/"))
    elif normalized.startswith("."):
        source_dir = os.path.dirname(source_rel)
        candidate_base = os.path.normpath(os.path.join(project_root, source_dir, normalized))
    else:
        return None

    candidate_base = os.path.normpath(candidate_base)

    if os.path.isfile(candidate_base):
        return ensure_relative(candidate_base, project_root)

    base_no_ext, ext = os.path.splitext(candidate_base)
    if ext:
        if os.path.isfile(candidate_base):
            return ensure_relative(candidate_base, project_root)
        return None

    for ext in extensions:
        candidate = base_no_ext + ext
        if os.path.isfile(candidate):
            return ensure_relative(candidate, project_root)

    index_candidates = [
        os.path.join(candidate_base, "index" + ext) for ext in extensions
    ]
    for candidate in index_candidates:
        if os.path.isfile(candidate):
            return ensure_relative(candidate, project_root)

    return None


def _prune_missing_functions(graph: GraphDB, rel_path: str, keep_names: set[str]) -> None:
    if not keep_names:
        graph.run("MATCH (fn:Function {file_path: $path}) DETACH DELETE fn", path=rel_path)
        return
    graph.run(
        "MATCH (fn:Function {file_path: $path}) WHERE NOT fn.name IN $names DETACH DELETE fn",
        path=rel_path,
        names=sorted(keep_names),
    )


def _prune_missing_classes(graph: GraphDB, rel_path: str, keep_names: set[str]) -> None:
    if not keep_names:
        graph.run("MATCH (c:Class {file_path: $path}) DETACH DELETE c", path=rel_path)
        return
    graph.run(
        "MATCH (c:Class {file_path: $path}) WHERE NOT c.name IN $names DETACH DELETE c",
        path=rel_path,
        names=sorted(keep_names),
    )


def upsert_code_file(
    graph: GraphDB,
    rel_path: str,
    parsed: ParsedCode,
    project_root: str,
    aliases: dict[str, str],
    extensions: tuple[str, ...],
    ignore: Iterable[str],
) -> None:
    graph.clear_file_relationships(rel_path)
    graph.ensure_file(rel_path)

    method_names = {name for name, _ in parsed.methods}
    keep_function_names = set(parsed.functions) | method_names
    _prune_missing_functions(graph, rel_path, keep_function_names)
    _prune_missing_classes(graph, rel_path, set(parsed.classes))

    for imported in parsed.imports:
        resolved = resolve_import(rel_path, imported, project_root, aliases, extensions)
        if resolved:
            if is_ignored(resolved, ignore):
                continue
            graph.ensure_file(resolved)
            graph.run(
                """
                MATCH (a:File {path: $source})
                MATCH (b:File {path: $target})
                MERGE (a)-[:IMPORTS]->(b)
                """,
                source=rel_path,
                target=resolved,
            )
        else:
            continue

    for class_name in parsed.classes:
        graph.run(
            "MERGE (c:Class {name: $name, file_path: $file_path})",
            name=class_name,
            file_path=rel_path,
        )
        graph.run(
            """
            MATCH (f:File {path: $path})
            MATCH (c:Class {name: $name, file_path: $file_path})
            MERGE (f)-[:DECLARES_CLASS]->(c)
            """,
            path=rel_path,
            name=class_name,
            file_path=rel_path,
        )

    for function_name in parsed.functions:
        graph.run(
            "MERGE (fn:Function {name: $name, file_path: $file_path})",
            name=function_name,
            file_path=rel_path,
        )
        graph.run(
            """
            MATCH (f:File {path: $path})
            MATCH (fn:Function {name: $name, file_path: $file_path})
            MERGE (f)-[:DECLARES_FUNCTION]->(fn)
            """,
            path=rel_path,
            name=function_name,
            file_path=rel_path,
        )

    for method_name, class_name in parsed.methods:
        graph.run(
            """
            MERGE (m:Function:Method {name: $name, file_path: $file_path})
            SET m.class_name = $class_name
            """,
            name=method_name,
            file_path=rel_path,
            class_name=class_name,
        )
        graph.run(
            """
            MATCH (f:File {path: $path})
            MATCH (m:Function:Method {name: $name, file_path: $file_path})
            MERGE (f)-[:DECLARES_FUNCTION]->(m)
            """,
            path=rel_path,
            name=method_name,
            file_path=rel_path,
        )
        if class_name:
            graph.run(
                """
                MATCH (c:Class {name: $class_name, file_path: $file_path})
                MATCH (m:Function:Method {name: $name, file_path: $file_path})
                MERGE (c)-[:HAS_METHOD]->(m)
                """,
                class_name=class_name,
                name=method_name,
                file_path=rel_path,
            )

    caller_names = sorted(keep_function_names)
    for caller in caller_names:
        for callee in parsed.calls:
            graph.run(
                """
                MATCH (caller:Function {name: $caller, file_path: $file_path})
                MATCH (callee:Function {name: $callee})
                MERGE (caller)-[:CALLS]->(callee)
                """,
                caller=caller,
                callee=callee,
                file_path=rel_path,
            )


def _word_pattern(term: str):
    return re.compile(rf"(?<!\\w){re.escape(term)}(?!\\w)")


def load_tables(graph: GraphDB) -> list[str]:
    rows = graph.run("MATCH (t:Table) RETURN t.name AS name")
    return sorted({row["name"] for row in rows if row.get("name")})


def load_columns(graph: GraphDB) -> dict[str, list[str]]:
    rows = graph.run("MATCH (c:Column) RETURN c.name AS name, c.table AS table")
    columns: dict[str, list[str]] = {}
    for row in rows:
        name = row.get("name")
        table = row.get("table")
        if not name or not table:
            continue
        columns.setdefault(table, []).append(name)
    return columns


def link_tables_for_file(
    graph: GraphDB,
    rel_path: str,
    content: str,
    tables: list[str],
    columns_by_table: dict[str, list[str]],
) -> None:
    table_patterns = {name: _word_pattern(name) for name in tables}
    column_patterns = {
        table: {name: _word_pattern(name) for name in sorted(set(columns))}
        for table, columns in columns_by_table.items()
    }

    graph.run("MERGE (f:File {path: $path})", path=rel_path)

    for table_name, pattern in table_patterns.items():
        if pattern.search(content):
            graph.run(
                """
                MATCH (f:File {path: $path})
                MATCH (t:Table {name: $table})
                MERGE (f)-[:USES_TABLE]->(t)
                """,
                path=rel_path,
                table=table_name,
            )

    for table_name, patterns in column_patterns.items():
        for column_name, pattern in patterns.items():
            if pattern.search(content):
                graph.run(
                    """
                    MATCH (f:File {path: $path})
                    MATCH (c:Column {name: $column, table: $table})
                    MERGE (f)-[:REFERENCES_COLUMN]->(c)
                    """,
                    path=rel_path,
                    table=table_name,
                    column=column_name,
                )


def upsert_schema(graph: GraphDB, tables: list[dict]):
    for table in tables:
        table_name = table["name"]
        graph.run("MERGE (t:Table {name: $name})", name=table_name)
        for column in table["columns"]:
            graph.run(
                "MERGE (c:Column {name: $column, table: $table})",
                column=column,
                table=table_name,
            )
            graph.run(
                """
                MATCH (t:Table {name: $table})
                MATCH (c:Column {name: $column, table: $table})
                MERGE (t)-[:HAS_COLUMN]->(c)
                """,
                table=table_name,
                column=column,
            )


def upsert_requirements(
    graph: GraphDB,
    requirements: list[RequirementDoc],
    tables: list[str],
    columns_by_table: dict[str, list[str]],
) -> None:
    table_patterns = {name: _word_pattern(name) for name in tables}
    column_patterns = {
        table: {name: _word_pattern(name) for name in columns}
        for table, columns in columns_by_table.items()
    }

    for requirement in requirements:
        graph.run(
            """
            MERGE (r:Requirement {id: $id})
            SET r.title = $title,
                r.description = $description
            """,
            id=requirement.req_id,
            title=requirement.title,
            description=requirement.description,
        )

        text = f"{requirement.title} {requirement.description}"

        for table_name, pattern in table_patterns.items():
            if pattern.search(text):
                graph.run(
                    """
                    MATCH (r:Requirement {id: $id})
                    MATCH (t:Table {name: $table})
                    MERGE (r)-[:GOVERNS]->(t)
                    """,
                    id=requirement.req_id,
                    table=table_name,
                )

        for table_name, patterns in column_patterns.items():
            for column_name, pattern in patterns.items():
                if pattern.search(text):
                    graph.run(
                        """
                        MATCH (r:Requirement {id: $id})
                        MATCH (c:Column {name: $column, table: $table})
                        MERGE (r)-[:GOVERNS]->(c)
                        """,
                        id=requirement.req_id,
                        column=column_name,
                        table=table_name,
                    )


def upsert_policies(graph: GraphDB, policies: list[PolicyDoc]) -> None:
    for policy in policies:
        graph.run(
            "MERGE (p:Policy {id: $id}) SET p.description = $description",
            id=policy.policy_id,
            description=policy.description,
        )

        for req_id in policy.requirement_ids:
            graph.run("MERGE (r:Requirement {id: $id})", id=req_id)
            graph.run(
                """
                MATCH (p:Policy {id: $policy_id})
                MATCH (r:Requirement {id: $req_id})
                MERGE (p)-[:ENFORCES]->(r)
                """,
                policy_id=policy.policy_id,
                req_id=req_id,
            )
