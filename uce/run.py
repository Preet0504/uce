import argparse
import logging
import os

from neo4j.exceptions import AuthError, ServiceUnavailable

from uce.core.config import load_config
from uce.core.graph_db import GraphDB
from uce.ingestion import code_parser
from uce.runtime.updater import GraphUpdater
from uce.runtime.watcher import start_watcher
from uce.server.mcp_server import run_server


def run_uce(
    config_path: str,
    neo4j_uri: str | None = None,
    neo4j_user: str | None = None,
    neo4j_password: str | None = None,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )

    config_path = os.path.abspath(config_path)
    config = load_config(config_path, project_root_override=None)
    if neo4j_uri or neo4j_user or neo4j_password:
        config = config.__class__(
            project_root=config.project_root,
            languages=config.languages,
            paths=config.paths,
            ignore=config.ignore,
            aliases=config.aliases,
            neo4j=config.neo4j.__class__(
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
        updater.full_refresh()
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
    args = parser.parse_args()

    run_uce(
        config_path=args.config,
        neo4j_uri=args.neo4j_uri,
        neo4j_user=args.neo4j_user,
        neo4j_password=args.neo4j_password,
    )


if __name__ == "__main__":
    main()
