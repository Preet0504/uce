from __future__ import annotations

import os
import tempfile
import unittest

from core.config import load_config


class ConfigRbacTests(unittest.TestCase):
    def test_load_rbac_settings_from_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.yaml")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "\n".join(
                        [
                            "project_root: .",
                            "languages: [python]",
                            "paths:",
                            "  code: [.]",
                            "rbac:",
                            "  enabled: true",
                            "  enforce_mode: enforced",
                            "  deny_default: true",
                            "  jwt_issuer: https://issuer.example.com/",
                            "  jwt_audience: uce-mcp",
                            "  jwks_uri: https://issuer.example.com/.well-known/jwks.json",
                            "  clock_skew_seconds: 90",
                        ]
                    )
                )

            config = load_config(config_path)
            self.assertTrue(config.rbac.enabled)
            self.assertEqual(config.rbac.enforce_mode, "enforced")
            self.assertTrue(config.rbac.deny_default)
            self.assertEqual(config.rbac.jwt_issuer, "https://issuer.example.com/")
            self.assertEqual(config.rbac.jwt_audience, "uce-mcp")
            self.assertEqual(
                config.rbac.jwks_uri,
                "https://issuer.example.com/.well-known/jwks.json",
            )
            self.assertEqual(config.rbac.clock_skew_seconds, 90)

    def test_env_overrides_yaml_rbac_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.yaml")
            with open(config_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "\n".join(
                        [
                            "project_root: .",
                            "languages: [python]",
                            "paths:",
                            "  code: [.]",
                            "rbac:",
                            "  enabled: false",
                            "  enforce_mode: advisory",
                        ]
                    )
                )

            old = dict(os.environ)
            try:
                os.environ["RBAC_ENABLED"] = "true"
                os.environ["RBAC_ENFORCE_MODE"] = "enforced"
                config = load_config(config_path)
            finally:
                os.environ.clear()
                os.environ.update(old)

            self.assertTrue(config.rbac.enabled)
            self.assertEqual(config.rbac.enforce_mode, "enforced")


if __name__ == "__main__":
    unittest.main()
