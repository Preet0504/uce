"""End-to-end tests for the propose_change / gate_token wiring in uce.server.mcp_server,
using a stubbed graph and RBAC layer so no Neo4j is required.

These exercise the REAL server functions (not just the isolated uce.reasoning.gate module)
to catch wiring bugs between propose_change, the gate token store, and write_file/delete_file.
"""
import os
from dataclasses import dataclass

import pytest
from fastmcp.exceptions import AuthorizationError

import uce.server.mcp_server as srv


FULL_BLAST_RADIUS = ["src/a.py", "src/b.py"]
VIOLATED_REQS = ["RQ-001"]
ENFORCED_POLS = ["P-001"]


@dataclass
class _FakeGate:
    enforcement: str = "enforced"
    strict_default: bool = True
    token_ttl_seconds: int = 900


@dataclass
class _FakeRbac:
    enabled: bool = False
    enforce_mode: str = "advisory"
    deny_default: bool = True


@dataclass
class _FakePaths:
    backend: tuple = ()


@dataclass
class _FakeConfig:
    project_root: str
    gate: _FakeGate
    rbac: _FakeRbac
    paths: _FakePaths


def _fake_impact_analysis(graph, entity_type, entity_name, backend_paths=None):
    return {
        "risk_score": 9,
        "risk_severity": "moderate",
        "affected_files": list(FULL_BLAST_RADIUS),
        "violated_requirements": list(VIOLATED_REQS),
        "enforced_policies": list(ENFORCED_POLS),
    }


def _fake_explain_change(graph, entity_type, entity_name, backend_paths=None):
    return {
        "affected_files": list(FULL_BLAST_RADIUS),
        "violated_requirements": list(VIOLATED_REQS),
        "enforced_policies": list(ENFORCED_POLS),
        "trace_paths": [
            f"Table({entity_name}) -> Requirement(RQ-001)",
            "Requirement(RQ-001) -> Policy(P-001)",
        ],
        "risk_score": 9,
        "risk_severity": "moderate",
    }


class _FakeGraph:
    @staticmethod
    def run(query, **params):
        if "ENFORCES" in query and "policy_ids" in query:
            return [{"policy_ids": ["P-001"]}]
        if "MATCH (r:Requirement)" in query:
            return [{"id": "RQ-001", "title": "Audit trail", "description": "Literal requirement text."}]
        if "MATCH (p:Policy)" in query:
            return [{"id": "P-001", "title": "Audit policy", "description": "Literal policy text."}]
        return []


def _make_fake_authorize_change(allowed: bool):
    def fake_authorize_change(paths, operation):
        return {
            "operation": operation,
            "authorized": allowed,
            "decisions": [
                {
                    "path": p, "allowed": allowed,
                    "reason": "ok" if allowed else "Denied by policy",
                    "matched_rule_id": None if allowed else "RULE-1",
                    "matched_policy_id": None,
                }
                for p in paths
            ],
            "denied_paths": [] if allowed else list(paths),
        }
    return fake_authorize_change


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    os.makedirs(tmp_path / "src", exist_ok=True)
    config = _FakeConfig(
        project_root=str(tmp_path),
        gate=_FakeGate(),
        rbac=_FakeRbac(),
        paths=_FakePaths(),
    )
    monkeypatch.setattr(srv, "_CONFIG", config)
    monkeypatch.setattr(srv, "_graph_from_config", lambda cfg: _FakeGraph())
    monkeypatch.setattr(srv.impact_module, "impact_analysis", _fake_impact_analysis)
    monkeypatch.setattr(srv.impact_module, "explain_change", _fake_explain_change)
    # Reset the module-level gate token store so tests don't see tokens from prior tests.
    monkeypatch.setattr(srv, "_GATE_TOKEN_STORE", None)
    monkeypatch.setattr(srv, "authorize_change", _make_fake_authorize_change(allowed=True))
    return config


def test_full_coverage_plan_allows_and_issues_token(gate_env):
    resp = srv.propose_change(
        operation="write",
        entity_type="table",
        entity_name="meetings",
        files_to_edit=FULL_BLAST_RADIUS,
        declared_requirements=VIOLATED_REQS,
    )
    assert resp["decision"] == "allow"
    assert resp["gate_token"]


def test_evidence_contains_literal_text_not_a_summary(gate_env):
    resp = srv.propose_change(
        operation="write",
        entity_type="table",
        entity_name="meetings",
        files_to_edit=FULL_BLAST_RADIUS,
        declared_requirements=VIOLATED_REQS,
    )
    violation = resp["evidence"]["violations"][0]
    assert violation["requirement_text"] == "Literal requirement text."
    assert violation["enforced_by"][0]["policy_text"] == "Literal policy text."


def test_write_file_without_token_is_rejected(gate_env):
    with pytest.raises(AuthorizationError, match="propose_change"):
        srv.write_file("src/rogue.py", "print('no gate')")
    assert not os.path.exists(os.path.join(gate_env.project_root, "src", "rogue.py"))


def test_write_file_with_valid_token_succeeds(gate_env):
    resp = srv.propose_change(
        operation="write",
        entity_type="table",
        entity_name="meetings",
        files_to_edit=FULL_BLAST_RADIUS,
        declared_requirements=VIOLATED_REQS,
    )
    token = resp["gate_token"]

    write_resp = srv.write_file("src/a.py", "print('hello')", gate_token=token)
    assert write_resp["written"] is True
    assert os.path.exists(os.path.join(gate_env.project_root, "src", "a.py"))


def test_token_is_single_use_per_declared_path(gate_env):
    resp = srv.propose_change(
        operation="write",
        entity_type="table",
        entity_name="meetings",
        files_to_edit=FULL_BLAST_RADIUS,
        declared_requirements=VIOLATED_REQS,
    )
    token = resp["gate_token"]

    srv.write_file("src/a.py", "print('first')", gate_token=token)
    with pytest.raises(AuthorizationError, match="already been used"):
        srv.write_file("src/a.py", "print('second')", gate_token=token)

    # The token still authorizes the OTHER declared file.
    second = srv.write_file("src/b.py", "print('other file')", gate_token=token)
    assert second["written"] is True


def test_incomplete_plan_blocks_and_issues_no_token(gate_env):
    resp = srv.propose_change(
        operation="write",
        entity_type="table",
        entity_name="meetings",
        files_to_edit=["src/a.py"],  # missing src/b.py from the real blast radius
        declared_requirements=VIOLATED_REQS,
    )
    assert resp["decision"] == "block"
    assert resp["gate_token"] is None
    assert resp["blast_radius"]["missed_files"] == ["src/b.py"]
    assert resp["remediation"]["required_additional_files"] == ["src/b.py"]


def test_rbac_deny_blocks_even_when_not_strict(gate_env, monkeypatch):
    monkeypatch.setattr(srv, "authorize_change", _make_fake_authorize_change(allowed=False))
    resp = srv.propose_change(
        operation="write",
        entity_type="table",
        entity_name="meetings",
        files_to_edit=FULL_BLAST_RADIUS,
        declared_requirements=VIOLATED_REQS,
        strict=False,
    )
    assert resp["decision"] == "block"
    assert resp["gate_token"] is None


def test_advisory_enforcement_mode_allows_write_without_token(gate_env, monkeypatch):
    monkeypatch.setattr(srv._CONFIG, "gate", _FakeGate(enforcement="advisory"))
    resp = srv.write_file("src/a.py", "print('advisory mode')")
    assert resp["written"] is True
    assert resp["gate_enforcement"] == "advisory"
