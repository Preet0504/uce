"""Tests for uce.reasoning.gate — the deterministic propose_change decision engine
and the mandatory gate_token store that makes calling it non-optional."""
import time

import pytest

from uce.reasoning.gate import GateTokenStore, evaluate_gate


# ---------------------------------------------------------------------------
# evaluate_gate — decision policy
# ---------------------------------------------------------------------------

def test_full_coverage_plan_allows():
    result = evaluate_gate(
        rbac_allowed=True,
        declared_files=["src/a.py", "src/b.py"],
        actual_files=["src/a.py", "src/b.py"],
        declared_requirements=["RQ-001"],
        violated_requirements=["RQ-001"],
        enforced_policies=["P-001"],
        strict=True,
    )
    assert result.decision == "allow"
    assert not result.gate_fires
    assert result.missed_files == ()
    assert result.silent_requirements == ()


def test_missing_files_blocks_when_strict():
    result = evaluate_gate(
        rbac_allowed=True,
        declared_files=["src/a.py"],
        actual_files=["src/a.py", "src/b.py", "src/c.py"],
        strict=True,
    )
    assert result.decision == "block"
    assert result.missed_files == ("src/b.py", "src/c.py")


def test_missing_files_warns_when_not_strict():
    result = evaluate_gate(
        rbac_allowed=True,
        declared_files=["src/a.py"],
        actual_files=["src/a.py", "src/b.py"],
        strict=False,
    )
    assert result.decision == "warn"
    assert result.missed_files == ("src/b.py",)


def test_rbac_deny_blocks_regardless_of_strict():
    result = evaluate_gate(
        rbac_allowed=False,
        rbac_reason="Denied by rule",
        declared_files=["src/a.py"],
        actual_files=["src/a.py"],
        strict=False,
    )
    assert result.decision == "block"


def test_silent_requirement_triggers_gate():
    result = evaluate_gate(
        rbac_allowed=True,
        declared_files=["src/a.py"],
        actual_files=["src/a.py"],
        declared_requirements=[],
        violated_requirements=["RQ-001"],
        strict=True,
    )
    assert result.decision == "block"
    assert result.silent_requirements == ("RQ-001",)


def test_declared_requirement_not_silent():
    result = evaluate_gate(
        rbac_allowed=True,
        declared_files=["src/a.py"],
        actual_files=["src/a.py"],
        declared_requirements=["RQ-001"],
        violated_requirements=["RQ-001"],
        strict=True,
    )
    assert result.decision == "allow"
    assert result.silent_requirements == ()


def test_path_normalization_prevents_false_missed_files():
    """Windows-style backslashes / leading slashes must not register as different files."""
    result = evaluate_gate(
        rbac_allowed=True,
        declared_files=["src\\a.py", "/src/b.py/"],
        actual_files=["src/a.py", "src/b.py"],
        strict=True,
    )
    assert result.decision == "allow"
    assert result.missed_files == ()


# ---------------------------------------------------------------------------
# GateTokenStore — mandatory-gate enforcement
# ---------------------------------------------------------------------------

def test_token_issued_only_covers_declared_files():
    store = GateTokenStore(ttl_seconds=60)
    token = store.issue("write", ["src/a.py", "src/b.py"])

    ok, err = store.consume(token, "write", "src/a.py")
    assert ok is True
    assert err == ""


def test_token_rejects_path_not_declared():
    store = GateTokenStore(ttl_seconds=60)
    token = store.issue("write", ["src/a.py"])

    ok, err = store.consume(token, "write", "src/other.py")
    assert ok is False
    assert "does not cover path" in err


def test_token_rejects_wrong_operation():
    store = GateTokenStore(ttl_seconds=60)
    token = store.issue("write", ["src/a.py"])

    ok, err = store.consume(token, "delete", "src/a.py")
    assert ok is False
    assert "was issued for operation" in err


def test_token_is_single_use_per_path():
    store = GateTokenStore(ttl_seconds=60)
    token = store.issue("write", ["src/a.py", "src/b.py"])

    ok1, _ = store.consume(token, "write", "src/a.py")
    assert ok1 is True

    ok2, err2 = store.consume(token, "write", "src/a.py")
    assert ok2 is False
    assert "already been used" in err2

    # The second declared file is still usable independently.
    ok3, _ = store.consume(token, "write", "src/b.py")
    assert ok3 is True


def test_token_deleted_after_all_declared_files_consumed():
    store = GateTokenStore(ttl_seconds=60)
    token = store.issue("write", ["src/a.py"])
    ok, _ = store.consume(token, "write", "src/a.py")
    assert ok is True

    # Token is now fully consumed and gone — reusing it must fail with the
    # generic "invalid/expired/already-used" message, not a per-file message.
    ok2, err2 = store.consume(token, "write", "src/a.py")
    assert ok2 is False
    assert "Invalid, expired, or already-used" in err2


def test_unknown_token_rejected():
    store = GateTokenStore(ttl_seconds=60)
    ok, err = store.consume("not-a-real-token", "write", "src/a.py")
    assert ok is False
    assert "Invalid, expired, or already-used" in err


def test_expired_token_rejected():
    store = GateTokenStore(ttl_seconds=0)  # clamps to 1s minimum internally
    store.ttl_seconds = 1
    token = store.issue("write", ["src/a.py"])
    time.sleep(1.1)

    ok, err = store.consume(token, "write", "src/a.py")
    assert ok is False
    assert "Invalid, expired, or already-used" in err


def test_backslash_paths_normalized_for_token_matching():
    store = GateTokenStore(ttl_seconds=60)
    token = store.issue("write", ["src\\windows\\path.py"])

    ok, err = store.consume(token, "write", "src/windows/path.py")
    assert ok is True, err
