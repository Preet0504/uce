import os
import re
from graph import GraphDB

POLICIES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "policies"))
REQ_ID_REGEX = re.compile(r"\bRQ-\d{3}\b")


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


def ingest_policies():
    if not os.path.isdir(POLICIES_DIR):
        raise FileNotFoundError(f"Policies directory not found: {POLICIES_DIR}")

    graph = GraphDB()

    for filename in sorted(os.listdir(POLICIES_DIR)):
        if not filename.endswith(".md"):
            continue

        full_path = os.path.join(POLICIES_DIR, filename)
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        policy_id, description, req_ids = _parse_policy(content)
        if not policy_id:
            continue

        graph.run(
            "MERGE (p:Policy {id: $id}) SET p.description = $description",
            id=policy_id,
            description=description,
        )

        for req_id in req_ids:
            graph.run(
                "MERGE (r:Requirement {id: $id})",
                id=req_id,
            )
            graph.run(
                """
                MATCH (p:Policy {id: $policy_id})
                MATCH (r:Requirement {id: $req_id})
                MERGE (p)-[:ENFORCES]->(r)
                """,
                policy_id=policy_id,
                req_id=req_id,
            )

    graph.close()


if __name__ == "__main__":
    ingest_policies()
