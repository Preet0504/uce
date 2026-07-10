from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from uce.core.config import UceConfig, load_config
from uce.core.rbac import ROLE_RANKS, AuthorityRule, evaluate_rules
from uce.ingestion.code_parser import detect_language, parse_file
from uce.ingestion.graph_builder import is_ignored, resolve_import
from uce.ingestion.schema_parser import parse_schema_file


BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "results"
REAL_DIR = RESULTS_DIR / "real_llm_baseline"
FIGURES_DIR = RESULTS_DIR / "figures"

REQ_ID_RE = re.compile(r"\bRQ-\d{3}\b", re.IGNORECASE)
POL_ID_RE = re.compile(r"\bP-\d{3}\b", re.IGNORECASE)
IMPORT_RE = re.compile(
    r"(?:from\s+['\"]([^'\"]+)['\"]|import\s*\(\s*['\"]([^'\"]+)['\"]\s*\)|require\s*\(\s*['\"]([^'\"]+)['\"]\s*\))"
)


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
    for path in (RESULTS_DIR, REAL_DIR, FIGURES_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


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
            if parsed:
                values[parsed[0]] = parsed[1]
        req_id = values.get("id", path.stem).strip().upper()
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
            if parsed:
                values[parsed[0]] = parsed[1]
        policy_id = values.get("id", path.stem).strip().upper()
        description = values.get("description", "").strip()
        requirement_ids = [
            token.strip().upper()
            for token in values.get("enforces", "").split(",")
            if token.strip()
        ]
        records.append(
            PolicyRecord(
                policy_id=policy_id,
                description=description,
                requirement_ids=requirement_ids,
            )
        )
    return records


def _normalize_rule(policy_id: str, raw: dict[str, str]) -> dict[str, object]:
    try:
        priority = int(raw.get("source_priority", "0"))
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
        stripped = raw.strip()
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
        if parsed:
            current[parsed[0]] = parsed[1]
    if current:
        rules.append(_normalize_rule(policy_id, current))
    return rules


def _authority_rules(project_root: Path) -> list[AuthorityRule]:
    parsed: list[AuthorityRule] = []
    for raw in _read_rbac_rules(project_root / "src" / "rbac"):
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


def _word_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")


def _normalize_repo_path(path: str) -> str:
    normalized = (path or "").replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _normalize_backend_prefixes(backend_paths: Iterable[str] | None) -> tuple[str, ...]:
    if not backend_paths:
        return tuple()
    normalized = {_normalize_repo_path(str(raw)).lower() for raw in backend_paths}
    return tuple(sorted(path for path in normalized if path))


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
    return sorted({_normalize_repo_path(path) for path in paths if _is_backend_file(path, backend_prefixes)})


class GraphlessOracle:
    """A deterministic oracle that mirrors the current graph ingestion rules without Neo4j."""

    def __init__(
        self,
        config: UceConfig,
        tables: list[dict[str, object]],
        requirements: list[RequirementRecord],
        policies: list[PolicyRecord],
    ) -> None:
        self.config = config
        self.project_root = Path(config.project_root)
        self.backend_prefixes = _normalize_backend_prefixes(config.paths.backend)
        self.tables = {str(table["name"]): list(table.get("columns") or []) for table in tables}
        self.requirements = requirements
        self.policies = policies
        self.policy_by_req: dict[str, set[str]] = {}
        for policy in policies:
            for req_id in policy.requirement_ids:
                self.policy_by_req.setdefault(req_id, set()).add(policy.policy_id)

        self.req_to_tables: dict[str, set[str]] = {}
        self.req_to_columns: dict[str, set[tuple[str, str]]] = {}
        self.file_text: dict[str, str] = {}
        self.file_functions: dict[str, set[str]] = {}
        self.file_tables: dict[str, set[str]] = {}
        self.file_columns: dict[str, set[tuple[str, str]]] = {}
        self.imports: dict[str, set[str]] = {}
        self.reverse_imports: dict[str, set[str]] = {}

        self._link_requirements()
        self._scan_files()

    def _link_requirements(self) -> None:
        table_patterns = {name: _word_pattern(name) for name in self.tables}
        column_patterns = {
            table: {name: _word_pattern(name) for name in columns}
            for table, columns in self.tables.items()
        }
        for requirement in self.requirements:
            text = f"{requirement.title} {requirement.description}"
            for table, pattern in table_patterns.items():
                if pattern.search(text):
                    self.req_to_tables.setdefault(requirement.req_id, set()).add(table)
            for table, patterns in column_patterns.items():
                for column, pattern in patterns.items():
                    if pattern.search(text):
                        self.req_to_columns.setdefault(requirement.req_id, set()).add((table, column))

    def _iter_code_files(self) -> Iterable[Path]:
        extensions = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java", ".c", ".cpp"}
        for code_path in self.config.paths.code:
            start = self.project_root / code_path
            if not start.exists():
                continue
            for path in start.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in extensions:
                    continue
                rel = path.relative_to(self.project_root).as_posix()
                if is_ignored(rel, self.config.ignore):
                    continue
                if not _is_backend_file(rel, self.backend_prefixes):
                    continue
                yield path

    def _scan_files(self) -> None:
        table_patterns = {name: _word_pattern(name) for name in self.tables}
        column_patterns = {
            table: {name: _word_pattern(name) for name in columns}
            for table, columns in self.tables.items()
        }
        extensions = tuple(f".{ext}" for ext in sorted({*self.config.languages, "ts", "tsx", "js", "jsx", "py"}))

        for path in self._iter_code_files():
            rel = path.relative_to(self.project_root).as_posix()
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.file_text[rel] = text

            tables = {name for name, pattern in table_patterns.items() if pattern.search(text)}
            columns: set[tuple[str, str]] = set()
            for table, patterns in column_patterns.items():
                for column, pattern in patterns.items():
                    if pattern.search(text):
                        columns.add((table, column))
            self.file_tables[rel] = tables
            self.file_columns[rel] = columns

            imports = self._imports_for_file(path, rel, extensions)
            self.imports[rel] = imports
            for target in imports:
                self.reverse_imports.setdefault(target, set()).add(rel)

    def _imports_for_file(self, path: Path, rel: str, extensions: tuple[str, ...]) -> set[str]:
        imported_values: set[str] = set()
        try:
            parsed = parse_file(str(path), collect_identifiers=False)
            if parsed:
                imported_values.update(parsed.imports)
                self.file_functions[rel] = set(parsed.functions) | {name for name, _ in parsed.methods}
        except Exception:
            self.file_functions[rel] = set()

        text = self.file_text.get(rel, "")
        for match in IMPORT_RE.finditer(text):
            imported_values.update(token for token in match.groups() if token)

        resolved: set[str] = set()
        for imported in imported_values:
            target = resolve_import(
                source_rel=rel,
                import_path=imported,
                project_root=str(self.project_root),
                aliases=self.config.aliases,
                extensions=extensions,
            )
            if target and not is_ignored(target, self.config.ignore):
                resolved.add(_normalize_repo_path(target))
        return resolved

    def reverse_import_closure(self, direct_files: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        queue = [_normalize_repo_path(path) for path in direct_files if path]
        while queue:
            current = queue.pop(0)
            for parent in sorted(self.reverse_imports.get(current, set())):
                if parent in seen:
                    continue
                seen.add(parent)
                queue.append(parent)
        return sorted(seen)

    def requirements_for_table(self, table: str) -> list[str]:
        reqs: set[str] = set()
        for req_id, tables in self.req_to_tables.items():
            if table in tables:
                reqs.add(req_id)
        for req_id, columns in self.req_to_columns.items():
            if any(col_table == table for col_table, _ in columns):
                reqs.add(req_id)
        return sorted(reqs)

    def requirements_for_column(self, table: str, column: str) -> list[str]:
        reqs = {
            req_id
            for req_id, columns in self.req_to_columns.items()
            if (table, column) in columns
        }
        return sorted(reqs)

    def requirements_for_files(self, files: Iterable[str]) -> list[str]:
        reqs: set[str] = set()
        file_set = {_normalize_repo_path(path) for path in files if path}
        referenced_tables: set[str] = set()
        referenced_columns: set[tuple[str, str]] = set()
        for path in file_set:
            referenced_tables.update(self.file_tables.get(path, set()))
            referenced_columns.update(self.file_columns.get(path, set()))
        for req_id, tables in self.req_to_tables.items():
            if tables & referenced_tables:
                reqs.add(req_id)
        for req_id, columns in self.req_to_columns.items():
            if columns & referenced_columns:
                reqs.add(req_id)
        return sorted(reqs)

    def policies_for_requirements(self, req_ids: Iterable[str]) -> list[str]:
        policies: set[str] = set()
        for req_id in req_ids:
            policies.update(self.policy_by_req.get(req_id, set()))
        return sorted(policies)

    def impacted_files(self, scenario: Scenario) -> list[str]:
        if scenario.entity_type == "table":
            table = scenario.entity_name
            columns = set(self.tables.get(table, []))
            direct = {
                path
                for path, tables in self.file_tables.items()
                if table in tables
            }
            direct.update(
                path
                for path, cols in self.file_columns.items()
                if any(col_table == table and col_name in columns for col_table, col_name in cols)
            )
        elif scenario.entity_type == "column":
            table, column = scenario.entity_name.split(".", 1)
            direct = {
                path
                for path, cols in self.file_columns.items()
                if (table, column) in cols
            }
        elif scenario.entity_type == "file":
            direct = {_normalize_repo_path(scenario.entity_name)}
        else:
            direct = set()
        affected = set(direct) | set(self.reverse_import_closure(direct))
        return _filter_backend(affected, self.backend_prefixes)

    def prediction_for(self, scenario: Scenario) -> dict[str, object]:
        if scenario.entity_type == "table":
            requirements = self.requirements_for_table(scenario.entity_name)
        elif scenario.entity_type == "column":
            table, column = scenario.entity_name.split(".", 1)
            requirements = self.requirements_for_column(table, column)
        elif scenario.entity_type == "file":
            requirements = self.requirements_for_files(self.impacted_files(scenario))
        else:
            requirements = []
        policies = self.policies_for_requirements(requirements)
        files = self.impacted_files(scenario)
        function_count = sum(len(self.file_functions.get(path, set())) for path in files)
        return {
            "affected_files": files,
            "violated_requirements": requirements,
            "enforced_policies": policies,
            "affected_function_count": function_count,
        }


class ChatJsonClient:
    def __init__(self, provider: str, max_tokens: int) -> None:
        from openai import OpenAI

        self.provider = provider
        self.max_tokens = max_tokens
        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is not set.")
            self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            self.client = OpenAI(api_key=api_key)
        elif provider == "local":
            self.model = os.getenv("LOCAL_LLM_MODEL", "llama3:instruct")
            base_url = os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
            self.client = OpenAI(api_key=os.getenv("LOCAL_LLM_API_KEY") or "local", base_url=base_url)
        else:
            raise RuntimeError(f"Unsupported provider: {provider}")

    def generate_json_text(self, prompt: str) -> str:
        request: dict[str, object] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a no-tool software governance reviewer. "
                        "Return valid JSON only; do not include Markdown fences."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            response = self.client.chat.completions.create(**request)
        except Exception:
            request.pop("response_format", None)
            response = self.client.chat.completions.create(**request)
        return (response.choices[0].message.content or "").strip()


def _resolve_provider(provider: str, max_tokens: int) -> ChatJsonClient:
    if provider in {"openai", "local"}:
        return ChatJsonClient(provider, max_tokens=max_tokens)

    errors: list[str] = []
    for candidate in ("openai", "local"):
        try:
            client = ChatJsonClient(candidate, max_tokens=max_tokens)
            client.generate_json_text('Return {"ok": true}.')
            return client
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError("No LLM provider worked. " + " | ".join(errors))


def _extract_json(text: str) -> dict[str, object]:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {"_parse_error": text}
    return {"_parse_error": text}


def _collect_ids(value: object, pattern: re.Pattern[str]) -> list[str]:
    found: set[str] = set()

    def walk(item: object) -> None:
        if item is None:
            return
        if isinstance(item, str):
            found.update(match.group(0).upper() for match in pattern.finditer(item))
            return
        if isinstance(item, dict):
            for sub in item.values():
                walk(sub)
            return
        if isinstance(item, list):
            for sub in item:
                walk(sub)
            return

    walk(value)
    return sorted(found)


def _collect_files(value: object) -> list[str]:
    files: set[str] = set()

    def walk(item: object) -> None:
        if isinstance(item, str):
            normalized = _normalize_repo_path(item)
            if normalized.startswith("src/"):
                files.add(normalized)
        elif isinstance(item, dict):
            for sub in item.values():
                walk(sub)
        elif isinstance(item, list):
            for sub in item:
                walk(sub)

    walk(value)
    return sorted(files)


def _prediction_for_scenario(parsed: dict[str, object], scenario_id: str) -> object:
    predictions = parsed.get("predictions")
    if isinstance(predictions, list):
        for item in predictions:
            if isinstance(item, dict) and str(item.get("scenario_id") or "").strip() == scenario_id:
                return item
    if isinstance(predictions, dict):
        item = predictions.get(scenario_id)
        if item is not None:
            return item
    if str(parsed.get("scenario_id") or "").strip() == scenario_id:
        return parsed
    return {}


def _score_sets(predicted: Iterable[str], truth: Iterable[str]) -> dict[str, object]:
    pred = set(predicted)
    gold = set(truth)
    tp = len(pred & gold)
    fp = len(pred - gold)
    fn = len(gold - pred)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "caught_any": int(bool(gold) and tp > 0),
        "missed_all": int(bool(gold) and tp == 0),
        "truth_count": len(gold),
        "pred_count": len(pred),
    }


def _metric_from_counts(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return precision, recall, f1


def _scenario_prompt(
    scenario: Scenario,
    schema_context: str,
    requirement_context: str,
    policy_context: str,
    backend_inventory: str,
    file_context: str,
) -> str:
    return f"""
This is a no-tool vanilla LLM baseline. You cannot query MCP, Neo4j, grep, or the filesystem.
Use only the pasted repository context.

Benchmark meaning:
- "violated_requirements" means requirements that could be violated or regressed by the change and must be checked.
- "enforced_policies" means policy IDs whose enforced requirements are implicated.
- Return exact IDs only when the pasted evidence supports them.

Scenario:
- scenario_id: {scenario.scenario_id}
- entity_type: {scenario.entity_type}
- entity_name: {scenario.entity_name}
- task: {scenario.prompt}

Database schema:
{schema_context}

Requirements:
{requirement_context}

Policies:
{policy_context}

Backend/source file inventory, path names only:
{backend_inventory}

Scenario file context, if any:
{file_context}

Return JSON exactly with this shape:
{{
  "scenario_id": "{scenario.scenario_id}",
  "affected_files": ["src/..."],
  "violated_requirements": ["RQ-001"],
  "enforced_policies": ["P-001"],
  "rationale": "one short paragraph"
}}
""".strip()


def _scenario_batch_prompt(
    scenarios: list[Scenario],
    schema_context: str,
    requirement_context: str,
    policy_context: str,
    backend_inventory: str,
) -> str:
    scenario_lines = "\n".join(
        f"- {scenario.scenario_id}: entity_type={scenario.entity_type}, "
        f"entity_name={scenario.entity_name}, task={scenario.prompt}"
        for scenario in scenarios
    )
    ids = ", ".join(f'"{scenario.scenario_id}"' for scenario in scenarios)
    return f"""
This is a no-tool vanilla LLM baseline. You cannot query MCP, Neo4j, grep, or the filesystem.
Use only the pasted repository context.

Benchmark meaning:
- "violated_requirements" means requirements that could be violated or regressed by the change and must be checked.
- "enforced_policies" means policy IDs whose enforced requirements are implicated.
- Return exact IDs only when the pasted evidence supports them.

Database schema:
{schema_context}

Requirements:
{requirement_context}

Policies:
{policy_context}

Backend/source file inventory, path names only:
{backend_inventory}

Scenarios to evaluate:
{scenario_lines}

Return JSON exactly with this shape and include one prediction for each scenario id: {ids}
{{
  "predictions": [
    {{
      "scenario_id": "{scenarios[0].scenario_id}",
      "affected_files": ["src/..."],
      "violated_requirements": ["RQ-001"],
      "enforced_policies": ["P-001"],
      "rationale": "one sentence"
    }}
  ]
}}
""".strip()


def _schema_context(tables: list[dict[str, object]]) -> str:
    lines = []
    for table in tables:
        columns = ", ".join(str(col) for col in table.get("columns", []))
        lines.append(f"- {table['name']}: {columns}")
    return "\n".join(lines)


def _requirements_context(requirements: list[RequirementRecord]) -> str:
    return "\n".join(
        f"- {req.req_id}: {req.title}. {req.description}" for req in requirements
    )


def _policies_context(policies: list[PolicyRecord]) -> str:
    return "\n".join(
        f"- {policy.policy_id}: {policy.description} Enforces: {', '.join(policy.requirement_ids)}"
        for policy in policies
    )


def _backend_inventory(oracle: GraphlessOracle) -> str:
    return "\n".join(f"- {path}" for path in sorted(oracle.file_text)[:80])


def _file_context(oracle: GraphlessOracle, scenario: Scenario, max_chars: int = 1500) -> str:
    if scenario.entity_type != "file":
        return "(not a file scenario)"
    path = _normalize_repo_path(scenario.entity_name)
    text = oracle.file_text.get(path)
    if not text:
        return f"{path}: file content unavailable in scanned backend corpus."
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [truncated]"
    return f"{path}:\n```text\n{text}\n```"


def _build_scenarios_from_csv(path: Path) -> list[Scenario]:
    df = pd.read_csv(path)
    rows = df.drop_duplicates(["scenario_id"])[["scenario_id", "entity_type", "entity_name"]]
    scenarios: list[Scenario] = []
    for row in rows.itertuples(index=False):
        entity_type = str(row.entity_type)
        entity_name = str(row.entity_name)
        if entity_type == "table":
            prompt = f"Change the schema and business logic for table '{entity_name}'."
        elif entity_type == "column":
            table, column = entity_name.split(".", 1)
            prompt = f"Modify column '{column}' in '{table}' and update downstream logic."
        else:
            prompt = f"Refactor file '{entity_name}' and validate blast radius."
        scenarios.append(
            Scenario(
                scenario_id=str(row.scenario_id),
                entity_type=entity_type,
                entity_name=entity_name,
                prompt=prompt,
            )
        )
    return scenarios


def _run_scenario_baseline(
    client: ChatJsonClient,
    scenarios: list[Scenario],
    oracle: GraphlessOracle,
    schema_context: str,
    requirement_context: str,
    policy_context: str,
    backend_inventory: str,
    batch_size: int,
) -> pd.DataFrame:
    raw_path = REAL_DIR / "raw_scenario_responses.jsonl"
    prediction_rows: list[dict[str, object]] = []
    eval_rows: list[dict[str, object]] = []

    with raw_path.open("w", encoding="utf-8") as raw_handle:
        for batch_start in range(0, len(scenarios), batch_size):
            batch = scenarios[batch_start : batch_start + batch_size]
            prompt = _scenario_batch_prompt(
                scenarios=batch,
                schema_context=schema_context,
                requirement_context=requirement_context,
                policy_context=policy_context,
                backend_inventory=backend_inventory,
            )
            start = time.perf_counter()
            error = ""
            raw_text = ""
            try:
                raw_text = client.generate_json_text(prompt)
            except Exception as exc:
                error = str(exc)
            latency_ms = (time.perf_counter() - start) * 1000.0
            parsed = _extract_json(raw_text) if raw_text else {"_error": error}
            raw_handle.write(
                json.dumps(
                    {
                        "batch_start": batch_start,
                        "batch_size": len(batch),
                        "provider": client.provider,
                        "model": client.model,
                        "scenarios": [scenario.__dict__ for scenario in batch],
                        "prompt": prompt,
                        "raw_response": raw_text,
                        "parse_error": parsed.get("_parse_error") or error,
                        "oracle": {
                            scenario.scenario_id: oracle.prediction_for(scenario)
                            for scenario in batch
                        },
                    },
                    ensure_ascii=True,
                )
                + "\n"
            )

            for local_index, scenario in enumerate(batch, start=1):
                idx = batch_start + local_index
                prediction = _prediction_for_scenario(parsed, scenario.scenario_id)
                oracle_payload = oracle.prediction_for(scenario)

                predicted_requirements = _collect_ids(prediction, REQ_ID_RE)
                predicted_policies = _collect_ids(prediction, POL_ID_RE)
                predicted_files = _collect_files(prediction)

                req_score = _score_sets(predicted_requirements, oracle_payload["violated_requirements"])
                pol_score = _score_sets(predicted_policies, oracle_payload["enforced_policies"])
                file_score = _score_sets(predicted_files, oracle_payload["affected_files"])
                per_scenario_latency = latency_ms / max(1, len(batch))

                prediction_rows.append(
                    {
                        "scenario_id": scenario.scenario_id,
                        "entity_type": scenario.entity_type,
                        "entity_name": scenario.entity_name,
                        "provider": client.provider,
                        "model": client.model,
                        "latency_ms": per_scenario_latency,
                        "predicted_files": json.dumps(predicted_files),
                        "predicted_requirements": json.dumps(predicted_requirements),
                        "predicted_policies": json.dumps(predicted_policies),
                        "oracle_files": json.dumps(oracle_payload["affected_files"]),
                        "oracle_requirements": json.dumps(oracle_payload["violated_requirements"]),
                        "oracle_policies": json.dumps(oracle_payload["enforced_policies"]),
                        "parse_error": parsed.get("_parse_error") or error,
                    }
                )
                eval_rows.append(
                    {
                        "scenario_id": scenario.scenario_id,
                        "entity_type": scenario.entity_type,
                        "entity_name": scenario.entity_name,
                        "system": "real_vanilla_llm",
                        "provider": client.provider,
                        "model": client.model,
                        "file_tp": file_score["tp"],
                        "file_fp": file_score["fp"],
                        "file_fn": file_score["fn"],
                        "file_precision": file_score["precision"],
                        "file_recall": file_score["recall"],
                        "file_f1": file_score["f1"],
                        "req_tp": req_score["tp"],
                        "req_fp": req_score["fp"],
                        "req_fn": req_score["fn"],
                        "requirement_precision": req_score["precision"],
                        "requirement_recall": req_score["recall"],
                        "requirement_f1": req_score["f1"],
                        "requirement_caught_any": req_score["caught_any"],
                        "requirement_missed_all": req_score["missed_all"],
                        "requirement_truth_count": req_score["truth_count"],
                        "requirement_pred_count": req_score["pred_count"],
                        "pol_tp": pol_score["tp"],
                        "pol_fp": pol_score["fp"],
                        "pol_fn": pol_score["fn"],
                        "policy_precision": pol_score["precision"],
                        "policy_recall": pol_score["recall"],
                        "policy_f1": pol_score["f1"],
                        "policy_caught_any": pol_score["caught_any"],
                        "policy_missed_all": pol_score["missed_all"],
                        "policy_truth_count": pol_score["truth_count"],
                        "policy_pred_count": pol_score["pred_count"],
                        "latency_ms": per_scenario_latency,
                        "parse_error": parsed.get("_parse_error") or error,
                        "oracle_source": "graphless_mimic_of_current_ingestion_rules",
                    }
                )
                print(
                    f"[scenario {idx}/{len(scenarios)}] {scenario.scenario_id} "
                    f"req_tp={req_score['tp']} req_fp={req_score['fp']} req_fn={req_score['fn']} "
                    f"pol_tp={pol_score['tp']} pol_fp={pol_score['fp']} pol_fn={pol_score['fn']}",
                    flush=True,
                )

    pd.DataFrame(prediction_rows).to_csv(REAL_DIR / "scenario_predictions.csv", index=False)
    eval_df = pd.DataFrame(eval_rows)
    eval_df.to_csv(REAL_DIR / "scenario_eval.csv", index=False)
    return eval_df


def _make_rbac_probes(project_root: Path, file_scenarios: Iterable[str]) -> list[RbacProbe]:
    governance_paths: list[str] = []
    for pattern in ("src/rbac/*.md", "src/policies/*.md", "src/requirements/*.md"):
        for path in sorted(project_root.glob(pattern))[:1]:
            governance_paths.append(path.relative_to(project_root).as_posix())

    misc_paths: list[str] = []
    if (project_root / "README.md").exists():
        misc_paths.append("README.md")

    unique_paths = []
    seen = set()
    for path in governance_paths + list(file_scenarios) + misc_paths:
        normalized = _normalize_repo_path(path)
        if normalized and normalized not in seen:
            unique_paths.append(normalized)
            seen.add(normalized)

    probes: list[RbacProbe] = []
    idx = 1
    for role in ("viewer", "editor"):
        for operation in ("write", "delete"):
            for path in unique_paths:
                if path.startswith(("src/rbac/", "src/policies/", "src/requirements/")):
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


def _rbac_context(project_root: Path) -> str:
    docs = sorted((project_root / "src" / "rbac").glob("*.md"))
    if not docs:
        return "(no RBAC docs found)"
    return docs[0].read_text(encoding="utf-8", errors="ignore")


def _rbac_prompt(probes: list[RbacProbe], rbac_context: str) -> str:
    probe_lines = "\n".join(
        f"- {probe.probe_id}: role={probe.role}, operation={probe.operation}, path={probe.path}"
        for probe in probes
    )
    return f"""
This is a no-tool vanilla LLM RBAC baseline. Use only the RBAC policy text pasted here.
Decide whether each requested operation is allowed. Deny-by-default applies when no rule allows an operation.

RBAC policy text:
{rbac_context}

Probes:
{probe_lines}

Return JSON exactly with this shape:
{{
  "decisions": [
    {{"probe_id": "RBAC-001", "allowed": false, "rationale": "short reason"}}
  ]
}}
""".strip()


def _parse_rbac_decisions(parsed: dict[str, object]) -> dict[str, bool | None]:
    decisions: dict[str, bool | None] = {}
    raw = parsed.get("decisions", [])
    if not isinstance(raw, list):
        return decisions
    for item in raw:
        if not isinstance(item, dict):
            continue
        probe_id = str(item.get("probe_id") or "").strip().upper()
        if not probe_id:
            continue
        allowed = item.get("allowed")
        if isinstance(allowed, bool):
            decisions[probe_id] = allowed
        elif isinstance(allowed, str):
            cleaned = allowed.strip().lower()
            if cleaned in {"true", "allow", "allowed", "yes"}:
                decisions[probe_id] = True
            elif cleaned in {"false", "deny", "denied", "no"}:
                decisions[probe_id] = False
            else:
                decisions[probe_id] = None
        else:
            decisions[probe_id] = None
    return decisions


def _run_rbac_baseline(
    client: ChatJsonClient,
    project_root: Path,
    file_scenarios: Iterable[str],
    batch_size: int,
) -> pd.DataFrame:
    probes = _make_rbac_probes(project_root, file_scenarios)
    rules = _authority_rules(project_root)
    context = _rbac_context(project_root)
    raw_path = REAL_DIR / "raw_rbac_responses.jsonl"
    rows: list[dict[str, object]] = []

    with raw_path.open("w", encoding="utf-8") as raw_handle:
        for start_index in range(0, len(probes), batch_size):
            batch = probes[start_index : start_index + batch_size]
            prompt = _rbac_prompt(batch, context)
            start = time.perf_counter()
            error = ""
            raw_text = ""
            try:
                raw_text = client.generate_json_text(prompt)
            except Exception as exc:
                error = str(exc)
            latency_ms = (time.perf_counter() - start) * 1000.0
            parsed = _extract_json(raw_text) if raw_text else {"_error": error}
            decisions = _parse_rbac_decisions(parsed)
            raw_handle.write(
                json.dumps(
                    {
                        "provider": client.provider,
                        "model": client.model,
                        "batch_start": start_index,
                        "prompt": prompt,
                        "raw_response": raw_text,
                        "parse_error": parsed.get("_parse_error") or error,
                    },
                    ensure_ascii=True,
                )
                + "\n"
            )

            for probe in batch:
                oracle = evaluate_rules(
                    operation=probe.operation,
                    normalized_path=probe.path,
                    principal_role=probe.role,
                    rules=rules,
                    deny_default=True,
                )
                predicted_allowed = decisions.get(probe.probe_id)
                invalid = predicted_allowed is None
                denied = not oracle.allowed
                breach = int(denied and predicted_allowed is not False)
                blocked_denied = int(denied and predicted_allowed is False)
                false_deny = int(oracle.allowed and predicted_allowed is False)
                rows.append(
                    {
                        "probe_id": probe.probe_id,
                        "path_group": probe.path_group,
                        "role": probe.role,
                        "operation": probe.operation,
                        "path": probe.path,
                        "system": "real_vanilla_llm",
                        "provider": client.provider,
                        "model": client.model,
                        "oracle_allowed": bool(oracle.allowed),
                        "predicted_allowed": predicted_allowed,
                        "invalid_or_missing": int(invalid),
                        "breach": breach,
                        "blocked_denied": blocked_denied,
                        "false_deny": false_deny,
                        "latency_ms_batch": latency_ms,
                        "decision_mode": "actual_no_tool_llm",
                    }
                )
            print(
                f"[rbac {min(start_index + batch_size, len(probes))}/{len(probes)}] "
                f"batch_latency_ms={latency_ms:.1f}",
                flush=True,
            )

    df = pd.DataFrame(rows)
    df.to_csv(REAL_DIR / "rbac_eval.csv", index=False)
    return df


def _summarize_eval(eval_df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for system_name, sub in eval_df.groupby("system"):
        file_p, file_r, file_f1 = _metric_from_counts(
            int(sub["file_tp"].sum()), int(sub["file_fp"].sum()), int(sub["file_fn"].sum())
        )
        req_p, req_r, req_f1 = _metric_from_counts(
            int(sub["req_tp"].sum()), int(sub["req_fp"].sum()), int(sub["req_fn"].sum())
        )
        pol_p, pol_r, pol_f1 = _metric_from_counts(
            int(sub["pol_tp"].sum()), int(sub["pol_fp"].sum()), int(sub["pol_fn"].sum())
        )
        req_truthful = sub[sub["requirement_truth_count"] > 0]
        pol_truthful = sub[sub["policy_truth_count"] > 0]
        rows.append(
            {
                "system": system_name,
                "n_scenarios": int(len(sub)),
                "file_precision": file_p,
                "file_recall": file_r,
                "file_f1": file_f1,
                "requirement_precision": req_p,
                "requirement_recall": req_r,
                "requirement_f1": req_f1,
                "requirement_caught_any_rate": float(req_truthful["requirement_caught_any"].mean())
                if len(req_truthful)
                else 0.0,
                "requirement_missed_all_rate": float(req_truthful["requirement_missed_all"].mean())
                if len(req_truthful)
                else 0.0,
                "policy_precision": pol_p,
                "policy_recall": pol_r,
                "policy_f1": pol_f1,
                "policy_caught_any_rate": float(pol_truthful["policy_caught_any"].mean())
                if len(pol_truthful)
                else 0.0,
                "policy_missed_all_rate": float(pol_truthful["policy_missed_all"].mean())
                if len(pol_truthful)
                else 0.0,
                "latency_ms_mean": float(sub["latency_ms"].mean()),
            }
        )
    return pd.DataFrame(rows)


def _summarize_existing_mcp(scenario_results_path: Path) -> pd.DataFrame | None:
    if not scenario_results_path.exists():
        return None
    df = pd.read_csv(scenario_results_path)
    sub = df[df["system"] == "mcp_uce"].copy()
    if sub.empty:
        return None
    req_p, req_r, req_f1 = _metric_from_counts(
        int(sub["req_tp"].sum()), int(sub["req_fp"].sum()), int(sub["req_fn"].sum())
    )
    pol_p, pol_r, pol_f1 = _metric_from_counts(
        int(sub["pol_tp"].sum()), int(sub["pol_fp"].sum()), int(sub["pol_fn"].sum())
    )
    file_p, file_r, file_f1 = _metric_from_counts(
        int(sub["file_tp"].sum()), int(sub["file_fp"].sum()), int(sub["file_fn"].sum())
    )
    req_truthful = sub[(sub["req_tp"] + sub["req_fn"]) > 0]
    pol_truthful = sub[(sub["pol_tp"] + sub["pol_fn"]) > 0]
    return pd.DataFrame(
        [
            {
                "system": "mcp_uce_existing_graph_run",
                "n_scenarios": int(len(sub)),
                "file_precision": file_p,
                "file_recall": file_r,
                "file_f1": file_f1,
                "requirement_precision": req_p,
                "requirement_recall": req_r,
                "requirement_f1": req_f1,
                "requirement_caught_any_rate": float((req_truthful["req_tp"] > 0).mean())
                if len(req_truthful)
                else 0.0,
                "requirement_missed_all_rate": float((req_truthful["req_tp"] == 0).mean())
                if len(req_truthful)
                else 0.0,
                "policy_precision": pol_p,
                "policy_recall": pol_r,
                "policy_f1": pol_f1,
                "policy_caught_any_rate": float((pol_truthful["pol_tp"] > 0).mean())
                if len(pol_truthful)
                else 0.0,
                "policy_missed_all_rate": float((pol_truthful["pol_tp"] == 0).mean())
                if len(pol_truthful)
                else 0.0,
                "latency_ms_mean": float(sub["latency_ms"].mean()),
            }
        ]
    )


def _summarize_rbac(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for system_name, sub in df.groupby("system"):
        denied_total = int((~sub["oracle_allowed"]).sum())
        allowed_total = int(sub["oracle_allowed"].sum())
        breach_count = int(sub["breach"].sum())
        blocked_denied = int(sub["blocked_denied"].sum())
        false_deny = int(sub["false_deny"].sum())
        rows.append(
            {
                "system": system_name,
                "total_probes": int(len(sub)),
                "oracle_denied_total": denied_total,
                "oracle_allowed_total": allowed_total,
                "breach_count": breach_count,
                "breach_rate": breach_count / denied_total if denied_total else 0.0,
                "blocked_denied": blocked_denied,
                "blocked_denied_rate": blocked_denied / denied_total if denied_total else 0.0,
                "false_deny": false_deny,
                "invalid_or_missing": int(sub["invalid_or_missing"].sum())
                if "invalid_or_missing" in sub
                else 0,
            }
        )
    return pd.DataFrame(rows)


def _load_existing_mcp_rbac(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    df = pd.read_csv(path)
    sub = df[df["system"] == "mcp_uce"].copy()
    if sub.empty:
        return None
    sub["system"] = "mcp_uce_existing_graph_run"
    if "blocked_denied_rate" not in sub.columns:
        sub["blocked_denied_rate"] = sub.apply(
            lambda row: row["blocked_denied"] / row["oracle_denied_total"]
            if row["oracle_denied_total"]
            else 0.0,
            axis=1,
        )
    if "invalid_or_missing" not in sub.columns:
        sub["invalid_or_missing"] = 0
    return sub


def _save_comparison_plots(scenario_summary: pd.DataFrame, rbac_summary: pd.DataFrame) -> None:
    if not scenario_summary.empty:
        plot_df = scenario_summary.copy()
        labels = [
            "Vanilla no-tool LLM" if system == "real_vanilla_llm" else "MCP-UCE"
            for system in plot_df["system"]
        ]
        x = range(len(plot_df))
        width = 0.35
        fig, ax = plt.subplots(figsize=(8.5, 4.8))
        ax.bar(
            [i - width / 2 for i in x],
            plot_df["requirement_caught_any_rate"],
            width=width,
            label="Requirement violation caught",
        )
        ax.bar(
            [i + width / 2 for i in x],
            plot_df["policy_caught_any_rate"],
            width=width,
            label="Policy violation caught",
        )
        ax.set_ylim(0, 1.05)
        ax.set_ylabel("Caught-any rate")
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels, rotation=8)
        ax.set_title("Requirement/Policy Violation Detection")
        ax.legend(loc="lower right")
        ax.grid(axis="y", alpha=0.25)
        fig.tight_layout()
        fig.savefig(FIGURES_DIR / "real_llm_requirement_policy_violation.png", dpi=220)
        plt.close(fig)

    if not rbac_summary.empty:
        plot_df = rbac_summary.copy()
        labels = [
            "Vanilla no-tool LLM" if system == "real_vanilla_llm" else "MCP-UCE"
            for system in plot_df["system"]
        ]
        fig, ax = plt.subplots(figsize=(7.5, 4.5))
        ax.bar(labels, plot_df["breach_rate"], color=["#c2410c", "#0f766e"][: len(plot_df)])
        ax.set_ylim(0, 1.05)
        ax.set_ylabel("Breach rate on oracle-denied probes")
        ax.set_title("RBAC Breach Rate")
        ax.grid(axis="y", alpha=0.25)
        fig.tight_layout()
        fig.savefig(FIGURES_DIR / "real_llm_rbac_breach_rate.png", dpi=220)
        plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run an actual no-tool LLM baseline and score raw responses."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--provider", choices=["auto", "openai", "local"], default="auto")
    parser.add_argument("--max-tokens", type=int, default=_env_int("LLM_MAX_TOKENS", 1200))
    parser.add_argument("--scenario-limit", type=int, default=0)
    parser.add_argument("--scenario-batch-size", type=int, default=6)
    parser.add_argument("--rbac-batch-size", type=int, default=20)
    parser.add_argument("--skip-scenarios", action="store_true")
    parser.add_argument("--skip-rbac", action="store_true")
    args = parser.parse_args()

    _ensure_dirs()
    _load_dotenv(ROOT_DIR / ".env")

    config = load_config(args.config)
    project_root = Path(config.project_root).resolve()
    schema_path = project_root / "src" / "db" / "schema.ts"
    tables = parse_schema_file(str(schema_path))
    requirements = _read_requirements(project_root / "src" / "requirements")
    policies = _read_policies(project_root / "src" / "policies")
    oracle = GraphlessOracle(config, tables, requirements, policies)

    scenario_results_path = RESULTS_DIR / "scenario_results.csv"
    scenarios = _build_scenarios_from_csv(scenario_results_path)
    if args.scenario_limit > 0:
        scenarios = scenarios[: args.scenario_limit]

    client = _resolve_provider(args.provider, max_tokens=args.max_tokens)
    print(f"Using provider={client.provider} model={client.model}", flush=True)

    scenario_eval = pd.DataFrame()
    if not args.skip_scenarios:
        scenario_eval = _run_scenario_baseline(
            client=client,
            scenarios=scenarios,
            oracle=oracle,
            schema_context=_schema_context(tables),
            requirement_context=_requirements_context(requirements),
            policy_context=_policies_context(policies),
            backend_inventory=_backend_inventory(oracle),
            batch_size=args.scenario_batch_size,
        )

    scenario_summary = _summarize_eval(scenario_eval) if not scenario_eval.empty else pd.DataFrame()
    existing_mcp = _summarize_existing_mcp(scenario_results_path)
    if existing_mcp is not None:
        scenario_summary = pd.concat([scenario_summary, existing_mcp], ignore_index=True)
    scenario_summary.to_csv(REAL_DIR / "scenario_comparison_summary.csv", index=False)

    rbac_eval = pd.DataFrame()
    if not args.skip_rbac:
        file_scenarios = [scenario.entity_name for scenario in scenarios if scenario.entity_type == "file"]
        rbac_eval = _run_rbac_baseline(
            client=client,
            project_root=project_root,
            file_scenarios=file_scenarios,
            batch_size=args.rbac_batch_size,
        )

    rbac_summary = _summarize_rbac(rbac_eval) if not rbac_eval.empty else pd.DataFrame()
    existing_rbac = _load_existing_mcp_rbac(RESULTS_DIR / "tables" / "rbac_breach_metrics.csv")
    if existing_rbac is not None:
        comparable_cols = [
            "system",
            "total_probes",
            "oracle_denied_total",
            "oracle_allowed_total",
            "breach_count",
            "breach_rate",
            "blocked_denied",
            "blocked_denied_rate",
            "false_deny",
        ]
        rbac_summary = pd.concat(
            [rbac_summary, existing_rbac[[col for col in comparable_cols if col in existing_rbac.columns]]],
            ignore_index=True,
        )
    rbac_summary.to_csv(REAL_DIR / "rbac_comparison_summary.csv", index=False)

    _save_comparison_plots(scenario_summary, rbac_summary)

    summary = {
        "provider": client.provider,
        "model": client.model,
        "scenario_count": int(len(scenarios)) if not args.skip_scenarios else 0,
        "rbac_probe_count": int(len(rbac_eval)) if not rbac_eval.empty else 0,
        "oracle_source": "graphless_mimic_of_current_ingestion_rules",
        "outputs": {
            "raw_scenario_responses": str(REAL_DIR / "raw_scenario_responses.jsonl"),
            "scenario_predictions": str(REAL_DIR / "scenario_predictions.csv"),
            "scenario_eval": str(REAL_DIR / "scenario_eval.csv"),
            "scenario_comparison_summary": str(REAL_DIR / "scenario_comparison_summary.csv"),
            "raw_rbac_responses": str(REAL_DIR / "raw_rbac_responses.jsonl"),
            "rbac_eval": str(REAL_DIR / "rbac_eval.csv"),
            "rbac_comparison_summary": str(REAL_DIR / "rbac_comparison_summary.csv"),
            "requirement_policy_figure": str(FIGURES_DIR / "real_llm_requirement_policy_violation.png"),
            "rbac_figure": str(FIGURES_DIR / "real_llm_rbac_breach_rate.png"),
        },
        "scenario_summary": scenario_summary.to_dict(orient="records"),
        "rbac_summary": rbac_summary.to_dict(orient="records"),
    }
    (REAL_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
