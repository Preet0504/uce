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
class RbacConfig:
    enabled: bool
    enforce_mode: str
    deny_default: bool
    jwt_issuer: str | None
    jwt_audience: str | None
    jwks_uri: str | None
    clock_skew_seconds: int


@dataclass(frozen=True)
class PathsConfig:
    code: tuple[str, ...]
    schema: tuple[str, ...]
    requirements: tuple[str, ...]
    policies: tuple[str, ...]
    rbac: tuple[str, ...]
    backend: tuple[str, ...]
    identifiers: tuple[str, ...]


@dataclass(frozen=True)
class UceConfig:
    project_root: str
    languages: tuple[str, ...]
    paths: PathsConfig
    ignore: tuple[str, ...]
    aliases: dict[str, str]
    neo4j: Neo4jConfig
    rbac: RbacConfig


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return tuple()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if v is not None)
    return (str(value),)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    cleaned = str(value).strip().lower()
    if cleaned in {"1", "true", "yes", "y", "on"}:
        return True
    if cleaned in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _normalize_path(root: str, path: str) -> str:
    joined = os.path.abspath(os.path.join(root, path))
    return os.path.normpath(joined)


def load_config(config_path: str, project_root_override: str | None = None) -> UceConfig:
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    config_dir = os.path.dirname(os.path.abspath(config_path))

    project_root = project_root_override or raw.get("project_root") or "."
    project_root = os.path.expanduser(str(project_root))
    if not os.path.isabs(project_root):
        project_root = os.path.abspath(os.path.join(config_dir, project_root))
    else:
        project_root = os.path.abspath(project_root)

    languages = _as_tuple(raw.get("languages") or [])
    languages = tuple(sorted({lang.lower() for lang in languages}))

    paths_raw = raw.get("paths") or {}
    paths = PathsConfig(
        code=_as_tuple(paths_raw.get("code") or ["."]),
        schema=_as_tuple(paths_raw.get("schema") or ["db"]),
        requirements=_as_tuple(paths_raw.get("requirements") or ["artifacts/requirements"]),
        policies=_as_tuple(paths_raw.get("policies") or ["artifacts/policies"]),
        rbac=_as_tuple(paths_raw.get("rbac") or ["artifacts/rbac"]),
        backend=_as_tuple(paths_raw.get("backend") or []),
        identifiers=_as_tuple(paths_raw.get("identifiers") or []),
    )

    ignore = _as_tuple(raw.get("ignore") or [])

    aliases = {str(k): str(v) for k, v in (raw.get("aliases") or {}).items()}

    neo4j_raw = raw.get("neo4j") or {}
    env_uri = os.getenv("NEO4J_URI")
    env_user = os.getenv("NEO4J_USER") or os.getenv("NEO4J_USERNAME")
    env_pass = os.getenv("NEO4J_PASSWORD") or os.getenv("NEO4J_PASS")
    neo4j = Neo4jConfig(
        uri=str(env_uri or neo4j_raw.get("uri") or "bolt://localhost:7687"),
        user=str(env_user or neo4j_raw.get("user") or "neo4j"),
        password=str(env_pass or neo4j_raw.get("password") or "password"),
    )

    rbac_raw = raw.get("rbac") or {}
    env_rbac_enabled = os.getenv("RBAC_ENABLED")
    env_rbac_mode = os.getenv("RBAC_ENFORCE_MODE")
    env_rbac_deny_default = os.getenv("RBAC_DENY_DEFAULT")
    env_rbac_issuer = os.getenv("RBAC_JWT_ISSUER")
    env_rbac_audience = os.getenv("RBAC_JWT_AUDIENCE")
    env_rbac_jwks = os.getenv("RBAC_JWKS_URI")
    env_rbac_clock_skew = os.getenv("RBAC_CLOCK_SKEW_SECONDS")

    enforce_mode = str(env_rbac_mode or rbac_raw.get("enforce_mode") or "advisory").strip().lower()
    if enforce_mode not in {"advisory", "enforced"}:
        enforce_mode = "advisory"

    try:
        clock_skew_seconds = int(
            str(env_rbac_clock_skew or rbac_raw.get("clock_skew_seconds") or "60").strip()
        )
    except ValueError:
        clock_skew_seconds = 60
    clock_skew_seconds = max(clock_skew_seconds, 0)

    jwt_issuer = str(env_rbac_issuer or rbac_raw.get("jwt_issuer") or "").strip() or None
    jwt_audience = str(env_rbac_audience or rbac_raw.get("jwt_audience") or "").strip() or None
    jwks_uri = str(env_rbac_jwks or rbac_raw.get("jwks_uri") or "").strip() or None

    rbac = RbacConfig(
        enabled=_as_bool(env_rbac_enabled if env_rbac_enabled is not None else rbac_raw.get("enabled"), default=False),
        enforce_mode=enforce_mode,
        deny_default=_as_bool(
            env_rbac_deny_default if env_rbac_deny_default is not None else rbac_raw.get("deny_default"),
            default=True,
        ),
        jwt_issuer=jwt_issuer,
        jwt_audience=jwt_audience,
        jwks_uri=jwks_uri,
        clock_skew_seconds=clock_skew_seconds,
    )

    return UceConfig(
        project_root=project_root,
        languages=languages,
        paths=paths,
        ignore=ignore,
        aliases=aliases,
        neo4j=neo4j,
        rbac=rbac,
    )


def resolve_paths(config: UceConfig) -> dict[str, tuple[str, ...]]:
    root = config.project_root
    return {
        "code": tuple(_normalize_path(root, p) for p in config.paths.code),
        "schema": tuple(_normalize_path(root, p) for p in config.paths.schema),
        "requirements": tuple(_normalize_path(root, p) for p in config.paths.requirements),
        "policies": tuple(_normalize_path(root, p) for p in config.paths.policies),
        "rbac": tuple(_normalize_path(root, p) for p in config.paths.rbac),
        "backend": tuple(_normalize_path(root, p) for p in config.paths.backend),
        "identifiers": tuple(_normalize_path(root, p) for p in config.paths.identifiers),
    }
