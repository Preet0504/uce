import os
import re
from graph import GraphDB, create_function, link_function_call
from config import PROJECT_ROOT
from ingest.file_graph import is_ignored, normalize


EXTENSIONS = {".ts", ".tsx", ".js", ".jsx"}

FUNC_DECL_REGEXES = [
    re.compile(r"\bfunction\s+([A-Za-z_]\w*)\s*\(", re.M),
    re.compile(r"\bconst\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>", re.M),
    re.compile(r"\bconst\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?[A-Za-z_]\w*\s*=>", re.M),
]

CALL_REGEX = re.compile(r"(?<!\.)\b([A-Za-z_]\w*)\s*\(", re.M)

IGNORED_CALLS = {
    "if",
    "for",
    "while",
    "switch",
    "return",
    "catch",
    "function",
    "const",
    "let",
    "var",
    "new",
    "class",
    "export",
    "import",
    "await",
    "async",
    "typeof",
    "instanceof",
    "in",
    "of",
    "try",
    "throw",
    "case",
    "break",
    "continue",
    "do",
    "else",
    "finally",
    "yield",
    "delete",
    "super",
    "this",
    "require",
}


def _iter_source_files():
    for root, _, files in os.walk(PROJECT_ROOT):
        for file in files:
            _, ext = os.path.splitext(file)
            if ext not in EXTENSIONS:
                continue
            full_path = os.path.join(root, file)
            relative_path = normalize(os.path.relpath(full_path, PROJECT_ROOT))
            if is_ignored(relative_path):
                continue
            yield relative_path, full_path


def _extract_function_names(content: str):
    names = set()
    for regex in FUNC_DECL_REGEXES:
        for match in regex.finditer(content):
            names.add(match.group(1))
    return sorted(names)


def _extract_calls(content: str):
    calls = set()
    for match in CALL_REGEX.finditer(content):
        name = match.group(1)
        if name in IGNORED_CALLS:
            continue
        calls.add(name)
    return sorted(calls)


def ingest_functions():
    graph = GraphDB()

    functions_by_name = {}
    functions_by_file = {}

    # Pass 1: create Function nodes and DECLARES_FUNCTION edges
    for relative_path, full_path in _iter_source_files():
        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        function_names = _extract_function_names(content)
        if not function_names:
            continue

        functions_by_file[relative_path] = function_names
        for name in function_names:
            functions_by_name.setdefault(name, set()).add(relative_path)
            create_function(graph, name, relative_path)

    # Pass 2: link CALLS edges based on simple call detection
    for relative_path, full_path in _iter_source_files():
        if relative_path not in functions_by_file:
            continue

        with open(full_path, "r", encoding="utf-8") as f:
            content = f.read()

        call_names = _extract_calls(content)
        callers = functions_by_file.get(relative_path, [])

        for callee_name in call_names:
            callee_files = functions_by_name.get(callee_name)
            if not callee_files:
                continue
            for caller_name in callers:
                for callee_file in callee_files:
                    link_function_call(
                        graph,
                        caller_name,
                        relative_path,
                        callee_name,
                        callee_file,
                    )

    graph.close()


if __name__ == "__main__":
    ingest_functions()
