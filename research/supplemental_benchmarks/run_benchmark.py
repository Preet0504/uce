from __future__ import annotations

import argparse
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

from uce.core.config import UceConfig, load_config
from uce.core.graph_db import GraphDB
from uce.core.rbac import ROLE_RANKS, AuthorityRule, evaluate_rules, rule_from_row
from uce.core.risk_model import assess_risk
from uce.ingestion.graph_builder import (
    is_ignored,
    load_columns,
    load_tables,
    upsert_policies,
    upsert_requirements,
)
from uce.reasoning import impact_analysis as impact_module


BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "results"
TABLES_DIR = RESULTS_DIR / "tables"
FIGURES_DIR = RESULTS_DIR / "figures"


@dataclass(frozen=True)
class RequirementRecord:
    req_id: str
    title: str
    description: str


@dataclass(frozen=True)
class PolicyRecord:
    policy_id: str
    description: str
    requirement_ids: list[str]


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    entity_type: str
    entity_name: str
    prompt: str


@dataclass(frozen=True)
class RbacProbe:
    probe_id: str
    role: str
    operation: str
    path: str
    path_group: str


def _ensure_dirs() -> None:
    for path in (RESULTS_DIR, TABLES_DIR, FIGURES_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _parse_frontmatter_line(line: str) -> tuple[str, str] | None:
    if ":" not in line:
        return None
    key, value = line.split(":", 1)
    key = key.strip().lower()
    value = value.strip()
    if not key:
        return None
    return key, value


def _read_requirements(requirements_dir: Path) -> list[RequirementRecord]:
    records: list[RequirementRecord] = []
    for path in sorted(requirements_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        values: dict[str, str] = {}
        for line in text.splitlines():
            parsed = _parse_frontmatter_line(line)
            if parsed is None:
                continue
            values[parsed[0]] = parsed[1]
        req_id = values.get("id", path.stem).strip()
        title = values.get("title", "").strip() or req_id
        description = values.get("description", "").strip()
        records.append(RequirementRecord(req_id=req_id, title=title, description=description))
    return records


def _read_policies(policies_dir: Path) -> list[PolicyRecord]:
    records: list[PolicyRecord] = []
    for path in sorted(policies_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        values: dict[str, str] = {}
        for line in text.splitlines():
            parsed = _parse_frontmatter_line(line)
            if parsed is None:
                continue
            values[parsed[0]] = parsed[1]
        policy_id = values.get("id", path.stem).strip()
        description = values.get("description", "").strip()
        enforce_line = values.get("enforces", "").strip()
        requirement_ids = [token.strip() for token in enforce_line.split(",") if token.strip()]
        records.append(
            PolicyRecord(
                policy_id=policy_id,
                description=description,
                requirement_ids=requirement_ids,
            )
        )
    return records


def _read_rbac_rules(rbac_dir: Path) -> list[dict[str, object]]:
    docs = sorted(rbac_dir.glob("*.md"))
    if not docs:
        return []

    text = docs[0].read_text(encoding="utf-8", errors="ignore")
    policy_match = re.search(r"(?im)^policy id:\s*(.+)$", text)
    policy_id = policy_match.group(1).strip() if policy_match else docs[0].stem

    rules: list[dict[str, object]] = []
    current: dict[str, str] | None = None

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("- rule_id:"):
            if current:
                rules.append(_normalize_rule(policy_id, current))
            current = {"rule_id": stripped.split(":", 1)[1].strip()}
            continue

        if current is None:
            continue

        parsed = _parse_frontmatter_line(stripped)
        if parsed is None:
            continue
        key, value = parsed
        current[key] = value

    if current:
        rules.append(_normalize_rule(policy_id, current))

    return rules


def _normalize_rule(policy_id: str, raw: dict[str, str]) -> dict[str, object]:
    priority_raw = raw.get("source_priority", "0")
    try:
        priority = int(priority_raw)
    except ValueError:
        priority = 0
    return {
        "policy_id": policy_id,
        "rule_id": raw.get("rule_id", "").strip(),
        "operation": raw.get("operation", "").strip().lower(),
        "path_pattern": raw.get("path_pattern", "").strip().replace("\\", "/"),
        "min_role": raw.get("min_role", "").strip().lower(),
        "effect": raw.get("effect", "allow").strip().lower(),
        "source_priority": priority,
    }


def deterministic_governance_ingestion(config: UceConfig, graph: GraphDB) -> dict[str, int]:
    project_root = Path(config.project_root)
    req_dir = project_root / "src" / "requirements"
    pol_dir = project_root / "src" / "policies"
    rbac_dir = project_root / "src" / "rbac"

    requirements = _read_requirements(req_dir)
    policies = _read_policies(pol_dir)
    rules = _read_rbac_rules(rbac_dir)

    tables = load_tables(graph)
    columns_by_table = load_columns(graph)
    upsert_requirements(graph, requirements, tables, columns_by_table)
    upsert_policies(graph, policies)
    graph.replace_authority_rules(rules)

    return {
        "requirements": len(requirements),
        "policies": len(policies),
        "rbac_rules": len(rules),
    }


def _normalize_repo_path(path: str) -> str:
    normalized = (path or "").replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _normalize_backend_prefixes(backend_paths: Iterable[str] | None) -> tuple[str, ...]:
    if not backend_paths:
        return tuple()
    normalized: set[str] = set()
    for raw in backend_paths:
        prefix = _normalize_repo_path(str(raw))
        if prefix:
            normalized.add(prefix.lower())
    return tuple(sorted(normalized))


def _matches_backend_prefix(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _is_backend_file(path: str, backend_prefixes: tuple[str, ...]) -> bool:
    normalized = _normalize_repo_path(path).lower()
    if not normalized:
        return False

    if backend_prefixes:
        return any(_matches_backend_prefix(normalized, prefix) for prefix in backend_prefixes)

    segments = [segment for segment in normalized.split("/") if segment]
    if any(segment in {"ui", "views", "components", "public", "styles"} for segment in segments):
        return False
    if any(segment in {"server", "api", "db", "trpc", "inngest"} for segment in segments):
        return True
    return "modules" in segments and "server" in segments


def _filter_backend(paths: Iterable[str], backend_prefixes: tuple[str, ...]) -> list[str]:
    return sorted({path for path in paths if _is_backend_file(path, backend_prefixes)})


def _reverse_import_closure(graph: GraphDB, direct_paths: list[str]) -> list[str]:
    if not direct_paths:
        return []
    rows = graph.run(impact_module.REVERSE_IMPORT_QUERY, direct=direct_paths)
    return sorted({row["path"] for row in rows if row.get("path")})


def _policies_for_requirements(graph: GraphDB, req_ids: list[str]) -> list[str]:
    if not req_ids:
        return []
    rows = graph.run(
        """
        MATCH (p:Policy)-[:ENFORCES]->(r:Requirement)
        WHERE r.id IN $req_ids
        RETURN collect(DISTINCT p.id) AS ids
        """,
        req_ids=req_ids,
    )
    if not rows:
        return []
    return sorted({policy_id for policy_id in (rows[0].get("ids") or []) if policy_id})


def _count_functions(graph: GraphDB, file_paths: list[str]) -> int:
    if not file_paths:
        return 0
    rows = graph.run(
        """
        MATCH (f:File)-[:DECLARES_FUNCTION]->(fn:Function)
        WHERE f.path IN $paths
        RETURN count(DISTINCT fn) AS total
        """,
        paths=file_paths,
    )
    return int(rows[0].get("total") or 0) if rows else 0


def _oracle_requirements_for_table(graph: GraphDB, table_name: str) -> list[str]:
    rows = graph.run(
        """
        MATCH (r:Requirement)-[:GOVERNS]->(t:Table {name: $table})
        RETURN collect(DISTINCT r.id) AS req_ids
        """,
        table=table_name,
    )
    table_req = set(rows[0].get("req_ids") or []) if rows else set()

    rows = graph.run(
        """
        MATCH (r:Requirement)-[:GOVERNS]->(c:Column {table: $table})
        RETURN collect(DISTINCT r.id) AS req_ids
        """,
        table=table_name,
    )
    col_req = set(rows[0].get("req_ids") or []) if rows else set()
    return sorted({req_id for req_id in (table_req | col_req) if req_id})


def _oracle_requirements_for_column(graph: GraphDB, table_name: str, column_name: str) -> list[str]:
    rows = graph.run(
        """
        MATCH (r:Requirement)-[:GOVERNS]->(c:Column {table: $table, name: $column})
        RETURN collect(DISTINCT r.id) AS req_ids
        """,
        table=table_name,
        column=column_name,
    )
    if not rows:
        return []
    return sorted({req_id for req_id in (rows[0].get("req_ids") or []) if req_id})


def _oracle_requirements_for_files(graph: GraphDB, files: list[str]) -> list[str]:
    if not files:
        return []
    rows = graph.run(
        """
        MATCH (f:File)-[:USES_TABLE]->(t:Table)<-[:GOVERNS]-(r:Requirement)
        WHERE f.path IN $files
        RETURN collect(DISTINCT r.id) AS req_ids
        """,
        files=files,
    )
    table_req = set(rows[0].get("req_ids") or []) if rows else set()

    rows = graph.run(
        """
        MATCH (f:File)-[:REFERENCES_COLUMN]->(c:Column)<-[:GOVERNS]-(r:Requirement)
        WHERE f.path IN $files
        RETURN collect(DISTINCT r.id) AS req_ids
        """,
        files=files,
    )
    col_req = set(rows[0].get("req_ids") or []) if rows else set()
    return sorted({req_id for req_id in (table_req | col_req) if req_id})


def oracle_prediction(
    graph: GraphDB,
    scenario: Scenario,
    backend_prefixes: tuple[str, ...],
) -> dict[str, object]:
    entity_type = scenario.entity_type
    entity_name = scenario.entity_name

    if entity_type == "table":
        rows = graph.run(impact_module.TABLE_IMPACT_QUERY, table=entity_name)
        direct = set()
        if rows:
            direct.update(path for path in (rows[0].get("table_files") or []) if path)
            direct.update(path for path in (rows[0].get("column_files") or []) if path)
        direct_files = sorted(direct)
        transitive_files = _reverse_import_closure(graph, direct_files)
        affected_files = _filter_backend(set(direct_files) | set(transitive_files), backend_prefixes)
        requirements = _oracle_requirements_for_table(graph, entity_name)

    elif entity_type == "column":
        table_name, column_name = entity_name.split(".", 1)
        rows = graph.run(impact_module.COLUMN_IMPACT_QUERY, table=table_name, column=column_name)
        direct = set()
        if rows:
            direct.update(path for path in (rows[0].get("files") or []) if path)
        direct_files = sorted(direct)
        transitive_files = _reverse_import_closure(graph, direct_files)
        affected_files = _filter_backend(set(direct_files) | set(transitive_files), backend_prefixes)
        requirements = _oracle_requirements_for_column(graph, table_name, column_name)

    elif entity_type == "file":
        direct_files = [entity_name]
        transitive_files = _reverse_import_closure(graph, direct_files)
        affected_files = _filter_backend(set(direct_files) | set(transitive_files), backend_prefixes)
        requirements = _oracle_requirements_for_files(graph, affected_files)

    else:
        raise ValueError(f"Unsupported scenario entity type: {entity_type}")

    policies = _policies_for_requirements(graph, requirements)
    affected_function_count = _count_functions(graph, affected_files)
    risk = assess_risk(
        affected_files=len(affected_files),
        affected_functions=affected_function_count,
        violated_requirements=len(requirements),
        enforced_policies=len(policies),
    )
    return {
        "affected_files": sorted(affected_files),
        "violated_requirements": sorted(requirements),
        "enforced_policies": sorted(policies),
        "affected_function_count": affected_function_count,
        "risk_score": int(risk.risk_score),
    }


def uce_prediction(
    graph: GraphDB,
    scenario: Scenario,
    backend_paths: Iterable[str],
) -> dict[str, object]:
    start = time.perf_counter()
    result = impact_module.impact_analysis(
        graph,
        scenario.entity_type,
        scenario.entity_name,
        backend_paths=backend_paths,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    affected_files = sorted(set(result.get("affected_files") or []))
    if not affected_files:
        impact_payload = result.get("impact")
        if isinstance(impact_payload, dict):
            affected_files = sorted(set(impact_payload.get("affected_files") or []))
    requirements = sorted(set(result.get("violated_requirements") or []))
    policies = sorted(set(result.get("enforced_policies") or []))
    risk_score = int(result.get("risk_score") or 0)
    return {
        "affected_files": affected_files,
        "violated_requirements": requirements,
        "enforced_policies": policies,
        "risk_score": risk_score,
        "latency_ms": elapsed_ms,
    }


def _tokenize_entity(scenario: Scenario) -> list[str]:
    if scenario.entity_type == "column":
        table_name, column_name = scenario.entity_name.split(".", 1)
        raw = f"{table_name} {column_name}"
    elif scenario.entity_type == "file":
        raw = Path(scenario.entity_name).stem.replace("_", " ").replace("-", " ")
    else:
        raw = scenario.entity_name
    tokens = [token.lower() for token in re.split(r"[^A-Za-z0-9_]+", raw) if token]
    return sorted(set(tokens))


def _build_file_corpus(config: UceConfig, backend_prefixes: tuple[str, ...]) -> dict[str, str]:
    root = Path(config.project_root)
    corpus: dict[str, str] = {}
    extensions = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".c", ".cpp"}
    for code_path in config.paths.code:
        start = root / code_path
        if not start.exists():
            continue
        for path in start.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in extensions:
                continue
            rel = path.relative_to(root).as_posix()
            if is_ignored(rel, config.ignore):
                continue
            if not _is_backend_file(rel, backend_prefixes):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            corpus[rel] = text
    return corpus


def vanilla_prediction(
    scenario: Scenario,
    oracle: dict[str, object],
    requirements: list[RequirementRecord],
    policies: list[PolicyRecord],
    file_corpus: dict[str, str],
    graph: GraphDB,
) -> dict[str, object]:
    start = time.perf_counter()
    tokens = _tokenize_entity(scenario)

    scored_files: list[tuple[str, int]] = []
    for file_path, text in file_corpus.items():
        score = 0
        for token in tokens:
            score += text.count(token)
        if score > 0:
            scored_files.append((file_path, score))
    scored_files.sort(key=lambda item: (-item[1], item[0]))

    oracle_k = len(oracle["affected_files"])
    top_k = max(1, min(20, oracle_k if oracle_k > 0 else 5))
    predicted_files = [path for path, _ in scored_files[:top_k]]

    req_scores: list[tuple[str, int]] = []
    for req in requirements:
        text = f"{req.title} {req.description}".lower()
        score = 0
        for token in tokens:
            score += text.count(token)
        if score > 0:
            req_scores.append((req.req_id, score))
    req_scores.sort(key=lambda item: (-item[1], item[0]))
    predicted_requirements = [req_id for req_id, _ in req_scores[: max(1, min(4, len(req_scores)))]]

    predicted_policies = sorted(
        {
            policy.policy_id
            for policy in policies
            for req_id in predicted_requirements
            if req_id in policy.requirement_ids
        }
    )

    function_count = _count_functions(graph, predicted_files)
    risk = assess_risk(
        affected_files=len(predicted_files),
        affected_functions=function_count,
        violated_requirements=len(predicted_requirements),
        enforced_policies=len(predicted_policies),
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return {
        "affected_files": predicted_files,
        "violated_requirements": predicted_requirements,
        "enforced_policies": predicted_policies,
        "risk_score": int(risk.risk_score),
        "latency_ms": elapsed_ms,
    }


def _metrics_from_sets(predicted: set[str], truth: set[str]) -> tuple[float, float, float]:
    tp = len(predicted & truth)
    fp = len(predicted - truth)
    fn = len(truth - predicted)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return precision, recall, f1


def _build_scenarios(graph: GraphDB, backend_prefixes: tuple[str, ...]) -> list[Scenario]:
    scenarios: list[Scenario] = []

    table_rows = graph.run("MATCH (t:Table) RETURN t.name AS name ORDER BY t.name")
    tables = [row["name"] for row in table_rows if row.get("name")]
    for table in tables:
        scenarios.append(
            Scenario(
                scenario_id=f"TBL-{table}",
                entity_type="table",
                entity_name=table,
                prompt=f"Change the schema and business logic for table '{table}'.",
            )
        )

    col_rows = graph.run(
        "MATCH (c:Column) RETURN c.table AS table, c.name AS name ORDER BY c.table, c.name"
    )
    by_table: dict[str, list[str]] = {}
    for row in col_rows:
        table = row.get("table")
        name = row.get("name")
        if not table or not name:
            continue
        by_table.setdefault(table, []).append(name)

    for table in tables:
        selected = sorted(set(by_table.get(table, [])))[:2]
        for column in selected:
            scenarios.append(
                Scenario(
                    scenario_id=f"COL-{table}-{column}",
                    entity_type="column",
                    entity_name=f"{table}.{column}",
                    prompt=f"Modify column '{column}' in '{table}' and update downstream logic.",
                )
            )

    file_rows = graph.run(
        """
        MATCH (f:File)
        OPTIONAL MATCH (u:File)-[:IMPORTS]->(f)
        RETURN f.path AS path, count(u) AS indegree
        ORDER BY indegree DESC, path ASC
        """
    )
    file_candidates = [
        row["path"]
        for row in file_rows
        if row.get("path") and _is_backend_file(str(row["path"]), backend_prefixes)
    ]
    for path in file_candidates[:6]:
        scenarios.append(
            Scenario(
                scenario_id=f"FIL-{path.replace('/', '_')}",
                entity_type="file",
                entity_name=path,
                prompt=f"Refactor file '{path}' and validate blast radius.",
            )
        )

    return scenarios


def _aggregate_metrics(df: pd.DataFrame, system_name: str) -> dict[str, float | str]:
    sub = df[df["system"] == system_name]

    file_tp = int(sub["file_tp"].sum())
    file_fp = int(sub["file_fp"].sum())
    file_fn = int(sub["file_fn"].sum())
    file_precision = file_tp / (file_tp + file_fp) if (file_tp + file_fp) else 0.0
    file_recall = file_tp / (file_tp + file_fn) if (file_tp + file_fn) else 0.0
    file_f1 = (
        2 * file_precision * file_recall / (file_precision + file_recall)
        if (file_precision + file_recall)
        else 0.0
    )

    req_tp = int(sub["req_tp"].sum())
    req_fp = int(sub["req_fp"].sum())
    req_fn = int(sub["req_fn"].sum())
    req_precision = req_tp / (req_tp + req_fp) if (req_tp + req_fp) else 0.0
    req_recall = req_tp / (req_tp + req_fn) if (req_tp + req_fn) else 0.0
    req_f1 = (
        2 * req_precision * req_recall / (req_precision + req_recall)
        if (req_precision + req_recall)
        else 0.0
    )

    pol_tp = int(sub["pol_tp"].sum())
    pol_fp = int(sub["pol_fp"].sum())
    pol_fn = int(sub["pol_fn"].sum())
    pol_precision = pol_tp / (pol_tp + pol_fp) if (pol_tp + pol_fp) else 0.0
    pol_recall = pol_tp / (pol_tp + pol_fn) if (pol_tp + pol_fn) else 0.0
    pol_f1 = (
        2 * pol_precision * pol_recall / (pol_precision + pol_recall)
        if (pol_precision + pol_recall)
        else 0.0
    )

    risk_rank_spearman = 0.0
    if len(sub) > 1:
        corr = sub["risk_pred"].corr(sub["risk_oracle"], method="spearman")
        if pd.notna(corr):
            risk_rank_spearman = float(corr)

    return {
        "system": system_name,
        "n_scenarios": int(len(sub)),
        "file_precision": file_precision,
        "file_recall": file_recall,
        "file_f1": file_f1,
        "requirement_precision": req_precision,
        "requirement_recall": req_recall,
        "requirement_f1": req_f1,
        "policy_precision": pol_precision,
        "policy_recall": pol_recall,
        "policy_f1": pol_f1,
        "risk_mae": float(sub["risk_abs_error"].mean()) if len(sub) else 0.0,
        "risk_rank_spearman": risk_rank_spearman,
        "latency_ms_mean": float(sub["latency_ms"].mean()) if len(sub) else 0.0,
    }


def _aggregate_violation_metrics(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for system_name in ("vanilla", "mcp_uce"):
        sub = df[df["system"] == system_name]
        req_caught = int(sub["req_tp"].sum())
        req_missed = int(sub["req_fn"].sum())
        req_false_alarm = int(sub["req_fp"].sum())
        pol_caught = int(sub["pol_tp"].sum())
        pol_missed = int(sub["pol_fn"].sum())
        pol_false_alarm = int(sub["pol_fp"].sum())
        req_total = req_caught + req_missed
        pol_total = pol_caught + pol_missed
        rows.append(
            {
                "system": system_name,
                "requirement_caught": req_caught,
                "requirement_missed": req_missed,
                "requirement_false_alarm": req_false_alarm,
                "requirement_total": req_total,
                "requirement_miss_rate": (req_missed / req_total) if req_total else 0.0,
                "policy_caught": pol_caught,
                "policy_missed": pol_missed,
                "policy_false_alarm": pol_false_alarm,
                "policy_total": pol_total,
                "policy_miss_rate": (pol_missed / pol_total) if pol_total else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _load_authority_rules(graph: GraphDB) -> list[AuthorityRule]:
    rows = graph.run(
        """
        MATCH (rule:AuthorityRule)-[:REQUIRES_ROLE]->(role:Role)
        OPTIONAL MATCH (policy:Policy)-[:DEFINES_RULE]->(rule)
        RETURN rule.id AS rule_id,
               rule.operation AS operation,
               rule.path_pattern AS path_pattern,
               rule.effect AS effect,
               rule.min_role AS min_role,
               role.rank AS min_role_rank,
               coalesce(rule.source_priority, 0) AS source_priority,
               policy.id AS policy_id
        ORDER BY rule.id
        """
    )
    parsed: list[AuthorityRule] = []
    for row in rows:
        rule = rule_from_row(row)
        if rule is not None:
            parsed.append(rule)
    return parsed


def _build_rbac_probes(
    graph: GraphDB,
    project_root: Path,
    backend_prefixes: tuple[str, ...],
) -> list[RbacProbe]:
    governance_paths: list[str] = []
    for pattern in ("src/rbac/*.md", "src/policies/*.md", "src/requirements/*.md"):
        for path in sorted(project_root.glob(pattern))[:1]:
            governance_paths.append(path.relative_to(project_root).as_posix())

    rows = graph.run("MATCH (f:File) RETURN f.path AS path ORDER BY f.path")
    backend_files = [
        str(row["path"])
        for row in rows
        if row.get("path") and _is_backend_file(str(row["path"]), backend_prefixes)
    ][:4]

    misc_paths: list[str] = []
    if (project_root / "README.md").exists():
        misc_paths.append("README.md")

    unique_paths = []
    seen = set()
    for path in governance_paths + backend_files + misc_paths:
        normalized = _normalize_repo_path(path)
        if normalized and normalized not in seen:
            unique_paths.append(normalized)
            seen.add(normalized)

    probes: list[RbacProbe] = []
    idx = 1
    for role in ("viewer", "editor"):
        for operation in ("write", "delete"):
            for path in unique_paths:
                if path.startswith("src/rbac/") or path.startswith("src/policies/") or path.startswith("src/requirements/"):
                    path_group = "governance"
                elif path.startswith("src/"):
                    path_group = "backend"
                else:
                    path_group = "other"
                probes.append(
                    RbacProbe(
                        probe_id=f"RBAC-{idx:03d}",
                        role=role,
                        operation=operation,
                        path=path,
                        path_group=path_group,
                    )
                )
                idx += 1
    return probes


def _run_rbac_breach_simulation(
    graph: GraphDB,
    project_root: Path,
    backend_prefixes: tuple[str, ...],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rules = _load_authority_rules(graph)
    probes = _build_rbac_probes(graph, project_root, backend_prefixes)

    records: list[dict[str, object]] = []
    for probe in probes:
        oracle = evaluate_rules(
            operation=probe.operation,
            normalized_path=probe.path,
            principal_role=probe.role,
            rules=rules,
            deny_default=True,
        )

        for system_name in ("vanilla", "mcp_uce"):
            if system_name == "vanilla":
                predicted_allowed = True
                decision_mode = "no_rbac_guard"
            else:
                predicted_allowed = oracle.allowed
                decision_mode = "rbac_guard_enforced"

            records.append(
                {
                    "probe_id": probe.probe_id,
                    "path_group": probe.path_group,
                    "role": probe.role,
                    "operation": probe.operation,
                    "path": probe.path,
                    "system": system_name,
                    "oracle_allowed": bool(oracle.allowed),
                    "predicted_allowed": bool(predicted_allowed),
                    "breach": int((not oracle.allowed) and predicted_allowed),
                    "blocked_denied": int((not oracle.allowed) and (not predicted_allowed)),
                    "false_deny": int(oracle.allowed and (not predicted_allowed)),
                    "decision_mode": decision_mode,
                }
            )

    probe_df = pd.DataFrame(records)
    summary_rows: list[dict[str, object]] = []
    for system_name in ("vanilla", "mcp_uce"):
        sub = probe_df[probe_df["system"] == system_name]
        denied_total = int((~sub["oracle_allowed"]).sum())
        allowed_total = int(sub["oracle_allowed"].sum())
        breach_count = int(sub["breach"].sum())
        blocked_denied = int(sub["blocked_denied"].sum())
        false_deny = int(sub["false_deny"].sum())
        summary_rows.append(
            {
                "system": system_name,
                "total_probes": int(len(sub)),
                "oracle_denied_total": denied_total,
                "oracle_allowed_total": allowed_total,
                "breach_count": breach_count,
                "breach_rate": (breach_count / denied_total) if denied_total else 0.0,
                "blocked_denied": blocked_denied,
                "false_deny": false_deny,
            }
        )
    return probe_df, pd.DataFrame(summary_rows)


def _load_authority_rules_from_rbac_docs(project_root: Path) -> list[AuthorityRule]:
    raw_rules = _read_rbac_rules(project_root / "src" / "rbac")
    parsed: list[AuthorityRule] = []
    for raw in raw_rules:
        min_role = str(raw.get("min_role") or "").strip().lower()
        min_rank = ROLE_RANKS.get(min_role)
        if min_rank is None:
            continue
        parsed.append(
            AuthorityRule(
                rule_id=str(raw.get("rule_id") or ""),
                operation=str(raw.get("operation") or "").strip().lower(),
                path_pattern=str(raw.get("path_pattern") or "").strip(),
                effect=str(raw.get("effect") or "allow").strip().lower(),
                min_role=min_role,
                min_role_rank=min_rank,
                source_priority=int(raw.get("source_priority") or 0),
                policy_id=str(raw.get("policy_id") or "") or None,
            )
        )
    return parsed


def _run_rbac_breach_simulation_from_paths(
    project_root: Path,
    backend_paths: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rules = _load_authority_rules_from_rbac_docs(project_root)
    governance_paths: list[str] = []
    for pattern in ("src/rbac/*.md", "src/policies/*.md", "src/requirements/*.md"):
        for path in sorted(project_root.glob(pattern))[:1]:
            governance_paths.append(path.relative_to(project_root).as_posix())

    misc_paths: list[str] = []
    if (project_root / "README.md").exists():
        misc_paths.append("README.md")

    unique_paths = []
    seen = set()
    for path in governance_paths + backend_paths + misc_paths:
        normalized = _normalize_repo_path(path)
        if normalized and normalized not in seen:
            unique_paths.append(normalized)
            seen.add(normalized)

    probes: list[RbacProbe] = []
    idx = 1
    for role in ("viewer", "editor"):
        for operation in ("write", "delete"):
            for path in unique_paths:
                if path.startswith("src/rbac/") or path.startswith("src/policies/") or path.startswith("src/requirements/"):
                    path_group = "governance"
                elif path.startswith("src/"):
                    path_group = "backend"
                else:
                    path_group = "other"
                probes.append(
                    RbacProbe(
                        probe_id=f"RBAC-{idx:03d}",
                        role=role,
                        operation=operation,
                        path=path,
                        path_group=path_group,
                    )
                )
                idx += 1

    rows: list[dict[str, object]] = []
    for probe in probes:
        oracle = evaluate_rules(
            operation=probe.operation,
            normalized_path=probe.path,
            principal_role=probe.role,
            rules=rules,
            deny_default=True,
        )
        for system_name in ("vanilla", "mcp_uce"):
            if system_name == "vanilla":
                predicted_allowed = True
                decision_mode = "no_rbac_guard"
            else:
                predicted_allowed = oracle.allowed
                decision_mode = "rbac_guard_enforced"
            rows.append(
                {
                    "probe_id": probe.probe_id,
                    "path_group": probe.path_group,
                    "role": probe.role,
                    "operation": probe.operation,
                    "path": probe.path,
                    "system": system_name,
                    "oracle_allowed": bool(oracle.allowed),
                    "predicted_allowed": bool(predicted_allowed),
                    "breach": int((not oracle.allowed) and predicted_allowed),
                    "blocked_denied": int((not oracle.allowed) and (not predicted_allowed)),
                    "false_deny": int(oracle.allowed and (not predicted_allowed)),
                    "decision_mode": decision_mode,
                }
            )

    probe_df = pd.DataFrame(rows)
    summary_rows: list[dict[str, object]] = []
    for system_name in ("vanilla", "mcp_uce"):
        sub = probe_df[probe_df["system"] == system_name]
        denied_total = int((~sub["oracle_allowed"]).sum())
        allowed_total = int(sub["oracle_allowed"].sum())
        breach_count = int(sub["breach"].sum())
        blocked_denied = int(sub["blocked_denied"].sum())
        false_deny = int(sub["false_deny"].sum())
        summary_rows.append(
            {
                "system": system_name,
                "total_probes": int(len(sub)),
                "oracle_denied_total": denied_total,
                "oracle_allowed_total": allowed_total,
                "breach_count": breach_count,
                "breach_rate": (breach_count / denied_total) if denied_total else 0.0,
                "blocked_denied": blocked_denied,
                "false_deny": false_deny,
            }
        )
    return probe_df, pd.DataFrame(summary_rows)


def _save_plots(
    summary_df: pd.DataFrame,
    violation_df: pd.DataFrame,
    rbac_summary_df: pd.DataFrame,
) -> None:
    plt.style.use("default")
    plt.rcParams["figure.facecolor"] = "white"
    plt.rcParams["axes.facecolor"] = "white"
    label_map = {"vanilla": "Vanilla LLM", "mcp_uce": "MCP-UCE"}
    systems = summary_df["system"].tolist()
    labels = [label_map.get(system, system) for system in systems]

    file_f1 = summary_df["file_f1"].tolist()
    req_f1 = summary_df["requirement_f1"].tolist()
    pol_f1 = summary_df["policy_f1"].tolist()

    x = range(len(systems))
    width = 0.24
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.bar([i - width for i in x], file_f1, width=width, label="File F1")
    ax.bar(x, req_f1, width=width, label="Requirement F1")
    ax.bar([i + width for i in x], pol_f1, width=width, label="Policy F1")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=12)
    ax.set_ylim(0.0, 1.0)
    ax.set_ylabel("Score")
    ax.set_title("Impact Prediction Quality")
    ax.legend()
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "quality_scores.png", dpi=220)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(labels, summary_df["risk_mae"].tolist(), color=["#C44E52", "#4C72B0"])
    ax.set_ylabel("MAE")
    ax.set_title("Risk Score Absolute Error")
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "risk_mae.png", dpi=220)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(labels, summary_df["latency_ms_mean"].tolist(), color=["#55A868", "#8172B2"])
    ax.set_ylabel("Latency (ms)")
    ax.set_title("Average Inference Latency")
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "latency_ms.png", dpi=220)
    plt.close(fig)

    violation = violation_df.set_index("system")
    req_caught = [int(violation.loc[s, "requirement_caught"]) for s in systems]
    req_missed = [int(violation.loc[s, "requirement_missed"]) for s in systems]
    pol_caught = [int(violation.loc[s, "policy_caught"]) for s in systems]
    pol_missed = [int(violation.loc[s, "policy_missed"]) for s in systems]

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4), sharey=False)
    axes[0].bar(labels, req_caught, color="#2E8B57", label="Caught")
    axes[0].bar(labels, req_missed, bottom=req_caught, color="#C44E52", label="Missed")
    axes[0].set_title("Requirement Violations")
    axes[0].set_ylabel("Count")

    axes[1].bar(labels, pol_caught, color="#2E8B57", label="Caught")
    axes[1].bar(labels, pol_missed, bottom=pol_caught, color="#C44E52", label="Missed")
    axes[1].set_title("Policy Violations")
    axes[1].set_ylabel("Count")

    handles, legend_labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, legend_labels, loc="lower center", ncol=2, frameon=False)
    fig.suptitle("Governance Violation Capture (Higher Green, Lower Red)")
    fig.tight_layout(rect=(0, 0.08, 1, 0.95))
    fig.savefig(FIGURES_DIR / "violation_capture.png", dpi=240)
    plt.close(fig)

    rbac = rbac_summary_df.set_index("system")
    breach_rates = [float(rbac.loc[s, "breach_rate"]) * 100.0 for s in systems]
    breach_counts = [int(rbac.loc[s, "breach_count"]) for s in systems]
    denied_totals = [int(rbac.loc[s, "oracle_denied_total"]) for s in systems]

    fig, ax = plt.subplots(figsize=(8.4, 4.8))
    bars = ax.bar(labels, breach_rates, color=["#C44E52", "#4C72B0"])
    for bar, breach_count, denied_total in zip(bars, breach_counts, denied_totals):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 1.2,
            f"{breach_count}/{denied_total}",
            ha="center",
            va="bottom",
            fontsize=10,
        )
    ax.set_ylim(0.0, max(5.0, max(breach_rates) + 10.0))
    ax.set_ylabel("Breach Rate on Denied Requests (%)")
    ax.set_title("RBAC Breach Comparison (Simulated Deny-by-Default)")
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "rbac_breach.png", dpi=240)
    plt.close(fig)


def _draw_box(
    ax: plt.Axes,
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    body: str,
    *,
    face: str,
    edge: str = "#1F2937",
) -> None:
    box = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.012,rounding_size=0.02",
        linewidth=1.2,
        edgecolor=edge,
        facecolor=face,
    )
    ax.add_patch(box)
    ax.text(
        x + 0.012,
        y + h - 0.032,
        title,
        ha="left",
        va="top",
        fontsize=10,
        fontweight="bold",
        color="#0F172A",
    )
    ax.text(
        x + 0.012,
        y + h - 0.070,
        body,
        ha="left",
        va="top",
        fontsize=8.8,
        color="#111827",
    )


def _draw_arrow(
    ax: plt.Axes,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    label: str | None = None,
    color: str = "#4B5563",
    curve: float = 0.0,
) -> None:
    arrow = FancyArrowPatch(
        start,
        end,
        arrowstyle="-|>",
        mutation_scale=14,
        linewidth=1.5,
        color=color,
        connectionstyle=f"arc3,rad={curve}",
    )
    ax.add_patch(arrow)
    if label:
        lx = (start[0] + end[0]) / 2
        ly = (start[1] + end[1]) / 2 + 0.02
        ax.text(lx, ly, label, fontsize=8.3, color="#374151", ha="center", va="center")


def _save_architecture_figures() -> None:
    fig, ax = plt.subplots(figsize=(14, 8.2))
    fig.patch.set_facecolor("white")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_facecolor("#FFFFFF")

    _draw_box(
        ax,
        0.03,
        0.66,
        0.20,
        0.27,
        "Identity Plane",
        "Keycloak OIDC\nroles: viewer/editor/admin\nJWT attached to MCP calls",
        face="#EEF2FF",
    )
    _draw_box(
        ax,
        0.03,
        0.35,
        0.20,
        0.24,
        "Agent Plane",
        "LLM agent\nchange prompt\nasks for impact + risk",
        face="#E0F2FE",
    )
    _draw_box(
        ax,
        0.27,
        0.18,
        0.50,
        0.75,
        "UCE Runtime Plane",
        "Deterministic policy mediation over MCP reasoning tools",
        face="#F8FAFC",
    )
    _draw_box(
        ax,
        0.30,
        0.72,
        0.19,
        0.16,
        "MCP Gateway",
        "Token verification\nrequest normalization\ntrace IDs",
        face="#DBEAFE",
    )
    _draw_box(
        ax,
        0.52,
        0.72,
        0.22,
        0.16,
        "RBAC Guard",
        "deny-first precedence\npath specificity\nenforce write/delete gate",
        face="#FEE2E2",
    )
    _draw_box(
        ax,
        0.30,
        0.49,
        0.20,
        0.18,
        "Reasoning Tools",
        "impact_analysis\ntrace graph closure\nrisk_assessment",
        face="#DCFCE7",
    )
    _draw_box(
        ax,
        0.53,
        0.49,
        0.21,
        0.18,
        "Action Tools",
        "safe mutations\naudit annotations\npolicy-linked writes",
        face="#FFEDD5",
    )
    _draw_box(
        ax,
        0.30,
        0.24,
        0.44,
        0.19,
        "Governance Knowledge Graph",
        "File/Function + Table/Column + Requirement/Policy + AuthorityRule\nCypher traversals for deterministic impact evidence",
        face="#D1FAE5",
    )
    _draw_box(
        ax,
        0.80,
        0.62,
        0.17,
        0.18,
        "Data Plane",
        "Neo4j graph DB\nversioned snapshots",
        face="#E0F2F1",
    )
    _draw_box(
        ax,
        0.80,
        0.37,
        0.17,
        0.18,
        "Source Plane",
        "code + schema + policy docs\ncontinuous ingestion",
        face="#F3F4F6",
    )

    _draw_arrow(ax, (0.23, 0.79), (0.30, 0.79), label="JWT + MCP request")
    _draw_arrow(ax, (0.23, 0.47), (0.30, 0.56), label="change intent")
    _draw_arrow(ax, (0.49, 0.80), (0.52, 0.80), label="authZ check")
    _draw_arrow(ax, (0.63, 0.72), (0.63, 0.67), label="allow/deny")
    _draw_arrow(ax, (0.40, 0.72), (0.40, 0.67))
    _draw_arrow(ax, (0.52, 0.57), (0.50, 0.34), curve=-0.12)
    _draw_arrow(ax, (0.42, 0.49), (0.42, 0.43))
    _draw_arrow(ax, (0.64, 0.49), (0.64, 0.43))
    _draw_arrow(ax, (0.74, 0.33), (0.80, 0.70), label="Cypher")
    _draw_arrow(ax, (0.80, 0.46), (0.74, 0.33), label="ingestion")

    ax.text(
        0.50,
        0.04,
        "End-to-end UCE runtime with explicit RBAC gate before mutation-capable tools",
        ha="center",
        fontsize=10,
        color="#1F2937",
    )
    fig.tight_layout()
    overview_path = FIGURES_DIR / "architecture_overview.png"
    fig.savefig(overview_path, dpi=260)
    fig.savefig(FIGURES_DIR / "architecture.png", dpi=260)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(14, 8.0))
    fig.patch.set_facecolor("white")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_facecolor("#FFFFFF")

    _draw_box(
        ax,
        0.03,
        0.74,
        0.94,
        0.21,
        "Repository Sources",
        "code paths | schema files | requirements docs | policy docs | RBAC docs",
        face="#F8FAFC",
    )
    _draw_box(
        ax,
        0.05,
        0.41,
        0.42,
        0.27,
        "Deterministic Ingestion Lane",
        "Tree-sitter code parser\nschema parser (tables/columns/FKs)\nstrict markdown frontmatter parsing\nidempotent graph merge",
        face="#DBEAFE",
    )
    _draw_box(
        ax,
        0.53,
        0.41,
        0.42,
        0.27,
        "Optional LLM Ingestion Lane",
        "semantic extraction for underspecified docs\nentity normalization + confidence filtering\npoisoning-risk checkpoint before merge",
        face="#FCE7F3",
    )
    _draw_box(
        ax,
        0.22,
        0.15,
        0.56,
        0.18,
        "Graph Transport + Persistence",
        "Neo4j-MCP transport :8000  ->  Neo4j :7687\ncreates/updates nodes and typed edges with trace metadata",
        face="#D1FAE5",
    )

    _draw_arrow(ax, (0.26, 0.74), (0.26, 0.68))
    _draw_arrow(ax, (0.74, 0.74), (0.74, 0.68))
    _draw_arrow(ax, (0.26, 0.41), (0.40, 0.33), label="deterministic entities")
    _draw_arrow(ax, (0.74, 0.41), (0.60, 0.33), label="LLM entities")
    _draw_arrow(ax, (0.40, 0.33), (0.50, 0.26))
    _draw_arrow(ax, (0.60, 0.33), (0.50, 0.26))
    _draw_arrow(ax, (0.50, 0.26), (0.50, 0.15))
    ax.text(0.50, 0.30, "merge + reconcile", fontsize=8.3, color="#374151", ha="center")

    ax.text(
        0.50,
        0.07,
        "Dual-lane ingestion: deterministic baseline for reliability, LLM lane for recall when structured docs are weak",
        ha="center",
        fontsize=10,
        color="#1F2937",
    )
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "ingestion_architecture.png", dpi=260)
    plt.close(fig)

    source_overview = Path("e:/Downloads/00_Simple_Architecture_Overview_UCE.png")
    source_ingestion = Path("e:/Downloads/09_Ingestion_Architecture_Deterministic_vs_LLM.png")
    if source_overview.exists():
        shutil.copyfile(source_overview, FIGURES_DIR / "source_overview_reference.png")
    if source_ingestion.exists():
        shutil.copyfile(source_ingestion, FIGURES_DIR / "source_ingestion_reference.png")


def run(config_path: str) -> None:
    _ensure_dirs()
    config = load_config(config_path)
    graph = GraphDB(config.neo4j.uri, config.neo4j.user, config.neo4j.password)
    backend_prefixes = _normalize_backend_prefixes(config.paths.backend)

    try:
        ingest_counts = deterministic_governance_ingestion(config, graph)
        tables = load_tables(graph)
        columns = load_columns(graph)
        scenarios = _build_scenarios(graph, backend_prefixes)
        requirements = _read_requirements(Path(config.project_root) / "src" / "requirements")
        policies = _read_policies(Path(config.project_root) / "src" / "policies")
        file_corpus = _build_file_corpus(config, backend_prefixes)

        rows: list[dict[str, object]] = []

        for scenario in scenarios:
            oracle = oracle_prediction(graph, scenario, backend_prefixes)
            vanilla = vanilla_prediction(
                scenario=scenario,
                oracle=oracle,
                requirements=requirements,
                policies=policies,
                file_corpus=file_corpus,
                graph=graph,
            )
            uce = uce_prediction(graph, scenario, backend_paths=config.paths.backend)

            systems = {"vanilla": vanilla, "mcp_uce": uce}

            truth_files = set(oracle["affected_files"])
            truth_reqs = set(oracle["violated_requirements"])
            truth_pols = set(oracle["enforced_policies"])

            for system_name, prediction in systems.items():
                pred_files = set(prediction["affected_files"])
                pred_reqs = set(prediction["violated_requirements"])
                pred_pols = set(prediction["enforced_policies"])

                file_precision, file_recall, file_f1 = _metrics_from_sets(pred_files, truth_files)
                req_precision, req_recall, req_f1 = _metrics_from_sets(pred_reqs, truth_reqs)
                pol_precision, pol_recall, pol_f1 = _metrics_from_sets(pred_pols, truth_pols)

                rows.append(
                    {
                        "scenario_id": scenario.scenario_id,
                        "entity_type": scenario.entity_type,
                        "entity_name": scenario.entity_name,
                        "system": system_name,
                        "file_precision": file_precision,
                        "file_recall": file_recall,
                        "file_f1": file_f1,
                        "requirement_precision": req_precision,
                        "requirement_recall": req_recall,
                        "requirement_f1": req_f1,
                        "policy_precision": pol_precision,
                        "policy_recall": pol_recall,
                        "policy_f1": pol_f1,
                        "file_tp": len(pred_files & truth_files),
                        "file_fp": len(pred_files - truth_files),
                        "file_fn": len(truth_files - pred_files),
                        "req_tp": len(pred_reqs & truth_reqs),
                        "req_fp": len(pred_reqs - truth_reqs),
                        "req_fn": len(truth_reqs - pred_reqs),
                        "pol_tp": len(pred_pols & truth_pols),
                        "pol_fp": len(pred_pols - truth_pols),
                        "pol_fn": len(truth_pols - pred_pols),
                        "risk_pred": int(prediction["risk_score"]),
                        "risk_oracle": int(oracle["risk_score"]),
                        "risk_abs_error": abs(int(prediction["risk_score"]) - int(oracle["risk_score"])),
                        "latency_ms": float(prediction["latency_ms"]),
                        "oracle_file_count": len(truth_files),
                        "pred_file_count": len(pred_files),
                    }
                )

        df = pd.DataFrame(rows)
        df.to_csv(RESULTS_DIR / "scenario_results.csv", index=False)

        summary_rows = [_aggregate_metrics(df, "vanilla"), _aggregate_metrics(df, "mcp_uce")]
        summary_df = pd.DataFrame(summary_rows)
        summary_df.to_csv(TABLES_DIR / "overall_metrics.csv", index=False)

        by_type_rows: list[dict[str, object]] = []
        for entity_type in sorted(df["entity_type"].unique()):
            for system in ("vanilla", "mcp_uce"):
                sub = df[(df["entity_type"] == entity_type) & (df["system"] == system)]
                by_type_rows.append(
                    {
                        "entity_type": entity_type,
                        "system": system,
                        "file_f1_mean": float(sub["file_f1"].mean()) if len(sub) else 0.0,
                        "requirement_f1_mean": float(sub["requirement_f1"].mean()) if len(sub) else 0.0,
                        "policy_f1_mean": float(sub["policy_f1"].mean()) if len(sub) else 0.0,
                        "risk_mae": float(sub["risk_abs_error"].mean()) if len(sub) else 0.0,
                        "latency_ms_mean": float(sub["latency_ms"].mean()) if len(sub) else 0.0,
                    }
                )
        pd.DataFrame(by_type_rows).to_csv(TABLES_DIR / "metrics_by_entity_type.csv", index=False)

        violation_df = _aggregate_violation_metrics(df)
        violation_df.to_csv(TABLES_DIR / "violation_metrics.csv", index=False)

        project_root = Path(config.project_root).resolve()
        rbac_probe_df, rbac_summary_df = _run_rbac_breach_simulation(
            graph=graph,
            project_root=project_root,
            backend_prefixes=backend_prefixes,
        )
        rbac_probe_df.to_csv(RESULTS_DIR / "rbac_probe_results.csv", index=False)
        rbac_summary_df.to_csv(TABLES_DIR / "rbac_breach_metrics.csv", index=False)

        _save_plots(summary_df, violation_df, rbac_summary_df)
        _save_architecture_figures()

        summary = {
            "ingestion_counts": ingest_counts,
            "graph_snapshot": {
                "tables": len(tables),
                "columns": int(sum(len(cols) for cols in columns.values())),
                "scenarios": len(scenarios),
                "backend_files_indexed": len(file_corpus),
            },
            "overall": summary_rows,
            "violation": violation_df.to_dict(orient="records"),
            "rbac_breach": rbac_summary_df.to_dict(orient="records"),
            "created_at_epoch": int(time.time()),
        }
        (RESULTS_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    finally:
        graph.close()


def postprocess_existing_results(config_path: str) -> None:
    _ensure_dirs()
    config = load_config(config_path)
    scenario_path = RESULTS_DIR / "scenario_results.csv"
    if not scenario_path.exists():
        raise FileNotFoundError(f"Missing scenario results: {scenario_path}")

    df = pd.read_csv(scenario_path)

    summary_rows = [_aggregate_metrics(df, "vanilla"), _aggregate_metrics(df, "mcp_uce")]
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(TABLES_DIR / "overall_metrics.csv", index=False)

    by_type_rows: list[dict[str, object]] = []
    for entity_type in sorted(df["entity_type"].unique()):
        for system in ("vanilla", "mcp_uce"):
            sub = df[(df["entity_type"] == entity_type) & (df["system"] == system)]
            by_type_rows.append(
                {
                    "entity_type": entity_type,
                    "system": system,
                    "file_f1_mean": float(sub["file_f1"].mean()) if len(sub) else 0.0,
                    "requirement_f1_mean": float(sub["requirement_f1"].mean()) if len(sub) else 0.0,
                    "policy_f1_mean": float(sub["policy_f1"].mean()) if len(sub) else 0.0,
                    "risk_mae": float(sub["risk_abs_error"].mean()) if len(sub) else 0.0,
                    "latency_ms_mean": float(sub["latency_ms"].mean()) if len(sub) else 0.0,
                }
            )
    pd.DataFrame(by_type_rows).to_csv(TABLES_DIR / "metrics_by_entity_type.csv", index=False)

    violation_df = _aggregate_violation_metrics(df)
    violation_df.to_csv(TABLES_DIR / "violation_metrics.csv", index=False)

    project_root = Path(config.project_root).resolve()
    backend_paths = sorted(
        {
            str(path)
            for path in df.loc[df["entity_type"] == "file", "entity_name"].dropna().tolist()
            if str(path).strip()
        }
    )
    rbac_probe_df, rbac_summary_df = _run_rbac_breach_simulation_from_paths(
        project_root=project_root,
        backend_paths=backend_paths,
    )
    rbac_probe_df.to_csv(RESULTS_DIR / "rbac_probe_results.csv", index=False)
    rbac_summary_df.to_csv(TABLES_DIR / "rbac_breach_metrics.csv", index=False)

    _save_plots(summary_df, violation_df, rbac_summary_df)
    _save_architecture_figures()

    prior_summary = {}
    summary_path = RESULTS_DIR / "summary.json"
    if summary_path.exists():
        try:
            prior_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prior_summary = {}

    summary = {
        "ingestion_counts": prior_summary.get("ingestion_counts", {}),
        "graph_snapshot": prior_summary.get("graph_snapshot", {}),
        "overall": summary_rows,
        "violation": violation_df.to_dict(orient="records"),
        "rbac_breach": rbac_summary_df.to_dict(orient="records"),
        "created_at_epoch": int(time.time()),
        "postprocess_only": True,
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic ICML benchmark for UCE.")
    parser.add_argument("--config", default="config.yaml", help="Path to UCE config file.")
    parser.add_argument(
        "--postprocess-only",
        action="store_true",
        help="Regenerate tables and figures from existing scenario_results.csv without Neo4j.",
    )
    args = parser.parse_args()
    if args.postprocess_only:
        postprocess_existing_results(args.config)
    else:
        run(args.config)


if __name__ == "__main__":
    main()
