import logging
import os
import threading
import time
from typing import Any, cast

from fastmcp import FastMCP
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.auth import AccessToken, JWTVerifier
from fastmcp.server.dependencies import get_access_token

from uce.core.config import UceConfig
from uce.core.graph_db import GraphDB
from uce.core.rbac import (
    ROLE_RANKS,
    AuthorizationDecision,
    evaluate_rules,
    normalize_operation,
    normalize_project_path,
    normalize_role,
    rule_from_row,
)
from uce.reasoning import impact_analysis as impact_module
from uce.reasoning.trace_engine import (
    _load_columns,
    _load_files,
    _load_tables,
    detect_entity,
    find_candidates,
    preflight_assessment,
)


mcp = FastMCP(name="UnifiedContextEngine", version="0.2.1")
_CONFIG: UceConfig | None = None
_LOGGER = logging.getLogger("uce.mcp")

# Shared long-lived GraphDB driver (connection pool reused across all requests)
_DB: GraphDB | None = None
_DB_LOCK = threading.Lock()

# RBAC authority rule cache — TTL-based to avoid hitting Neo4j on every request
_RBAC_CACHE: list[dict] | None = None
_RBAC_CACHE_EXPIRES: float = 0.0
_RBAC_CACHE_TTL = float(os.getenv("UCE_RBAC_CACHE_TTL_SECONDS", "300"))
_RBAC_CACHE_LOCK = threading.Lock()


class _SkewJWTVerifier(JWTVerifier):
    def __init__(self, *args: Any, clock_skew_seconds: int = 0, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.clock_skew_seconds = max(int(clock_skew_seconds), 0)

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            verification_key = await self._get_verification_key(token)
            claims = self.jwt.decode(token, verification_key)

            client_id = (
                claims.get("client_id")
                or claims.get("azp")
                or claims.get("sub")
                or "unknown"
            )

            now = time.time()
            skew = float(self.clock_skew_seconds)

            exp = claims.get("exp")
            if exp is not None and float(exp) < (now - skew):
                _LOGGER.info("Bearer token rejected for client %s (expired)", client_id)
                return None

            nbf = claims.get("nbf")
            if nbf is not None and float(nbf) > (now + skew):
                _LOGGER.info("Bearer token rejected for client %s (nbf in future)", client_id)
                return None

            iat = claims.get("iat")
            if iat is not None and float(iat) > (now + skew):
                _LOGGER.info("Bearer token rejected for client %s (iat in future)", client_id)
                return None

            if self.issuer:
                token_issuer = claims.get("iss")
                issuer_valid = False
                if isinstance(self.issuer, list):
                    issuer_valid = token_issuer in self.issuer
                else:
                    issuer_valid = token_issuer == self.issuer
                if not issuer_valid:
                    _LOGGER.info("Bearer token rejected for client %s (issuer mismatch)", client_id)
                    return None

            if self.audience:
                token_audience = claims.get("aud")
                audience_valid = False
                if isinstance(self.audience, list):
                    if isinstance(token_audience, list):
                        audience_valid = any(expected in token_audience for expected in self.audience)
                    else:
                        audience_valid = token_audience in cast(list, self.audience)
                else:
                    if isinstance(token_audience, list):
                        audience_valid = self.audience in token_audience
                    else:
                        audience_valid = token_audience == self.audience
                if not audience_valid:
                    _LOGGER.info("Bearer token rejected for client %s (audience mismatch)", client_id)
                    return None

            scopes = self._extract_scopes(claims)
            if self.required_scopes:
                token_scopes = set(scopes)
                required_scopes = set(self.required_scopes)
                if not required_scopes.issubset(token_scopes):
                    _LOGGER.info(
                        "Bearer token rejected for client %s (missing required scopes)",
                        client_id,
                    )
                    return None

            expires_at = int(exp) if exp is not None else None
            return AccessToken(
                token=token,
                client_id=str(client_id),
                scopes=scopes,
                expires_at=expires_at,
                claims=claims,
            )
        except Exception:
            _LOGGER.debug("Token validation failed", exc_info=True)
            return None

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.load_access_token(token)


def _normalize_file_path(path: str, config: UceConfig | None) -> str:
    if not path:
        return path
    normalized = path.replace("\\", "/")
    if config:
        root = os.path.abspath(config.project_root)
        abs_path = os.path.abspath(path)
        try:
            if os.path.commonpath([abs_path, root]) == root:
                rel = os.path.relpath(abs_path, root)
                return rel.replace("\\", "/")
        except ValueError:
            pass
    return normalized


def _graph_from_config(config: UceConfig | None) -> GraphDB:
    """Return the shared GraphDB instance, creating it if needed."""
    global _DB
    if config is None:
        raise RuntimeError("UCE server config not initialized")
    with _DB_LOCK:
        if _DB is None:
            _DB = GraphDB(config.neo4j.uri, config.neo4j.user, config.neo4j.password)
        return _DB


def invalidate_rbac_cache() -> None:
    """Invalidate the in-process RBAC authority rule cache (call after RBAC re-ingestion)."""
    global _RBAC_CACHE, _RBAC_CACHE_EXPIRES
    with _RBAC_CACHE_LOCK:
        _RBAC_CACHE = None
        _RBAC_CACHE_EXPIRES = 0.0


def _backend_paths_from_config(config: UceConfig | None) -> tuple[str, ...]:
    if config is None:
        return tuple()
    return tuple(config.paths.backend)


def _collect_affected(result: dict):
    affected_files = result.get("affected_files")
    if affected_files:
        return sorted(set(affected_files))
    direct_files = result.get("direct_files") or []
    transitive_files = result.get("transitive_files") or []
    return sorted(set(direct_files) | set(transitive_files))


def _pick_first_nonempty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        cleaned = str(value).strip()
        if cleaned:
            return cleaned
    return ""


def _resolve_preflight_change(
    proposed_change: str = "",
    path: str = "",
    file_path: str = "",
    target_path: str = "",
    payload_input: dict[str, Any] | None = None,
) -> str:
    tool_input = payload_input or {}
    return _pick_first_nonempty(
        proposed_change,
        path,
        file_path,
        target_path,
        tool_input.get("proposed_change"),
        tool_input.get("path"),
        tool_input.get("file_path"),
        tool_input.get("target_path"),
    )


def _access_token_claims() -> dict[str, Any]:
    try:
        token = get_access_token()
    except Exception:
        return {}
    if token is None:
        return {}
    claims = getattr(token, "claims", None)
    return claims if isinstance(claims, dict) else {}


def _highest_known_role(candidates: list[str]) -> str | None:
    """Return the highest-ranked known role (viewer<editor<admin) from candidates."""
    best: str | None = None
    best_rank = -1
    for candidate in candidates:
        role = normalize_role(candidate)
        if role is None:
            continue
        rank = ROLE_RANKS[role]
        if rank > best_rank:
            best = role
            best_rank = rank
    return best


def _current_role_claim() -> str | None:
    """Resolve the caller's role from the access token.

    Supports both a custom flat ``role`` claim and the standard Keycloak
    locations (``realm_access.roles`` and ``resource_access.<client>.roles``).
    When multiple known roles are present the highest-ranked one wins.
    """
    claims = _access_token_claims()
    candidates: list[str] = []

    role = claims.get("role")
    if isinstance(role, str):
        candidates.append(role)
    elif isinstance(role, list):
        candidates.extend(str(item) for item in role)

    realm_access = claims.get("realm_access")
    if isinstance(realm_access, dict):
        realm_roles = realm_access.get("roles")
        if isinstance(realm_roles, list):
            candidates.extend(str(item) for item in realm_roles)

    resource_access = claims.get("resource_access")
    if isinstance(resource_access, dict):
        for entry in resource_access.values():
            if isinstance(entry, dict):
                client_roles = entry.get("roles")
                if isinstance(client_roles, list):
                    candidates.extend(str(item) for item in client_roles)

    return _highest_known_role(candidates)


def _rbac_enabled(config: UceConfig | None) -> bool:
    return bool(config and config.rbac.enabled)


def _rbac_mode(config: UceConfig | None) -> str:
    if config is None:
        return "advisory"
    return config.rbac.enforce_mode


def _load_rbac_rows_cached(config: UceConfig) -> list[dict]:
    """Load all RBAC authority rules with TTL caching to reduce Neo4j round-trips.

    All rules are loaded once per cache window without operation/path filtering.
    The caller's evaluate_rules call performs Python-side filtering, so the
    cached result is correct for any operation/path combination.
    """
    global _RBAC_CACHE, _RBAC_CACHE_EXPIRES
    now = time.time()
    with _RBAC_CACHE_LOCK:
        if _RBAC_CACHE is None or now >= _RBAC_CACHE_EXPIRES:
            graph = _graph_from_config(config)
            all_rows = graph.load_all_authority_rules()
            _RBAC_CACHE = all_rows
            _RBAC_CACHE_EXPIRES = now + _RBAC_CACHE_TTL
            return all_rows
        return _RBAC_CACHE


def _evaluate_rbac_decision(operation: str, raw_path: str) -> tuple[str, str, AuthorizationDecision]:
    config = _CONFIG
    if config is None:
        raise RuntimeError("UCE server config not initialized")

    absolute_path, normalized_path = normalize_project_path(config.project_root, raw_path)

    if not _rbac_enabled(config):
        decision = AuthorizationDecision(
            allowed=True,
            operation=normalize_operation(operation),
            path=normalized_path,
            role=str(_current_role_claim() or ""),
            reason="RBAC is disabled.",
        )
        return absolute_path, normalized_path, decision

    role = _current_role_claim()
    rows = _load_rbac_rows_cached(config)

    rules = []
    for row in rows:
        parsed = rule_from_row(row)
        if parsed is not None:
            rules.append(parsed)

    decision = evaluate_rules(
        operation=operation,
        normalized_path=normalized_path,
        principal_role=role,
        rules=rules,
        deny_default=config.rbac.deny_default,
    )
    return absolute_path, normalized_path, decision


def _enforce_or_advise(operation: str, raw_path: str) -> tuple[str, str, AuthorizationDecision]:
    absolute_path, normalized_path, decision = _evaluate_rbac_decision(operation, raw_path)
    config = _CONFIG
    if config is None:
        raise RuntimeError("UCE server config not initialized")

    if not decision.allowed and _rbac_mode(config) == "enforced":
        raise AuthorizationError(
            f"Not authorized for {operation} on '{normalized_path}': {decision.reason}"
        )
    return absolute_path, normalized_path, decision


@mcp.tool
def impact_analysis(entity_type: str, entity_name: str) -> dict:
    """
    Analyze graph impact for a concrete entity and return direct plus transitive blast radius.

    This tool is intended for dependency and governance analysis over the UCE knowledge graph.
    It is read-only and never performs authorization or file mutations. Use this when the caller
    already knows the exact target entity type and name.

    Args:
        entity_type: Graph entity category such as table, column, file, function, class, or method.
        entity_name: Canonical entity identifier for the given type (for example, "users" table name
            or "src/app.py" file path).

    Returns:
        A dictionary from `reasoning.impact_analysis.impact_analysis(...)` including risk indicators,
        impacted files, and requirement/policy traces when available in graph data.
    """
    graph = _graph_from_config(_CONFIG)
    return impact_module.impact_analysis(
        graph,
        entity_type,
        entity_name,
        backend_paths=_backend_paths_from_config(_CONFIG),
    )


@mcp.tool
def explain_change(entity_type: str, entity_name: str) -> dict:
    """
    Explain why a change is risky or safe by returning a trace-oriented impact explanation.

    This tool complements `impact_analysis` by emphasizing explanatory output suitable for users and
    auditors. It is read-only and does not evaluate RBAC authorization.

    Args:
        entity_type: Graph entity category to explain.
        entity_name: Concrete identifier for the selected entity category.

    Returns:
        A dictionary from `reasoning.impact_analysis.explain_change(...)` that summarizes affected
        artifacts and rationale paths discovered in the graph.
    """
    graph = _graph_from_config(_CONFIG)
    return impact_module.explain_change(
        graph,
        entity_type,
        entity_name,
        backend_paths=_backend_paths_from_config(_CONFIG),
    )


@mcp.tool
def risk_assessment(proposed_change: str) -> dict:
    """
    Score risk for a natural-language proposed change using the reasoning preflight engine.

    This tool is designed for high-level pre-change assessment when entity mapping may be fuzzy.
    It does not authorize or deny write/delete operations and must not be treated as a permission
    decision. For RBAC allow/deny decisions, use `authorize_change`.

    Args:
        proposed_change: Free-form change description, for example "add column x to table y" or
            "modify file path/to/file.py".

    Returns:
        A dictionary from `reasoning.trace_engine.preflight_assessment(...)` containing risk score,
        impacted files, and policy/requirement signals when available.
    """
    graph = _graph_from_config(_CONFIG)
    return preflight_assessment(
        graph,
        proposed_change,
        backend_paths=_backend_paths_from_config(_CONFIG),
    )


# Backwards-compatible tools

@mcp.tool
def impact_table(table_name: str) -> dict:
    """
    Backward-compatible table-specific impact analysis entry point.

    Prefer `impact_analysis(entity_type="table", entity_name=...)` for new integrations, but this
    tool remains for older clients.

    Args:
        table_name: Database table name present in graph nodes.

    Returns:
        The same unified impact dictionary as `impact_analysis(entity_type="table")`,
        so the reported `risk_score` is consistent across every tool. The legacy
        table-centric fields (`target_table`, `direct_files`, ...) remain available
        under the nested `impact` key.
    """
    graph = _graph_from_config(_CONFIG)
    return impact_module.impact_analysis(
        graph,
        "table",
        table_name,
        backend_paths=_backend_paths_from_config(_CONFIG),
    )


@mcp.tool
def impact_column(table_name: str, column_name: str) -> dict:
    """
    Backward-compatible column-specific impact analysis entry point.

    Prefer `impact_analysis(entity_type="column", entity_name="<table>.<column>")` for new clients,
    but this API is preserved for compatibility.

    Args:
        table_name: Parent table name for the column.
        column_name: Column name within the table.

    Returns:
        The same unified impact dictionary as
        `impact_analysis(entity_type="column", entity_name="<table>.<column>")`,
        so the reported `risk_score` is consistent across every tool. The legacy
        column-centric fields remain available under the nested `impact` key.
    """
    graph = _graph_from_config(_CONFIG)
    return impact_module.impact_analysis(
        graph,
        "column",
        f"{table_name}.{column_name}",
        backend_paths=_backend_paths_from_config(_CONFIG),
    )


@mcp.tool
def preflight_check(
    proposed_change: str = "",
    path: str = "",
    file_path: str = "",
    target_path: str = "",
) -> dict:
    """
    Run preflight impact analysis with flexible input fields for change targeting.

    This tool accepts either a textual `proposed_change` or path-like fields (`path`, `file_path`,
    `target_path`) and normalizes them into a single analysis input. It detects an entity candidate
    (table, column, file, or unknown), runs the most appropriate impact query, and returns risk
    signals. It is strictly read-only and does not enforce RBAC policy.

    Args:
        proposed_change: Primary free-form change text.
        path: Optional path alias accepted for client compatibility.
        file_path: Optional file path alias accepted for client compatibility.
        target_path: Optional target path alias accepted for client compatibility.

    Returns:
        A dictionary with normalized assessment fields:
            - entity/entity_type: Detected target.
            - risk_score/recommendation: Risk summary.
            - violated_requirements/affected_files: Governance impact indicators.
            - authorization_evaluated: Always false for this tool.
            - authorization_hint: Guidance to call `authorize_change` for RBAC decisions.
    """
    proposed_change = _resolve_preflight_change(
        proposed_change=proposed_change,
        path=path,
        file_path=file_path,
        target_path=target_path,
    )
    if not proposed_change:
        proposed_change = "unspecified change"

    graph = _graph_from_config(_CONFIG)
    tables = _load_tables(graph)
    columns_by_table = _load_columns(graph)
    files = _load_files(graph)

    entity_type, detected = detect_entity(proposed_change, tables, columns_by_table, files)

    # Route every detected entity through the single authoritative analysis so the
    # risk_score reported here is identical to risk_assessment / impact_analysis for
    # the same entity (previously table/column/file each scored risk differently).
    if entity_type in {"table", "column", "file"} and detected:
        result = impact_module.impact_analysis(
            graph,
            entity_type,
            detected,
            backend_paths=_backend_paths_from_config(_CONFIG),
        )
    else:
        result = {
            "affected_files": [],
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
    elif risk_score >= 20:
        recommendation = "High risk"
    elif risk_score >= 8:
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
        "authorization_evaluated": False,
        "authorization_hint": "Call authorize_change for RBAC allow/deny.",
    }


@mcp.tool
def validate_change(proposed_change: str) -> dict:
    """
    Compatibility alias of `preflight_check` for legacy clients.

    This tool preserves historical naming but returns the same impact/risk semantics as
    `preflight_check`. It never performs RBAC authorization.

    Args:
        proposed_change: Free-form description of the intended change.

    Returns:
        The exact output payload from `preflight_check(proposed_change=...)`.
    """
    return preflight_check(proposed_change)


@mcp.tool
def preflight_validation(
    payload: dict | None = None,
    proposed_change: str = "",
    path: str = "",
    file_path: str = "",
    target_path: str = "",
) -> dict:
    """
    RPC-style wrapper around `preflight_check` for tool-router compatibility.

    Some clients send `{tool, input}` envelopes rather than direct arguments. This function accepts
    both styles, validates the declared tool name when present, resolves path/proposed-change aliases,
    and returns a stable RPC-shaped response. This tool is read-only and not an authorization gate.

    Args:
        payload: Optional RPC envelope with keys like `tool` and `input`.
        proposed_change: Direct argument fallback for change text.
        path: Optional path alias.
        file_path: Optional file-path alias.
        target_path: Optional target-path alias.

    Returns:
        A dictionary containing:
            - tool: `"preflight_validation"`.
            - input: Normalized input object.
            - output: Result from `preflight_check(...)`.
            - error: Present only for invalid wrapped tool names.
    """
    tool_input: dict[str, Any] = {}
    tool_name = ""
    if isinstance(payload, dict):
        tool_name = str(payload.get("tool") or "").strip()
        raw_input = payload.get("input")
        if isinstance(raw_input, dict):
            tool_input = raw_input

    if tool_name and tool_name not in {
        "preflight_validation",
        "preflightValidation",
        "preflight_check",
        "preflightCheck",
    }:
        return {
            "tool": "preflight_validation",
            "error": "Invalid tool name",
        }

    proposed_change = _resolve_preflight_change(
        proposed_change=proposed_change,
        path=path,
        file_path=file_path,
        target_path=target_path,
        payload_input=tool_input,
    )
    if not proposed_change:
        proposed_change = "unspecified change"

    result = preflight_check(proposed_change)
    return {
        "tool": "preflight_validation",
        "input": {"proposed_change": proposed_change},
        "output": result,
    }


@mcp.tool
def explain_change_rpc(
    payload: dict | None = None,
    entity_type: str = "",
    entity_name: str = "",
) -> dict:
    """
    RPC-style wrapper for `explain_change` with envelope compatibility.

    This tool is useful when an orchestration layer sends tool name plus nested `input` fields.
    It validates the wrapper tool label (if provided), resolves argument fallbacks, and returns a
    stable `{tool, input, output}` shape for client interoperability.

    Args:
        payload: Optional envelope carrying `tool` and nested `input` fields.
        entity_type: Direct entity type fallback.
        entity_name: Direct entity name fallback.

    Returns:
        A wrapper dictionary containing normalized input plus the underlying
        `explain_change(entity_type, entity_name)` output.
    """
    tool_input: dict[str, Any] = {}
    tool_name = ""
    if isinstance(payload, dict):
        tool_name = str(payload.get("tool") or "").strip()
        raw_input = payload.get("input")
        if isinstance(raw_input, dict):
            tool_input = raw_input

    if tool_name and tool_name not in {"explain_change", "explain_change_rpc"}:
        return {
            "tool": "explain_change",
            "error": "Invalid tool name",
        }

    if not entity_type:
        entity_type = str(tool_input.get("entity_type") or "")
    if not entity_name:
        entity_name = str(tool_input.get("entity_name") or "")

    result = explain_change(entity_type, entity_name)
    return {
        "tool": "explain_change",
        "input": {"entity_type": entity_type, "entity_name": entity_name},
        "output": result,
    }


@mcp.tool
def logic_trace(entity: str) -> dict:
    """
    Return diagnostic trace metadata for entity-detection and reasoning query selection.

    This tool is primarily for transparency/debugging. It reports which query templates would be
    used for the detected entity category and includes global graph node/edge counts. It does not
    perform any write action or RBAC authorization.

    Args:
        entity: Free-form entity hint used by detection logic.

    Returns:
        A dictionary with:
            - cypher_queries_executed: Query templates selected by detected entity type.
            - node_count/edge_count: Current graph cardinality snapshot.
    """
    graph = _graph_from_config(_CONFIG)
    tables = _load_tables(graph)
    columns_by_table = _load_columns(graph)
    files = _load_files(graph)

    entity_type, _ = detect_entity(entity, tables, columns_by_table, files)

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

    node_count = int(node_rows[0]["count"]) if node_rows else 0
    edge_count = int(edge_rows[0]["count"]) if edge_rows else 0

    return {
        "cypher_queries_executed": queries,
        "node_count": node_count,
        "edge_count": edge_count,
    }


@mcp.tool
def count_functions_in_file(file_path: str) -> dict:
    """
    Count total function declarations and method declarations for a file node.

    The input path is normalized relative to project root when possible to match stored graph paths.
    This is a read-only metric query.

    Args:
        file_path: Absolute or relative path to the target file.

    Returns:
        A dictionary with:
            - file: Normalized path used for lookup.
            - function_count: Distinct declared function+method count.
            - method_count: Distinct method subset count.
            - non_method_count: Convenience derived value.
    """
    graph = _graph_from_config(_CONFIG)
    normalized = _normalize_file_path(file_path, _CONFIG)
    rows = graph.run(
        """
        MATCH (f:File {path: $path})-[:DECLARES_FUNCTION]->(fn:Function)
        RETURN count(DISTINCT fn) AS total,
               count(DISTINCT CASE WHEN 'Method' IN labels(fn) THEN fn END) AS methods
        """,
        path=normalized,
    )

    total = 0
    methods = 0
    if rows:
        total = int(rows[0].get("total") or 0)
        methods = int(rows[0].get("methods") or 0)
    return {
        "file": normalized,
        "function_count": total,
        "method_count": methods,
        "non_method_count": max(total - methods, 0),
    }


@mcp.tool
def find_identifier_usage(identifier: str) -> dict:
    """
    Find all files that reference a given identifier token.

    This query traverses `(:File)-[:USES_IDENTIFIER]->(:Identifier)` edges and returns unique file
    paths. It is useful for lightweight rename-impact exploration and debugging identifier indexing.

    Args:
        identifier: Identifier name token to look up (case-sensitive as stored).

    Returns:
        A dictionary containing the original identifier and a sorted unique list of matching files.
    """
    graph = _graph_from_config(_CONFIG)
    rows = graph.run(
        """
        MATCH (f:File)-[:USES_IDENTIFIER]->(i:Identifier {name: $name})
        RETURN collect(DISTINCT f.path) AS files
        """,
        name=identifier,
    )

    files = []
    if rows:
        files = [p for p in (rows[0].get("files") or []) if p]
    return {
        "identifier": identifier,
        "files": sorted(set(files)),
    }


@mcp.tool
def find_entity_candidates(proposed_change: str, max_results: int = 10) -> dict:
    """
    Return a ranked list of candidate graph entities matching a free-text description.

    Unlike ``preflight_check`` (which picks the single best entity and runs impact analysis),
    this tool surfaces ALL plausible matches with scores so the caller can inspect ambiguous
    cases, handle aliases, and select the most appropriate entity before querying impact.

    Returns up to ``max_results`` candidates ordered by descending specificity score.
    Column matches score higher than table matches; exact path matches score higher than stem
    matches. Use this when you are unsure which table/column/file a change description refers to.

    Args:
        proposed_change: Free-text description of the intended change.
        max_results: Maximum number of candidates to return (1–20, default 10).

    Returns:
        A dict with ``candidates`` list, each entry containing ``entity_type``,
        ``entity_name``, ``score``, and ``match_type``.
    """
    graph = _graph_from_config(_CONFIG)
    tables = _load_tables(graph)
    columns_by_table = _load_columns(graph)
    files = _load_files(graph)

    max_results = max(1, min(20, int(max_results or 10)))
    results = find_candidates(proposed_change, tables, columns_by_table, files, max_results=max_results)

    return {
        "query": proposed_change,
        "total_candidates": len(results),
        "candidates": results,
    }


@mcp.tool
def authorize_change(paths: list[str], operation: str) -> dict:
    """
    Compute authoritative RBAC allow/deny decisions for path mutations.

    This is the permission gate tool that callers should use before mutation tools. It normalizes
    operation/path values, evaluates matching authority rules from graph policy state, and returns
    per-path decision detail including matched rule/policy identifiers.

    Args:
        paths: One or more candidate file paths to authorize.
        operation: Requested operation, currently `write` or `delete`.

    Returns:
        A dictionary containing:
            - operation: Normalized operation string.
            - rbac_enabled/enforce_mode: Effective RBAC runtime status.
            - decisions: Per-path decision objects with reason and matched rule IDs.
            - denied_paths: Unique list of denied path strings.
            - authorized: True only when all paths are authorized.

    Notes:
        This tool reports authorization intent but does not mutate filesystem state.
        Mutation tools still enforce RBAC independently at execution time.
    """
    normalized_operation = normalize_operation(operation)
    decisions: list[dict[str, Any]] = []
    denied: list[str] = []

    for path in paths:
        try:
            _, normalized_path, decision = _evaluate_rbac_decision(normalized_operation, path)
            decision_payload = {
                "path": normalized_path,
                "allowed": decision.allowed,
                "reason": decision.reason,
                "matched_rule_id": decision.matched_rule_id,
                "matched_policy_id": decision.matched_policy_id,
            }
            decisions.append(decision_payload)
            if not decision.allowed:
                denied.append(normalized_path)
        except Exception as exc:
            message = str(exc)
            decisions.append(
                {
                    "path": str(path),
                    "allowed": False,
                    "reason": message,
                    "matched_rule_id": None,
                    "matched_policy_id": None,
                }
            )
            denied.append(str(path))

    config = _CONFIG
    return {
        "operation": normalized_operation,
        "rbac_enabled": bool(config and config.rbac.enabled),
        "enforce_mode": _rbac_mode(config),
        "decisions": decisions,
        "denied_paths": sorted(set(denied)),
        "authorized": len(denied) == 0,
    }


@mcp.tool
def write_file(file_path: str, content: str) -> dict:
    """
    Write UTF-8 text content to a file path after RBAC enforcement.

    The path is validated against project-root traversal constraints and RBAC rules are enforced
    according to current mode. In `enforced` mode, denied requests raise authorization errors.
    Parent directories are created automatically when authorized.

    Args:
        file_path: Target file path, relative to project root or absolute within root.
        content: Full file content to write.

    Returns:
        A dictionary with mutation and RBAC context:
            - path/written/bytes_written.
            - rbac_allowed/rbac_reason/enforce_mode.
            - rbac_advisory when advisory mode allowed a denied decision.

    Guidance:
        Call `authorize_change` first in orchestrated flows for clearer pre-check UX.
    """
    absolute_path, normalized_path, decision = _enforce_or_advise("write", file_path)
    parent = os.path.dirname(absolute_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with open(absolute_path, "w", encoding="utf-8") as handle:
        handle.write(content)

    response = {
        "path": normalized_path,
        "written": True,
        "bytes_written": len(content.encode("utf-8")),
        "rbac_allowed": decision.allowed,
        "rbac_reason": decision.reason,
        "enforce_mode": _rbac_mode(_CONFIG),
    }
    if not decision.allowed:
        response["rbac_advisory"] = (
            "Operation proceeded in advisory mode despite denied RBAC decision."
        )
    return response


@mcp.tool
def delete_file(file_path: str) -> dict:
    """
    Delete a file after path validation and RBAC enforcement.

    Directory targets are rejected explicitly. In enforced RBAC mode, denied decisions raise an
    authorization error before any delete operation occurs.

    Args:
        file_path: Target file path to delete.

    Returns:
        A dictionary with:
            - path/deleted (whether file existed and was removed).
            - rbac_allowed/rbac_reason/enforce_mode.
            - rbac_advisory when advisory mode allowed a denied decision.

    Guidance:
        Use `authorize_change` first for explicit pre-decision reporting in LLM workflows.
    """
    absolute_path, normalized_path, decision = _enforce_or_advise("delete", file_path)

    if os.path.isdir(absolute_path):
        raise ValueError("delete_file only supports file paths, not directories.")

    existed = os.path.exists(absolute_path)
    if existed:
        os.remove(absolute_path)

    response = {
        "path": normalized_path,
        "deleted": bool(existed),
        "rbac_allowed": decision.allowed,
        "rbac_reason": decision.reason,
        "enforce_mode": _rbac_mode(_CONFIG),
    }
    if not decision.allowed:
        response["rbac_advisory"] = (
            "Operation proceeded in advisory mode despite denied RBAC decision."
        )
    return response


@mcp.tool
def graph_stats() -> dict:
    """
    Return diagnostic statistics about the UCE knowledge graph.

    This tool is read-only and does not require RBAC authorization. It queries node and
    relationship counts by label/type, providing an at-a-glance health check.

    Returns:
        A dictionary with:
            - nodes: Map of {label: count}.
            - edges: Map of {relationship_type: count}.
            - total_nodes: Total node count.
            - total_edges: Total relationship count.
    """
    graph = _graph_from_config(_CONFIG)
    node_rows = graph.run(
        "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS count ORDER BY label"
    )
    edge_rows = graph.run(
        "MATCH ()-[r]->() RETURN type(r) AS rel_type, count(r) AS count ORDER BY rel_type"
    )

    nodes_by_label = {
        row["label"]: int(row["count"])
        for row in node_rows
        if row.get("label") is not None
    }
    edges_by_type = {
        row["rel_type"]: int(row["count"])
        for row in edge_rows
        if row.get("rel_type") is not None
    }

    return {
        "nodes": nodes_by_label,
        "edges": edges_by_type,
        "total_nodes": sum(nodes_by_label.values()),
        "total_edges": sum(edges_by_type.values()),
    }


@mcp.tool
def ci_impact_report(changed_files: list[str]) -> dict:
    """
    Generate a governance impact report for a set of changed files.

    Suitable for CI/CD pre-merge governance checks. For each changed file, this tool
    runs impact analysis and aggregates violated requirements, enforced policies, and
    overall risk across the whole change set.

    Args:
        changed_files: List of file paths that are modified in the change (relative to project root).

    Returns:
        A dictionary with:
            - changed_files: Echo of the input list.
            - total_violated_requirements: Count of unique violated requirements.
            - total_enforced_policies: Count of unique enforced policies.
            - total_affected_files: Count of unique transitively affected files.
            - max_risk_score: Highest single-file risk score.
            - recommendation: "block" | "warn" | "pass".
            - details: Per-file impact summaries.
    """
    graph = _graph_from_config(_CONFIG)
    backend_paths = _backend_paths_from_config(_CONFIG)

    all_violated: set[str] = set()
    all_policies: set[str] = set()
    all_affected: set[str] = set()
    max_risk = 0
    details = []

    for file_path in changed_files:
        normalized = _normalize_file_path(file_path, _CONFIG)
        try:
            result = impact_module.impact_analysis(
                graph, "file", normalized, backend_paths=backend_paths
            )
        except Exception as exc:
            details.append({"file": normalized, "error": str(exc)})
            continue

        violated = result.get("violated_requirements") or []
        policies = result.get("enforced_policies") or []
        affected = _collect_affected(result)
        risk = int(result.get("risk_score") or 0)

        all_violated.update(violated)
        all_policies.update(policies)
        all_affected.update(affected)
        max_risk = max(max_risk, risk)

        details.append({
            "file": normalized,
            "risk_score": risk,
            "risk_severity": result.get("risk_severity", "low"),
            "violated_requirements": sorted(violated),
            "enforced_policies": sorted(policies),
            "affected_files": affected,
            "trace_paths": sorted(result.get("trace_paths") or []),
        })

    if all_violated:
        recommendation = "block"
    elif max_risk >= 8:
        recommendation = "warn"
    else:
        recommendation = "pass"

    return {
        "changed_files": changed_files,
        "total_violated_requirements": len(all_violated),
        "total_enforced_policies": len(all_policies),
        "total_affected_files": len(all_affected),
        "violated_requirements": sorted(all_violated),
        "enforced_policies": sorted(all_policies),
        "max_risk_score": max_risk,
        "recommendation": recommendation,
        "details": details,
    }


def _configure_auth(config: UceConfig) -> None:
    if not config.rbac.enabled:
        mcp.auth = None
        return

    if not config.rbac.jwks_uri:
        raise RuntimeError("RBAC is enabled but no RBAC_JWKS_URI/jwks_uri is configured.")
    if not config.rbac.jwt_issuer:
        raise RuntimeError("RBAC is enabled but no RBAC_JWT_ISSUER/jwt_issuer is configured.")
    if not config.rbac.jwt_audience:
        raise RuntimeError("RBAC is enabled but no RBAC_JWT_AUDIENCE/jwt_audience is configured.")

    mcp.auth = _SkewJWTVerifier(
        jwks_uri=config.rbac.jwks_uri,
        issuer=config.rbac.jwt_issuer,
        audience=config.rbac.jwt_audience,
        clock_skew_seconds=config.rbac.clock_skew_seconds,
    )


@mcp.tool
def find_personal_data(query: str = "") -> dict:
    """
    Identify personal data columns in the project schema that fall under GDPR.

    Scans all columns that have been classified by the GDPR/PII classifier during
    schema ingestion and returns detailed entries covering: column, table, GDPR
    sensitivity level (high/medium/low), applicable GDPR articles, data-subject type,
    and files that access the table.

    Use this tool before modifying any table/column that might hold personal data,
    and to generate a Data Protection Impact Assessment (DPIA) inventory.

    Args:
        query: Optional filter string — can be a category name (e.g. "contact",
               "health", "credentials"), sensitivity level ("high", "medium", "low"),
               table name, or column name.  Leave empty to list all classified columns.

    Returns:
        Dict with ``total``, ``summary`` (counts by sensitivity), and ``columns`` list.
    """
    graph = _graph_from_config(_CONFIG)
    results = graph.find_personal_data(query)

    high = [r for r in results if r.get("sensitivity") == "high"]
    medium = [r for r in results if r.get("sensitivity") == "medium"]
    low = [r for r in results if r.get("sensitivity") == "low"]

    return {
        "query": query,
        "total": len(results),
        "summary": {
            "high_sensitivity": len(high),
            "medium_sensitivity": len(medium),
            "low_sensitivity": len(low),
        },
        "columns": results,
    }


@mcp.tool
def erasure_impact(subject_type: str = "") -> dict:
    """
    Return the full blast radius of a GDPR Art. 17 right-to-erasure request.

    Identifies every table column that stores personal data for the specified type of
    data subject (e.g. ``"user"``, ``"customer"``, ``"employee"``), ranks results by
    sensitivity, and lists the source-code files that process each column so that
    engineers know exactly what code must be updated to honour the erasure.

    Args:
        subject_type: Data-subject category to scope the erasure (e.g. "user",
                      "customer", "employee").  Leave empty to cover all subject types.

    Returns:
        Dict with ``subject_type``, ``total_columns``, ``total_files_affected``,
        ``affected_files`` list, and a ``columns_to_erase`` list sorted by sensitivity.
    """
    graph = _graph_from_config(_CONFIG)
    return graph.erasure_impact(subject_type)


@mcp.tool
def purpose_check(table: str, column: str = "") -> dict:
    """
    Check the lawful basis and purpose classification for a table or specific column.

    Returns all PersonalData classifications for the given table/column together with
    the GDPR articles that apply and a human-readable rationale for why the column
    is treated as personal data.  Use this before processing, sharing, or retaining
    data to verify that your use case aligns with the recorded legal basis.

    Args:
        table: Name of the database table to inspect.
        column: Optional specific column name within the table.

    Returns:
        Dict with ``table``, ``column`` (if specified), and ``classifications`` list.
    """
    graph = _graph_from_config(_CONFIG)
    classifications = graph.purpose_check(table, column)
    return {
        "table": table,
        "column": column,
        "total_pii_columns": len(classifications),
        "classifications": classifications,
    }


def _transport_from_env(config: UceConfig) -> tuple[str, dict[str, Any]]:
    transport = (os.getenv("UCE_MCP_TRANSPORT") or "").strip().lower()
    if not transport:
        transport = "http" if config.rbac.enabled else "stdio"

    if transport == "stdio":
        if config.rbac.enabled:
            raise RuntimeError(
                "RBAC bearer-token enforcement requires HTTP transport. "
                "Set UCE_MCP_TRANSPORT=http."
            )
        return "stdio", {}

    if transport not in {"http", "sse", "streamable-http"}:
        raise RuntimeError(
            "Unsupported UCE_MCP_TRANSPORT value. Use stdio, http, sse, or streamable-http."
        )

    host = os.getenv("UCE_MCP_SERVER_HOST") or "127.0.0.1"
    port_value = os.getenv("UCE_MCP_SERVER_PORT") or "9001"
    path = os.getenv("UCE_MCP_SERVER_PATH") or "/mcp/"
    try:
        port = int(port_value)
    except ValueError as exc:
        raise RuntimeError("UCE_MCP_SERVER_PORT must be an integer.") from exc

    kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "path": path,
    }
    if transport in {"http", "streamable-http"}:
        kwargs["stateless_http"] = True
    return transport, kwargs


def run_server(config: UceConfig):
    global _CONFIG, _DB
    _CONFIG = config
    _configure_auth(config)

    # Initialize shared DB driver and ensure indexes at startup
    with _DB_LOCK:
        _DB = GraphDB(config.neo4j.uri, config.neo4j.user, config.neo4j.password)
    try:
        _DB.ensure_indexes()
        _LOGGER.info("Neo4j indexes ensured at MCP server startup.")
    except Exception as exc:
        _LOGGER.warning("Failed to ensure Neo4j indexes at startup (non-fatal): %s", exc)

    transport, transport_kwargs = _transport_from_env(config)
    _LOGGER.info("Starting UCE MCP server transport=%s", transport)
    mcp.run(transport=transport, **transport_kwargs)
