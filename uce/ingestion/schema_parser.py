from __future__ import annotations

import os
import re

PGTABLE_REGEX = re.compile(r"pgTable\s*\(\s*[\"']([^\"']+)[\"']\s*,\s*\{", re.M)


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


def extract_tables(schema_text: str):
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


def parse_schema_file(schema_path: str):
    if not os.path.exists(schema_path):
        return []
    with open(schema_path, "r", encoding="utf-8") as handle:
        schema_text = handle.read()
    return extract_tables(schema_text)
