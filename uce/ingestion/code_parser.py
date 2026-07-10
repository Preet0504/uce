import os
from dataclasses import dataclass, field
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
    "generator_function",
    "generator_function_declaration",
    "generator_function_expression",
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
NAME_IDENTIFIER_NODES = {
    "identifier",
    "property_identifier",
    "field_identifier",
}


@dataclass(frozen=True)
class ParsedCode:
    language: str
    imports: tuple[str, ...]
    functions: tuple[str, ...]
    classes: tuple[str, ...]
    methods: tuple[tuple[str, str | None], ...]
    # Each entry is (caller_name, callee_name) — scoped to the enclosing function.
    calls: tuple[tuple[str, str], ...]
    identifiers: tuple[str, ...]
    # Maps entity key → (start_line, end_line), 1-indexed.
    # Keys: function names, "class:<ClassName>", "<ClassName>.<method_name>".
    spans: dict[str, tuple[int, int]] = field(default_factory=dict)


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


def _first_name_identifier(node, source: bytes) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return _node_text(name_node, source)

    for child in node.children:
        if child.type in NAME_IDENTIFIER_NODES:
            return _node_text(child, source)
    return None


def _first_name_in_tree(node, source: bytes) -> str | None:
    for child in _walk(node):
        if child.type in NAME_IDENTIFIER_NODES:
            return _node_text(child, source)
    return None


def _last_name_in_tree(node, source: bytes) -> str | None:
    last = None
    for child in _walk(node):
        if child.type in NAME_IDENTIFIER_NODES:
            last = _node_text(child, source)
    return last


def _infer_assigned_name(node, source: bytes) -> str | None:
    parent = node.parent
    for _ in range(4):
        if parent is None:
            return None
        ptype = parent.type

        if ptype == "variable_declarator":
            name_node = parent.child_by_field_name("name") or parent.child_by_field_name("pattern")
            if name_node is not None:
                name = _first_name_in_tree(name_node, source)
                if name:
                    return name

        if ptype in {"assignment_expression", "assignment", "augmented_assignment"}:
            target = parent.child_by_field_name("left") or parent.child_by_field_name("target")
            if target is not None:
                name = _last_name_in_tree(target, source) or _first_name_in_tree(target, source)
                if name:
                    return name

        if ptype in {"pair", "object_pair", "property_assignment"}:
            key = parent.child_by_field_name("key") or parent.child_by_field_name("name")
            if key is not None:
                name = _first_name_in_tree(key, source)
                if name:
                    return name

        if ptype in {"property_definition", "public_field_definition", "field_definition"}:
            name_node = parent.child_by_field_name("name")
            if name_node is not None:
                name = _first_name_in_tree(name_node, source)
                if name:
                    return name

        parent = parent.parent

    return None


def _extract_function_name(node, source: bytes) -> str | None:
    name = _first_name_identifier(node, source)
    if name:
        return name

    declarator = node.child_by_field_name("declarator")
    if declarator is not None:
        name = _first_name_in_tree(declarator, source)
        if name:
            return name

    return _infer_assigned_name(node, source)


def _extract_import(node, source: bytes) -> str | None:
    for child in node.children:
        if child.type in STRING_NODES:
            value = _node_text(child, source)
            return value.strip("\"'` ")

    # Python's import_statement/import_from_statement have no quoted module string.
    # The module path is a dotted_name ("uce.core.rbac") or, for relative imports
    # ("from . import x" / "from .config import x"), a relative_import node. Either
    # always appears before the "import" keyword and any imported-name dotted_names,
    # so the first match in document order is the module reference, not an imported name.
    for child in node.children:
        if child.type in ("dotted_name", "relative_import"):
            return _node_text(child, source)

    for child in node.children:
        if child.type in IDENTIFIER_NODES:
            return _node_text(child, source)

    text = _node_text(node, source).strip()
    return text or None


def _extract_call_name(node, source: bytes) -> str | None:
    target = node.child_by_field_name("function") or node.child_by_field_name("callee")
    if target is None:
        target = node

    # If the call target is itself an identifier (e.g. Python `helper()` where
    # tree-sitter gives a bare `identifier` node with no children), return it directly.
    if target.type in IDENTIFIER_NODES:
        return _node_text(target, source)

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


def _node_span(node) -> tuple[int, int]:
    """Return 1-indexed (start_line, end_line) for a tree-sitter node."""
    return node.start_point[0] + 1, node.end_point[0] + 1


def parse_source(source_bytes: bytes, language: str, collect_identifiers: bool = True) -> ParsedCode:
    parser = _get_parser(language)
    tree = parser.parse(source_bytes)
    root = tree.root_node

    imports: set[str] = set()
    functions: set[str] = set()
    classes: set[str] = set()
    methods: set[tuple[str, str | None]] = set()
    calls: set[tuple[str, str]] = set()
    identifiers: set[str] = set()
    spans: dict[str, tuple[int, int]] = {}

    class_stack: list[str] = []
    function_stack: list[str] = []

    def visit(node):
        nonlocal class_stack, function_stack

        if node.type in CLASS_NODES:
            class_name = _first_identifier(node, source_bytes)
            if class_name:
                classes.add(class_name)
                spans[f"class:{class_name}"] = _node_span(node)
                class_stack.append(class_name)
            for child in node.children:
                visit(child)
            if class_name:
                class_stack.pop()
            return

        if node.type in METHOD_NODES:
            method_name = _extract_function_name(node, source_bytes)
            class_of_method = class_stack[-1] if class_stack else None
            if method_name:
                methods.add((method_name, class_of_method))
                span_key = f"{class_of_method or '__unknown__'}.{method_name}"
                spans[span_key] = _node_span(node)
                function_stack.append(method_name)
            for child in node.children:
                visit(child)
            if method_name:
                function_stack.pop()
            return

        if node.type in FUNCTION_NODES:
            fn_name = _extract_function_name(node, source_bytes)
            if fn_name:
                if class_stack:
                    # Function defined inside a class → treat as method (e.g. Python).
                    # Languages with dedicated method_definition nodes (JS/TS/Java)
                    # are already handled by the METHOD_NODES branch above.
                    class_of_method = class_stack[-1]
                    methods.add((fn_name, class_of_method))
                    span_key = f"{class_of_method}.{fn_name}"
                    spans[span_key] = _node_span(node)
                else:
                    functions.add(fn_name)
                    spans[fn_name] = _node_span(node)
                function_stack.append(fn_name)
            for child in node.children:
                visit(child)
            if fn_name:
                function_stack.pop()
            return

        if node.type in CALL_NODES:
            call_name = _extract_call_name(node, source_bytes)
            if call_name and function_stack:
                # Only record calls that are inside a named function/method
                calls.add((function_stack[-1], call_name))

        if collect_identifiers and node.type in IDENTIFIER_NODES:
            value = _node_text(node, source_bytes).strip()
            if value:
                identifiers.add(value)

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
        identifiers=tuple(sorted(identifiers)) if collect_identifiers else tuple(),
        spans=spans,
    )


def parse_file(path: str, collect_identifiers: bool = True) -> ParsedCode | None:
    language = detect_language(path)
    if not language:
        return None
    with open(path, "rb") as handle:
        source_bytes = handle.read()
    return parse_source(source_bytes, language, collect_identifiers=collect_identifiers)


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
