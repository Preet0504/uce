import logging
import os
import re
import zipfile
from xml.etree import ElementTree
from typing import Any, Iterable, TYPE_CHECKING

from uce.ingestion.llm_client import LLMClient, LLMClientError
from uce.ingestion.llm_extract import extract_graph_updates, LLMExtractionError
from uce.ingestion.mcp_neo4j import McpNeo4jClient, McpNeo4jError

if TYPE_CHECKING:
    from uce.core.graph_db import GraphDB

logger = logging.getLogger("uce.ingestion.llm")

ARTIFACTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "artifacts"))
REQUIREMENTS_DIR = os.path.join(ARTIFACTS_DIR, "requirements")
POLICIES_DIR = os.path.join(ARTIFACTS_DIR, "policies")
SUPPORTED_DOC_EXTENSIONS = (".md", ".txt", ".pdf", ".docx", ".doc")

# Static UCE graph schema for LLM context (more reliable than APOC get-schema)
_UCE_STATIC_SCHEMA = {
    "nodes": [
        {"label": "File", "properties": ["path", "id", "language", "last_modified"]},
        {"label": "Table", "properties": ["name"]},
        {"label": "Column", "properties": ["name", "table"]},
        {"label": "Requirement", "properties": ["id", "description"]},
        {"label": "Policy", "properties": ["id", "description"]},
        {"label": "Function", "properties": ["name", "file_path", "id"]},
        {"label": "Class", "properties": ["name", "file_path", "id"]},
        {"label": "Method", "properties": ["name", "file_path", "class_name", "id"]},
        {"label": "Identifier", "properties": ["name"]},
        {"label": "Role", "properties": ["name", "rank"]},
        {"label": "AuthorityRule", "properties": ["id", "operation", "path_pattern", "min_role", "effect"]},
    ],
    "relationships": [
        "File-IMPORTS->File", "File-USES_TABLE->Table", "File-REFERENCES_COLUMN->Column",
        "File-DECLARES_FUNCTION->Function", "File-DECLARES_CLASS->Class",
        "Table-HAS_COLUMN->Column", "Requirement-GOVERNS->Table", "Requirement-GOVERNS->Column",
        "Policy-ENFORCES->Requirement", "Function-CALLS->Function", "Class-HAS_METHOD->Method",
        "File-USES_IDENTIFIER->Identifier", "Requirement-IMPLEMENTED_BY->File",
        "Policy-APPLIES_TO->File",
    ],
}


class DocumentReadError(RuntimeError):
    pass


def _extract_metadata(content: str, filename: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "filename": filename,
        "doc_id": None,
        "owner": None,
        "effective_date": None,
    }
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if lower.startswith("id:"):
            metadata["doc_id"] = stripped.split(":", 1)[1].strip()
        elif lower.startswith("owner:"):
            metadata["owner"] = stripped.split(":", 1)[1].strip()
        elif lower.startswith("effective date:") or lower.startswith("effective_date:"):
            metadata["effective_date"] = stripped.split(":", 1)[1].strip()
    return metadata


def _list_documents(directories: Iterable[str]) -> list[str]:
    files: list[str] = []
    for directory in directories:
        for root, _, names in os.walk(directory):
            for name in sorted(names):
                if os.path.splitext(name)[1].lower() in SUPPORTED_DOC_EXTENSIONS:
                    files.append(os.path.join(root, name))
    return sorted(files)


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as file:
        return file.read()


def _read_pdf_file(path: str) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - optional dependency
        raise DocumentReadError(
            "PDF parsing requires pypdf. Install with `pip install pypdf`."
        ) from exc

    try:
        reader = PdfReader(path)
        pages: list[str] = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            if page_text:
                pages.append(page_text)
        return "\n".join(pages).strip()
    except Exception as exc:
        raise DocumentReadError(f"Failed to parse PDF: {exc}") from exc


def _read_docx_file(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            xml_bytes = archive.read("word/document.xml")
    except Exception as exc:
        raise DocumentReadError(f"Failed to open DOCX: {exc}") from exc

    try:
        root = ElementTree.fromstring(xml_bytes)
    except Exception as exc:
        raise DocumentReadError(f"Failed to parse DOCX XML: {exc}") from exc

    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", namespace):
        texts = []
        for node in paragraph.findall(".//w:t", namespace):
            if node.text:
                texts.append(node.text)
        line = "".join(texts).strip()
        if line:
            paragraphs.append(line)

    return "\n".join(paragraphs).strip()


def _read_doc_file(path: str) -> str:
    try:
        import textract
    except Exception as exc:  # pragma: no cover - optional dependency
        raise DocumentReadError(
            "DOC parsing requires textract. Install with `pip install textract`, or convert .doc to .docx/.txt/.pdf."
        ) from exc

    try:
        raw = textract.process(path)
    except Exception as exc:
        raise DocumentReadError(f"Failed to parse DOC: {exc}") from exc
    return raw.decode("utf-8", errors="ignore").strip()


def _read_document(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in {".md", ".txt"}:
        return _read_text_file(path)
    if ext == ".pdf":
        return _read_pdf_file(path)
    if ext == ".docx":
        return _read_docx_file(path)
    if ext == ".doc":
        return _read_doc_file(path)
    raise DocumentReadError(f"Unsupported document extension: {ext}")


def _sanitize_document_text(content: str) -> str:
    text = content.replace("\x00", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_graph_context(mcp: McpNeo4jClient) -> dict[str, Any]:
    tables_rows = mcp.read_cypher("MATCH (t:Table) RETURN t.name AS name ORDER BY name")
    columns_rows = mcp.read_cypher(
        "MATCH (c:Column) RETURN c.name AS name, c.table AS table ORDER BY c.table, c.name"
    )
    requirement_rows = mcp.read_cypher("MATCH (r:Requirement) RETURN r.id AS id ORDER BY id")
    policy_rows = mcp.read_cypher("MATCH (p:Policy) RETURN p.id AS id ORDER BY id")

    tables: list[str] = []
    if isinstance(tables_rows, list):
        for row in tables_rows:
            if isinstance(row, dict):
                name = row.get("name")
                if name:
                    tables.append(name)

    columns: list[dict[str, str]] = []
    if isinstance(columns_rows, list):
        for row in columns_rows:
            if isinstance(row, dict):
                name = row.get("name")
                table = row.get("table")
                if name and table:
                    columns.append({"name": name, "table": table})

    requirements: list[str] = []
    if isinstance(requirement_rows, list):
        for row in requirement_rows:
            if isinstance(row, dict):
                req_id = row.get("id")
                if req_id:
                    requirements.append(req_id)

    policies: list[str] = []
    if isinstance(policy_rows, list):
        for row in policy_rows:
            if isinstance(row, dict):
                pol_id = row.get("id")
                if pol_id:
                    policies.append(pol_id)

    code_limit = _env_int("LLM_CODE_CONTEXT_LIMIT", 200)
    files: list[dict[str, str]] = []
    functions: list[dict[str, str]] = []
    classes: list[dict[str, str]] = []
    methods: list[dict[str, str]] = []

    if code_limit > 0:
        file_rows = mcp.read_cypher(
            "MATCH (f:File) RETURN f.id AS id, f.path AS path ORDER BY f.path LIMIT $limit",
            {"limit": code_limit},
        )
        if isinstance(file_rows, list):
            for row in file_rows:
                if isinstance(row, dict) and row.get("id") and row.get("path"):
                    files.append({"id": row["id"], "path": row["path"]})

        function_rows = mcp.read_cypher(
            "MATCH (fn:Function) RETURN fn.id AS id, fn.name AS name, fn.file_path AS file_path ORDER BY fn.file_path, fn.name LIMIT $limit",
            {"limit": code_limit},
        )
        if isinstance(function_rows, list):
            for row in function_rows:
                if isinstance(row, dict) and row.get("id"):
                    functions.append(
                        {
                            "id": row.get("id"),
                            "name": row.get("name"),
                            "file_path": row.get("file_path"),
                        }
                    )

        class_rows = mcp.read_cypher(
            "MATCH (c:Class) RETURN c.id AS id, c.name AS name, c.file_path AS file_path ORDER BY c.file_path, c.name LIMIT $limit",
            {"limit": code_limit},
        )
        if isinstance(class_rows, list):
            for row in class_rows:
                if isinstance(row, dict) and row.get("id"):
                    classes.append(
                        {
                            "id": row.get("id"),
                            "name": row.get("name"),
                            "file_path": row.get("file_path"),
                        }
                    )

        method_rows = mcp.read_cypher(
            "MATCH (m:Method) RETURN m.id AS id, m.name AS name, m.class_name AS class_name, m.file_path AS file_path ORDER BY m.file_path, m.class_name, m.name LIMIT $limit",
            {"limit": code_limit},
        )
        if isinstance(method_rows, list):
            for row in method_rows:
                if isinstance(row, dict) and row.get("id"):
                    methods.append(
                        {
                            "id": row.get("id"),
                            "name": row.get("name"),
                            "class_name": row.get("class_name"),
                            "file_path": row.get("file_path"),
                        }
                    )

    return {
        "tables": sorted(set(tables)),
        "columns": columns,
        "requirements": sorted(set(requirements)),
        "policies": sorted(set(policies)),
        "files": files,
        "functions": functions,
        "classes": classes,
        "methods": methods,
    }


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _safe_label(value: str) -> bool:
    if not value or value[0].isdigit():
        return False
    for char in value:
        if not (char.isalnum() or char == "_"):
            return False
    return True


REFERENCE_NODE_TYPES = {"Table", "Column", "File", "Function", "Class", "Method"}


def _parse_column_id(node_id: str) -> tuple[str, str] | None:
    if not node_id:
        return None
    for sep in (".", ":"):
        if sep in node_id:
            table, column = node_id.split(sep, 1)
            table = table.strip()
            column = column.strip()
            if table and column:
                return table, column
    return None


def _match_node(alias: str, node_type: str, node_id: str) -> tuple[str | None, dict[str, Any] | None]:
    if node_type == "Table":
        return (
            f"MATCH ({alias}:Table {{name: ${alias}_name}})",
            {f"{alias}_name": node_id},
        )
    if node_type == "Column":
        parsed = _parse_column_id(node_id)
        if not parsed:
            return None, None
        table, column = parsed
        return (
            f"MATCH ({alias}:Column {{table: ${alias}_table, name: ${alias}_name}})",
            {f"{alias}_table": table, f"{alias}_name": column},
        )
    return (
        f"MATCH ({alias}:{node_type} {{id: ${alias}_id}})",
        {f"{alias}_id": node_id},
    )


def _apply_graph_updates(mcp: McpNeo4jClient, extraction: dict[str, Any]) -> None:
    nodes = extraction.get("nodes") or []
    edges = extraction.get("edges") or []

    id_to_type: dict[str, str] = {}
    for node in nodes:
        node_id = node["id"]
        node_type = node["type"]
        existing = id_to_type.get(node_id)
        if existing and existing != node_type:
            raise ValueError(f"Conflicting node types for id {node_id}: {existing} vs {node_type}")
        id_to_type[node_id] = node_type

    missing_ids: set[str] = set()

    for node in nodes:
        node_id = node["id"]
        node_type = node["type"]
        if not _safe_label(node_type):
            raise ValueError(f"Unsafe node label: {node_type}")

        if node_type in REFERENCE_NODE_TYPES:
            match_query, params = _match_node("n", node_type, node_id)
            if not match_query or params is None:
                missing_ids.add(node_id)
                continue
            rows = mcp.read_cypher(match_query + " RETURN n LIMIT 1", params)
            if not rows:
                missing_ids.add(node_id)
            continue

        props: dict[str, Any] = {}
        for field in ("description", "category"):
            value = node.get(field)
            if value is not None:
                props[field] = value

        mcp.write_cypher(
            f"MERGE (n:{node_type} {{id: $id}}) SET n += $props REMOVE n.title, n.confidence, n.evidence_spans",
            {"id": node_id, "props": props},
        )

    for edge in edges:
        rel_type = edge["type"]
        if not _safe_label(rel_type):
            raise ValueError(f"Unsafe relationship type: {rel_type}")

        source_id = edge["source_id"]
        target_id = edge["target_id"]
        if source_id in missing_ids or target_id in missing_ids:
            continue

        source_type = id_to_type[source_id]
        target_type = id_to_type[target_id]
        if not _safe_label(source_type) or not _safe_label(target_type):
            raise ValueError("Unsafe node label in edge endpoints.")

        source_match, source_params = _match_node("s", source_type, source_id)
        target_match, target_params = _match_node("t", target_type, target_id)
        if not source_match or not target_match or source_params is None or target_params is None:
            continue

        params: dict[str, Any] = {
            **source_params,
            **target_params,
        }

        mcp.write_cypher(
            f"{source_match}\n{target_match}\nMERGE (s)-[r:{rel_type}]->(t)",
            params,
        )


def _coerce_dirs(value: str | Iterable[str] | None, default_dir: str) -> list[str]:
    if value is None:
        return [default_dir]
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value if v]


def _short_error(exc: Exception, limit: int = 160) -> str:
    text = str(exc).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _ingest_documents(
    doc_kind: str,
    directories: Iterable[str],
) -> None:
    existing_dirs = [path for path in directories if os.path.isdir(path)]
    if not existing_dirs:
        raise FileNotFoundError(
            f"{doc_kind} directory not found: " + ", ".join(directories)
        )

    mcp = McpNeo4jClient()
    llm = LLMClient()
    summary: list[dict[str, Any]] = []
    processed = 0
    skipped = 0
    total_nodes = 0
    total_edges = 0

    files = _list_documents(existing_dirs)
    logger.info("LLM %s ingestion: %d file(s) to process", doc_kind.lower(), len(files))

    try:
        if mcp.read_only:
            raise McpNeo4jError(f"NEO4J_READ_ONLY is true; {doc_kind.lower()} ingestion requires write access.")

        logger.info("Fetching graph schema from MCP...")
        schema = mcp.get_schema()
        logger.info("Fetching graph context from MCP...")
        context = _load_graph_context(mcp)
        logger.info("Schema/context ready. Processing files...")

        for idx, full_path in enumerate(files, 1):
            filename = os.path.basename(full_path)
            logger.info("[%d/%d] %s: reading %s", idx, len(files), doc_kind, filename)
            try:
                content = _sanitize_document_text(_read_document(full_path))
            except DocumentReadError as exc:
                skipped += 1
                summary.append(
                    {
                        "filename": filename,
                        "nodes": 0,
                        "edges": 0,
                        "status": f"error: {_short_error(exc)}",
                    }
                )
                logger.warning(
                    "[%d/%d] %s: skipping %s due to read error: %s",
                    idx,
                    len(files),
                    doc_kind,
                    filename,
                    exc,
                )
                continue

            if not content:
                skipped += 1
                summary.append(
                    {
                        "filename": filename,
                        "nodes": 0,
                        "edges": 0,
                        "status": "error: document text is empty after extraction",
                    }
                )
                logger.warning(
                    "[%d/%d] %s: skipping %s because extracted text is empty",
                    idx,
                    len(files),
                    doc_kind,
                    filename,
                )
                continue

            metadata = _extract_metadata(content, filename)
            logger.info("[%d/%d] %s: calling LLM for %s", idx, len(files), doc_kind, filename)
            try:
                extraction = extract_graph_updates(
                    document_text=content,
                    metadata=metadata,
                    graph_schema=schema,
                    graph_context=context,
                    doc_kind=doc_kind,
                    llm_client=llm,
                )
                logger.info(
                    "[%d/%d] %s: applying %d node(s) / %d edge(s) for %s",
                    idx,
                    len(files),
                    doc_kind,
                    len(extraction.get("nodes") or []),
                    len(extraction.get("edges") or []),
                    filename,
                )
                _apply_graph_updates(mcp, extraction)
                nodes_count = len(extraction.get("nodes") or [])
                edges_count = len(extraction.get("edges") or [])
                processed += 1
                total_nodes += nodes_count
                total_edges += edges_count
                summary.append(
                    {
                        "filename": filename,
                        "nodes": nodes_count,
                        "edges": edges_count,
                        "status": "ok",
                    }
                )
                logger.info("[%d/%d] %s: done %s", idx, len(files), doc_kind, filename)
            except (LLMExtractionError, LLMClientError, ValueError, McpNeo4jError) as exc:
                skipped += 1
                summary.append(
                    {
                        "filename": filename,
                        "nodes": 0,
                        "edges": 0,
                        "status": f"error: {_short_error(exc)}",
                    }
                )
                logger.warning(
                    "[%d/%d] %s: skipping %s due to error: %s",
                    idx,
                    len(files),
                    doc_kind,
                    filename,
                    exc,
                )
                continue
    finally:
        if summary:
            logger.info(
                "LLM %s ingestion summary: processed=%d skipped=%d nodes=%d edges=%d",
                doc_kind.lower(),
                processed,
                skipped,
                total_nodes,
                total_edges,
            )
            for item in summary:
                logger.info(
                    "LLM %s file summary: %s nodes=%d edges=%d status=%s",
                    doc_kind.lower(),
                    item["filename"],
                    item["nodes"],
                    item["edges"],
                    item["status"],
                )
        mcp.close()


def ingest_requirements(requirements_dir: str | Iterable[str] | None = None) -> None:
    dirs = _coerce_dirs(requirements_dir, REQUIREMENTS_DIR)
    _ingest_documents("Requirement", dirs)


def ingest_policies(policies_dir: str | Iterable[str] | None = None) -> None:
    dirs = _coerce_dirs(policies_dir, POLICIES_DIR)
    _ingest_documents("Policy", dirs)


# ---------------------------------------------------------------------------
# Direct-GraphDB path — no McpNeo4jClient subprocess required
# ---------------------------------------------------------------------------

def _load_graph_context_from_db(graph: "GraphDB") -> dict[str, Any]:
    """Load LLM context from a live GraphDB instance (no MCP sidecar needed)."""
    tables = sorted({
        row["name"] for row in graph.run("MATCH (t:Table) RETURN t.name AS name ORDER BY name")
        if row.get("name")
    })
    columns_rows = graph.run(
        "MATCH (c:Column) RETURN c.name AS name, c.table AS table ORDER BY c.table, c.name"
    )
    columns = [
        {"name": r["name"], "table": r["table"]}
        for r in columns_rows if r.get("name") and r.get("table")
    ]
    requirements = sorted({
        row["id"] for row in graph.run("MATCH (r:Requirement) RETURN r.id AS id ORDER BY id")
        if row.get("id")
    })
    policies = sorted({
        row["id"] for row in graph.run("MATCH (p:Policy) RETURN p.id AS id ORDER BY id")
        if row.get("id")
    })

    code_limit = _env_int("LLM_CODE_CONTEXT_LIMIT", 200)
    files: list[dict] = []
    functions: list[dict] = []
    classes: list[dict] = []
    methods: list[dict] = []

    if code_limit > 0:
        for row in graph.run(
            "MATCH (f:File) RETURN f.id AS id, f.path AS path ORDER BY f.path LIMIT $limit",
            limit=code_limit,
        ):
            if row.get("id") and row.get("path"):
                files.append({"id": row["id"], "path": row["path"]})

        for row in graph.run(
            "MATCH (fn:Function) RETURN fn.id AS id, fn.name AS name, fn.file_path AS file_path "
            "ORDER BY fn.file_path, fn.name LIMIT $limit",
            limit=code_limit,
        ):
            if row.get("id"):
                functions.append({"id": row["id"], "name": row.get("name"), "file_path": row.get("file_path")})

        for row in graph.run(
            "MATCH (c:Class) RETURN c.id AS id, c.name AS name, c.file_path AS file_path "
            "ORDER BY c.file_path, c.name LIMIT $limit",
            limit=code_limit,
        ):
            if row.get("id"):
                classes.append({"id": row["id"], "name": row.get("name"), "file_path": row.get("file_path")})

        for row in graph.run(
            "MATCH (m:Method) RETURN m.id AS id, m.name AS name, m.class_name AS class_name, "
            "m.file_path AS file_path ORDER BY m.file_path, m.class_name, m.name LIMIT $limit",
            limit=code_limit,
        ):
            if row.get("id"):
                methods.append({
                    "id": row["id"], "name": row.get("name"),
                    "class_name": row.get("class_name"), "file_path": row.get("file_path"),
                })

    return {
        "tables": tables,
        "columns": columns,
        "requirements": requirements,
        "policies": policies,
        "files": files,
        "functions": functions,
        "classes": classes,
        "methods": methods,
    }


def _apply_graph_updates_to_db(graph: "GraphDB", extraction: dict[str, Any]) -> None:
    """Apply LLM-extracted graph updates using a direct GraphDB connection."""
    nodes = extraction.get("nodes") or []
    edges = extraction.get("edges") or []

    id_to_type: dict[str, str] = {}
    for node in nodes:
        node_id = node["id"]
        node_type = node["type"]
        existing = id_to_type.get(node_id)
        if existing and existing != node_type:
            raise ValueError(f"Conflicting node types for id {node_id}: {existing} vs {node_type}")
        id_to_type[node_id] = node_type

    REFERENCE_NODE_TYPES = {"Table", "Column", "File", "Function", "Class", "Method"}
    missing_ids: set[str] = set()

    for node in nodes:
        node_id = node["id"]
        node_type = node["type"]

        if node_type in REFERENCE_NODE_TYPES:
            if node_type == "Table":
                rows = graph.run("MATCH (n:Table {name: $name}) RETURN n LIMIT 1", name=node_id)
            elif node_type == "Column":
                for sep in (".", ":"):
                    if sep in node_id:
                        table, column = node_id.split(sep, 1)
                        rows = graph.run(
                            "MATCH (n:Column {table: $table, name: $name}) RETURN n LIMIT 1",
                            table=table.strip(), name=column.strip(),
                        )
                        break
                else:
                    missing_ids.add(node_id)
                    continue
            else:
                rows = graph.run(f"MATCH (n:{node_type} {{id: $id}}) RETURN n LIMIT 1", id=node_id)

            if not rows:
                missing_ids.add(node_id)
            continue

        props: dict[str, Any] = {}
        for field in ("description", "category"):
            value = node.get(field)
            if value is not None:
                props[field] = value

        graph.run(
            f"MERGE (n:{node_type} {{id: $id}}) SET n += $props "
            "REMOVE n.title, n.confidence, n.evidence_spans",
            id=node_id, props=props,
        )

    for edge in edges:
        source_id = edge["source_id"]
        target_id = edge["target_id"]
        if source_id in missing_ids or target_id in missing_ids:
            continue

        rel_type = edge["type"]
        source_type = id_to_type[source_id]
        target_type = id_to_type[target_id]

        def _match_clause(alias: str, ntype: str, nid: str) -> tuple[str, dict]:
            if ntype == "Table":
                return f"MATCH ({alias}:Table {{name: ${alias}_name}})", {f"{alias}_name": nid}
            if ntype == "Column":
                for sep in (".", ":"):
                    if sep in nid:
                        tbl, col = nid.split(sep, 1)
                        return (
                            f"MATCH ({alias}:Column {{table: ${alias}_table, name: ${alias}_name}})",
                            {f"{alias}_table": tbl.strip(), f"{alias}_name": col.strip()},
                        )
                return "", {}
            return f"MATCH ({alias}:{ntype} {{id: ${alias}_id}})", {f"{alias}_id": nid}

        src_clause, src_params = _match_clause("s", source_type, source_id)
        tgt_clause, tgt_params = _match_clause("t", target_type, target_id)
        if not src_clause or not tgt_clause:
            continue

        graph.run(
            f"{src_clause}\n{tgt_clause}\nMERGE (s)-[r:{rel_type}]->(t)",
            **src_params,
            **tgt_params,
        )


def _ingest_documents_with_graph(
    doc_kind: str,
    directories: Iterable[str],
    graph: "GraphDB",
) -> None:
    """Ingest documents using a direct GraphDB connection — no McpNeo4jClient needed."""
    existing_dirs = [path for path in directories if os.path.isdir(path)]
    if not existing_dirs:
        logger.warning(
            "%s ingestion skipped: no directories found in %s",
            doc_kind,
            list(directories),
        )
        return

    llm = LLMClient()
    files = _list_documents(existing_dirs)
    logger.info("LLM %s ingestion (direct): %d file(s) to process", doc_kind.lower(), len(files))

    context = _load_graph_context_from_db(graph)
    processed = 0
    skipped = 0

    for idx, full_path in enumerate(files, 1):
        filename = os.path.basename(full_path)
        logger.info("[%d/%d] %s: reading %s", idx, len(files), doc_kind, filename)
        try:
            content = _sanitize_document_text(_read_document(full_path))
        except DocumentReadError as exc:
            skipped += 1
            logger.warning("[%d/%d] %s: skipping %s: %s", idx, len(files), doc_kind, filename, exc)
            continue

        if not content:
            skipped += 1
            logger.warning("[%d/%d] %s: skipping %s: empty text", idx, len(files), doc_kind, filename)
            continue

        metadata = _extract_metadata(content, filename)
        logger.info("[%d/%d] %s: calling LLM for %s", idx, len(files), doc_kind, filename)
        try:
            extraction = extract_graph_updates(
                document_text=content,
                metadata=metadata,
                graph_schema=_UCE_STATIC_SCHEMA,
                graph_context=context,
                doc_kind=doc_kind,
                llm_client=llm,
            )
            _apply_graph_updates_to_db(graph, extraction)
            processed += 1
            logger.info("[%d/%d] %s: done %s", idx, len(files), doc_kind, filename)
        except (LLMExtractionError, LLMClientError, ValueError) as exc:
            skipped += 1
            logger.warning(
                "[%d/%d] %s: skipping %s: %s", idx, len(files), doc_kind, filename,
                _short_error(exc),
            )

    logger.info(
        "LLM %s ingestion (direct) summary: processed=%d skipped=%d",
        doc_kind.lower(), processed, skipped,
    )


def ingest_requirements_with_graph(
    requirements_dirs: Iterable[str],
    graph: "GraphDB",
) -> None:
    """Ingest requirements using a direct GraphDB connection."""
    _ingest_documents_with_graph("Requirement", requirements_dirs, graph)


def ingest_policies_with_graph(
    policies_dirs: Iterable[str],
    graph: "GraphDB",
) -> None:
    """Ingest policies using a direct GraphDB connection."""
    _ingest_documents_with_graph("Policy", policies_dirs, graph)


if __name__ == "__main__":
    ingest_requirements()
