from __future__ import annotations

from neo4j import GraphDatabase

from core.rbac import ROLE_RANKS


def _has_wildcards(path_pattern: str) -> bool:
    return any(token in path_pattern for token in ("*", "?", "["))


class GraphDB:
    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self.driver.close()

    def run(self, query: str, **params):
        with self.driver.session() as session:
            return list(session.run(query, **params))

    def ensure_file(self, path: str) -> None:
        self.run(
            "MERGE (f:File {path: $path}) SET f.id = $id",
            path=path,
            id=path,
        )

    def clear_file_relationships(self, path: str) -> None:
        self.run(
            "MATCH (f:File {path: $path})-[r]->() DELETE r",
            path=path,
        )
        self.run(
            "MATCH (fn:Function {file_path: $path})-[r]->() DELETE r",
            path=path,
        )
        self.run(
            "MATCH (c:Class {file_path: $path})-[r]->() DELETE r",
            path=path,
        )
        self.run(
            "MATCH (m:Method {file_path: $path})-[r]->() DELETE r",
            path=path,
        )

    def delete_file(self, path: str) -> None:
        self.run("MATCH (f:File {path: $path}) DETACH DELETE f", path=path)
        self.run("MATCH (fn:Function {file_path: $path}) DETACH DELETE fn", path=path)
        self.run("MATCH (c:Class {file_path: $path}) DETACH DELETE c", path=path)
        self.run("MATCH (m:Method {file_path: $path}) DETACH DELETE m", path=path)

    def cleanup_orphan_identifiers(self) -> None:
        self.run(
            "MATCH (i:Identifier) WHERE NOT ( ()-[:USES_IDENTIFIER]->(i) ) DETACH DELETE i"
        )

    def cleanup_missing_files(self, known_paths: list[str]) -> None:
        if not known_paths:
            return
        self.run(
            "MATCH (f:File) WHERE NOT f.path IN $paths DETACH DELETE f",
            paths=known_paths,
        )

    def clear_authority_rules(self) -> None:
        self.run("MATCH (r:AuthorityRule) DETACH DELETE r")
        self.run(
            """
            MATCH (p:ResourcePattern)
            WHERE NOT (:AuthorityRule)-[:TARGETS_PATTERN]->(p)
            DETACH DELETE p
            """
        )

    def upsert_rbac_role(self, role_name: str, rank: int) -> None:
        self.run(
            """
            MERGE (role:Role {name: $name})
            SET role.rank = $rank
            """,
            name=role_name,
            rank=rank,
        )

    def ensure_default_roles(self) -> None:
        for name, rank in ROLE_RANKS.items():
            self.upsert_rbac_role(name, rank)

    def upsert_authority_rule(
        self,
        policy_id: str,
        rule_id: str,
        operation: str,
        path_pattern: str,
        min_role: str,
        effect: str,
        source_priority: int,
    ) -> None:
        self.run(
            """
            MERGE (p:Policy {id: $policy_id})
            MERGE (rule:AuthorityRule {id: $rule_id})
            SET rule.operation = $operation,
                rule.path_pattern = $path_pattern,
                rule.min_role = $min_role,
                rule.effect = $effect,
                rule.source_priority = $source_priority
            MERGE (p)-[:DEFINES_RULE]->(rule)
            """,
            policy_id=policy_id,
            rule_id=rule_id,
            operation=operation,
            path_pattern=path_pattern,
            min_role=min_role,
            effect=effect,
            source_priority=source_priority,
        )
        self.run(
            """
            MATCH (rule:AuthorityRule {id: $rule_id})
            MATCH (role:Role {name: $min_role})
            MERGE (rule)-[:REQUIRES_ROLE]->(role)
            """,
            rule_id=rule_id,
            min_role=min_role,
        )
        self._link_authority_rule_targets(
            rule_id=rule_id,
            path_pattern=path_pattern,
        )

    def _link_authority_rule_targets(self, rule_id: str, path_pattern: str) -> None:
        normalized = path_pattern.replace("\\", "/").strip().strip("/")
        if not normalized:
            return

        self.run(
            """
            MATCH (rule:AuthorityRule {id: $rule_id})-[r:TARGETS_FILE|TARGETS_PATTERN]->()
            DELETE r
            """,
            rule_id=rule_id,
        )

        self.run(
            """
            MATCH (rule:AuthorityRule {id: $rule_id})
            MERGE (pattern:ResourcePattern {pattern: $path_pattern})
            SET pattern.kind = "path_pattern"
            MERGE (rule)-[:TARGETS_PATTERN]->(pattern)
            """,
            rule_id=rule_id,
            path_pattern=normalized,
        )

        if _has_wildcards(normalized):
            return

        self.run(
            """
            MATCH (rule:AuthorityRule {id: $rule_id})
            MATCH (f:File)
            WHERE f.path = $path_pattern
               OR f.path STARTS WITH $path_prefix
            MERGE (rule)-[:TARGETS_FILE]->(f)
            """,
            rule_id=rule_id,
            path_pattern=normalized,
            path_prefix=f"{normalized}/",
        )

    def replace_authority_rules(self, rules: list[dict[str, object]]) -> None:
        self.ensure_default_roles()
        self.clear_authority_rules()
        for rule in rules:
            self.upsert_authority_rule(
                policy_id=str(rule.get("policy_id") or ""),
                rule_id=str(rule.get("rule_id") or ""),
                operation=str(rule.get("operation") or ""),
                path_pattern=str(rule.get("path_pattern") or ""),
                min_role=str(rule.get("min_role") or ""),
                effect=str(rule.get("effect") or "allow"),
                source_priority=int(rule.get("source_priority") or 0),
            )

    def load_authority_rules(self, operation: str, normalized_path: str) -> list[dict]:
        return self.run(
            """
            MATCH (rule:AuthorityRule)-[:REQUIRES_ROLE]->(role:Role)
            OPTIONAL MATCH (policy:Policy)-[:DEFINES_RULE]->(rule)
            WHERE rule.operation IN [$operation, "*"]
              AND (
                   rule.path_pattern CONTAINS "*"
                   OR rule.path_pattern CONTAINS "?"
                   OR rule.path_pattern CONTAINS "["
                   OR rule.path_pattern = $normalized_path
                   OR $normalized_path STARTS WITH rule.path_pattern + "/"
              )
            RETURN rule.id AS rule_id,
                   rule.operation AS operation,
                   rule.path_pattern AS path_pattern,
                   rule.effect AS effect,
                   rule.min_role AS min_role,
                   role.rank AS min_role_rank,
                   coalesce(rule.source_priority, 0) AS source_priority,
                   policy.id AS policy_id
            """,
            operation=operation,
            normalized_path=normalized_path,
        )
