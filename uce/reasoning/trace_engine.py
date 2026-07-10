import re
from typing import Iterable

from uce.core.graph_db import GraphDB
from uce.core.risk_model import assess_risk
from uce.reasoning.impact_analysis import impact_analysis


def _word_pattern(term: str, case_insensitive: bool = False) -> re.Pattern:
    flags = re.IGNORECASE if case_insensitive else 0
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", flags)


def _load_tables(graph: GraphDB):
    rows = graph.run("MATCH (t:Table) RETURN t.name AS name")
    return sorted({row["name"] for row in rows if row.get("name")})


def _load_columns(graph: GraphDB):
    rows = graph.run("MATCH (c:Column) RETURN c.name AS name, c.table AS table")
    columns = {}
    for row in rows:
        name = row.get("name")
        table = row.get("table")
        if not name or not table:
            continue
        columns.setdefault(table, []).append(name)
    return columns


def _load_files(graph: GraphDB):
    rows = graph.run("MATCH (f:File) RETURN f.path AS path")
    return sorted({row["path"] for row in rows if row.get("path")})


def detect_entity(text: str, tables: list[str], columns_by_table: dict[str, list[str]], files: list[str]):
    table_hits = []
    for table in tables:
        if _word_pattern(table, case_insensitive=True).search(text):
            table_hits.append(table)
    table_hits = sorted(table_hits)

    for table in table_hits:
        for column in sorted(set(columns_by_table.get(table, []))):
            if _word_pattern(column, case_insensitive=True).search(text):
                return "column", f"{table}.{column}"

    if table_hits:
        return "table", table_hits[0]

    column_to_tables: dict[str, set[str]] = {}
    for table, columns in columns_by_table.items():
        for column in columns:
            column_to_tables.setdefault(column, set()).add(table)

    for column in sorted(column_to_tables):
        if _word_pattern(column, case_insensitive=True).search(text):
            tables_for_column = sorted(column_to_tables[column])
            if len(tables_for_column) == 1:
                return "column", f"{tables_for_column[0]}.{column}"
            # Multi-table: pick the table that also appears in text, or the first alphabetically
            for candidate in tables_for_column:
                if _word_pattern(candidate, case_insensitive=True).search(text):
                    return "column", f"{candidate}.{column}"
            return "column", f"{tables_for_column[0]}.{column}"

    text_lower = text.lower()
    for path in files:
        if path in text or path.lower() in text_lower:
            return "file", path

    return "unknown", ""


def find_candidates(
    text: str,
    tables: list[str],
    columns_by_table: dict[str, list[str]],
    files: list[str],
    max_results: int = 10,
) -> list[dict]:
    """Return a ranked list of candidate entities matching the free-text description.

    Unlike ``detect_entity`` which returns only the single best match,
    ``find_candidates`` returns ALL matching entities ranked by a simple
    specificity score (column match > table match > file match), so callers
    can present alternatives and handle ambiguity.

    Each result is::

        {
            "entity_type": "table" | "column" | "file",
            "entity_name": str,
            "score": int,          # higher = more specific / more mentions
            "match_type": "exact_word" | "substring",
        }
    """
    text_lower = text.lower()
    candidates: list[dict] = []
    seen: set[str] = set()

    # --- Tables and their columns (word-boundary matches score higher) ---
    for table in sorted(tables):
        pat = _word_pattern(table, case_insensitive=True)
        match_count = len(pat.findall(text))
        if match_count == 0:
            continue
        score = 10 * match_count
        # If a column is also mentioned, promote that result and demote the bare table
        for column in sorted(set(columns_by_table.get(table, []))):
            col_pat = _word_pattern(column, case_insensitive=True)
            col_count = len(col_pat.findall(text))
            if col_count > 0:
                key = f"column:{table}.{column}"
                if key not in seen:
                    seen.add(key)
                    candidates.append({
                        "entity_type": "column",
                        "entity_name": f"{table}.{column}",
                        "score": score + 20 * col_count,
                        "match_type": "exact_word",
                    })
        key = f"table:{table}"
        if key not in seen:
            seen.add(key)
            candidates.append({
                "entity_type": "table",
                "entity_name": table,
                "score": score,
                "match_type": "exact_word",
            })

    # --- Cross-table column matches (column name appears but table not mentioned) ---
    column_to_tables: dict[str, set[str]] = {}
    for table, columns in columns_by_table.items():
        for column in columns:
            column_to_tables.setdefault(column, set()).add(table)

    for column in sorted(column_to_tables):
        col_pat = _word_pattern(column, case_insensitive=True)
        col_count = len(col_pat.findall(text))
        if col_count == 0:
            continue
        for table in sorted(column_to_tables[column]):
            key = f"column:{table}.{column}"
            if key not in seen:
                seen.add(key)
                candidates.append({
                    "entity_type": "column",
                    "entity_name": f"{table}.{column}",
                    "score": 15 * col_count,
                    "match_type": "exact_word",
                })

    # --- Files (exact path/stem substring match) ---
    for path in sorted(files):
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        key = f"file:{path}"
        if key in seen:
            continue
        if path in text or path.lower() in text_lower:
            seen.add(key)
            candidates.append({
                "entity_type": "file",
                "entity_name": path,
                "score": 8,
                "match_type": "exact_word",
            })
        elif stem and stem in text_lower and len(stem) > 3:
            seen.add(key)
            candidates.append({
                "entity_type": "file",
                "entity_name": path,
                "score": 3,
                "match_type": "substring",
            })

    candidates.sort(key=lambda c: (-c["score"], c["entity_type"], c["entity_name"]))
    return candidates[:max_results]


def preflight_assessment(
    graph: GraphDB,
    proposed_change: str,
    backend_paths: Iterable[str] | None = None,
):
    tables = _load_tables(graph)
    columns_by_table = _load_columns(graph)
    files = _load_files(graph)

    entity_type, entity_name = detect_entity(proposed_change, tables, columns_by_table, files)

    if entity_type == "unknown":
        return {
            "entity": "unknown",
            "entity_type": "unknown",
            "risk_score": 0,
            "affected_files": [],
            "affected_functions": [],
            "violated_requirements": [],
            "enforced_policies": [],
            "trace_paths": [],
            "summary": "No matching entity detected.",
        }

    analysis = impact_analysis(graph, entity_type, entity_name, backend_paths=backend_paths)
    impact = analysis.get("impact", analysis)

    affected_files = analysis.get("affected_files", [])
    affected_functions = analysis.get("affected_functions", [])
    violated_requirements = analysis.get("violated_requirements", [])
    enforced_policies = analysis.get("enforced_policies", [])
    trace_paths = analysis.get("trace_paths", [])

    # Reuse the exact risk computed by impact_analysis rather than recomputing it
    # here. `affected_files` above is the full import/call closure (a superset of
    # the backend-filtered set the score is based on), so recomputing from its
    # length would report a different risk than every other tool for the same
    # change. The backend file count is carried in risk_breakdown.
    risk_breakdown = analysis.get("risk_breakdown")
    backend_file_count = (
        int(risk_breakdown.get("backend_files", 0))
        if isinstance(risk_breakdown, dict)
        else len(affected_files)
    )
    risk = assess_risk(
        affected_files=backend_file_count,
        affected_functions=len(affected_functions),
        violated_requirements=len(violated_requirements),
        enforced_policies=len(enforced_policies),
    )

    return {
        "entity": entity_name,
        "entity_type": entity_type,
        "impact": impact,
        "analysis": analysis,
        "risk_score": risk.risk_score,
        "risk_severity": risk.severity,
        "risk_rationale": risk.rationale,
        "affected_files": affected_files,
        "affected_functions": affected_functions,
        "violated_requirements": violated_requirements,
        "enforced_policies": enforced_policies,
        "trace_paths": trace_paths,
    }
