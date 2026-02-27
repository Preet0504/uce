import os
import re
from graph import GraphDB

REQUIREMENTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "requirements"))


def _word_pattern(term: str):
    return re.compile(rf"(?<!\\w){re.escape(term)}(?!\\w)")


def _parse_requirement(content: str):
    req_id = None
    title = None
    description_lines = []
    in_description = False

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("ID:"):
            req_id = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("Title:"):
            title = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("Description:"):
            in_description = True
            description_lines.append(stripped.split(":", 1)[1].strip())
            continue
        if in_description and stripped:
            description_lines.append(stripped)

    description = " ".join([line for line in description_lines if line])
    return req_id, title, description


def _load_tables(graph: GraphDB):
    rows = graph.run("MATCH (t:Table) RETURN t.name AS name")
    return sorted({row["name"] for row in rows if row["name"]})


def _load_columns(graph: GraphDB):
    rows = graph.run("MATCH (c:Column) RETURN c.name AS name, c.table AS table")
    columns = []
    for row in rows:
        name = row["name"]
        table = row["table"]
        if not name or not table:
            continue
        columns.append((name, table))
    return columns


def ingest_requirements():
    if not os.path.isdir(REQUIREMENTS_DIR):
        raise FileNotFoundError(f"Requirements directory not found: {REQUIREMENTS_DIR}")

    graph = GraphDB()
    tables = _load_tables(graph)
    columns = _load_columns(graph)

    for filename in sorted(os.listdir(REQUIREMENTS_DIR)):
        if not filename.endswith(".md"):
            continue

        full_path = os.path.join(REQUIREMENTS_DIR, filename)
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        req_id, title, description = _parse_requirement(content)
        if not req_id or not title:
            continue

        graph.run(
            """
            MERGE (r:Requirement {id: $id})
            SET r.title = $title,
                r.description = $description
            """,
            id=req_id,
            title=title,
            description=description,
        )

        text = f"{title} {description}"

        for table_name in tables:
            if _word_pattern(table_name).search(text):
                graph.run(
                    """
                    MATCH (r:Requirement {id: $id})
                    MATCH (t:Table {name: $table})
                    MERGE (r)-[:GOVERNS]->(t)
                    """,
                    id=req_id,
                    table=table_name,
                )

        for column_name, table_name in columns:
            if _word_pattern(column_name).search(text):
                graph.run(
                    """
                    MATCH (r:Requirement {id: $id})
                    MATCH (c:Column {name: $column, table: $table})
                    MERGE (r)-[:GOVERNS]->(c)
                    """,
                    id=req_id,
                    column=column_name,
                    table=table_name,
                )

    graph.close()


if __name__ == "__main__":
    ingest_requirements()
