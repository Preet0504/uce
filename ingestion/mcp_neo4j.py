import json
import os
import shlex
import subprocess
import time
import urllib.request
from typing import Any


class McpNeo4jError(RuntimeError):
    pass


def _env_bool(name: str) -> bool:
    value = os.getenv(name, "")
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _safe_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _safe_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _build_http_auth_header() -> str | None:
    header = os.getenv("NEO4J_HTTP_AUTH")
    if header:
        return header.strip()
    bearer = os.getenv("NEO4J_HTTP_BEARER_TOKEN")
    if bearer:
        return f"Bearer {bearer.strip()}"
    basic = os.getenv("NEO4J_HTTP_BASIC_TOKEN")
    if basic:
        return f"Basic {basic.strip()}"
    return None


def _latest_protocol_version() -> str:
    try:  # pragma: no cover - optional dependency
        from mcp import types

        value = getattr(types, "LATEST_PROTOCOL_VERSION", None)
        if value:
            return str(value)
    except Exception:
        pass
    return "2025-11-25"


def _parse_sse_payload(body: str) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                data = "\n".join(data_lines).strip()
                data_lines = []
                if not data or data == "[DONE]":
                    continue
                try:
                    messages.append(json.loads(data))
                except json.JSONDecodeError:
                    continue
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        data = "\n".join(data_lines).strip()
        if data and data != "[DONE]":
            try:
                messages.append(json.loads(data))
            except json.JSONDecodeError:
                pass
    if messages:
        return messages[-1]
    raise McpNeo4jError("Invalid MCP HTTP response.")


class McpNeo4jClient:
    def __init__(self) -> None:
        self._transport = (
            os.getenv("NEO4J_TRANSPORT_MODE")
            or os.getenv("NEO4J_TRANSPORT")
            or "stdio"
        ).strip().lower()
        self._uri = os.getenv("NEO4J_URI") or ""

        self._database = os.getenv("NEO4J_DATABASE") or None
        self._read_only = _env_bool("NEO4J_READ_ONLY")
        self._schema_sample_size = _safe_int(os.getenv("NEO4J_SCHEMA_SAMPLE_SIZE"))
        self._log_level = os.getenv("NEO4J_LOG_LEVEL") or None
        self._log_format = os.getenv("NEO4J_LOG_FORMAT") or None
        self._telemetry = os.getenv("NEO4J_TELEMETRY") or None
        self._http_timeout = _safe_int(os.getenv("NEO4J_MCP_HTTP_TIMEOUT")) or 30
        self._http_retries = _safe_int(os.getenv("NEO4J_MCP_HTTP_RETRIES")) or 1
        self._http_retry_delay = _safe_float(os.getenv("NEO4J_MCP_HTTP_RETRY_DELAY")) or 0.5

        self._initialized = False
        self._next_id = 1
        self._process: subprocess.Popen[bytes] | None = None

        if self._transport == "stdio":
            if not self._uri:
                raise McpNeo4jError(
                    "NEO4J_URI is required to connect to the Neo4j MCP server."
                )
            self._username = os.getenv("NEO4J_USERNAME")
            self._password = os.getenv("NEO4J_PASSWORD")
            if not self._username or not self._password:
                raise McpNeo4jError(
                    "NEO4J_USERNAME and NEO4J_PASSWORD are required in stdio transport mode."
                )
            self._http_url = None
            self._auth_header = None
        elif self._transport == "http":
            self._username = None
            self._password = None
            self._http_url = os.getenv("NEO4J_MCP_HTTP_URL") or self._uri
            if not self._http_url:
                raise McpNeo4jError(
                    "NEO4J_MCP_HTTP_URL (or NEO4J_URI) is required for MCP HTTP transport."
                )
            if not self._http_url.lower().startswith("http"):
                raise McpNeo4jError(
                    "NEO4J_MCP_HTTP_URL must be an http(s) URL when NEO4J_TRANSPORT_MODE=http."
                )
            self._auth_header = _build_http_auth_header()
        else:
            raise McpNeo4jError(
                f"Unsupported NEO4J_TRANSPORT_MODE: {self._transport}. Use 'stdio' or 'http'."
            )

    @property
    def read_only(self) -> bool:
        return self._read_only

    def close(self) -> None:
        if self._process is not None:
            try:
                self._process.terminate()
            finally:
                self._process = None

    def get_schema(self) -> Any:
        args: dict[str, Any] = {}
        if self._schema_sample_size is not None:
            args["sample_size"] = self._schema_sample_size
        return self._call_tool("get-schema", args)

    def read_cypher(self, query: str, params: dict[str, Any] | None = None) -> Any:
        if not query:
            raise ValueError("read_cypher requires a query")
        payload: dict[str, Any] = {"query": query, "params": params or {}}
        result = self._call_tool("read-cypher", payload)
        return self._normalize_rows(result)

    def write_cypher(self, query: str, params: dict[str, Any] | None = None) -> Any:
        if self._read_only:
            raise McpNeo4jError("NEO4J_READ_ONLY is true; write-cypher calls are disabled.")
        if not query:
            raise ValueError("write_cypher requires a query")
        payload: dict[str, Any] = {"query": query, "params": params or {}}
        return self._call_tool("write-cypher", payload)

    def list_gds_procedures(self) -> list[Any]:
        try:
            result = self._call_tool("list-gds-procedures", {})
        except McpNeo4jError as exc:
            message = str(exc).lower()
            if "not found" in message or "unknown" in message:
                return []
            raise
        rows = self._normalize_rows(result)
        return rows if isinstance(rows, list) else []

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            self._ensure_initialized()
            request = {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
            self._next_id += 1
            result = self._send_request(request)
            return self._extract_tool_result(result)
        except McpNeo4jError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise McpNeo4jError(
                f"Failed to call MCP tool '{name}'. The MCP server may be unreachable."
            ) from exc

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        init_request = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "initialize",
            "params": {
                "protocolVersion": _latest_protocol_version(),
                "clientInfo": {"name": "uce-ingest", "version": "0.1"},
                "capabilities": {},
            },
        }
        self._next_id += 1
        self._send_request(init_request)
        self._send_notification({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._initialized = True

    def _send_request(self, message: dict[str, Any]) -> Any:
        if self._transport == "stdio":
            return self._send_request_stdio(message)
        return self._send_request_http(message)

    def _send_notification(self, message: dict[str, Any]) -> None:
        if self._transport == "stdio":
            self._send_notification_stdio(message)
        else:
            self._send_request_http(message)

    def _send_request_stdio(self, message: dict[str, Any]) -> Any:
        process = self._ensure_process()
        self._write_message(process, message)
        response = self._read_message(process)
        return self._handle_response(response)

    def _send_notification_stdio(self, message: dict[str, Any]) -> None:
        process = self._ensure_process()
        self._write_message(process, message)

    def _send_request_http(self, message: dict[str, Any]) -> Any:
        if not self._http_url:
            raise McpNeo4jError("HTTP transport requires a valid NEO4J_URI URL.")
        data = json.dumps(message).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._auth_header:
            headers["Authorization"] = self._auth_header

        last_exc: Exception | None = None
        retries = max(1, self._http_retries)
        for attempt in range(retries):
            request = urllib.request.Request(self._http_url, data=data, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self._http_timeout) as response:
                    body = response.read().decode("utf-8")
                    content_type = response.headers.get("Content-Type", "")
            except Exception as exc:  # pragma: no cover - network dependent
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(self._http_retry_delay)
                    continue
                raise McpNeo4jError(
                    "Neo4j MCP server is unreachable via HTTP. Check NEO4J_URI and auth settings."
                ) from exc

            try:
                if not body.strip():
                    payload = {}
                elif "text/event-stream" in (content_type or "").lower():
                    payload = _parse_sse_payload(body)
                else:
                    payload = json.loads(body)
                return self._handle_response(payload)
            except Exception as exc:  # pragma: no cover - network dependent
                last_exc = exc
                if attempt < retries - 1:
                    time.sleep(self._http_retry_delay)
                    continue
                raise McpNeo4jError("Invalid MCP HTTP response.") from exc

        if last_exc:
            raise McpNeo4jError("Neo4j MCP server is unreachable via HTTP.") from last_exc
        raise McpNeo4jError("Neo4j MCP server is unreachable via HTTP.")

    def _ensure_process(self) -> subprocess.Popen[bytes]:
        if self._process is not None and self._process.poll() is None:
            return self._process

        command = os.getenv("NEO4J_MCP_COMMAND", "neo4j-mcp-server")
        args = shlex.split(command, posix=os.name != "nt")
        env = os.environ.copy()
        env["NEO4J_URI"] = self._uri
        env["NEO4J_USERNAME"] = self._username or ""
        env["NEO4J_PASSWORD"] = self._password or ""
        if self._database:
            env["NEO4J_DATABASE"] = self._database
        if self._schema_sample_size is not None:
            env["NEO4J_SCHEMA_SAMPLE_SIZE"] = str(self._schema_sample_size)
        if self._log_level:
            env["NEO4J_LOG_LEVEL"] = self._log_level
        if self._log_format:
            env["NEO4J_LOG_FORMAT"] = self._log_format
        if self._telemetry:
            env["NEO4J_TELEMETRY"] = self._telemetry
        if self._read_only:
            env["NEO4J_READ_ONLY"] = "true"

        try:
            self._process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            raise McpNeo4jError(
                "Neo4j MCP server command not found. Install the official Neo4j MCP server "
                "or set NEO4J_MCP_COMMAND to the server executable."
            ) from exc
        except Exception as exc:
            raise McpNeo4jError("Failed to start the Neo4j MCP server process.") from exc

        return self._process

    def _read_process_stderr(self, process: subprocess.Popen[bytes], max_bytes: int = 4096) -> str:
        if process.stderr is None:
            return ""
        try:
            data = process.stderr.read(max_bytes)
        except Exception:
            return ""
        if not data:
            return ""
        return data.decode("utf-8", errors="replace").strip()

    def _write_message(self, process: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
        if process.stdin is None:
            raise McpNeo4jError("Neo4j MCP server stdin is unavailable.")
        payload = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(payload)}\r\n\r\n".encode("utf-8")
        try:
            process.stdin.write(header)
            process.stdin.write(payload)
            process.stdin.flush()
        except Exception as exc:  # pragma: no cover - process dependent
            raise McpNeo4jError("Failed to write to the Neo4j MCP server.") from exc

    def _read_message(self, process: subprocess.Popen[bytes]) -> dict[str, Any]:
        if process.stdout is None:
            raise McpNeo4jError("Neo4j MCP server stdout is unavailable.")

        headers: dict[str, str] = {}
        while True:
            line = process.stdout.readline()
            if not line:
                exit_code = process.poll()
                stderr_text = self._read_process_stderr(process)
                details: list[str] = ["Neo4j MCP server closed the connection unexpectedly."]
                if exit_code is not None:
                    details.append(f"exit_code={exit_code}")
                if stderr_text:
                    details.append(f"stderr={stderr_text}")
                raise McpNeo4jError(" ".join(details))
            text = line.decode("utf-8").strip()
            if not text:
                break
            if ":" in text:
                key, value = text.split(":", 1)
                headers[key.strip().lower()] = value.strip()

        length_text = headers.get("content-length")
        if not length_text:
            raise McpNeo4jError("Neo4j MCP server response missing Content-Length header.")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise McpNeo4jError("Invalid Content-Length header from Neo4j MCP server.") from exc

        body = process.stdout.read(length)
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise McpNeo4jError("Invalid JSON received from Neo4j MCP server.") from exc

    def _handle_response(self, response: dict[str, Any]) -> Any:
        if "error" in response:
            message = response["error"].get("message") if isinstance(response["error"], dict) else None
            raise McpNeo4jError(message or "Neo4j MCP server returned an error.")
        return response.get("result")

    def _extract_tool_result(self, result: Any) -> Any:
        if isinstance(result, dict) and "content" in result:
            content = result.get("content")
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict):
                    if "json" in first:
                        return first["json"]
                    if "text" in first:
                        text = first.get("text", "")
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError:
                            return text
            return result
        return result

    def _normalize_rows(self, result: Any) -> Any:
        if isinstance(result, dict):
            for key in ("rows", "records", "data", "result"):
                if key in result:
                    return result[key]
        return result
