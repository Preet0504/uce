import os
import re
from graph import GraphDB
from config import PROJECT_ROOT
from ingest.file_graph import is_ignored, normalize


def _word_pattern(term: str):
    return re.compile(rf"(?<!\\w){re.escape(term)}(?!\\w)")


def _load_tables(graph: GraphDB):
    rows = graph.run("MATCH (t:Table) RETURN t.name AS name")
    return sorted({row["name"] for row in rows if row["name"]})


def _load_columns(graph: GraphDB):
    rows = graph.run(
        "MATCH (c:Column) RETURN c.name AS name, c.table AS table"
    )
    columns = {}
    for row in rows:
        name = row["name"]
        table = row["table"]
        if not name or not table:
            continue
        columns.setdefault(table, []).append(name)
    return columns


def _iter_business_files():
    for root, _, files in os.walk(PROJECT_ROOT):
        for file in files:
            if not (file.endswith(".ts") or file.endswith(".tsx")):
                continue
            full_path = os.path.join(root, file)
            relative_path = normalize(os.path.relpath(full_path, PROJECT_ROOT))
            if is_ignored(relative_path):
                continue
            yield relative_path, full_path


def link_tables_and_columns():
    graph = GraphDB()

    tables = _load_tables(graph)
    columns_by_table = _load_columns(graph)

    table_patterns = {name: _word_pattern(name) for name in tables}
    column_patterns = {}
    for table, columns in columns_by_table.items():
        column_patterns[table] = {
            name: _word_pattern(name) for name in sorted(set(columns))
        }

    for relative_path, full_path in _iter_business_files():
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        graph.run("MERGE (f:File {path: $path})", path=relative_path)

        for table_name, pattern in table_patterns.items():
            if pattern.search(content):
                graph.run(
                    """
                    MATCH (f:File {path: $path})
                    MATCH (t:Table {name: $table})
                    MERGE (f)-[:USES_TABLE]->(t)
                    """,
                    path=relative_path,
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
                        path=relative_path,
                        table=table_name,
                        column=column_name,
                    )

    graph.close()


if __name__ == "__main__":
    link_tables_and_columns()
