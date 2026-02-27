import os
import re
from graph import GraphDB, create_function, link_function_to_api, create_service
from config import PROJECT_ROOT
from ingest.file_graph import is_ignored, normalize

EXTENSIONS = {".ts", ".tsx", ".js", ".jsx"}

METHOD_REGEXES = [
    re.compile(r"\bexport\s+async\s+function\s+(GET|POST|PUT|DELETE|PATCH)\s*\(", re.M),
    re.compile(r"\bexport\s+function\s+(GET|POST|PUT|DELETE|PATCH)\s*\(", re.M),
    re.compile(r"\bexport\s+const\s+(GET|POST|PUT|DELETE|PATCH)\s*=", re.M),
]


def _iter_api_files():
    for root, _, files in os.walk(PROJECT_ROOT):
        for file in files:
            _, ext = os.path.splitext(file)
            if ext not in EXTENSIONS:
                continue
            full_path = os.path.join(root, file)
            relative_path = normalize(os.path.relpath(full_path, PROJECT_ROOT))
            if is_ignored(relative_path):
                continue
            if "/app/api/" in f"/{relative_path}" or "/routers/" in f"/{relative_path}":
                yield relative_path, full_path


def _extract_methods(content: str):
    methods = set()
    for regex in METHOD_REGEXES:
        for match in regex.finditer(content):
            methods.add(match.group(1).upper())
    return sorted(methods)


def _derive_route(relative_path: str):
    normalized = relative_path.replace("\\", "/")
    marker = "app/api/"
    if marker not in normalized:
        return None
    tail = normalized.split(marker, 1)[1]
    parts = tail.split("/")
    if not parts:
        return "/api"

    last = parts[-1]
    if last.startswith("route."):
        parts = parts[:-1]
    else:
        parts[-1] = os.path.splitext(last)[0]

    route_path = "/".join([p for p in parts if p])
    if route_path:
        return f"/api/{route_path}"
    return "/api"


def _derive_service_name(relative_path: str, route: str | None):
    parts = relative_path.replace("\\", "/").split("/")
    if "modules" in parts:
        idx = parts.index("modules")
        if idx + 1 < len(parts) and "server" in parts[idx + 2 :]:
            return parts[idx + 1]

    if route and route.startswith("/api/"):
        service = route.split("/", 3)
        if len(service) >= 3 and service[2]:
            return service[2]

    return None


def ingest_apis():
    graph = GraphDB()

    for relative_path, full_path in _iter_api_files():
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        methods = _extract_methods(content)
        if not methods:
            continue

        route = _derive_route(relative_path)
        if not route:
            continue

        service_name = _derive_service_name(relative_path, route)
        if service_name:
            create_service(graph, service_name)

        for method in methods:
            create_function(graph, method, relative_path)
            link_function_to_api(
                graph,
                method,
                relative_path,
                route,
                method,
                service_name=service_name,
            )

    graph.close()


if __name__ == "__main__":
    ingest_apis()
