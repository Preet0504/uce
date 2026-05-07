import argparse
import logging
import os
from typing import Any, Union

import tiktoken

logger = logging.getLogger("mcp_neo4j_cypher")
logger.setLevel(logging.INFO)

_DEFAULT_UCE_CONFIG_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "config.yaml")
)
_DEFAULT_DOTENV_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".env")
)


def _load_dotenv(path: str, override: bool = False) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key:
                    continue
                value = value.strip()
                if (
                    (value.startswith('"') and value.endswith('"'))
                    or (value.startswith("'") and value.endswith("'"))
                ):
                    value = value[1:-1]
                if not override and key in os.environ:
                    continue
                os.environ[key] = value
    except OSError:
        return


def _load_uce_config_defaults() -> dict[str, str]:
    config_path = os.getenv("UCE_CONFIG_PATH")
    if not config_path:
        cwd_config = os.path.abspath(os.path.join(os.getcwd(), "config.yaml"))
        if os.path.exists(cwd_config):
            config_path = cwd_config
        else:
            config_path = _DEFAULT_UCE_CONFIG_PATH
    if not config_path or not os.path.exists(config_path):
        return {}
    try:
        import yaml
    except Exception as exc:
        logger.warning(
            "Warning: PyYAML not available; cannot read config.yaml for Neo4j defaults: %s",
            exc,
        )
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except Exception as exc:
        logger.warning(
            "Warning: Failed to read config.yaml at %s: %s", config_path, exc
        )
        return {}

    neo4j = raw.get("neo4j") or {}
    defaults: dict[str, str] = {}
    if neo4j.get("uri"):
        defaults["db_url"] = str(neo4j.get("uri"))
    if neo4j.get("user"):
        defaults["username"] = str(neo4j.get("user"))
    if neo4j.get("password"):
        defaults["password"] = str(neo4j.get("password"))
    if neo4j.get("database"):
        defaults["database"] = str(neo4j.get("database"))
    return defaults


def parse_boolean_safely(value: Union[str, bool]) -> bool:
    """
    Safely parse a string value to boolean with strict validation.

    Parameters
    ----------
    value : Union[str, bool]
        The value to parse to boolean.

    Returns
    -------
    bool
        The parsed boolean value.
    """

    if isinstance(value, bool):
        return value

    elif isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        elif normalized == "false":
            return False
        else:
            raise ValueError(
                f"Invalid boolean value: '{value}'. Must be 'true' or 'false'"
            )
    # we shouldn't get here, but just in case
    else:
        raise ValueError(f"Invalid boolean value: '{value}'. Must be 'true' or 'false'")


def process_config(args: argparse.Namespace) -> dict[str, Union[str, int, None]]:
    """
    Process the command line arguments and environment variables to create a config dictionary.
    This may then be used as input to the main server function.
    If any value is not provided, then a warning is logged and a default value is used, if appropriate.

    Parameters
    ----------
    args : argparse.Namespace
        The command line arguments.

    Returns
    -------
    config : dict[str, str]
        The configuration dictionary.
    """

    dotenv_path = os.getenv("UCE_DOTENV_PATH")
    if not dotenv_path:
        cwd_env = os.path.abspath(os.path.join(os.getcwd(), ".env"))
        if os.path.exists(cwd_env):
            dotenv_path = cwd_env
        else:
            dotenv_path = _DEFAULT_DOTENV_PATH
    _load_dotenv(dotenv_path, override=False)

    config = dict()

    config_defaults = _load_uce_config_defaults()

    # parse uri
    if args.db_url is not None:
        config["db_url"] = args.db_url
    else:
        if os.getenv("NEO4J_URL") is not None:
            config["db_url"] = os.getenv("NEO4J_URL")
        else:
            if os.getenv("NEO4J_URI") is not None:
                config["db_url"] = os.getenv("NEO4J_URI")
            else:
                if config_defaults.get("db_url"):
                    config["db_url"] = config_defaults["db_url"]
                    logger.info(
                        "Info: Using Neo4j URI from config.yaml: %s", config["db_url"]
                    )
                else:
                    logger.warning(
                        "Warning: No Neo4j connection URL provided. Using default: bolt://localhost:7687"
                    )
                    config["db_url"] = "bolt://localhost:7687"

    # parse username
    if args.username is not None:
        config["username"] = args.username
    else:
        if os.getenv("NEO4J_USERNAME") is not None:
            config["username"] = os.getenv("NEO4J_USERNAME")
        else:
            if config_defaults.get("username"):
                config["username"] = config_defaults["username"]
                logger.info(
                    "Info: Using Neo4j username from config.yaml: %s",
                    config["username"],
                )
            else:
                logger.warning(
                    "Warning: No Neo4j username provided. Using default: neo4j"
                )
                config["username"] = "neo4j"

    # parse password
    if args.password is not None:
        config["password"] = args.password
    else:
        if os.getenv("NEO4J_PASSWORD") is not None:
            config["password"] = os.getenv("NEO4J_PASSWORD")
        else:
            if config_defaults.get("password"):
                config["password"] = config_defaults["password"]
                logger.info("Info: Using Neo4j password from config.yaml.")
            else:
                logger.warning(
                    "Warning: No Neo4j password provided. Using default: password"
                )
                config["password"] = "password"

    # parse database
    if args.database is not None:
        config["database"] = args.database
    else:
        if os.getenv("NEO4J_DATABASE") is not None:
            config["database"] = os.getenv("NEO4J_DATABASE")
        else:
            if config_defaults.get("database"):
                config["database"] = config_defaults["database"]
                logger.info(
                    "Info: Using Neo4j database from config.yaml: %s",
                    config["database"],
                )
            else:
                logger.warning(
                    "Warning: No Neo4j database provided. Using default: neo4j"
                )
                config["database"] = "neo4j"

    # parse namespace
    if args.namespace is not None:
        config["namespace"] = args.namespace
    else:
        if os.getenv("NEO4J_NAMESPACE") is not None:
            config["namespace"] = os.getenv("NEO4J_NAMESPACE")
        else:
            logger.info("Info: No namespace provided. No namespace will be used.")
            config["namespace"] = ""

    # parse transport
    if args.transport is not None:
        config["transport"] = args.transport
    else:
        env_transport = os.getenv("NEO4J_TRANSPORT_MODE") or os.getenv("NEO4J_TRANSPORT")
        if env_transport is not None:
            config["transport"] = env_transport
        else:
            logger.warning("Warning: No transport type provided. Using default: stdio")
            config["transport"] = "stdio"

    # parse server host
    if args.server_host is not None:
        if config["transport"] == "stdio":
            logger.warning(
                "Warning: Server host provided, but transport is `stdio`. The `server_host` argument will be set, but ignored."
            )
        config["host"] = args.server_host
    else:
        if os.getenv("NEO4J_MCP_SERVER_HOST") is not None:
            if config["transport"] == "stdio":
                logger.warning(
                    "Warning: Server host provided, but transport is `stdio`. The `NEO4J_MCP_SERVER_HOST` environment variable will be set, but ignored."
                )
            config["host"] = os.getenv("NEO4J_MCP_SERVER_HOST")
        elif config["transport"] != "stdio":
            logger.warning(
                "Warning: No server host provided and transport is not `stdio`. Using default server host: 127.0.0.1"
            )
            config["host"] = "127.0.0.1"
        else:
            logger.info(
                "Info: No server host provided and transport is `stdio`. `server_host` will be None."
            )
            config["host"] = None

    # parse server port
    if args.server_port is not None:
        if config["transport"] == "stdio":
            logger.warning(
                "Warning: Server port provided, but transport is `stdio`. The `server_port` argument will be set, but ignored."
            )
        config["port"] = args.server_port
    else:
        if os.getenv("NEO4J_MCP_SERVER_PORT") is not None:
            if config["transport"] == "stdio":
                logger.warning(
                    "Warning: Server port provided, but transport is `stdio`. The `NEO4J_MCP_SERVER_PORT` environment variable will be set, but ignored."
                )
            config["port"] = int(os.getenv("NEO4J_MCP_SERVER_PORT"))
        elif config["transport"] != "stdio":
            logger.warning(
                "Warning: No server port provided and transport is not `stdio`. Using default server port: 8000"
            )
            config["port"] = 8000
        else:
            logger.info(
                "Info: No server port provided and transport is `stdio`. `server_port` will be None."
            )
            config["port"] = None

    # parse server path
    if args.server_path is not None:
        if config["transport"] == "stdio":
            logger.warning(
                "Warning: Server path provided, but transport is `stdio`. The `server_path` argument will be set, but ignored."
            )
        config["path"] = args.server_path
    else:
        if os.getenv("NEO4J_MCP_SERVER_PATH") is not None:
            if config["transport"] == "stdio":
                logger.warning(
                    "Warning: Server path provided, but transport is `stdio`. The `NEO4J_MCP_SERVER_PATH` environment variable will be set, but ignored."
                )
            config["path"] = os.getenv("NEO4J_MCP_SERVER_PATH")
        elif config["transport"] != "stdio":
            logger.warning(
                "Warning: No server path provided and transport is not `stdio`. Using default server path: /mcp/"
            )
            config["path"] = "/mcp/"
        else:
            logger.info(
                "Info: No server path provided and transport is `stdio`. `server_path` will be None."
            )
            config["path"] = None

    # parse allow origins
    if args.allow_origins is not None:
        # Handle comma-separated string from CLI

        config["allow_origins"] = [
            origin.strip() for origin in args.allow_origins.split(",") if origin.strip()
        ]

    else:
        if os.getenv("NEO4J_MCP_SERVER_ALLOW_ORIGINS") is not None:
            # split comma-separated string into list
            config["allow_origins"] = [
                origin.strip()
                for origin in os.getenv("NEO4J_MCP_SERVER_ALLOW_ORIGINS", "").split(",")
                if origin.strip()
            ]
        else:
            logger.info(
                "Info: No allow origins provided. Defaulting to no allowed origins."
            )
            config["allow_origins"] = list()

    # parse allowed hosts for DNS rebinding protection
    if args.allowed_hosts is not None:
        # Handle comma-separated string from CLI
        config["allowed_hosts"] = [
            host.strip() for host in args.allowed_hosts.split(",") if host.strip()
        ]

    else:
        if os.getenv("NEO4J_MCP_SERVER_ALLOWED_HOSTS") is not None:
            # split comma-separated string into list
            config["allowed_hosts"] = [
                host.strip()
                for host in os.getenv("NEO4J_MCP_SERVER_ALLOWED_HOSTS", "").split(",")
                if host.strip()
            ]
        else:
            logger.info(
                "Info: No allowed hosts provided. Defaulting to secure mode - only localhost and 127.0.0.1 allowed."
            )
            config["allowed_hosts"] = ["localhost", "127.0.0.1"]

    # parse token limit
    if args.token_limit is not None:
        config["token_limit"] = args.token_limit
    else:
        if os.getenv("NEO4J_RESPONSE_TOKEN_LIMIT") is not None:
            config["token_limit"] = int(os.getenv("NEO4J_RESPONSE_TOKEN_LIMIT"))
            logger.info(
                f"Info: Cypher read query token limit provided. Using provided value: {config['token_limit']} tokens"
            )
        else:
            logger.info("Info: No token limit provided. No token limit will be used.")
            config["token_limit"] = None

    # parse read timeout
    if args.read_timeout is not None:
        config["read_timeout"] = args.read_timeout
    else:
        if os.getenv("NEO4J_READ_TIMEOUT") is not None:
            try:
                config["read_timeout"] = int(os.getenv("NEO4J_READ_TIMEOUT"))
                logger.info(
                    f"Info: Cypher read query timeout provided. Using provided value: {config['read_timeout']} seconds"
                )
                config["read_timeout"] = config["read_timeout"]
            except ValueError:
                logger.warning(
                    "Warning: Invalid read timeout provided. Using default: 30 seconds"
                )
                config["read_timeout"] = 30
        else:
            logger.info("Info: No read timeout provided. Using default: 30 seconds")
            config["read_timeout"] = 30

    # parse read-only
    if args.read_only:
        config["read_only"] = True
        logger.info(
            f"Info: Read-only mode set to {config['read_only']} via command line argument."
        )
    elif os.getenv("NEO4J_READ_ONLY") is not None:
        config["read_only"] = parse_boolean_safely(os.getenv("NEO4J_READ_ONLY"))
        logger.info(
            f"Info: Read-only mode set to {config['read_only']} via environment variable."
        )
    else:
        logger.info(
            "Info: No read-only setting provided. Write queries will be allowed."
        )
        config["read_only"] = False

    # parse schema sample size
    if args.schema_sample_size is not None:
        config["schema_sample_size"] = args.schema_sample_size
        logger.info(
            f"Info: Default sample size set to {config['schema_sample_size']} via command line argument."
        )
    else:
        if os.getenv("NEO4J_SCHEMA_SAMPLE_SIZE") is not None:
            try:
                config["schema_sample_size"] = int(os.getenv("NEO4J_SCHEMA_SAMPLE_SIZE"))
                logger.info(
                    f"Info: Default sample size set to {config['schema_sample_size']} via environment variable."
                )
            except ValueError:
                logger.warning(
                    "Warning: Invalid sample size provided in NEO4J_SCHEMA_SAMPLE_SIZE environment variable. No default sample will be used."
                )
                config["schema_sample_size"] = 1000
        else:
            logger.info(
                "Info: No default sample size provided. Schema operations will scan entire graph unless explicitly specified."
            )
            config["schema_sample_size"] = 1000

    return config


def _value_sanitize(d: Any, list_limit: int = 128) -> Any:
    """
    Sanitize the input dictionary or list.

    Sanitizes the input by removing embedding-like values,
    lists with more than 128 elements, that are mostly irrelevant for
    generating answers in a LLM context. These properties, if left in
    results, can occupy significant context space and detract from
    the LLM's performance by introducing unnecessary noise and cost.

    Sourced from: https://github.com/neo4j/neo4j-graphrag-python/blob/main/src/neo4j_graphrag/schema.py#L88

    Parameters
    ----------
    d : Any
        The input dictionary or list to sanitize.
    list_limit : int
        The limit for the number of elements in a list.

    Returns
    -------
    Any
        The sanitized dictionary or list.
    """
    if isinstance(d, dict):
        new_dict = {}
        for key, value in d.items():
            if isinstance(value, dict):
                sanitized_value = _value_sanitize(value)
                if (
                    sanitized_value is not None
                ):  # Check if the sanitized value is not None
                    new_dict[key] = sanitized_value
            elif isinstance(value, list):
                if len(value) < list_limit:
                    sanitized_value = _value_sanitize(value)
                    if (
                        sanitized_value is not None
                    ):  # Check if the sanitized value is not None
                        new_dict[key] = sanitized_value
                # Do not include the key if the list is oversized
            else:
                new_dict[key] = value
        return new_dict
    elif isinstance(d, list):
        if len(d) < list_limit:
            return [
                _value_sanitize(item) for item in d if _value_sanitize(item) is not None
            ]
        else:
            return None
    else:
        return d


def _truncate_string_to_tokens(
    text: str, token_limit: int, model: str = "gpt-4"
) -> str:
    """
    Truncates the input string to fit within the specified token limit.

    Parameters
    ----------
    text : str
        The input text string.
    token_limit : int
        Maximum number of tokens allowed.
    model : str
        Model name (affects tokenization). Defaults to "gpt-4".

    Returns
    -------
    str
        The truncated string that fits within the token limit.
    """
    # Load encoding for the chosen model
    encoding = tiktoken.encoding_for_model(model)

    # Encode text into tokens
    tokens = encoding.encode(text)

    # Truncate tokens if they exceed the limit
    if len(tokens) > token_limit:
        tokens = tokens[:token_limit]

    # Decode back into text
    truncated_text = encoding.decode(tokens)
    return truncated_text
