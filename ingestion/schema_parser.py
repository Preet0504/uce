from __future__ import annotations

import os
import re

PGTABLE_REGEX = re.compile(
    r"(?:pgTable|sqliteTable|mysqlTable)\s*\(\s*[\"\']([^\"\']+)[\"\']\s*,\s*\{",
    re.M,
)
SQL_CREATE_TABLE_REGEX = re.compile(
    r"create\s+table\s+(if\s+not\s+exists\s+)?(?P<name>[^\s(]+)",
    re.IGNORECASE,
)


def _scan_object(text: str, start_index: int):
    depth = 1
    i = start_index
    in_string = None
    in_line_comment = False
    in_block_comment = False

    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue

        if ch in ('"', "'", "`"):
            in_string = ch
            i += 1
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start_index:i], i

        i += 1

    return None, None


def _scan_parens(text: str, start_index: int):
    depth = 1
    i = start_index
    in_string = None
    in_line_comment = False
    in_block_comment = False
    in_sql_line_comment = False

    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_sql_line_comment:
            if ch == "\n":
                in_sql_line_comment = False
            i += 1
            continue

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_sql_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue

        if ch in ('"', "'", "`", "["):
            in_string = "]" if ch == "[" else ch
            i += 1
            continue

        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start_index:i], i

        i += 1

    return None, None


def _split_top_level_entries(text: str):
    entries = []
    start = 0
    i = 0
    in_string = None
    in_line_comment = False
    in_block_comment = False
    paren_depth = 0
    brace_depth = 0
    bracket_depth = 0

    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue

        if ch in ('"', "'", "`"):
            in_string = ch
            i += 1
            continue

        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth = max(0, paren_depth - 1)
        elif ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth = max(0, brace_depth - 1)
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif ch == "," and paren_depth == 0 and brace_depth == 0 and bracket_depth == 0:
            entry = text[start:i].strip()
            if entry:
                entries.append(entry)
            start = i + 1

        i += 1

    tail = text[start:].strip()
    if tail:
        entries.append(tail)

    return entries


def _split_sql_entries(text: str):
    entries = []
    start = 0
    i = 0
    in_string = None
    in_line_comment = False
    in_block_comment = False
    in_sql_line_comment = False
    paren_depth = 0

    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if in_sql_line_comment:
            if ch == "\n":
                in_sql_line_comment = False
            i += 1
            continue

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_sql_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue

        if ch in ('"', "'", "`", "["):
            in_string = "]" if ch == "[" else ch
            i += 1
            continue

        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth = max(0, paren_depth - 1)
        elif ch == "," and paren_depth == 0:
            entry = text[start:i].strip()
            if entry:
                entries.append(entry)
            start = i + 1

        i += 1

    tail = text[start:].strip()
    if tail:
        entries.append(tail)

    return entries


def _split_key_value(entry: str):
    i = 0
    in_string = None
    in_line_comment = False
    in_block_comment = False
    paren_depth = 0
    brace_depth = 0
    bracket_depth = 0

    while i < len(entry):
        ch = entry[i]
        nxt = entry[i + 1] if i + 1 < len(entry) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue

        if ch in ('"', "'", "`"):
            in_string = ch
            i += 1
            continue

        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth = max(0, paren_depth - 1)
        elif ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth = max(0, brace_depth - 1)
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]":
            bracket_depth = max(0, bracket_depth - 1)
        elif ch == ":" and paren_depth == 0 and brace_depth == 0 and bracket_depth == 0:
            key = entry[:i].strip()
            value = entry[i + 1 :].strip()
            return key, value

        i += 1

    return None, None


def _first_string_literal(text: str):
    i = 0
    in_string = None
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                return text[string_start:i]
            i += 1
            continue

        if ch in ('"', "'"):
            in_string = ch
            string_start = i + 1
            i += 1
            continue

        i += 1

    return None


def _extract_pgtable(schema_text: str):
    tables = []
    for match in PGTABLE_REGEX.finditer(schema_text):
        table_name = match.group(1)
        object_text, _ = _scan_object(schema_text, match.end())
        if object_text is None:
            continue

        columns = []
        entries = _split_top_level_entries(object_text)
        for entry in entries:
            key, value = _split_key_value(entry)
            if not key or not value:
                continue
            column_name = _first_string_literal(value)
            if column_name:
                columns.append(column_name)

        tables.append({"name": table_name, "columns": sorted(set(columns))})

    return tables


def _strip_identifier(token: str) -> str:
    cleaned = token.strip()
    if not cleaned:
        return cleaned
    if cleaned[0] in ('"', "'", "`", "["):
        end = "]" if cleaned[0] == "[" else cleaned[0]
        if cleaned.endswith(end):
            cleaned = cleaned[1:-1]
    return cleaned.strip()


def _extract_sql_table_name(raw: str) -> str | None:
    if not raw:
        return None
    name = raw.strip()
    name = _strip_identifier(name)
    if not name:
        return None
    if "." in name:
        name = name.split(".")[-1].strip()
        name = _strip_identifier(name)
    return name or None


def _extract_sql_column_name(entry: str) -> str | None:
    entry = entry.strip()
    if not entry:
        return None
    lowered = entry.lower()
    if lowered.startswith(
        (
            "constraint ",
            "primary ",
            "foreign ",
            "unique ",
            "key ",
            "index ",
            "check ",
        )
    ):
        return None

    if entry[0] in ('"', "'", "`", "["):
        end = "]" if entry[0] == "[" else entry[0]
        end_idx = entry.find(end, 1)
        if end_idx != -1:
            return entry[1:end_idx].strip() or None
        return None

    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", entry)
    if match:
        return match.group(0)

    return None


def _extract_sql(schema_text: str):
    tables = []
    for match in SQL_CREATE_TABLE_REGEX.finditer(schema_text):
        raw_name = match.group("name")
        table_name = _extract_sql_table_name(raw_name)
        if not table_name:
            continue

        open_paren = schema_text.find("(", match.end())
        if open_paren == -1:
            continue

        columns_block, _ = _scan_parens(schema_text, open_paren + 1)
        if columns_block is None:
            continue

        columns = []
        entries = _split_sql_entries(columns_block)
        for entry in entries:
            col = _extract_sql_column_name(entry)
            if col:
                columns.append(col)

        tables.append({"name": table_name, "columns": sorted(set(columns))})

    return tables


def extract_tables(schema_text: str):
    tables_by_name: dict[str, set[str]] = {}

    for table in _extract_pgtable(schema_text):
        name = table.get("name")
        if not name:
            continue
        tables_by_name.setdefault(name, set()).update(table.get("columns", []))

    for table in _extract_sql(schema_text):
        name = table.get("name")
        if not name:
            continue
        tables_by_name.setdefault(name, set()).update(table.get("columns", []))

    return [
        {"name": name, "columns": sorted(cols)}
        for name, cols in sorted(tables_by_name.items())
    ]


def parse_schema_file(schema_path: str):
    if not os.path.exists(schema_path):
        return []
    with open(schema_path, "r", encoding="utf-8") as handle:
        schema_text = handle.read()
    return extract_tables(schema_text)
