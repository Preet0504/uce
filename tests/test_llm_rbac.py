from __future__ import annotations

import unittest

from ingestion.llm_rbac import validate_extracted_rules


class LlmRbacValidationTests(unittest.TestCase):
    def test_validate_extracted_rules_accepts_valid_rows(self) -> None:
        payload = {
            "rules": [
                {
                    "rule_id": "RBAC-001",
                    "operation": "write",
                    "path_pattern": "core/config.py",
                    "min_role": "admin",
                    "effect": "deny",
                    "source_priority": 100,
                },
                {
                    "operation": "delete",
                    "path": "runtime/*",
                    "min_role": "editor",
                    "effect": "allow",
                },
            ]
        }
        rules = validate_extracted_rules(payload, default_policy_id="P-001")
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]["rule_id"], "RBAC-001")
        self.assertEqual(rules[1]["operation"], "delete")
        self.assertTrue(rules[1]["rule_id"].startswith("P-001::RBAC::"))

    def test_validate_extracted_rules_rejects_invalid_rows(self) -> None:
        payload = {
            "rules": [
                {
                    "operation": "write",
                    "path_pattern": "/etc/passwd",
                    "min_role": "admin",
                    "effect": "deny",
                },
                {
                    "operation": "read",
                    "path_pattern": "core/config.py",
                    "min_role": "admin",
                    "effect": "deny",
                },
                {
                    "operation": "write",
                    "path_pattern": "core/config.py",
                    "min_role": "superadmin",
                    "effect": "deny",
                },
            ]
        }
        rules = validate_extracted_rules(payload, default_policy_id="P-002")
        self.assertEqual(rules, [])


if __name__ == "__main__":
    unittest.main()
