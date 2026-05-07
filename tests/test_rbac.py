from __future__ import annotations

import os
import tempfile
import unittest

from core.rbac import (
    AuthorityRule,
    evaluate_rules,
    normalize_operation,
    normalize_project_path,
)


class RbacPathTests(unittest.TestCase):
    def test_normalize_project_path_allows_in_root_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "core", "..", "config.py")
            abs_path, rel_path = normalize_project_path(tmp, target)
            self.assertTrue(abs_path.endswith("config.py"))
            self.assertEqual(rel_path, "config.py")

    def test_normalize_project_path_rejects_out_of_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                normalize_project_path(tmp, "..\\..\\outside.txt")


class RbacDecisionTests(unittest.TestCase):
    def test_precedence_deny_over_allow(self) -> None:
        rules = [
            AuthorityRule(
                rule_id="ALLOW-CORE",
                operation="write",
                path_pattern="core/*",
                effect="allow",
                min_role="viewer",
                min_role_rank=1,
                source_priority=1,
            ),
            AuthorityRule(
                rule_id="DENY-SECRET",
                operation="write",
                path_pattern="core/secret.py",
                effect="deny",
                min_role="admin",
                min_role_rank=3,
                source_priority=1,
            ),
        ]
        decision = evaluate_rules(
            operation="write",
            normalized_path="core/secret.py",
            principal_role="viewer",
            rules=rules,
            deny_default=True,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.matched_rule_id, "DENY-SECRET")

    def test_most_specific_path_wins(self) -> None:
        rules = [
            AuthorityRule(
                rule_id="ALLOW-WIDE",
                operation="write",
                path_pattern="core/*",
                effect="allow",
                min_role="viewer",
                min_role_rank=1,
                source_priority=5,
            ),
            AuthorityRule(
                rule_id="ALLOW-NARROW",
                operation="write",
                path_pattern="core/config.py",
                effect="allow",
                min_role="viewer",
                min_role_rank=1,
                source_priority=5,
            ),
        ]
        decision = evaluate_rules(
            operation="write",
            normalized_path="core/config.py",
            principal_role="admin",
            rules=rules,
            deny_default=True,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.matched_rule_id, "ALLOW-NARROW")

    def test_source_priority_breaks_ties(self) -> None:
        rules = [
            AuthorityRule(
                rule_id="RULE-LOW",
                operation="write",
                path_pattern="core/file.py",
                effect="allow",
                min_role="viewer",
                min_role_rank=1,
                source_priority=10,
            ),
            AuthorityRule(
                rule_id="RULE-HIGH",
                operation="write",
                path_pattern="core/file.py",
                effect="allow",
                min_role="viewer",
                min_role_rank=1,
                source_priority=100,
            ),
        ]
        decision = evaluate_rules(
            operation="write",
            normalized_path="core/file.py",
            principal_role="viewer",
            rules=rules,
            deny_default=True,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.matched_rule_id, "RULE-HIGH")

    def test_unknown_file_denied_by_default(self) -> None:
        decision = evaluate_rules(
            operation="write",
            normalized_path="unknown/path.py",
            principal_role="editor",
            rules=[],
            deny_default=True,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("deny-by-default", decision.reason)

    def test_missing_role_claim_denied(self) -> None:
        decision = evaluate_rules(
            operation="delete",
            normalized_path="core/config.py",
            principal_role=None,
            rules=[],
            deny_default=True,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("role", decision.reason.lower())

    def test_operation_validation(self) -> None:
        with self.assertRaises(ValueError):
            normalize_operation("read")


if __name__ == "__main__":
    unittest.main()
