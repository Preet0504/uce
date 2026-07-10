import json
import logging
import os
import re
from typing import Any

from uce.ingestion.llm_client import LLMClient, LLMClientError


class LLMExtractionError(RuntimeError):
    pass


logger = logging.getLogger("uce.llm_extract")


def _env_csv(name: str) -> list[str]:
    value = os.getenv(name)
    if not value:
        return []
    parts = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
    return parts


def _default_allowed_node_types(doc_kind: str) -> list[str]:
    kind = doc_kind.lower().strip()
    if kind == "requirement":
        return ["Requirement", "Table", "Column", "File", "Function", "Class", "Method"]
    if kind == "policy":
        return ["Policy", "Requirement", "Table", "Column", "File", "Function", "Class", "Method"]
    return []


def _default_allowed_edge_types(doc_kind: str) -> list[str]:
    kind = doc_kind.lower().strip()
    if kind == "requirement":
        return ["GOVERNS", "IMPLEMENTED_BY"]
    if kind == "policy":
        return ["ENFORCES", "APPLIES_TO", "GOVERNS"]
    return []


def _allowed_node_types(doc_kind: str) -> list[str]:
    override = _env_csv("LLM_ALLOWED_NODE_TYPES")
    if override:
        return override
    return _default_allowed_node_types(doc_kind)


def _allowed_edge_types(doc_kind: str) -> list[str]:
    override = _env_csv("LLM_ALLOWED_EDGE_TYPES")
    if override:
        return override
    return _default_allowed_edge_types(doc_kind)


def _safe_symbol(value: str) -> bool:
    if not value or value[0].isdigit():
        return False
    for char in value:
        if not (char.isalnum() or char == "_"):
            return False
    return True


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LLMExtractionError("Optional fields must be strings when present.")
    cleaned = value.strip()
    return cleaned or None


def _safe_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=True, default=str)


def build_prompt(
    document_text: str,
    metadata: dict[str, Any],
    graph_schema: Any,
    graph_context: dict[str, Any],
    doc_kind: str,
    allowed_node_types: list[str],
    allowed_edge_types: list[str],
) -> str:
    schema_definition = {
        "nodes": [
            {
                "id": "string",
                "type": "string",
                "description": "string (optional)",
                "category": "string (optional)",
            }
        ],
        "edges": [
            {
                "type": "string",
                "source_id": "string",
                "target_id": "string",
            }
        ],
    }

    instructions = [
        "Return ONLY valid JSON. No markdown, no comments.",
        "Include nodes for every edge endpoint, even if the node already exists.",
        "Use existing graph context to choose node IDs and types when possible.",
    ]
    if allowed_node_types:
        instructions.append("Only use node types from: " + ", ".join(allowed_node_types) + ".")
    if allowed_edge_types:
        instructions.append("Only use edge types from: " + ", ".join(allowed_edge_types) + ".")
    if "Table" in allowed_node_types:
        instructions.append("When referencing a Table, set node.type='Table' and node.id to the exact table name.")
    if "Column" in allowed_node_types:
        instructions.append("When referencing a Column, set node.type='Column' and node.id to 'table.column'.")
    if "File" in allowed_node_types:
        instructions.append("For File nodes, set node.id to the relative file path (same as File.path).")
    if "Function" in allowed_node_types:
        instructions.append("For Function nodes, set node.id to '<file_path>::<function_name>'.")
    if "Class" in allowed_node_types:
        instructions.append("For Class nodes, set node.id to '<file_path>::<class_name>'.")
    if "Method" in allowed_node_types:
        instructions.append("For Method nodes, set node.id to '<file_path>::<class_name>::<method_name>' and omit Method nodes if class name is unknown.")

    if doc_kind.lower() == "requirement":
        instructions.append("For requirements, relate Requirement nodes to Table/Column nodes using GOVERNS.")
        instructions.append("For requirements, relate Requirement nodes to File/Function/Class/Method nodes using IMPLEMENTED_BY.")
    if doc_kind.lower() == "policy":
        instructions.append("For policies, relate Policy nodes to Requirement nodes using ENFORCES.")
        instructions.append("For policies, relate Policy nodes to File/Function/Class/Method nodes using APPLIES_TO.")
        instructions.append("For policies, relate Policy nodes to Table/Column nodes using GOVERNS when applicable.")

    prompt = (
        "You are a strict JSON extraction engine.\n"
        f"Document type: {doc_kind}\n\n"
        "Rules:\n"
        + "\n".join(f"- {line}" for line in instructions)
        + "\n\nSchema:\n"
        + _safe_json(schema_definition)
        + "\n\nDocument metadata:\n"
        + _safe_json(metadata)
        + "\n\nGraph schema (from MCP get-schema):\n"
        + _safe_json(graph_schema)
        + "\n\nGraph context (from MCP read-cypher):\n"
        + _safe_json(graph_context)
        + "\n\nDocument text:\n"
        + document_text
    )
    return prompt


def _candidate_json_strings(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []

    candidates: list[str] = [text]

    # Common model pattern: wrap JSON in markdown code fences.
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE):
        block = match.group(1).strip()
        if block:
            candidates.append(block)

    # Fallback: recover the first parseable JSON payload from surrounding text.
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            _, end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        snippet = text[idx : idx + end].strip()
        if snippet:
            candidates.append(snippet)
            break

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def _parse_llm_json_output(raw: str) -> Any:
    candidates = _candidate_json_strings(raw)
    if not candidates:
        raise json.JSONDecodeError("No JSON content found in LLM output.", raw or "", 0)

    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    raise json.JSONDecodeError("Unable to parse LLM output as JSON.", raw or "", 0)


def validate_extraction(
    data: Any,
    allowed_node_types: list[str] | None = None,
    allowed_edge_types: list[str] | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise LLMExtractionError("LLM output must be a JSON object.")
    nodes = data.get("nodes")
    edges = data.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise LLMExtractionError("LLM output must include 'nodes' and 'edges' lists.")

    cleaned_nodes: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            raise LLMExtractionError("Each node must be an object.")
        node_id = node.get("id")
        node_type = node.get("type")
        if not isinstance(node_id, str) or not node_id.strip():
            raise LLMExtractionError("Node id is required.")
        if not isinstance(node_type, str) or not node_type.strip():
            raise LLMExtractionError("Node type is required.")
        node_type = node_type.strip()
        if not _safe_symbol(node_type):
            raise LLMExtractionError("Node type must be alphanumeric/underscore and not start with a digit.")
        if allowed_node_types and node_type not in allowed_node_types:
            raise LLMExtractionError(f"Node type '{node_type}' is not allowed.")

        description = _optional_str(node.get("description"))
        category = _optional_str(node.get("category"))

        cleaned_node: dict[str, Any] = {"id": node_id.strip(), "type": node_type}
        if description is not None:
            cleaned_node["description"] = description
        if category is not None:
            cleaned_node["category"] = category
        cleaned_nodes.append(cleaned_node)

    if not cleaned_nodes:
        if not strict:
            return {"nodes": [], "edges": []}
        raise LLMExtractionError("LLM output contained no nodes.")

    node_ids = {node["id"] for node in cleaned_nodes}

    cleaned_edges: list[dict[str, Any]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            raise LLMExtractionError("Each edge must be an object.")
        edge_type = edge.get("type")
        source_id = edge.get("source_id")
        target_id = edge.get("target_id")
        if not isinstance(edge_type, str) or not edge_type.strip():
            raise LLMExtractionError("Edge type is required.")
        edge_type = edge_type.strip()
        if not _safe_symbol(edge_type):
            raise LLMExtractionError("Edge type must be alphanumeric/underscore and not start with a digit.")
        if allowed_edge_types and edge_type not in allowed_edge_types:
            raise LLMExtractionError(f"Edge type '{edge_type}' is not allowed.")
        if not isinstance(source_id, str) or not source_id.strip():
            raise LLMExtractionError("Edge source_id is required.")
        if not isinstance(target_id, str) or not target_id.strip():
            raise LLMExtractionError("Edge target_id is required.")

        source_id = source_id.strip()
        target_id = target_id.strip()
        if source_id not in node_ids or target_id not in node_ids:
            if strict:
                raise LLMExtractionError(
                    "Edges must reference node ids that are included in the nodes list."
                )
            logger.warning(
                "Skipping edge %s -> %s due to missing node references.", source_id, target_id
            )
            continue

        cleaned_edges.append(
            {
                "type": edge_type,
                "source_id": source_id,
                "target_id": target_id,
            }
        )

    return {"nodes": cleaned_nodes, "edges": cleaned_edges}


def extract_graph_updates(
    document_text: str,
    metadata: dict[str, Any],
    graph_schema: Any,
    graph_context: dict[str, Any],
    doc_kind: str,
    llm_client: LLMClient | None = None,
) -> dict[str, Any]:
    if not document_text:
        raise LLMExtractionError("Document text is empty.")

    client = llm_client or LLMClient()
    allowed_node_types = _allowed_node_types(doc_kind)
    allowed_edge_types = _allowed_edge_types(doc_kind)
    prompt = build_prompt(
        document_text,
        metadata,
        graph_schema,
        graph_context,
        doc_kind,
        allowed_node_types=allowed_node_types,
        allowed_edge_types=allowed_edge_types,
    )

    errors: list[str] = []
    providers: list[str] = []
    for provider in [client.primary, client.fallback]:
        if provider and provider not in providers:
            providers.append(provider)

    for provider in providers:
        if not provider:
            continue
        try:
            raw = client.generate(prompt, provider=provider)
            parsed = _parse_llm_json_output(raw)
            return validate_extraction(
                parsed,
                allowed_node_types=allowed_node_types,
                allowed_edge_types=allowed_edge_types,
                strict=True,
            )
        except (json.JSONDecodeError, LLMClientError) as exc:
            errors.append(f"{provider}: {exc}")
            continue
        except LLMExtractionError as exc:
            errors.append(f"{provider}: {exc}")
            continue

    raise LLMExtractionError(
        "LLM extraction failed after trying primary and fallback providers. "
        + " | ".join(errors)
    )
