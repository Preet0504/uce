import argparse
import asyncio
import json
import logging
import re
from typing import Any, Literal, Optional

from fastmcp.exceptions import ToolError
from fastmcp.server import FastMCP
from fastmcp.tools.tool import TextContent, ToolResult
from mcp.types import ToolAnnotations
from neo4j import AsyncDriver, AsyncGraphDatabase, Query, RoutingControl
from neo4j.exceptions import ClientError, Neo4jError
from pydantic import Field
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

try:
    from .utils import _truncate_string_to_tokens, _value_sanitize, process_config
except ImportError:
    from utils import _truncate_string_to_tokens, _value_sanitize, process_config

logger = logging.getLogger("mcp_neo4j_cypher")


def _format_namespace(namespace: str) -> str:
    if namespace:
        if namespace.endswith("-"):
            return namespace
        else:
            return namespace + "-"
    else:
        return ""


def _is_write_query(query: str) -> bool:
    """Check if the query is a write query."""
    return (
        re.search(r"\b(MERGE|CREATE|INSERT|SET|DELETE|REMOVE|ADD)\b", query, re.IGNORECASE)
        is not None
    )


def create_mcp_server(
    neo4j_driver: AsyncDriver,
    database: str = "neo4j",
    namespace: str = "",
    read_timeout: int = 30,
    token_limit: Optional[int] = None,
    read_only: bool = False,
    config_sample_size: int = 1000,
) -> FastMCP:
    mcp: FastMCP = FastMCP(
        "mcp-neo4j-cypher"
    )

    namespace_prefix = _format_namespace(namespace)
    allow_writes = not read_only

    @mcp.tool(
        name=namespace_prefix + "get_neo4j_schema",
        annotations=ToolAnnotations(
            title="Get Neo4j Schema",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def get_neo4j_schema(sample_size: int = Field(default=config_sample_size, description="The sample size used to infer the graph schema. Larger samples are slower, but more accurate. Smaller samples are faster, but might miss information.")) -> list[ToolResult]:
        """
        Returns nodes, their properties (with types and indexed flags), and relationships
        using APOC's schema inspection.

        You should only provide a `sample_size` value if requested by the user, or tuning the retrieval performance.

        Performance Notes:
            - If `sample_size` is not provided, uses the server's default sample setting defined in the server configuration.
            - If retrieving the schema times out, try lowering the sample size, e.g. `sample_size=100`.
            - To sample the entire graph use `sample_size=-1`.
        """

        # Use provided sample_size, otherwise fall back to server default - 1000
        effective_sample_size = sample_size if sample_size else config_sample_size

        logger.info(f"Running `get_neo4j_schema` with sample size {effective_sample_size}.")

        get_schema_query = f"CALL apoc.meta.schema({{sample: {effective_sample_size}}}) YIELD value RETURN value"

        def clean_schema(schema: dict) -> dict:
            cleaned = {}

            for key, entry in schema.items():
                new_entry = {"type": entry["type"]}
                if "count" in entry:
                    new_entry["count"] = entry["count"]

                labels = entry.get("labels", [])
                if labels:
                    new_entry["labels"] = labels

                props = entry.get("properties", {})
                clean_props = {}
                for pname, pinfo in props.items():
                    cp = {}
                    if "indexed" in pinfo:
                        cp["indexed"] = pinfo["indexed"]
                    if "type" in pinfo:
                        cp["type"] = pinfo["type"]
                    if cp:
                        clean_props[pname] = cp
                if clean_props:
                    new_entry["properties"] = clean_props

                if entry.get("relationships"):
                    rels_out = {}
                    for rel_name, rel in entry["relationships"].items():
                        cr = {}
                        if "direction" in rel:
                            cr["direction"] = rel["direction"]
                        # nested labels
                        rlabels = rel.get("labels", [])
                        if rlabels:
                            cr["labels"] = rlabels
                        # nested properties
                        rprops = rel.get("properties", {})
                        clean_rprops = {}
                        for rpname, rpinfo in rprops.items():
                            crp = {}
                            if "indexed" in rpinfo:
                                crp["indexed"] = rpinfo["indexed"]
                            if "type" in rpinfo:
                                crp["type"] = rpinfo["type"]
                            if crp:
                                clean_rprops[rpname] = crp
                        if clean_rprops:
                            cr["properties"] = clean_rprops

                        if cr:
                            rels_out[rel_name] = cr

                    if rels_out:
                        new_entry["relationships"] = rels_out

                cleaned[key] = new_entry

            return cleaned

        try:
            query_obj = Query(get_schema_query, timeout=float(read_timeout))
            results_json = await neo4j_driver.execute_query(
                query_obj,
                routing_control=RoutingControl.READ,
                database_=database,
                result_transformer_=lambda r: r.data(),
            )

            logger.debug(f"Read query returned {len(results_json)} rows")

            schema_clean = clean_schema(results_json[0].get("value"))

            schema_clean_str = json.dumps(schema_clean, default=str)

            return ToolResult(content=[TextContent(type="text", text=schema_clean_str)])

        except ClientError as e:
            if "Neo.ClientError.Procedure.ProcedureNotFound" in str(e):
                raise ToolError(
                    "Neo4j Client Error: This instance of Neo4j does not have the APOC plugin installed. Please install and enable the APOC plugin to use the `get_neo4j_schema` tool."
                )
            else:
                raise ToolError(f"Neo4j Client Error: {e}")

        except Neo4jError as e:
            raise ToolError(f"Neo4j Error: {e}")

        except Exception as e:
            logger.error(f"Error retrieving Neo4j database schema: {e}")
            raise ToolError(f"Unexpected Error: {e}")

    @mcp.tool(
        name=namespace_prefix + "read_neo4j_cypher",
        annotations=ToolAnnotations(
            title="Read Neo4j Cypher",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def read_neo4j_cypher(
        query: str = Field(..., description="The Cypher query to execute."),
        params: dict[str, Any] = Field(
            dict(), description="The parameters to pass to the Cypher query."
        ),
    ) -> list[ToolResult]:
        """Execute a read Cypher query on the neo4j database."""

        if _is_write_query(query):
            raise ValueError("Only MATCH queries are allowed for read-query")

        try:
            query_obj = Query(query, timeout=float(read_timeout))
            results = await neo4j_driver.execute_query(
                query_obj,
                parameters_=params,
                routing_control=RoutingControl.READ,
                database_=database,
                result_transformer_=lambda r: r.data(),
            )
            sanitized_results = [_value_sanitize(el) for el in results]
            results_json_str = json.dumps(sanitized_results, default=str)
            if token_limit:
                results_json_str = _truncate_string_to_tokens(
                    results_json_str, token_limit
                )

            logger.debug(f"Read query returned {len(results_json_str)} rows")

            return ToolResult(content=[TextContent(type="text", text=results_json_str)])

        except Neo4jError as e:
            logger.error(f"Neo4j Error executing read query: {e}\n{query}\n{params}")
            raise ToolError(f"Neo4j Error: {e}\n{query}\n{params}")

        except Exception as e:
            logger.error(f"Error executing read query: {e}\n{query}\n{params}")
            raise ToolError(f"Error: {e}\n{query}\n{params}")

    @mcp.tool(
        name=namespace_prefix + "write_neo4j_cypher",
        annotations=ToolAnnotations(
            title="Write Neo4j Cypher",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def write_neo4j_cypher(
        query: str = Field(..., description="The Cypher query to execute."),
        params: dict[str, Any] = Field(
            dict(), description="The parameters to pass to the Cypher query."
        ),
    ) -> list[ToolResult]:
        """Execute a write Cypher query on the neo4j database."""

        if not allow_writes:
            raise ToolError("Write queries are disabled (read-only mode).")

        if not _is_write_query(query):
            raise ValueError("Only write queries are allowed for write-query")

        try:
            _, summary, _ = await neo4j_driver.execute_query(
                query,
                parameters_=params,
                routing_control=RoutingControl.WRITE,
                database_=database,
            )

            counters_json_str = json.dumps(summary.counters.__dict__, default=str)

            logger.debug(f"Write query affected {counters_json_str}")

            return ToolResult(
                content=[TextContent(type="text", text=counters_json_str)]
            )

        except Neo4jError as e:
            logger.error(f"Neo4j Error executing write query: {e}\n{query}\n{params}")
            raise ToolError(f"Neo4j Error: {e}\n{query}\n{params}")

        except Exception as e:
            logger.error(f"Error executing write query: {e}\n{query}\n{params}")
            raise ToolError(f"Error: {e}\n{query}\n{params}")

    # Compatibility aliases for UCE MCP client
    @mcp.tool(
        name=namespace_prefix + "get-schema",
        annotations=ToolAnnotations(
            title="Get Neo4j Schema",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def get_schema(sample_size: int = Field(default=config_sample_size, description="The sample size used to infer the graph schema. Larger samples are slower, but more accurate. Smaller samples are faster, but might miss information.")) -> list[ToolResult]:
        return await get_neo4j_schema(sample_size)

    @mcp.tool(
        name=namespace_prefix + "read-cypher",
        annotations=ToolAnnotations(
            title="Read Neo4j Cypher",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def read_cypher(
        query: str = Field(..., description="The Cypher query to execute."),
        params: dict[str, Any] = Field(
            dict(), description="The parameters to pass to the Cypher query."
        ),
    ) -> list[ToolResult]:
        return await read_neo4j_cypher(query=query, params=params)

    @mcp.tool(
        name=namespace_prefix + "write-cypher",
        annotations=ToolAnnotations(
            title="Write Neo4j Cypher",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def write_cypher(
        query: str = Field(..., description="The Cypher query to execute."),
        params: dict[str, Any] = Field(
            dict(), description="The parameters to pass to the Cypher query."
        ),
    ) -> list[ToolResult]:
        return await write_neo4j_cypher(query=query, params=params)

    return mcp


async def main(
    db_url: str,
    username: str,
    password: str,
    database: str,
    transport: Literal["stdio", "sse", "http"] = "stdio",
    namespace: str = "",
    host: str = "127.0.0.1",
    port: int = 8000,
    path: str = "/mcp/",
    allow_origins: list[str] = [],
    allowed_hosts: list[str] = [],
    read_timeout: int = 30,
    token_limit: Optional[int] = None,
    read_only: bool = False,
    schema_sample_size: Optional[int] = None, # this is known as the config_sample_size in the create_mcp_server function
) -> None:
    logger.info("Starting MCP neo4j Server")

    neo4j_driver = AsyncGraphDatabase.driver(
        db_url,
        auth=(
            username,
            password,
        ),
    )
    custom_middleware = [
        Middleware(
            CORSMiddleware,
            allow_origins=allow_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        ),
        Middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts),
    ]

    mcp = create_mcp_server(
        neo4j_driver, database, namespace, read_timeout, token_limit, read_only, schema_sample_size
    )

    # Run the server with the specified transport
    match transport:
        case "http":
            logger.info(
                f"Running Neo4j Cypher MCP Server with HTTP transport on {host}:{port}..."
            )
            await mcp.run_http_async(
                host=host,
                port=port,
                path=path,
                middleware=custom_middleware,
                stateless_http=True,
            )
        case "stdio":
            logger.info("Running Neo4j Cypher MCP Server with stdio transport...")
            await mcp.run_stdio_async()
        case "sse":
            logger.info(
                f"Running Neo4j Cypher MCP Server with SSE transport on {host}:{port}..."
            )
            await mcp.run_http_async(
                host=host,
                port=port,
                path=path,
                middleware=custom_middleware,
                transport="sse",
                stateless_http=True,
            )
        case _:
            logger.error(
                f"Invalid transport: {transport} | Must be either 'stdio', 'sse', or 'http'"
            )
            raise ValueError(
                f"Invalid transport: {transport} | Must be either 'stdio', 'sse', or 'http'"
            )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Neo4j MCP Server")
    parser.add_argument("--db-url", dest="db_url", default=None, help="Neo4j bolt URI")
    parser.add_argument("--username", dest="username", default=None, help="Neo4j username")
    parser.add_argument("--password", dest="password", default=None, help="Neo4j password")
    parser.add_argument("--database", dest="database", default=None, help="Neo4j database")
    parser.add_argument("--namespace", dest="namespace", default=None, help="Tool namespace prefix")
    parser.add_argument("--transport", dest="transport", default=None, help="stdio, http, or sse")
    parser.add_argument("--server-host", dest="server_host", default=None, help="HTTP/SSE host")
    parser.add_argument("--server-port", dest="server_port", type=int, default=None, help="HTTP/SSE port")
    parser.add_argument("--server-path", dest="server_path", default=None, help="HTTP/SSE path")
    parser.add_argument("--allow-origins", dest="allow_origins", default=None, help="Comma-separated CORS origins")
    parser.add_argument("--allowed-hosts", dest="allowed_hosts", default=None, help="Comma-separated allowed hosts")
    parser.add_argument("--token-limit", dest="token_limit", type=int, default=None, help="Response token limit")
    parser.add_argument("--read-timeout", dest="read_timeout", type=int, default=None, help="Read timeout seconds")
    parser.add_argument("--read-only", dest="read_only", action="store_true", help="Enable read-only mode")
    parser.add_argument("--schema-sample-size", dest="schema_sample_size", type=int, default=None, help="APOC schema sample size")
    return parser


def cli() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    config = process_config(args)

    host = config.get("host") or "127.0.0.1"
    port = config.get("port") or 8000
    path = config.get("path") or "/mcp/"

    asyncio.run(
        main(
            db_url=config["db_url"],
            username=config["username"],
            password=config["password"],
            database=config["database"],
            transport=config["transport"],
            namespace=config["namespace"],
            host=host,
            port=port,
            path=path,
            allow_origins=config.get("allow_origins") or [],
            allowed_hosts=config.get("allowed_hosts") or [],
            read_timeout=int(config.get("read_timeout") or 30),
            token_limit=config.get("token_limit"),
            read_only=bool(config.get("read_only")),
            schema_sample_size=config.get("schema_sample_size"),
        )
    )


if __name__ == "__main__":
    cli()
