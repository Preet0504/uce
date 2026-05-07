import argparse
from dataclasses import replace
import logging
import os
import sys

from neo4j.exceptions import AuthError, ServiceUnavailable

from core.config import load_config
from core.graph_db import GraphDB
from ingestion import code_parser
from runtime.updater import GraphUpdater
from runtime.watcher import start_watcher
from server.mcp_server import run_server


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


def _load_default_dotenv(config_path: str | None = None) -> None:
    path = os.getenv("UCE_DOTENV_PATH")
    if not path and config_path:
        config_dir = os.path.dirname(os.path.abspath(config_path))
        candidate = os.path.join(config_dir, ".env")
        if os.path.exists(candidate):
            path = candidate
    if not path:
        path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".env"))
    _load_dotenv(path, override=False)


def run_uce(
    config_path: str,
    neo4j_uri: str | None = None,
    neo4j_user: str | None = None,
    neo4j_password: str | None = None,
    skip_refresh: bool = False,
    skip_llm_ingestion: bool = False,
    skip_watcher: bool = False,
) -> None:
    config_path = os.path.abspath(config_path)
    _load_default_dotenv(config_path)
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )

    config = load_config(config_path, project_root_override=None)
    if neo4j_uri or neo4j_user or neo4j_password:
        config = replace(
            config,
            neo4j=replace(
                config.neo4j,
                uri=neo4j_uri or config.neo4j.uri,
                user=neo4j_user or config.neo4j.user,
                password=neo4j_password or config.neo4j.password,
            ),
        )

    try:
        code_parser.validate_languages(config.languages)
    except RuntimeError as exc:
        logging.error(str(exc))
        raise SystemExit(2) from exc

    graph = GraphDB(config.neo4j.uri, config.neo4j.user, config.neo4j.password)
    try:
        graph.run("RETURN 1 AS ok")
    except AuthError as exc:
        logging.error(
            "Neo4j authentication failed. Check config at %s or set NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD.",
            config_path,
        )
        raise SystemExit(2) from exc
    except ServiceUnavailable as exc:
        logging.error(
            "Neo4j service unavailable at %s. Verify the server is running.",
            config.neo4j.uri,
        )
        raise SystemExit(2) from exc

    updater = GraphUpdater(config, graph)
    observer = None
    try:
        if skip_refresh:
            logging.info("Skipping full ingestion refresh.")
        else:
            updater.full_refresh()

        if skip_llm_ingestion:
            logging.info("Skipping LLM ingestion.")
        else:
            updater.run_llm_ingestion()

        if skip_watcher:
            logging.info("Skipping filesystem watcher.")
        else:
            observer = start_watcher(config, updater)
        run_server(config)
    except KeyboardInterrupt:
        logging.info("Shutting down UCE")
    finally:
        if observer:
            observer.stop()
            observer.join(timeout=5)
        graph.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Unified Context Engine")
    parser.add_argument(
        "--config",
        help="Path to config.yaml",
        default="config.yaml",
    )
    parser.add_argument(
        "--neo4j-uri",
        help="Override Neo4j URI",
        default=None,
    )
    parser.add_argument(
        "--neo4j-user",
        help="Override Neo4j user",
        default=None,
    )
    parser.add_argument(
        "--neo4j-password",
        help="Override Neo4j password",
        default=None,
    )
    parser.add_argument(
        "--skip-refresh",
        action="store_true",
        help="Skip deterministic full refresh ingestion on startup.",
    )
    parser.add_argument(
        "--skip-llm-ingestion",
        action="store_true",
        help="Skip LLM-based requirements/policies/RBAC ingestion on startup.",
    )
    parser.add_argument(
        "--skip-ingestion",
        action="store_true",
        help="Skip both full refresh and LLM ingestion on startup.",
    )
    parser.add_argument(
        "--no-watcher",
        action="store_true",
        help="Disable filesystem watcher.",
    )
    args = parser.parse_args()

    skip_refresh = bool(args.skip_refresh or args.skip_ingestion)
    skip_llm_ingestion = bool(args.skip_llm_ingestion or args.skip_ingestion)

    run_uce(
        config_path=args.config,
        neo4j_uri=args.neo4j_uri,
        neo4j_user=args.neo4j_user,
        neo4j_password=args.neo4j_password,
        skip_refresh=skip_refresh,
        skip_llm_ingestion=skip_llm_ingestion,
        skip_watcher=bool(args.no_watcher),
    )


if __name__ == "__main__":
    main()
