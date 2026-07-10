"""Tests for uce.server.mcp_server.explain_violation — verifies the tool returns literal,
verbatim requirement/policy text and exact trace chains rather than a summary or paraphrase."""
from dataclasses import dataclass

import pytest

import uce.server.mcp_server as srv


@dataclass
class _FakeGate:
    enforcement: str = "enforced"
    strict_default: bool = True
    token_ttl_seconds: int = 900


def _fake_explain_change(graph, entity_type, entity_name, backend_paths=None):
    return {
        "affected_files": ["src/a.py", "src/b.py"],
        "violated_requirements": ["RQ-001"],
        "enforced_policies": ["P-001"],
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
            return [{
                "id": "RQ-001",
                "title": "Store core meeting status and timestamps",
                "description": (
                    "Every record in the `meetings` table MUST include `status`, "
                    "`created_at`, and `updated_at`."
                ),
            }]
        if "MATCH (p:Policy)" in query:
            return [{
                "id": "P-001",
                "title": "Meeting Data Audit Integrity Policy",
                "description": "Meeting data must preserve audit integrity at all times.",
            }]
        return []


@pytest.fixture
def explain_env(monkeypatch):
    monkeypatch.setattr(srv, "_CONFIG", type("Cfg", (), {"gate": _FakeGate(), "paths": None})())
    monkeypatch.setattr(srv, "_graph_from_config", lambda cfg: _FakeGraph())
    monkeypatch.setattr(srv, "_backend_paths_from_config", lambda cfg: ())
    monkeypatch.setattr(srv.impact_module, "explain_change", _fake_explain_change)


def test_requirement_text_is_returned_verbatim(explain_env):
    result = srv.explain_violation("table", "meetings")
    violation = result["violations"][0]
    assert violation["requirement_id"] == "RQ-001"
    assert violation["requirement_text"] == (
        "Every record in the `meetings` table MUST include `status`, "
        "`created_at`, and `updated_at`."
    )
    assert violation["requirement_title"] == "Store core meeting status and timestamps"


def test_policy_text_is_returned_verbatim(explain_env):
    result = srv.explain_violation("table", "meetings")
    enforced_by = result["violations"][0]["enforced_by"][0]
    assert enforced_by["policy_id"] == "P-001"
    assert enforced_by["policy_text"] == "Meeting data must preserve audit integrity at all times."


def test_trace_path_is_exact_not_summarized(explain_env):
    result = srv.explain_violation("table", "meetings")
    violation = result["violations"][0]
    assert violation["trace_path"] == "Table(meetings) -> Requirement(RQ-001)"
    assert violation["enforced_by"][0]["trace_path"] == "Requirement(RQ-001) -> Policy(P-001)"


def test_affected_files_and_risk_passed_through(explain_env):
    result = srv.explain_violation("table", "meetings")
    assert result["affected_files"] == ["src/a.py", "src/b.py"]
    assert result["risk_score"] == 9
    assert result["risk_severity"] == "moderate"
    assert result["entity"] == {"type": "table", "name": "meetings"}
