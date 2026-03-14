from __future__ import annotations

import os
import re
from dataclasses import dataclass

REQ_ID_REGEX = re.compile(r"\bRQ-\d{3}\b")


@dataclass(frozen=True)
class PolicyDoc:
    policy_id: str
    description: str
    requirement_ids: tuple[str, ...]


def _parse_policy(content: str):
    policy_id = None
    description = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("ID:"):
            policy_id = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Description:"):
            description = stripped.split(":", 1)[1].strip()
    req_ids = sorted(set(REQ_ID_REGEX.findall(content)))
    return policy_id, description, req_ids


def parse_policies(policies_dir: str) -> list[PolicyDoc]:
    if not os.path.isdir(policies_dir):
        return []

    policies: list[PolicyDoc] = []
    for filename in sorted(os.listdir(policies_dir)):
        if not filename.endswith(".md"):
            continue
        full_path = os.path.join(policies_dir, filename)
        with open(full_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        policy_id, description, req_ids = _parse_policy(content)
        if not policy_id:
            continue
        policies.append(
            PolicyDoc(policy_id=policy_id, description=description, requirement_ids=tuple(req_ids))
        )

    return policies
