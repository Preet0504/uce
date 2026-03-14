import os
from dataclasses import dataclass
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - explicit runtime guidance
    raise ImportError(
        "PyYAML is required. Install with `pip install pyyaml`."
    ) from exc


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str
    user: str
    password: str


@dataclass(frozen=True)
class PathsConfig:
    code: tuple[str, ...]
    schema: tuple[str, ...]
    requirements: tuple[str, ...]
    policies: tuple[str, ...]


@dataclass(frozen=True)
class UceConfig:
    project_root: str
    languages: tuple[str, ...]
    paths: PathsConfig
    ignore: tuple[str, ...]
    aliases: dict[str, str]
    neo4j: Neo4jConfig


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return tuple()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if v is not None)
    return (str(value),)


def _normalize_path(root: str, path: str) -> str:
    joined = os.path.abspath(os.path.join(root, path))
    return os.path.normpath(joined)


def load_config(config_path: str, project_root_override: str | None = None) -> UceConfig:
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    project_root = project_root_override or raw.get("project_root") or "."
    project_root = os.path.abspath(project_root)

    languages = _as_tuple(raw.get("languages") or [])
    languages = tuple(sorted({lang.lower() for lang in languages}))

    paths_raw = raw.get("paths") or {}
    paths = PathsConfig(
        code=_as_tuple(paths_raw.get("code") or ["."]),
        schema=_as_tuple(paths_raw.get("schema") or ["db"]),
        requirements=_as_tuple(paths_raw.get("requirements") or ["requirements"]),
        policies=_as_tuple(paths_raw.get("policies") or ["policies"]),
    )

    ignore = _as_tuple(raw.get("ignore") or [])

    aliases = {str(k): str(v) for k, v in (raw.get("aliases") or {}).items()}

    neo4j_raw = raw.get("neo4j") or {}
    env_uri = os.getenv("NEO4J_URI")
    env_user = os.getenv("NEO4J_USER")
    env_pass = os.getenv("NEO4J_PASSWORD")
    neo4j = Neo4jConfig(
        uri=str(env_uri or neo4j_raw.get("uri") or "bolt://localhost:7687"),
        user=str(env_user or neo4j_raw.get("user") or "neo4j"),
        password=str(env_pass or neo4j_raw.get("password") or "password"),
    )

    return UceConfig(
        project_root=project_root,
        languages=languages,
        paths=paths,
        ignore=ignore,
        aliases=aliases,
        neo4j=neo4j,
    )


def resolve_paths(config: UceConfig) -> dict[str, tuple[str, ...]]:
    root = config.project_root
    return {
        "code": tuple(_normalize_path(root, p) for p in config.paths.code),
        "schema": tuple(_normalize_path(root, p) for p in config.paths.schema),
        "requirements": tuple(_normalize_path(root, p) for p in config.paths.requirements),
        "policies": tuple(_normalize_path(root, p) for p in config.paths.policies),
    }
