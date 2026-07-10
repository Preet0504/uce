"""Tests for uce.core.risk_model — risk scoring and severity bands."""
import pytest

from uce.core.risk_model import RiskAssessment, assess_risk, score_from_counts


# ---------------------------------------------------------------------------
# score_from_counts
# ---------------------------------------------------------------------------

def test_score_zero():
    assert score_from_counts(0, 0, 0, 0) == 0


def test_score_files_only():
    score = score_from_counts(affected_files=5, affected_functions=0, violated_requirements=0, enforced_policies=0)
    assert score > 0


def test_score_requirements_weigh_more():
    # A single violated requirement must score higher than a single affected file,
    # reflecting that governance violations carry more risk than blast-radius size alone.
    score_one_req = score_from_counts(0, 0, 1, 0)
    score_one_file = score_from_counts(1, 0, 0, 0)
    assert score_one_req > score_one_file

    # Two requirements must also beat two files.
    assert score_from_counts(0, 0, 2, 0) > score_from_counts(2, 0, 0, 0)


def test_score_monotone_files():
    assert score_from_counts(2, 0, 0, 0) > score_from_counts(1, 0, 0, 0)


def test_score_monotone_requirements():
    assert score_from_counts(0, 0, 2, 0) > score_from_counts(0, 0, 1, 0)


# ---------------------------------------------------------------------------
# assess_risk
# ---------------------------------------------------------------------------

def test_assess_risk_returns_dataclass():
    result = assess_risk(0, 0, 0, 0)
    assert isinstance(result, RiskAssessment)


def test_assess_risk_low():
    result = assess_risk(1, 0, 0, 0)
    assert result.severity == "low"


def test_assess_risk_moderate():
    # Force a moderate score: enough files, no requirements
    result = assess_risk(affected_files=8, affected_functions=5, violated_requirements=0, enforced_policies=0)
    assert result.severity in ("moderate", "high")


def test_assess_risk_high_with_requirements():
    result = assess_risk(affected_files=5, affected_functions=10, violated_requirements=3, enforced_policies=2)
    assert result.severity == "high"
    assert result.risk_score > 0


def test_assess_risk_score_gt_zero_when_nonzero_inputs():
    result = assess_risk(1, 1, 1, 1)
    assert result.risk_score > 0


def test_assess_risk_score_consistent_with_score_from_counts():
    files, fns, reqs, policies = 3, 5, 2, 1
    manual = score_from_counts(files, fns, reqs, policies)
    assessed = assess_risk(files, fns, reqs, policies)
    assert assessed.risk_score == manual


# ---------------------------------------------------------------------------
# Severity thresholds are internally consistent
# ---------------------------------------------------------------------------

def test_severity_ordering():
    low = assess_risk(0, 0, 0, 0)
    moderate = assess_risk(5, 3, 0, 0)
    high = assess_risk(10, 10, 3, 2)
    # The ordering should be low ≤ moderate ≤ high
    assert low.risk_score <= moderate.risk_score <= high.risk_score
