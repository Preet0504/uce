from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable
from importlib import metadata

try:
    from tree_sitter_languages import get_parser
except ImportError as exc:  # pragma: no cover - explicit runtime guidance
    raise ImportError(
        "tree_sitter_languages is required. Install with `pip install tree_sitter_languages`."
    ) from exc


LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
}

FUNCTION_NODES = {
    "function_definition",
    "function_declaration",
    "function_item",
    "function",
    "function_expression",
    "function_signature",
    "arrow_function",
    "lambda_expression",
    "function_literal",
    "method_declaration",
    "method_definition",
}

CLASS_NODES = {
    "class_definition",
    "class_declaration",
    "class_specifier",
    "struct_specifier",
}

METHOD_NODES = {
    "method_definition",
    "method_declaration",
    "constructor_declaration",
}

CALL_NODES = {
    "call_expression",
    "call",
    "function_call",
}

IMPORT_NODES = {
    "import_statement",
    "import_from_statement",
    "import_declaration",
    "import_clause",
    "import_spec",
    "import_specifier",
    "require_call",
}

STRING_NODES = {"string", "string_literal", "interpreted_string_literal", "raw_string_literal"}
IDENTIFIER_NODES = {
    "identifier",
    "property_identifier",
    "type_identifier",
    "field_identifier",
}


@dataclass(frozen=True)
class ParsedCode:
    language: str
    imports: tuple[str, ...]
    functions: tuple[str, ...]
    classes: tuple[str, ...]
    methods: tuple[tuple[str, str | None], ...]
    calls: tuple[str, ...]


_PARSER_CACHE: dict[str, object] = {}
_PARSER_ERROR: Exception | None = None


def detect_language(path: str) -> str | None:
    _, ext = os.path.splitext(path)
    return LANGUAGE_BY_EXTENSION.get(ext.lower())


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _first_identifier(node, source: bytes) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return _node_text(name_node, source)

    for child in node.children:
        if child.type in IDENTIFIER_NODES:
            return _node_text(child, source)
    return None


def _last_identifier(node, source: bytes) -> str | None:
    identifiers = []
    for child in node.children:
        if child.type in IDENTIFIER_NODES:
            identifiers.append(_node_text(child, source))
    if identifiers:
        return identifiers[-1]
    return None


def _extract_import(node, source: bytes) -> str | None:
    for child in node.children:
        if child.type in STRING_NODES:
            value = _node_text(child, source)
            return value.strip("\"'` ")

    for child in node.children:
        if child.type in IDENTIFIER_NODES:
            return _node_text(child, source)

    text = _node_text(node, source).strip()
    return text or None


def _extract_call_name(node, source: bytes) -> str | None:
    target = node.child_by_field_name("function") or node.child_by_field_name("callee")
    if target is None:
        target = node

    name = _last_identifier(target, source)
    if name:
        return name

    for child in target.children:
        if child.type in IDENTIFIER_NODES:
            return _node_text(child, source)

    return None


def _walk(node) -> Iterable:
    yield node
    for child in node.children:
        yield from _walk(child)


def parse_source(source_bytes: bytes, language: str) -> ParsedCode:
    parser = _get_parser(language)
    tree = parser.parse(source_bytes)
    root = tree.root_node

    imports: set[str] = set()
    functions: set[str] = set()
    classes: set[str] = set()
    methods: set[tuple[str, str | None]] = set()
    calls: set[str] = set()

    class_stack: list[str] = []

    def visit(node):
        nonlocal class_stack
        if node.type in CLASS_NODES:
            class_name = _first_identifier(node, source_bytes)
            if class_name:
                classes.add(class_name)
                class_stack.append(class_name)
            for child in node.children:
                visit(child)
            if class_name:
                class_stack.pop()
            return

        if node.type in METHOD_NODES:
            method_name = _first_identifier(node, source_bytes)
            if method_name:
                methods.add((method_name, class_stack[-1] if class_stack else None))
            for child in node.children:
                visit(child)
            return

        if node.type in FUNCTION_NODES:
            fn_name = _first_identifier(node, source_bytes)
            if fn_name:
                functions.add(fn_name)

        if node.type in CALL_NODES:
            call_name = _extract_call_name(node, source_bytes)
            if call_name:
                calls.add(call_name)

        if node.type in IMPORT_NODES:
            imported = _extract_import(node, source_bytes)
            if imported:
                imports.add(imported)

        for child in node.children:
            visit(child)

    visit(root)

    return ParsedCode(
        language=language,
        imports=tuple(sorted(imports)),
        functions=tuple(sorted(functions)),
        classes=tuple(sorted(classes)),
        methods=tuple(sorted(methods)),
        calls=tuple(sorted(calls)),
    )


def parse_file(path: str) -> ParsedCode | None:
    language = detect_language(path)
    if not language:
        return None
    with open(path, "rb") as handle:
        source_bytes = handle.read()
    return parse_source(source_bytes, language)


def _get_parser(language: str):
    global _PARSER_ERROR
    if _PARSER_ERROR is not None:
        raise RuntimeError(_parser_error_message(_PARSER_ERROR)) from _PARSER_ERROR

    if language in _PARSER_CACHE:
        return _PARSER_CACHE[language]

    try:
        parser = get_parser(language)
    except TypeError as exc:
        _PARSER_ERROR = exc
        raise RuntimeError(_parser_error_message(exc)) from exc
    except Exception as exc:
        _PARSER_ERROR = exc
        raise RuntimeError(
            f"Failed to load tree-sitter parser for '{language}': {exc}"
        ) from exc

    _PARSER_CACHE[language] = parser
    return parser


def validate_languages(languages: Iterable[str]) -> None:
    for lang in sorted({lang for lang in languages if lang}):
        _get_parser(lang)


def _parser_error_message(exc: Exception) -> str:
    try:
        ts_version = metadata.version("tree_sitter")
    except Exception:
        ts_version = "unknown"
    try:
        tsl_version = metadata.version("tree_sitter_languages")
    except Exception:
        tsl_version = "unknown"

    return (
        "Tree-sitter parser load failed due to an incompatibility between "
        f"tree_sitter ({ts_version}) and tree_sitter_languages ({tsl_version}). "
        "Install a compatible pair, for example `pip install tree_sitter==0.20.1` "
        "or upgrade tree_sitter_languages to match your tree_sitter version."
    )
