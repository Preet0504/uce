from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

ROLE_RANKS: dict[str, int] = {
    "viewer": 1,
    "editor": 2,
    "admin": 3,
}

SUPPORTED_OPERATIONS = {"write", "delete"}
_WILDCARD_PATTERN = re.compile(r"[*?\[\]]")


@dataclass(frozen=True)
class AuthorityRule:
    rule_id: str
    operation: str
    path_pattern: str
    effect: str
    min_role: str
    min_role_rank: int
    source_priority: int
    policy_id: str | None = None


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    operation: str
    path: str
    role: str
    reason: str
    matched_rule_id: str | None = None
    matched_policy_id: str | None = None


def normalize_operation(operation: str) -> str:
    normalized = (operation or "").strip().lower()
    if normalized not in SUPPORTED_OPERATIONS:
        raise ValueError(f"Unsupported RBAC operation: {operation}")
    return normalized


def normalize_role(role: str | None) -> str | None:
    if role is None:
        return None
    normalized = role.strip().lower()
    if not normalized:
        return None
    if normalized not in ROLE_RANKS:
        return None
    return normalized


def role_rank(role: str | None) -> int | None:
    normalized = normalize_role(role)
    if normalized is None:
        return None
    return ROLE_RANKS[normalized]


def normalize_project_path(project_root: str, raw_path: str) -> tuple[str, str]:
    if not raw_path or not str(raw_path).strip():
        raise ValueError("Path cannot be empty.")
    if "\x00" in raw_path:
        raise ValueError("Path contains null bytes.")

    root_abs = os.path.realpath(os.path.abspath(project_root))
    candidate = os.path.expanduser(str(raw_path).strip())
    if os.path.isabs(candidate):
        target_abs = os.path.realpath(os.path.abspath(candidate))
    else:
        target_abs = os.path.realpath(os.path.abspath(os.path.join(root_abs, candidate)))

    try:
        if os.path.commonpath([target_abs, root_abs]) != root_abs:
            raise ValueError("Path resolves outside project root.")
    except ValueError as exc:
        raise ValueError("Path resolves outside project root.") from exc

    rel_path = os.path.relpath(target_abs, root_abs)
    rel_path = rel_path.replace("\\", "/").strip("/")
    if not rel_path or rel_path == ".":
        raise ValueError("Path must target a file within project root.")
    if rel_path.startswith("../"):
        raise ValueError("Path traversal is not allowed.")
    return target_abs, rel_path


def _canonical_for_match(path: str) -> str:
    normalized = (path or "").replace("\\", "/").strip("/")
    if os.name == "nt":
        normalized = normalized.lower()
    return normalized


def _pattern_specificity(pattern: str) -> tuple[int, int, int, int]:
    normalized = (pattern or "").replace("\\", "/").strip("/")
    literal_chars = len(_WILDCARD_PATTERN.sub("", normalized))
    segment_count = normalized.count("/") + (1 if normalized else 0)
    wildcard_count = len(_WILDCARD_PATTERN.findall(normalized))
    return (literal_chars, segment_count, -wildcard_count, len(normalized))


def _operation_matches(rule_operation: str, requested_operation: str) -> bool:
    normalized = (rule_operation or "").strip().lower()
    return normalized in {requested_operation, "*"}


def _path_matches(path_pattern: str, requested_path: str) -> bool:
    candidate = _canonical_for_match(requested_path)
    pattern = _canonical_for_match(path_pattern)
    if not pattern:
        return False
    return fnmatch.fnmatchcase(candidate, pattern)


def _rule_outcome(rule: AuthorityRule, principal_role_rank: int) -> str | None:
    if rule.effect == "allow":
        if principal_role_rank >= rule.min_role_rank:
            return "allow"
        return None
    if rule.effect == "deny":
        if principal_role_rank < rule.min_role_rank:
            return "deny"
        return None
    return None


def evaluate_rules(
    operation: str,
    normalized_path: str,
    principal_role: str | None,
    rules: list[AuthorityRule],
    deny_default: bool = True,
) -> AuthorizationDecision:
    op = normalize_operation(operation)
    role = normalize_role(principal_role)
    if role is None:
        return AuthorizationDecision(
            allowed=False,
            operation=op,
            path=normalized_path,
            role=str(principal_role or ""),
            reason="Missing or invalid role claim.",
        )

    principal_rank = role_rank(role)
    if principal_rank is None:
        return AuthorizationDecision(
            allowed=False,
            operation=op,
            path=normalized_path,
            role=role,
            reason="Missing or invalid role claim.",
        )

    candidates: list[tuple[int, tuple[int, int, int, int], int, AuthorityRule, str]] = []
    for rule in rules:
        if not _operation_matches(rule.operation, op):
            continue
        if not _path_matches(rule.path_pattern, normalized_path):
            continue

        outcome = _rule_outcome(rule, principal_rank)
        if outcome is None:
            continue

        deny_priority = 1 if outcome == "deny" else 0
        specificity = _pattern_specificity(rule.path_pattern)
        candidates.append((deny_priority, specificity, rule.source_priority, rule, outcome))

    if not candidates:
        if deny_default:
            return AuthorizationDecision(
                allowed=False,
                operation=op,
                path=normalized_path,
                role=role,
                reason="No matching authority rule (deny-by-default).",
            )
        return AuthorizationDecision(
            allowed=True,
            operation=op,
            path=normalized_path,
            role=role,
            reason="No matching authority rule (allow-by-default).",
        )

    winner = sorted(
        candidates,
        key=lambda item: (
            -item[0],  # deny > allow
            -item[1][0],  # most literal chars first
            -item[1][1],  # most path segments first
            -item[1][2],  # fewer wildcards first
            -item[1][3],  # longer normalized pattern first
            -item[2],  # higher source priority first
            item[3].rule_id,  # stable tie-break
        ),
    )[0]
    selected_rule = winner[3]
    selected_outcome = winner[4]
    allowed = selected_outcome == "allow"

    if allowed:
        reason = (
            f"Authorized by rule '{selected_rule.rule_id}' "
            f"(requires role>={selected_rule.min_role})."
        )
    else:
        reason = (
            f"Denied by rule '{selected_rule.rule_id}' "
            f"(requires role>={selected_rule.min_role})."
        )

    return AuthorizationDecision(
        allowed=allowed,
        operation=op,
        path=normalized_path,
        role=role,
        reason=reason,
        matched_rule_id=selected_rule.rule_id,
        matched_policy_id=selected_rule.policy_id,
    )


def rule_from_row(row: Mapping[str, Any]) -> AuthorityRule | None:
    try:
        rule_id = str(row.get("rule_id") or "").strip()
        operation = str(row.get("operation") or "").strip().lower()
        path_pattern = str(row.get("path_pattern") or "").strip()
        effect = str(row.get("effect") or "allow").strip().lower()
        min_role = str(row.get("min_role") or "").strip().lower()
        min_role_rank_value = int(row.get("min_role_rank"))
        source_priority = int(row.get("source_priority") or 0)
        policy_id_raw = row.get("policy_id")
        policy_id = str(policy_id_raw).strip() if policy_id_raw else None
    except Exception:
        return None

    if not rule_id or not path_pattern:
        return None
    if operation not in SUPPORTED_OPERATIONS and operation != "*":
        return None
    if effect not in {"allow", "deny"}:
        return None
    if min_role not in ROLE_RANKS:
        return None

    return AuthorityRule(
        rule_id=rule_id,
        operation=operation,
        path_pattern=path_pattern,
        effect=effect,
        min_role=min_role,
        min_role_rank=min_role_rank_value,
        source_priority=source_priority,
        policy_id=policy_id,
    )
