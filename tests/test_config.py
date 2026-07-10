"""Tests for uce.core.config — config loading and validation."""
import os
import textwrap
import tempfile
import pytest

from uce.core.config import UceConfig, Neo4jConfig, RbacConfig, PathsConfig, load_config


SAMPLE_YAML = textwrap.dedent("""
neo4j:
  uri: bolt://localhost:7688
  user: neo4j
  password: testpass

paths:
  code:
    - src/
  schema:
    - db/
  requirements:
    - docs/requirements/
  policies:
    - docs/policies/

rbac:
  enabled: false
  enforce_mode: advisory
""")

# Minimal yaml used by defaults tests (no rbac, no paths, no project_root sections).
MINIMAL_YAML = textwrap.dedent("""
neo4j:
  uri: bolt://localhost:7687
  user: neo4j
  password: testpass
""")


# ---------------------------------------------------------------------------
# Dataclass field verification
# ---------------------------------------------------------------------------

def test_neo4j_config_fields():
    cfg = Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="test")
    assert cfg.uri == "bolt://localhost:7687"
    assert cfg.user == "neo4j"
    assert cfg.password == "test"


def test_rbac_config_defaults():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(MINIMAL_YAML)
        config_path = f.name
    try:
        cfg = load_config(config_path)
        assert cfg.rbac.enabled is False
        assert cfg.rbac.enforce_mode in ("advisory", "enforced")
    finally:
        os.unlink(config_path)


def test_paths_config_defaults():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(MINIMAL_YAML)
        config_path = f.name
    try:
        cfg = load_config(config_path)
        assert isinstance(cfg.paths.code, tuple)
        assert isinstance(cfg.paths.schema, tuple)
        assert isinstance(cfg.paths.requirements, tuple)
        assert isinstance(cfg.paths.policies, tuple)
        assert isinstance(cfg.paths.backend, tuple)
    finally:
        os.unlink(config_path)


# ---------------------------------------------------------------------------
# load_config from yaml file
# ---------------------------------------------------------------------------

def test_load_config_from_yaml():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_YAML)
        config_path = f.name
    try:
        cfg = load_config(config_path)
        assert cfg.neo4j.uri == "bolt://localhost:7688"
        assert cfg.neo4j.user == "neo4j"
        assert cfg.neo4j.password == "testpass"
        assert cfg.rbac.enabled is False
    finally:
        os.unlink(config_path)


def test_load_config_env_override(monkeypatch):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(SAMPLE_YAML)
        config_path = f.name
    try:
        monkeypatch.setenv("NEO4J_URI", "bolt://override:7687")
        monkeypatch.setenv("NEO4J_PASSWORD", "envpass")
        cfg = load_config(config_path)
        assert cfg.neo4j.uri == "bolt://override:7687"
        assert cfg.neo4j.password == "envpass"
    finally:
        os.unlink(config_path)
        monkeypatch.delenv("NEO4J_URI", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)


def test_load_config_missing_file_raises():
    with pytest.raises((FileNotFoundError, RuntimeError, Exception)):
        load_config("/nonexistent/config.yaml")


# ---------------------------------------------------------------------------
# UceConfig project_root
# ---------------------------------------------------------------------------

def test_uce_config_project_root_default():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(MINIMAL_YAML)
        config_path = f.name
    try:
        cfg = load_config(config_path)
        assert cfg.project_root is not None
        assert isinstance(cfg.project_root, str)
    finally:
        os.unlink(config_path)
