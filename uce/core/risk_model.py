from dataclasses import dataclass


@dataclass(frozen=True)
class RiskAssessment:
    risk_score: int
    severity: str
    rationale: str


def score_from_counts(
    affected_files: int,
    affected_functions: int,
    violated_requirements: int,
    enforced_policies: int,
) -> int:
    return (
        2 * affected_files
        + affected_functions
        + 4 * violated_requirements
        + 3 * enforced_policies
    )


def assess_risk(
    affected_files: int,
    affected_functions: int,
    violated_requirements: int,
    enforced_policies: int,
) -> RiskAssessment:
    score = score_from_counts(
        affected_files,
        affected_functions,
        violated_requirements,
        enforced_policies,
    )

    if score >= 20:
        severity = "high"
    elif score >= 8:
        severity = "moderate"
    else:
        severity = "low"

    rationale = (
        f"files={affected_files}, functions={affected_functions}, "
        f"violated_requirements={violated_requirements}, enforced_policies={enforced_policies}"
    )

    return RiskAssessment(risk_score=score, severity=severity, rationale=rationale)
