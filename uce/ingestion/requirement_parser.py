from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RequirementDoc:
    req_id: str
    title: str
    description: str


def _parse_requirement(content: str):
    req_id = None
    title = None
    description_lines = []
    in_description = False

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("ID:"):
            req_id = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("Title:"):
            title = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("Description:"):
            in_description = True
            description_lines.append(stripped.split(":", 1)[1].strip())
            continue
        if in_description and stripped:
            description_lines.append(stripped)

    description = " ".join([line for line in description_lines if line])
    return req_id, title, description


def parse_requirements(requirements_dir: str) -> list[RequirementDoc]:
    if not os.path.isdir(requirements_dir):
        return []

    requirements: list[RequirementDoc] = []
    for filename in sorted(os.listdir(requirements_dir)):
        if not filename.endswith(".md"):
            continue
        full_path = os.path.join(requirements_dir, filename)
        with open(full_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        req_id, title, description = _parse_requirement(content)
        if not req_id or not title:
            continue
        requirements.append(RequirementDoc(req_id=req_id, title=title, description=description))

    return requirements
