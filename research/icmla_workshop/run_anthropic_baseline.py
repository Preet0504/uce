"""
Real-LLM (Anthropic) no-tool baseline, scored against the SAME independent oracle as UCE.

This is the strong baseline: a frontier LLM (Claude) given the full repository context as text but
NO tools (no MCP, no graph, no grep). It answers impact + governance + RBAC questions from context
alone. We score it against:
  - scenarios: the independent import-resolver + governance-doc oracle (independent_oracle.py)
  - RBAC     : the deterministic rule evaluator uce.core.rbac.evaluate_rules (rules ARE the spec)

The headline comparison is RBAC enforcement: a no-tool LLM cannot deterministically enforce
deny-by-default policy, whereas UCE blocks 100% of oracle-denied operations by construction.

Requires ANTHROPIC_API_KEY (read from uce/.env or talkai-main/.env). Model from ANTHROPIC_MODEL.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from uce.core.config import load_config
from uce.core.rbac import ROLE_RANKS, AuthorityRule, evaluate_rules

from independent_oracle import (
    build_import_graph,
    governance_oracle,
    independent_file_oracle,
    is_backend_file,
    normalize_repo_path,
    parse_policies,
    parse_requirements,
    parse_schema,
)
from run_independent_eval import build_scenarios, prf

BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "results"
LLM_DIR = RESULTS_DIR / "anthropic_baseline"

REQ_ID_RE = re.compile(r"\bRQ-\d{3}\b", re.IGNORECASE)
POL_ID_RE = re.compile(r"\bP-\d{3}\b", re.IGNORECASE)


def _load_env() -> None:
    for env_path in (REPO_ROOT / ".env", REPO_ROOT.parent / "talkai-main" / ".env"):
        if not env_path.exists():
            continue
        for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Anthropic client
# ---------------------------------------------------------------------------

class AnthropicClient:
    def __init__(self, max_tokens: int = 2000) -> None:
        import anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (checked uce/.env and talkai-main/.env).")
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic(api_key=api_key)

    def json_text(self, system: str, prompt: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        return "\n".join(parts).strip()


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        try:
            v = json.loads(text[s:e + 1])
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    return {"_parse_error": text[:500]}


def _collect_ids(value, pattern) -> set[str]:
    found = set()
    def walk(x):
        if isinstance(x, str):
            found.update(m.group(0).upper() for m in pattern.finditer(x))
        elif isinstance(x, dict):
            for s in x.values():
                walk(s)
        elif isinstance(x, list):
            for s in x:
                walk(s)
    walk(value)
    return found


def _collect_files(value) -> set[str]:
    files = set()
    def walk(x):
        if isinstance(x, str):
            n = normalize_repo_path(x)
            if n.startswith("src/"):
                files.add(n)
        elif isinstance(x, dict):
            for s in x.values():
                walk(s)
        elif isinstance(x, list):
            for s in x:
                walk(s)
    walk(value)
    return files


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RbacProbe:
    probe_id: str
    role: str
    operation: str
    path: str


def _parse_rbac_rules(rbac_dir: Path) -> list[AuthorityRule]:
    docs = sorted(rbac_dir.glob("*.md"))
    if not docs:
        return []
    text = docs[0].read_text(encoding="utf-8", errors="ignore")
    pm = re.search(r"(?im)^policy id:\s*(.+)$", text)
    policy_id = pm.group(1).strip() if pm else docs[0].stem
    rules: list[AuthorityRule] = []
    cur: dict[str, str] | None = None

    def flush(c):
        if not c:
            return
        min_role = (c.get("min_role") or "").strip().lower()
        rank = ROLE_RANKS.get(min_role)
        if rank is None:
            return
        try:
            prio = int(c.get("source_priority", "0"))
        except ValueError:
            prio = 0
        rules.append(AuthorityRule(
            rule_id=(c.get("rule_id") or "").strip(),
            operation=(c.get("operation") or "").strip().lower(),
            path_pattern=(c.get("path_pattern") or "").strip().replace("\\", "/"),
            effect=(c.get("effect") or "allow").strip().lower(),
            min_role=min_role, min_role_rank=rank, source_priority=prio,
            policy_id=policy_id,
        ))

    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("- rule_id:"):
            flush(cur)
            cur = {"rule_id": s.split(":", 1)[1].strip()}
            continue
        if cur is None:
            continue
        if ":" in s:
            k, v = s.split(":", 1)
            cur[k.strip().lower()] = v.strip()
    flush(cur)
    return rules


def _build_rbac_probes(project_root: Path, import_graph, backend_prefixes) -> list[RbacProbe]:
    paths: list[str] = []
    for pat in ("src/rbac/*.md", "src/policies/*.md", "src/requirements/*.md"):
        for p in sorted(project_root.glob(pat))[:1]:
            paths.append(p.relative_to(project_root).as_posix())
    backend = [f for f in import_graph.files if is_backend_file(f, backend_prefixes)][:4]
    paths.extend(backend)
    if (project_root / "README.md").exists():
        paths.append("README.md")
    seen, uniq = set(), []
    for p in paths:
        n = normalize_repo_path(p)
        if n and n not in seen:
            uniq.append(n); seen.add(n)
    probes, idx = [], 1
    for role in ("viewer", "editor"):
        for op in ("write", "delete"):
            for p in uniq:
                probes.append(RbacProbe(f"RBAC-{idx:03d}", role, op, p)); idx += 1
    return probes


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SCENARIO_SYS = ("You are a no-tool software governance reviewer. You cannot query any database, "
                "graph, or filesystem. Use ONLY the pasted context. Return valid JSON only.")
RBAC_SYS = ("You are a no-tool access-control decision engine. Use ONLY the pasted RBAC policy text. "
            "Apply deny-by-default: deny unless a rule explicitly allows. Return valid JSON only.")


def _schema_ctx(schema) -> str:
    return "\n".join(f"- {t.sql_name}: {', '.join(sorted(t.sql_to_prop))}" for t in schema.values())


def _req_ctx(reqs) -> str:
    return "\n".join(f"- {r.req_id}: {r.description}" for r in reqs)


def _pol_ctx(pols) -> str:
    return "\n".join(f"- {p.policy_id}: enforces {', '.join(sorted(p.enforces))}" for p in pols)


def _scenario_prompt(batch, schema_ctx, req_ctx, pol_ctx, inventory) -> str:
    lines = "\n".join(f"- {s['id']}: entity_type={s['type']}, entity_name={s['name']}" for s in batch)
    ids = ", ".join(f'"{s["id"]}"' for s in batch)
    return f"""A developer wants to change each entity below. Identify the blast radius.
- affected_files: backend source files (src/...) that may need changes or could break.
- violated_requirements: requirement IDs that could be regressed.
- enforced_policies: policy IDs whose enforced requirements are implicated.

Database schema:
{schema_ctx}

Requirements:
{req_ctx}

Policies:
{pol_ctx}

Backend file inventory (paths only):
{inventory}

Scenarios:
{lines}

Return JSON: {{"predictions": [{{"scenario_id": <one of {ids}>, "affected_files": ["src/..."], "violated_requirements": ["RQ-001"], "enforced_policies": ["P-001"]}}]}}"""


def _rbac_prompt(batch, rbac_text) -> str:
    lines = "\n".join(f"- {p.probe_id}: role={p.role}, operation={p.operation}, path={p.path}" for p in batch)
    return f"""RBAC policy text:
{rbac_text}

Decide allow/deny for each request (deny-by-default).
Probes:
{lines}

Return JSON: {{"decisions": [{{"probe_id": "RBAC-001", "allowed": false}}]}}"""


def _parse_decisions(parsed) -> dict[str, bool | None]:
    out: dict[str, bool | None] = {}
    for item in (parsed.get("decisions") or []):
        if not isinstance(item, dict):
            continue
        pid = str(item.get("probe_id") or "").strip().upper()
        a = item.get("allowed")
        if isinstance(a, bool):
            out[pid] = a
        elif isinstance(a, str):
            c = a.strip().lower()
            out[pid] = True if c in {"true", "allow", "allowed", "yes"} else (False if c in {"false", "deny", "denied", "no"} else None)
    return out


def run(config_path: str, scenario_batch: int, rbac_batch: int, skip_scenarios: bool, skip_rbac: bool) -> None:
    _load_env()
    LLM_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config(config_path)
    project_root = Path(config.project_root)
    backend_prefixes = tuple(normalize_repo_path(p).lower() for p in (config.paths.backend or ()) if normalize_repo_path(p))

    schema = parse_schema(project_root / "src" / "db" / "schema.ts")
    schema_rel_candidates = {"src/db/schema.ts", "src/db/index.ts"}
    alias_map = dict(config.aliases) if config.aliases else {"@/": "src/"}
    alias_map.setdefault("@/", "src/")
    import_graph = build_import_graph(project_root, config.paths.code or ("src",), alias_map, config.ignore)
    requirements = parse_requirements(project_root / "src" / "requirements", schema)
    policies = parse_policies(project_root / "src" / "policies")

    client = AnthropicClient()
    print(f"Anthropic model={client.model}", flush=True)

    # ---- Scenarios ----
    scenario_summary = {}
    if not skip_scenarios:
        scenarios = build_scenarios(schema, import_graph, backend_prefixes)
        inventory = "\n".join(f"- {f}" for f in import_graph.files if is_backend_file(f, backend_prefixes))
        agg = {"file": [0, 0, 0], "req": [0, 0, 0], "pol": [0, 0, 0]}
        rows = []
        raw = (LLM_DIR / "raw_scenarios.jsonl").open("w", encoding="utf-8")
        for i in range(0, len(scenarios), scenario_batch):
            batch = scenarios[i:i + scenario_batch]
            prompt = _scenario_prompt(batch, _schema_ctx(schema), _req_ctx(requirements), _pol_ctx(policies), inventory)
            t0 = time.perf_counter()
            try:
                text = client.json_text(SCENARIO_SYS, prompt)
            except Exception as exc:
                text = ""; print("ERROR:", exc)
            lat = (time.perf_counter() - t0) * 1000.0
            parsed = _extract_json(text) if text else {}
            raw.write(json.dumps({"batch": i, "raw": text}) + "\n")
            preds = {}
            for it in (parsed.get("predictions") or []):
                if isinstance(it, dict):
                    preds[str(it.get("scenario_id") or "").strip()] = it
            for sc in batch:
                truth_files = independent_file_oracle(import_graph, schema, schema_rel_candidates, sc["type"], sc["name"], backend_prefixes)
                truth_reqs, truth_pols = governance_oracle(sc["type"], sc["name"], requirements, policies)
                pred = preds.get(sc["id"], {})
                pf = _collect_files(pred); pr = _collect_ids(pred, REQ_ID_RE); pp = _collect_ids(pred, POL_ID_RE)
                for key, predset, truth in (("file", pf, truth_files), ("req", pr, truth_reqs), ("pol", pp, truth_pols)):
                    tp, fp, fn, *_ = prf(predset, truth)
                    agg[key][0] += tp; agg[key][1] += fp; agg[key][2] += fn
                rows.append({"scenario_id": sc["id"], "latency_ms": lat / len(batch)})
            print(f"[scenarios {min(i+scenario_batch,len(scenarios))}/{len(scenarios)}] latency_ms={lat:.0f}", flush=True)
        raw.close()
        def micro(k):
            tp, fp, fn = agg[k]
            p = tp/(tp+fp) if tp+fp else 0.0; r = tp/(tp+fn) if tp+fn else 0.0
            return {"precision": round(p,4), "recall": round(r,4), "f1": round(2*p*r/(p+r),4) if p+r else 0.0}
        scenario_summary = {"file": micro("file"), "requirement": micro("req"), "policy": micro("pol")}
        print("Anthropic scenario micro-F1:", json.dumps(scenario_summary))

    # ---- RBAC ----
    rbac_summary = {}
    if not skip_rbac:
        rules = _parse_rbac_rules(project_root / "src" / "rbac")
        rbac_text = sorted((project_root / "src" / "rbac").glob("*.md"))[0].read_text(encoding="utf-8", errors="ignore")
        probes = _build_rbac_probes(project_root, import_graph, backend_prefixes)
        raw = (LLM_DIR / "raw_rbac.jsonl").open("w", encoding="utf-8")
        decisions: dict[str, bool | None] = {}
        for i in range(0, len(probes), rbac_batch):
            batch = probes[i:i + rbac_batch]
            try:
                text = client.json_text(RBAC_SYS, _rbac_prompt(batch, rbac_text))
            except Exception as exc:
                text = ""; print("ERROR:", exc)
            raw.write(json.dumps({"batch": i, "raw": text}) + "\n")
            decisions.update(_parse_decisions(_extract_json(text) if text else {}))
            print(f"[rbac {min(i+rbac_batch,len(probes))}/{len(probes)}]", flush=True)
        raw.close()
        denied = breach = blocked = invalid = 0
        for p in probes:
            oracle = evaluate_rules(operation=p.operation, normalized_path=p.path, principal_role=p.role, rules=rules, deny_default=True)
            pred = decisions.get(p.probe_id.upper())
            if not oracle.allowed:
                denied += 1
                if pred is None:
                    invalid += 1; breach += 1  # missing decision = unsafe default
                elif pred is True:
                    breach += 1
                else:
                    blocked += 1
        rbac_summary = {
            "total_probes": len(probes), "oracle_denied": denied,
            "llm_breach_count": breach, "llm_breach_rate": round(breach/denied, 4) if denied else 0.0,
            "llm_blocked_denied": blocked, "invalid_or_missing": invalid,
            "uce_breach_count": 0, "uce_breach_rate": 0.0,
        }
        print("RBAC:", json.dumps(rbac_summary))

    summary = {"model": client.model, "scenario_summary_vs_independent_oracle": scenario_summary, "rbac_summary": rbac_summary}
    (LLM_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("\nWrote", LLM_DIR / "summary.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="F:/UIC/CS540/Projects/talkai-main/config.yaml")
    ap.add_argument("--scenario-batch", type=int, default=6)
    ap.add_argument("--rbac-batch", type=int, default=20)
    ap.add_argument("--skip-scenarios", action="store_true")
    ap.add_argument("--skip-rbac", action="store_true")
    args = ap.parse_args()
    run(args.config, args.scenario_batch, args.rbac_batch, args.skip_scenarios, args.skip_rbac)


if __name__ == "__main__":
    main()
