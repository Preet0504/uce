"""
Independent ground-truth oracle for the UCE impact-analysis evaluation.

CRITICAL DESIGN GOAL
--------------------
The original benchmark (research/supplemental_benchmarks/run_benchmark.py) computes its
"oracle" using the *same* Cypher queries that UCE itself executes. That makes the evaluation
circular: UCE is graded against its own output and trivially scores ~1.0.

This module derives ground truth using mechanisms that are DELIBERATELY INDEPENDENT of the
UCE Neo4j graph:

  1. File impact: a from-scratch TypeScript/JavaScript ESM *import resolver* that parses the raw
     source tree, resolves `@/` path aliases + relative specifiers + index files, builds the true
     import graph, and computes reverse-reachability. This does NOT read UCE's IMPORTS edges.

  2. Table/column -> file: seeded from files that *import the Drizzle table symbol* from the schema
     module (detected by parsing import statements), then propagated through the independent import
     graph. Column refinement uses the camelCase<->snake_case property map parsed straight from
     schema.ts.

  3. Requirement / policy impact: parsed straight from the governance markdown documents
     (src/requirements/*.md, src/policies/*.md). A requirement governs a table/column iff its
     natural-language description references that schema symbol. This is independent of UCE's
     GOVERNS edge construction.

A small manual gold-standard override file (gold_overrides.yaml) lets a human correct individual
scenarios after inspecting the source; corrections are logged so the agreement rate between the
automatic oracle and the manual gold can be reported as an oracle-quality metric.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Backend-file classification (mirrors the convention used across UCE configs)
# ---------------------------------------------------------------------------

CODE_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx"}
_NON_BACKEND_SEGMENTS = {"ui", "views", "components", "public", "styles", "assets", "css", "scss"}
_BACKEND_SEGMENTS = {"server", "api", "db", "trpc", "inngest"}


def normalize_repo_path(path: str) -> str:
    p = (path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


def is_backend_file(rel_path: str, backend_prefixes: tuple[str, ...] = ()) -> bool:
    norm = normalize_repo_path(rel_path).lower()
    if not norm:
        return False
    if backend_prefixes:
        return any(norm == pre or norm.startswith(pre + "/") for pre in backend_prefixes)
    segments = [s for s in norm.split("/") if s]
    if any(s in _NON_BACKEND_SEGMENTS for s in segments):
        return False
    if any(s in _BACKEND_SEGMENTS for s in segments):
        return True
    return "modules" in segments and "server" in segments


# ---------------------------------------------------------------------------
# Schema parsing: table export symbol, SQL name, and column property<->sql map
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TableSchema:
    export_symbol: str            # e.g. "meetings"
    sql_name: str                 # e.g. "meetings"
    # property (camelCase, as used in TS) -> sql column name (snake_case, as stored in graph)
    prop_to_sql: dict[str, str]
    sql_to_prop: dict[str, str]


# Drizzle table head: `export const users = sqliteTable('users', {` (pg/sqlite/mysql dialects).
_DRIZZLE_HEAD_RE = re.compile(
    r"export\s+const\s+(\w+)\s*=\s*(?:pg|sqlite|mysql)Table\(\s*[\"'`]([^\"'`]+)[\"'`]\s*,\s*",
    re.DOTALL,
)
# A column definition inside a Drizzle table body: `prop: text('sql_name')`. The `\w+\(` guard
# means option objects like `{ mode: 'timestamp' }` are NOT mistaken for columns.
_COLUMN_RE = re.compile(r"(\w+)\s*:\s*\w+\(\s*[\"'`]([^\"'`]+)[\"'`]")

# SQL DDL: `CREATE TABLE [IF NOT EXISTS] [schema.]name (` (Postgres/SQLite/MySQL/Supabase).
_SQL_TABLE_HEAD_RE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:[\"'`]?\w+[\"'`]?\.)?[\"'`]?(\w+)[\"'`]?\s*\(",
    re.IGNORECASE,
)
_SQL_SKIP_TOKENS = {
    "constraint", "primary", "foreign", "unique", "check", "key", "index",
    "exclude", "like", "create", "alter", "comment",
}


def _match_braced_body(text: str, open_idx: int) -> tuple[str, int]:
    """Return (body, index_after_close) for the brace/paren block starting at `open_idx`.

    Tracks `{}`/`()` depth and skips over string literals so nested option objects and
    `references(() => x)` do not terminate the block early.
    """
    open_ch = text[open_idx]
    close_ch = "}" if open_ch == "{" else ")"
    depth = 0
    i = open_idx
    n = len(text)
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
        elif ch in "{(":
            depth += 1
        elif ch in "})":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i], i + 1
        i += 1
    return text[open_idx + 1:], n


def _parse_drizzle_schema(text: str) -> dict[str, TableSchema]:
    tables: dict[str, TableSchema] = {}
    for m in _DRIZZLE_HEAD_RE.finditer(text):
        export_symbol = m.group(1)
        sql_name = m.group(2)
        brace_idx = text.find("{", m.end() - 1)
        if brace_idx < 0:
            continue
        body, _ = _match_braced_body(text, brace_idx)
        prop_to_sql: dict[str, str] = {}
        sql_to_prop: dict[str, str] = {}
        for cm in _COLUMN_RE.finditer(body):
            prop, sql = cm.group(1), cm.group(2)
            prop_to_sql[prop] = sql
            sql_to_prop[sql] = prop
        tables[sql_name] = TableSchema(export_symbol, sql_name, prop_to_sql, sql_to_prop)
    return tables


def _parse_sql_schema(text: str) -> dict[str, TableSchema]:
    tables: dict[str, TableSchema] = {}
    for m in _SQL_TABLE_HEAD_RE.finditer(text):
        sql_name = m.group(1)
        body, _ = _match_braced_body(text, m.end() - 1)
        prop_to_sql: dict[str, str] = {}
        sql_to_prop: dict[str, str] = {}
        # Split top-level columns on commas not nested in parentheses.
        depth = 0
        buf = []
        cols: list[str] = []
        for ch in body:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            if ch == "," and depth == 0:
                cols.append("".join(buf)); buf = []
            else:
                buf.append(ch)
        if buf:
            cols.append("".join(buf))
        for col in cols:
            col = col.strip().strip(",").strip()
            if not col:
                continue
            first = re.match(r"[\"'`]?(\w+)[\"'`]?", col)
            if not first:
                continue
            name = first.group(1)
            if name.lower() in _SQL_SKIP_TOKENS:
                continue
            # SQL columns are already the storage name; identity property map.
            prop_to_sql[name] = name
            sql_to_prop[name] = name
        if sql_name in tables:  # merge ALTER/CREATE fragments across migrations
            merged = dict(tables[sql_name].sql_to_prop)
            merged.update(sql_to_prop)
            sql_to_prop = merged
            prop_to_sql = {v: v for v in merged}
        tables[sql_name] = TableSchema(sql_name, sql_name, prop_to_sql, sql_to_prop)
    return tables


def parse_schema(schema_path: Path | list[Path], kind: str = "auto") -> dict[str, TableSchema]:
    """Parse one or more schema files.

    kind: "drizzle" | "sql" | "auto" (infer from suffix). Multiple SQL files (e.g. Supabase
    migrations) are concatenated so CREATE TABLE fragments merge into one schema.
    """
    paths = [schema_path] if isinstance(schema_path, Path) else list(schema_path)
    tables: dict[str, TableSchema] = {}
    for p in paths:
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        resolved = kind
        if resolved == "auto":
            resolved = "sql" if p.suffix.lower() == ".sql" else "drizzle"
        parsed = _parse_sql_schema(text) if resolved == "sql" else _parse_drizzle_schema(text)
        for name, ts in parsed.items():
            if name in tables:
                merged = dict(tables[name].sql_to_prop); merged.update(ts.sql_to_prop)
                tables[name] = TableSchema(ts.export_symbol, name, {v: k for k, v in merged.items()}, merged)
            else:
                tables[name] = ts
    return tables


# ---------------------------------------------------------------------------
# Independent TypeScript/JavaScript import resolver
# ---------------------------------------------------------------------------

_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[\w*{}\n\s,]+?\s+from\s+)?|export\s+[\w*{}\n\s,]+?\s+from\s+|require\(\s*)"""
    r"""[\"']([^\"']+)[\"']""",
    re.MULTILINE,
)
# Capture the imported bindings of an import statement (for symbol-level detection)
_IMPORT_BINDINGS_RE = re.compile(
    r"import\s+(?P<bindings>[\w*\s{},]+?)\s+from\s+[\"'](?P<spec>[^\"']+)[\"']",
    re.MULTILINE,
)


@dataclass
class ImportGraph:
    project_root: Path
    alias_map: dict[str, str]                      # e.g. {"@/": "src/"}
    files: list[str] = field(default_factory=list)
    # rel_path -> set of rel_paths it imports
    imports: dict[str, set[str]] = field(default_factory=dict)
    # rel_path -> set of rel_paths that import it (reverse)
    imported_by: dict[str, set[str]] = field(default_factory=dict)
    # rel_path -> raw text (lowercased cache for symbol scans)
    _text: dict[str, str] = field(default_factory=dict)
    # rel_path -> set of (symbol, resolved_spec_rel_path) imported bindings
    symbol_imports: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def text(self, rel_path: str) -> str:
        return self._text.get(rel_path, "")


def _candidate_resolutions(base_no_ext: Path) -> list[Path]:
    cands: list[Path] = []
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        cands.append(base_no_ext.with_suffix(ext))
    for idx in ("index.ts", "index.tsx", "index.js", "index.jsx"):
        cands.append(base_no_ext / idx)
    return cands


def _resolve_specifier(
    spec: str,
    importer_rel: str,
    project_root: Path,
    alias_map: dict[str, str],
    known_files: set[str],
) -> str | None:
    # Skip bare package imports (node_modules) — not part of the repo graph.
    resolved_base: Path | None = None

    matched_alias = None
    for alias, target in alias_map.items():
        a = alias.rstrip("/")
        if spec == a or spec.startswith(a + "/"):
            matched_alias = (alias, target)
            break

    if matched_alias is not None:
        alias, target = matched_alias
        a = alias.rstrip("/")
        rest = spec[len(a):].lstrip("/")
        resolved_base = (project_root / normalize_repo_path(target) / rest)
    elif spec.startswith("."):
        importer_dir = (project_root / importer_rel).parent
        resolved_base = (importer_dir / spec)
    else:
        return None

    try:
        resolved_base = resolved_base.resolve()
    except Exception:
        return None

    for cand in _candidate_resolutions(resolved_base):
        try:
            rel = cand.resolve().relative_to(project_root.resolve()).as_posix()
        except Exception:
            continue
        if rel in known_files:
            return rel
    return None


def build_import_graph(
    project_root: Path,
    code_dirs: Iterable[str],
    alias_map: dict[str, str],
    ignore_dirs: Iterable[str] = (),
) -> ImportGraph:
    project_root = project_root.resolve()
    ignore = {d.lower() for d in ignore_dirs}

    # 1) Collect all code files
    rel_files: list[str] = []
    for code_dir in code_dirs:
        start = project_root / normalize_repo_path(code_dir)
        if not start.exists():
            continue
        for path in start.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in CODE_EXTENSIONS:
                continue
            parts = {p.lower() for p in path.relative_to(project_root).parts}
            if parts & ignore:
                continue
            rel_files.append(path.relative_to(project_root).as_posix())

    known = set(rel_files)
    g = ImportGraph(project_root=project_root, alias_map=alias_map, files=sorted(known))

    # 2) Parse imports + resolve
    for rel in g.files:
        text = (project_root / rel).read_text(encoding="utf-8", errors="ignore")
        g._text[rel] = text
        g.imports.setdefault(rel, set())
        g.imported_by.setdefault(rel, set())
        g.symbol_imports.setdefault(rel, [])

    for rel in g.files:
        text = g._text[rel]
        for m in _IMPORT_RE.finditer(text):
            spec = m.group(1)
            target = _resolve_specifier(spec, rel, project_root, alias_map, known)
            if target and target != rel:
                g.imports[rel].add(target)
                g.imported_by[target].add(rel)
        for bm in _IMPORT_BINDINGS_RE.finditer(text):
            bindings_raw = bm.group("bindings")
            spec = bm.group("spec")
            target = _resolve_specifier(spec, rel, project_root, alias_map, known)
            symbols = re.findall(r"\b(\w+)\b", bindings_raw)
            for sym in symbols:
                if sym in {"import", "from", "as", "type"}:
                    continue
                g.symbol_imports[rel].append((sym, target or spec))

    return g


def reverse_reachable(g: ImportGraph, seeds: set[str]) -> set[str]:
    """All files that transitively import any seed (plus the seeds themselves)."""
    result = set(seeds)
    frontier = list(seeds)
    while frontier:
        node = frontier.pop()
        for importer in g.imported_by.get(node, ()):
            if importer not in result:
                result.add(importer)
                frontier.append(importer)
    return result


# ---------------------------------------------------------------------------
# Independent file-impact oracle
# ---------------------------------------------------------------------------

def _files_importing_table(g: ImportGraph, table: TableSchema, schema_rel_candidates: set[str]) -> set[str]:
    seeds: set[str] = set()
    for rel, syms in g.symbol_imports.items():
        for sym, target in syms:
            if sym == table.export_symbol:
                # Confirm the import target is the schema module (best effort).
                if (target in schema_rel_candidates) or ("schema" in str(target).lower()) or ("db" in str(target).lower()):
                    seeds.add(rel)
    if seeds:
        return seeds
    # Fallback for ORM-less stacks (e.g. Supabase `.from('table')`): seed files that reference
    # the table name as a quoted string literal. Used only when no symbol import is found.
    quoted = re.compile(rf"""[\"'`]{re.escape(table.sql_name)}[\"'`]""")
    for rel in g.files:
        if quoted.search(g.text(rel)):
            seeds.add(rel)
    return seeds


def scenario_seeds(
    g: ImportGraph,
    schema: dict[str, TableSchema],
    schema_rel_candidates: set[str],
    entity_type: str,
    entity_name: str,
) -> set[str]:
    """Seed file set for a scenario, BEFORE reverse-reachability.

    Shared by the oracle (propagates over its own resolver) and the static baseline
    (propagates over madge's graph), so only the closure mechanism differs.
    """
    if entity_type == "table":
        table = schema.get(entity_name)
        if table is None:
            return set()
        return _files_importing_table(g, table, schema_rel_candidates)

    if entity_type == "column":
        tbl_name, col_name = entity_name.split(".", 1)
        table = schema.get(tbl_name)
        if table is None:
            return set()
        prop = table.sql_to_prop.get(col_name, col_name)
        importers = _files_importing_table(g, table, schema_rel_candidates)
        col_seeds: set[str] = set()
        prop_re = re.compile(rf"(?<![\w.]){re.escape(prop)}(?!\w)")
        sql_re = re.compile(rf"(?<![\w]){re.escape(col_name)}(?!\w)")
        for rel in importers:
            t = g.text(rel)
            if prop_re.search(t) or sql_re.search(t):
                col_seeds.add(rel)
        # Generic columns (created_at, id, …) are rarely referenced by symbol; use table importers.
        if not col_seeds:
            col_seeds = set(importers)
        return col_seeds

    if entity_type == "file":
        rel = normalize_repo_path(entity_name)
        if rel not in g.imports:
            matches = [f for f in g.files if f == rel or f.endswith("/" + rel)]
            if not matches:
                return {rel} if rel else set()
            rel = matches[0]
        return {rel}

    raise ValueError(f"Unknown entity_type: {entity_type}")


def independent_file_oracle(
    g: ImportGraph,
    schema: dict[str, TableSchema],
    schema_rel_candidates: set[str],
    entity_type: str,
    entity_name: str,
    backend_prefixes: tuple[str, ...] = (),
) -> set[str]:
    seeds = scenario_seeds(g, schema, schema_rel_candidates, entity_type, entity_name)
    affected = reverse_reachable(g, seeds)
    return {f for f in affected if is_backend_file(f, backend_prefixes)}


# ---------------------------------------------------------------------------
# Independent governance oracle (requirements + policies from documents)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RequirementDoc:
    req_id: str
    description: str
    governed_tables: frozenset[str]
    governed_columns: frozenset[str]          # "table.sql_col"


@dataclass(frozen=True)
class PolicyDoc:
    policy_id: str
    enforces: frozenset[str]


def _frontmatter(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def parse_requirements(req_dir: Path, schema: dict[str, TableSchema]) -> list[RequirementDoc]:
    docs: list[RequirementDoc] = []
    for path in sorted(req_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        fm = _frontmatter(text)
        req_id = fm.get("id", path.stem)
        description = fm.get("description", "")
        body_lower = text.lower()

        governed_tables: set[str] = set()
        governed_columns: set[str] = set()
        for tbl_name, ts in schema.items():
            # table referenced by sql name or export symbol as a whole word
            if re.search(rf"(?<!\w){re.escape(tbl_name)}(?!\w)", body_lower) or re.search(
                rf"(?<!\w){re.escape(ts.export_symbol.lower())}(?!\w)", body_lower
            ):
                governed_tables.add(tbl_name)
            for sql_col, prop in ts.sql_to_prop.items():
                if re.search(rf"(?<!\w){re.escape(sql_col)}(?!\w)", body_lower) or re.search(
                    rf"(?<!\w){re.escape(prop.lower())}(?!\w)", body_lower
                ):
                    governed_columns.add(f"{tbl_name}.{sql_col}")
                    governed_tables.add(tbl_name)
        docs.append(
            RequirementDoc(
                req_id=req_id,
                description=description,
                governed_tables=frozenset(governed_tables),
                governed_columns=frozenset(governed_columns),
            )
        )
    return docs


def parse_policies(pol_dir: Path) -> list[PolicyDoc]:
    docs: list[PolicyDoc] = []
    for path in sorted(pol_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        fm = _frontmatter(text)
        policy_id = fm.get("id", path.stem)
        enforces_raw = fm.get("enforces", "")
        enforces = frozenset(tok.strip() for tok in enforces_raw.split(",") if tok.strip())
        docs.append(PolicyDoc(policy_id=policy_id, enforces=enforces))
    return docs


def governance_oracle(
    entity_type: str,
    entity_name: str,
    requirements: list[RequirementDoc],
    policies: list[PolicyDoc],
) -> tuple[set[str], set[str]]:
    violated: set[str] = set()
    if entity_type == "table":
        for r in requirements:
            if entity_name in r.governed_tables:
                violated.add(r.req_id)
    elif entity_type == "column":
        tbl, _ = entity_name.split(".", 1)
        for r in requirements:
            if entity_name in r.governed_columns:
                violated.add(r.req_id)
    # file scenarios have no direct governance target in this corpus
    enforced: set[str] = set()
    for p in policies:
        if p.enforces & violated:
            enforced.add(p.policy_id)
    return violated, enforced
