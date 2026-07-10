from neo4j import GraphDatabase

from uce.core.rbac import ROLE_RANKS


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

    def ensure_indexes(self) -> None:
        """Create indexes and uniqueness constraints. Safe to call multiple times."""
        # Uniqueness constraints (also create a backing index automatically).
        constraint_statements = [
            "CREATE CONSTRAINT uce_file_path_unique IF NOT EXISTS FOR (f:File) REQUIRE (f.path) IS UNIQUE",
            "CREATE CONSTRAINT uce_table_name_unique IF NOT EXISTS FOR (t:Table) REQUIRE (t.name) IS UNIQUE",
            "CREATE CONSTRAINT uce_requirement_id_unique IF NOT EXISTS FOR (r:Requirement) REQUIRE (r.id) IS UNIQUE",
            "CREATE CONSTRAINT uce_policy_id_unique IF NOT EXISTS FOR (p:Policy) REQUIRE (p.id) IS UNIQUE",
            "CREATE CONSTRAINT uce_identifier_name_unique IF NOT EXISTS FOR (i:Identifier) REQUIRE (i.name) IS UNIQUE",
            "CREATE CONSTRAINT uce_authority_rule_id_unique IF NOT EXISTS FOR (ar:AuthorityRule) REQUIRE (ar.id) IS UNIQUE",
            "CREATE CONSTRAINT uce_role_name_unique IF NOT EXISTS FOR (r:Role) REQUIRE (r.name) IS UNIQUE",
        ]
        for stmt in constraint_statements:
            try:
                self.run(stmt)
            except Exception:
                pass

        # Composite indexes for lookups that have no uniqueness guarantee
        # (a name can appear in multiple files).
        index_statements = [
            "CREATE INDEX uce_column_lookup IF NOT EXISTS FOR (c:Column) ON (c.name, c.table)",
            "CREATE INDEX uce_function_lookup IF NOT EXISTS FOR (fn:Function) ON (fn.name, fn.file_path)",
            "CREATE INDEX uce_class_lookup IF NOT EXISTS FOR (c:Class) ON (c.name, c.file_path)",
        ]
        for stmt in index_statements:
            try:
                self.run(stmt)
            except Exception:
                pass

    def ensure_file(self, path: str, language: str | None = None, last_modified: float | None = None) -> None:
        props: dict[str, object] = {"id": path}
        if language is not None:
            props["language"] = language
        if last_modified is not None:
            props["last_modified"] = last_modified
        self.run(
            "MERGE (f:File {path: $path}) SET f += $props",
            path=path,
            props=props,
        )

    def cleanup_stale_schema(self, live_table_names: list[str]) -> None:
        """Remove Table and Column nodes whose names are no longer in the live schema."""
        if not live_table_names:
            return
        self.run(
            """
            MATCH (c:Column)
            WHERE NOT c.table IN $live_tables
            DETACH DELETE c
            """,
            live_tables=live_table_names,
        )
        self.run(
            """
            MATCH (t:Table)
            WHERE NOT t.name IN $live_tables
            DETACH DELETE t
            """,
            live_tables=live_table_names,
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

    # ------------------------------------------------------------------
    # GDPR / data-governance graph operations
    # ------------------------------------------------------------------

    def upsert_personal_data_classification(
        self,
        column: str,
        table: str,
        category: str,
        sensitivity: str,
        gdpr_articles: list[str],
        subject_type: str,
        rationale: str,
    ) -> None:
        """Create/update a PersonalData node and link it to the Column node."""
        pd_id = f"pd:{table}.{column}"
        self.run(
            """
            MERGE (pd:PersonalData {id: $pd_id})
            SET pd.column    = $column,
                pd.table     = $table,
                pd.category  = $category,
                pd.sensitivity = $sensitivity,
                pd.gdpr_articles = $gdpr_articles,
                pd.subject_type  = $subject_type,
                pd.rationale     = $rationale
            """,
            pd_id=pd_id,
            column=column,
            table=table,
            category=category,
            sensitivity=sensitivity,
            gdpr_articles=gdpr_articles,
            subject_type=subject_type,
            rationale=rationale,
        )
        self.run(
            """
            MATCH (c:Column {name: $column, table: $table})
            MATCH (pd:PersonalData {id: $pd_id})
            MERGE (c)-[:CLASSIFIED_AS]->(pd)
            """,
            column=column,
            table=table,
            pd_id=pd_id,
        )

    def find_personal_data(self, query: str = "") -> list[dict]:
        """Return all PersonalData nodes (optionally filtered by category or sensitivity)."""
        q_lower = query.strip().lower()
        rows = self.run(
            """
            MATCH (c:Column)-[:CLASSIFIED_AS]->(pd:PersonalData)
            OPTIONAL MATCH (t:Table {name: pd.table})
            OPTIONAL MATCH (f:File)-[:USES_TABLE]->(t)
            RETURN pd.id         AS pd_id,
                   pd.column     AS column,
                   pd.table      AS table,
                   pd.category   AS category,
                   pd.sensitivity AS sensitivity,
                   pd.gdpr_articles AS gdpr_articles,
                   pd.subject_type  AS subject_type,
                   pd.rationale     AS rationale,
                   collect(DISTINCT f.path) AS files
            ORDER BY pd.sensitivity DESC, pd.table, pd.column
            """
        )
        results = []
        for row in rows:
            if q_lower and q_lower not in str(row.get("category", "")).lower() \
                    and q_lower not in str(row.get("sensitivity", "")).lower() \
                    and q_lower not in str(row.get("table", "")).lower() \
                    and q_lower not in str(row.get("column", "")).lower():
                continue
            results.append({
                "pd_id": row.get("pd_id"),
                "column": row.get("column"),
                "table": row.get("table"),
                "category": row.get("category"),
                "sensitivity": row.get("sensitivity"),
                "gdpr_articles": list(row.get("gdpr_articles") or []),
                "subject_type": row.get("subject_type"),
                "rationale": row.get("rationale"),
                "files": sorted(f for f in (row.get("files") or []) if f),
            })
        return results

    def erasure_impact(self, subject_type: str = "") -> dict:
        """Return all personal-data columns + handling files relevant to a data-erasure request.

        This answers the GDPR Art. 17 "right to erasure" question:
        'Which tables/columns contain personal data for this type of data subject,
         and which files process it?'
        """
        rows = self.run(
            """
            MATCH (c:Column)-[:CLASSIFIED_AS]->(pd:PersonalData)
            OPTIONAL MATCH (t:Table {name: pd.table})<-[:USES_TABLE]-(f:File)
            WHERE $subject_type = '' OR toLower(pd.subject_type) = toLower($subject_type)
            RETURN pd.table      AS table,
                   pd.column     AS column,
                   pd.category   AS category,
                   pd.sensitivity AS sensitivity,
                   pd.gdpr_articles AS gdpr_articles,
                   collect(DISTINCT f.path) AS files
            ORDER BY pd.sensitivity DESC, pd.table, pd.column
            """,
            subject_type=subject_type or "",
        )
        columns_to_erase = []
        all_files: set[str] = set()
        for row in rows:
            files = sorted(f for f in (row.get("files") or []) if f)
            all_files.update(files)
            columns_to_erase.append({
                "table": row.get("table"),
                "column": row.get("column"),
                "category": row.get("category"),
                "sensitivity": row.get("sensitivity"),
                "gdpr_articles": list(row.get("gdpr_articles") or []),
                "files": files,
            })
        return {
            "subject_type": subject_type or "all",
            "columns_to_erase": columns_to_erase,
            "total_columns": len(columns_to_erase),
            "total_files_affected": len(all_files),
            "affected_files": sorted(all_files),
        }

    def purpose_check(self, table: str, column: str = "") -> list[dict]:
        """Return PersonalData nodes for a table/column with their purpose/basis metadata."""
        if column:
            rows = self.run(
                """
                MATCH (c:Column {name: $column, table: $table})-[:CLASSIFIED_AS]->(pd:PersonalData)
                RETURN pd.id AS pd_id, pd.table AS table, pd.column AS column,
                       pd.category AS category, pd.sensitivity AS sensitivity,
                       pd.gdpr_articles AS gdpr_articles, pd.rationale AS rationale
                """,
                column=column,
                table=table,
            )
        else:
            rows = self.run(
                """
                MATCH (c:Column {table: $table})-[:CLASSIFIED_AS]->(pd:PersonalData)
                RETURN pd.id AS pd_id, pd.table AS table, pd.column AS column,
                       pd.category AS category, pd.sensitivity AS sensitivity,
                       pd.gdpr_articles AS gdpr_articles, pd.rationale AS rationale
                """,
                table=table,
            )
        return [
            {
                "pd_id": r.get("pd_id"),
                "table": r.get("table"),
                "column": r.get("column"),
                "category": r.get("category"),
                "sensitivity": r.get("sensitivity"),
                "gdpr_articles": list(r.get("gdpr_articles") or []),
                "rationale": r.get("rationale"),
            }
            for r in rows
        ]

    def load_all_authority_rules(self) -> list[dict]:
        """Load every authority rule without operation/path filtering.

        Used by the MCP server's TTL cache so that Python-side evaluate_rules
        handles all filtering, and a single Neo4j round-trip covers any
        operation/path combination for the duration of the cache window.
        """
        return self.run(
            """
            MATCH (rule:AuthorityRule)-[:REQUIRES_ROLE]->(role:Role)
            OPTIONAL MATCH (policy:Policy)-[:DEFINES_RULE]->(rule)
            RETURN rule.id AS rule_id,
                   rule.operation AS operation,
                   rule.path_pattern AS path_pattern,
                   rule.effect AS effect,
                   rule.min_role AS min_role,
                   role.rank AS min_role_rank,
                   coalesce(rule.source_priority, 0) AS source_priority,
                   policy.id AS policy_id
            """
        )
