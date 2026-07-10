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


# ---------------------------------------------------------------------------
# Prisma schema parser (model ModelName { field Type ... })
# ---------------------------------------------------------------------------

_PRISMA_MODEL_REGEX = re.compile(r"\bmodel\s+(\w+)\s*\{([^}]*)\}", re.DOTALL)
_PRISMA_FIELD_LINE = re.compile(r"^\s+(\w+)\s+\w", re.MULTILINE)
_PRISMA_SKIP_PREFIXES = {"@@", "@"}


def _extract_prisma(schema_text: str) -> list[dict]:
    tables = []
    for match in _PRISMA_MODEL_REGEX.finditer(schema_text):
        model_name = match.group(1)
        body = match.group(2)
        columns = []
        for field_match in _PRISMA_FIELD_LINE.finditer(body):
            field_name = field_match.group(1)
            # Skip directive lines and relation-only lines
            if field_name.startswith("@"):
                continue
            line_text = field_match.group(0).strip()
            # Skip lines that are Prisma annotations like @@index, @@unique
            if line_text.startswith("@@"):
                continue
            columns.append(field_name)
        if columns:
            tables.append({"name": model_name.lower(), "columns": sorted(set(columns))})
    return tables


# ---------------------------------------------------------------------------
# SQLAlchemy parser (declarative + core Table())
# ---------------------------------------------------------------------------

_SA_TABLENAME_REGEX = re.compile(r'__tablename__\s*=\s*["\']([^"\']+)["\']')
_SA_COLUMN_ATTR_REGEX = re.compile(
    r'^\s{2,}(\w+)\s*(?::\s*\w+\s*)?=\s*(?:mapped_column|Column)\s*\(',
    re.MULTILINE,
)
_SA_CORE_TABLE_REGEX = re.compile(
    r'\bTable\s*\(\s*["\']([^"\']+)["\']',
)
_SA_CORE_COLUMN_REGEX = re.compile(
    r'\bColumn\s*\(\s*["\']([^"\']+)["\']',
)


def _extract_sqlalchemy(schema_text: str) -> list[dict]:
    tables: list[dict] = []

    # Declarative-style: find __tablename__ and scan surrounding class body for Column attributes
    lines = schema_text.splitlines()
    for i, line in enumerate(lines):
        m = _SA_TABLENAME_REGEX.search(line)
        if not m:
            continue
        table_name = m.group(1)
        # Scan the next 80 lines for Column/mapped_column attribute assignments
        chunk = "\n".join(lines[max(0, i - 5) : i + 80])
        columns = [cm.group(1) for cm in _SA_COLUMN_ATTR_REGEX.finditer(chunk)]
        if columns:
            tables.append({"name": table_name, "columns": sorted(set(columns))})

    # Core-style: Table('name', metadata, Column('col', ...))
    for core_match in _SA_CORE_TABLE_REGEX.finditer(schema_text):
        table_name = core_match.group(1)
        # Find end of Table() call by scanning for matching parens
        start = core_match.end()
        depth = 1
        end = start
        while end < len(schema_text) and depth > 0:
            ch = schema_text[end]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            end += 1
        body = schema_text[start:end]
        columns = [cm.group(1) for cm in _SA_CORE_COLUMN_REGEX.finditer(body)]
        if columns:
            existing = next((t for t in tables if t["name"] == table_name), None)
            if existing:
                existing["columns"] = sorted(set(existing["columns"]) | set(columns))
            else:
                tables.append({"name": table_name, "columns": sorted(set(columns))})

    return tables


# ---------------------------------------------------------------------------
# Django ORM parser (class Foo(models.Model): field = models.XField(...))
# ---------------------------------------------------------------------------

_DJANGO_CLASS_REGEX = re.compile(
    r"class\s+(\w+)\s*\([^)]*\bmodels\.Model\b[^)]*\)\s*:(.*?)(?=\nclass\s|\Z)",
    re.DOTALL,
)
_DJANGO_FIELD_REGEX = re.compile(
    r"^\s{2,}(\w+)\s*=\s*models\.\w+Field",
    re.MULTILINE,
)
_DJANGO_DB_TABLE_REGEX = re.compile(r'db_table\s*=\s*["\']([^"\']+)["\']')


def _extract_django(schema_text: str) -> list[dict]:
    tables = []
    for class_match in _DJANGO_CLASS_REGEX.finditer(schema_text):
        class_name = class_match.group(1)
        body = class_match.group(2)

        # Check for explicit db_table override
        db_table_match = _DJANGO_DB_TABLE_REGEX.search(body)
        table_name = db_table_match.group(1) if db_table_match else class_name.lower()

        columns = [m.group(1) for m in _DJANGO_FIELD_REGEX.finditer(body)]
        if columns:
            tables.append({"name": table_name, "columns": sorted(set(columns))})
    return tables


# ---------------------------------------------------------------------------
# TypeORM parser (@Entity / @Column decorators)
# ---------------------------------------------------------------------------

_TYPEORM_ENTITY_REGEX = re.compile(
    r'@Entity\s*\(\s*(?:["\']([^"\']*)["\'])?\s*\)',
)
_TYPEORM_CLASS_REGEX = re.compile(r'\bclass\s+(\w+)')
_TYPEORM_COLUMN_REGEX = re.compile(
    r'@(?:Column|PrimaryColumn|PrimaryGeneratedColumn|CreateDateColumn|UpdateDateColumn'
    r'|DeleteDateColumn|VersionColumn|ViewColumn)\s*[^;]*?\n\s+(\w+)\s*[?!]?\s*[=:]',
)


def _extract_typeorm(schema_text: str) -> list[dict]:
    tables = []
    # Find each @Entity annotation and the class following it
    for entity_match in _TYPEORM_ENTITY_REGEX.finditer(schema_text):
        explicit_name = entity_match.group(1)
        rest = schema_text[entity_match.end():]

        # Find the class name immediately after @Entity(...)
        class_match = _TYPEORM_CLASS_REGEX.search(rest[:300])
        if not class_match:
            continue
        class_name = class_match.group(1)
        table_name = explicit_name if explicit_name else class_name.lower()

        # Find the class body (up to next top-level class or 150 lines)
        class_body_start = entity_match.end() + class_match.end()
        # Extract a reasonable chunk
        chunk = schema_text[class_body_start : class_body_start + 3000]
        columns = [m.group(1) for m in _TYPEORM_COLUMN_REGEX.finditer(chunk)]
        if columns:
            tables.append({"name": table_name, "columns": sorted(set(columns))})
    return tables


# ---------------------------------------------------------------------------
# Unified extractor
# ---------------------------------------------------------------------------

def extract_tables(schema_text: str):
    tables_by_name: dict[str, set[str]] = {}

    extractors = [
        _extract_pgtable,
        _extract_sql,
        _extract_prisma,
        _extract_sqlalchemy,
        _extract_django,
        _extract_typeorm,
    ]
    for extractor in extractors:
        for table in extractor(schema_text):
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
