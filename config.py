import os

try:
    from uce.core.config import load_config
except Exception:  # pragma: no cover - fallback to defaults
    load_config = None

_CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "config.yaml"))

_config = None
if load_config:
    try:
        _config = load_config(_CONFIG_PATH)
    except Exception:
        _config = None

if _config:
    NEO4J_URI = _config.neo4j.uri
    NEO4J_USER = _config.neo4j.user
    NEO4J_PASS = _config.neo4j.password
    PROJECT_ROOT = _config.project_root
else:
    NEO4J_URI = "bolt://localhost:7687"
    NEO4J_USER = "neo4j"
    NEO4J_PASS = "testpassword"
    PROJECT_ROOT = os.path.abspath(".")
