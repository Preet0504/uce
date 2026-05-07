from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Iterable

from ingestion.llm_client import LLMClient, LLMClientError
from ingestion.llm_ingest import (
    DocumentReadError,
    _extract_metadata,
    _list_documents,
    _read_document,
    _sanitize_document_text,
    _short_error,
)

logger = logging.getLogger("uce.ingestion.llm_rbac")

VALID_OPERATIONS = {"write", "delete", "*"}
VALID_EFFECTS = {"allow", "deny"}
VALID_ROLES = {"viewer", "editor", "admin"}


class LLMRbacExtractionError(RuntimeError):
    pass


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, default=str)


def _candidate_json_strings(raw: str) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []

    candidates: list[str] = [text]
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE):
        block = match.group(1).strip()
        if block:
            candidates.append(block)

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
        raise LLMRbacExtractionError("No JSON content found in RBAC extraction output.")

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
    if last_error:
        raise LLMRbacExtractionError(f"Invalid JSON from RBAC extraction: {last_error}") from last_error
    raise LLMRbacExtractionError("Unable to parse RBAC extraction output.")


def _build_rbac_prompt(document_text: str, metadata: dict[str, Any], policy_id: str) -> str:
    schema = {
        "rules": [
            {
                "rule_id": "string (required, unique within this document)",
                "operation": "write|delete|*",
                "path_pattern": "relative/path/*",
                "min_role": "viewer|editor|admin",
                "effect": "allow|deny",
                "source_priority": 0,
            }
        ]
    }
    instructions = [
        "Return ONLY valid JSON. No markdown, no comments.",
        "Extract file authority RBAC rules from the text.",
        "Only include rules you are confident are explicitly stated.",
        "Use operation in {write, delete, *}.",
        "Use min_role in {viewer, editor, admin}.",
        "Use effect in {allow, deny}.",
        "Use path_pattern as repository-relative path or wildcard pattern.",
        "If no RBAC rules are present, return {\"rules\": []}.",
    ]
    return (
        "You are a strict RBAC extraction engine.\n"
        f"Policy id: {policy_id}\n\n"
        "Rules:\n"
        + "\n".join(f"- {line}" for line in instructions)
        + "\n\nOutput schema:\n"
        + _safe_json(schema)
        + "\n\nDocument metadata:\n"
        + _safe_json(metadata)
        + "\n\nDocument text:\n"
        + document_text
    )


def _is_relative_pattern(path_pattern: str) -> bool:
    cleaned = path_pattern.replace("\\", "/").strip()
    if not cleaned:
        return False
    if cleaned.startswith("/") or cleaned.startswith("../") or "/../" in cleaned:
        return False
    if re.match(r"^[A-Za-z]:[/\\]", cleaned):
        return False
    return True


def validate_extracted_rules(
    extracted: Any,
    *,
    default_policy_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(extracted, dict):
        raise LLMRbacExtractionError("RBAC extraction output must be a JSON object.")
    rows = extracted.get("rules")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise LLMRbacExtractionError("RBAC extraction output must include a 'rules' list.")

    cleaned: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        raw_rule_id = str(row.get("rule_id") or "").strip()
        rule_id = raw_rule_id or f"{default_policy_id}::RBAC::{index}"
        operation = str(row.get("operation") or "").strip().lower()
        path_pattern = str(row.get("path_pattern") or row.get("path") or "").strip()
        min_role = str(row.get("min_role") or "").strip().lower()
        effect = str(row.get("effect") or "").strip().lower()

        if operation not in VALID_OPERATIONS:
            continue
        if effect not in VALID_EFFECTS:
            continue
        if min_role not in VALID_ROLES:
            continue
        if not _is_relative_pattern(path_pattern):
            continue

        try:
            source_priority = int(row.get("source_priority", 0))
        except (TypeError, ValueError):
            source_priority = 0

        cleaned.append(
            {
                "policy_id": default_policy_id,
                "rule_id": rule_id,
                "operation": operation,
                "path_pattern": path_pattern.replace("\\", "/"),
                "min_role": min_role,
                "effect": effect,
                "source_priority": source_priority,
            }
        )
    return cleaned


def ingest_rbac_rules(rbac_dirs: Iterable[str]) -> list[dict[str, Any]]:
    directories = [path for path in rbac_dirs if os.path.isdir(path)]
    if not directories:
        logger.warning("LLM RBAC ingestion skipped: no RBAC directories found.")
        return []

    files = _list_documents(directories)
    if not files:
        logger.info("LLM RBAC ingestion: no documents found in configured RBAC directories.")
        return []

    llm = LLMClient()
    providers: list[str] = []
    for provider in [llm.primary, llm.fallback]:
        if provider and provider not in providers:
            providers.append(provider)

    by_rule_id: dict[str, dict[str, Any]] = {}

    logger.info("LLM RBAC ingestion: %d file(s) to process", len(files))
    for idx, full_path in enumerate(files, start=1):
        filename = os.path.basename(full_path)
        logger.info("[%d/%d] RBAC: reading %s", idx, len(files), filename)
        try:
            content = _sanitize_document_text(_read_document(full_path))
        except DocumentReadError as exc:
            logger.warning(
                "[%d/%d] RBAC: skipping %s due to read error: %s",
                idx,
                len(files),
                filename,
                _short_error(exc),
            )
            continue
        if not content:
            logger.warning("[%d/%d] RBAC: skipping %s due to empty text", idx, len(files), filename)
            continue

        metadata = _extract_metadata(content, filename)
        policy_id = str(
            metadata.get("doc_id")
            or os.path.splitext(filename)[0]
        ).strip()
        if not policy_id:
            policy_id = f"RBAC_DOC_{idx}"

        prompt = _build_rbac_prompt(content, metadata, policy_id)
        extracted: Any | None = None
        errors: list[str] = []
        for provider in providers:
            try:
                raw = llm.generate(prompt, provider=provider)
                extracted = _parse_llm_json_output(raw)
                break
            except (LLMClientError, LLMRbacExtractionError) as exc:
                errors.append(f"{provider}: {_short_error(exc)}")
                continue

        if extracted is None:
            logger.warning(
                "[%d/%d] RBAC: extraction failed for %s (%s)",
                idx,
                len(files),
                filename,
                " | ".join(errors),
            )
            continue

        try:
            rules = validate_extracted_rules(extracted, default_policy_id=policy_id)
        except LLMRbacExtractionError as exc:
            logger.warning(
                "[%d/%d] RBAC: invalid extraction schema for %s: %s",
                idx,
                len(files),
                filename,
                _short_error(exc),
            )
            continue

        for rule in rules:
            existing = by_rule_id.get(rule["rule_id"])
            if existing is not None:
                logger.info(
                    "RBAC rule id collision '%s' detected; last one wins.",
                    rule["rule_id"],
                )
            by_rule_id[rule["rule_id"]] = rule

        logger.info(
            "[%d/%d] RBAC: extracted %d valid rule(s) from %s",
            idx,
            len(files),
            len(rules),
            filename,
        )

    rules_out = list(by_rule_id.values())
    logger.info("LLM RBAC ingestion complete: %d rule(s) validated", len(rules_out))
    return rules_out
