"""Tests for uce.core.rbac — rule evaluation and normalization."""
import pytest

from uce.core.rbac import (
    ROLE_RANKS,
    SUPPORTED_OPERATIONS,
    AuthorityRule,
    AuthorizationDecision,
    evaluate_rules,
    normalize_operation,
    normalize_project_path,
    normalize_role,
    role_rank,
)


# ---------------------------------------------------------------------------
# normalize_operation
# ---------------------------------------------------------------------------

def test_normalize_operation_known():
    assert normalize_operation("write") == "write"
    assert normalize_operation("WRITE") == "write"
    assert normalize_operation("delete") == "delete"
    assert normalize_operation("read") == "read"


def test_normalize_operation_unknown_raises():
    with pytest.raises(ValueError):
        normalize_operation("unknown_op")


def test_supported_operations_includes_read():
    assert "read" in SUPPORTED_OPERATIONS
    assert "write" in SUPPORTED_OPERATIONS
    assert "delete" in SUPPORTED_OPERATIONS


# ---------------------------------------------------------------------------
# normalize_role
# ---------------------------------------------------------------------------

def test_normalize_role():
    assert normalize_role("Admin") == "admin"
    assert normalize_role("  EDITOR  ") == "editor"
    assert normalize_role("unknown_role") is None
    assert normalize_role(None) is None


# ---------------------------------------------------------------------------
# role_rank
# ---------------------------------------------------------------------------

def test_role_rank_known():
    assert role_rank("admin") > role_rank("editor")
    assert role_rank("editor") > role_rank("viewer")


def test_role_rank_unknown():
    assert role_rank("unknown_role") is None


# ---------------------------------------------------------------------------
# evaluate_rules — basic allow/deny
# ---------------------------------------------------------------------------

def _make_rule(operation, path_pattern, min_role, effect="allow"):
    return AuthorityRule(
        rule_id=f"{effect}-{operation}-{path_pattern}",
        operation=operation,
        path_pattern=path_pattern,
        min_role=min_role,
        min_role_rank=ROLE_RANKS[min_role],
        effect=effect,
        source_priority=1,
    )


def test_evaluate_rules_allow_matching_role():
    rules = [_make_rule("write", "src/**", "editor")]
    decision = evaluate_rules(
        operation="write",
        normalized_path="src/app.py",
        principal_role="editor",
        rules=rules,
        deny_default=True,
    )
    assert decision.allowed is True


def test_evaluate_rules_deny_insufficient_role():
    rules = [_make_rule("write", "src/**", "admin")]
    decision = evaluate_rules(
        operation="write",
        normalized_path="src/app.py",
        principal_role="editor",
        rules=rules,
        deny_default=True,
    )
    assert decision.allowed is False


def test_evaluate_rules_deny_wins_over_allow():
    # Allow rule: broad wildcard for editors+; Deny rule: explicit file for non-admins.
    # As editor (rank 2): allow triggers (2>=2), deny triggers (2<3). Deny wins.
    rules = [
        _make_rule("write", "src/**", "editor", effect="allow"),
        _make_rule("write", "src/app.py", "admin", effect="deny"),
    ]
    decision = evaluate_rules(
        operation="write",
        normalized_path="src/app.py",
        principal_role="editor",
        rules=rules,
        deny_default=False,
    )
    assert decision.allowed is False


def test_evaluate_rules_deny_default_no_match():
    decision = evaluate_rules(
        operation="write",
        normalized_path="src/app.py",
        principal_role="editor",
        rules=[],
        deny_default=True,
    )
    assert decision.allowed is False


def test_evaluate_rules_allow_default_no_match():
    decision = evaluate_rules(
        operation="write",
        normalized_path="src/app.py",
        principal_role="editor",
        rules=[],
        deny_default=False,
    )
    assert decision.allowed is True


def test_evaluate_rules_wildcard_path():
    rules = [_make_rule("write", "src/**", "editor")]
    decision = evaluate_rules(
        operation="write",
        normalized_path="src/nested/deep/file.py",
        principal_role="editor",
        rules=rules,
        deny_default=True,
    )
    assert decision.allowed is True


def test_evaluate_rules_no_role_with_deny_default():
    rules = [_make_rule("write", "src/**", "editor")]
    decision = evaluate_rules(
        operation="write",
        normalized_path="src/app.py",
        principal_role=None,
        rules=rules,
        deny_default=True,
    )
    assert decision.allowed is False


def test_evaluate_rules_read_operation():
    rules = [_make_rule("read", "docs/**", "viewer")]
    decision = evaluate_rules(
        operation="read",
        normalized_path="docs/api.md",
        principal_role="viewer",
        rules=rules,
        deny_default=True,
    )
    assert decision.allowed is True


def test_evaluate_rules_case_insensitive_path_match_cross_platform():
    """Path matching must be case-insensitive on every platform.

    A deny rule authored in lower case must still deny a differently-cased
    request path, so authorization does not diverge between a Windows dev host
    and the Linux/Docker deployment.
    """
    rules = [_make_rule("write", "src/secret.py", "admin", effect="deny")]
    decision = evaluate_rules(
        operation="write",
        normalized_path="SRC/Secret.py",
        principal_role="editor",
        rules=rules,
        deny_default=False,
    )
    assert decision.allowed is False
    assert decision.matched_rule_id == "deny-write-src/secret.py"


# ---------------------------------------------------------------------------
# normalize_project_path
# ---------------------------------------------------------------------------

def test_normalize_project_path_relative():
    import os
    root = os.path.abspath("/tmp/project")
    abs_path, norm = normalize_project_path(root, "src/app.py")
    assert not os.path.isabs(norm)
    assert norm == "src/app.py" or norm.endswith("src/app.py")


def test_normalize_project_path_empty():
    with pytest.raises(ValueError):
        normalize_project_path("/tmp/project", "")
