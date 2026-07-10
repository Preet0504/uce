"""
RBAC enforcement under policy complexity: does a frontier LLM still comply when the policy has
precedence conflicts, path-specificity traps, and deny-overrides-allow cases?

Compares:
  - Claude (no-tool) decisions, prompted with the raw policy text
  - UCE: deterministic uce.core.rbac.evaluate_rules (0 breaches by construction)
against the deterministic oracle (evaluate_rules on the parsed rules).

Metrics: breach rate (LLM allows an oracle-denied op), false-deny rate (LLM denies an oracle-allowed
op), and exact-decision accuracy. UCE is exact by construction (0 breach, 0 false-deny).

Uses ANTHROPIC_API_KEY. Self-contained: needs only the Anthropic key + the hard policy asset.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
REPO_ROOT = BASE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from uce.core.rbac import ROLE_RANKS, AuthorityRule, evaluate_rules
from run_anthropic_baseline import AnthropicClient, RBAC_SYS, _extract_json, _parse_decisions

RESULTS_DIR = BASE_DIR / "results"
OUT_DIR = RESULTS_DIR / "rbac_complexity"


@dataclass(frozen=True)
class RbacProbe:
    probe_id: str
    role: str
    operation: str
    path: str


def parse_rules_from_text(text: str) -> list[AuthorityRule]:
    pm = re.search(r"(?im)^policy id:\s*(.+)$", text)
    policy_id = pm.group(1).strip() if pm else "POLICY"
    rules: list[AuthorityRule] = []
    cur: dict[str, str] | None = None

    def flush(c):
        if not c:
            return
        mr = (c.get("min_role") or "").strip().lower()
        rank = ROLE_RANKS.get(mr)
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
            min_role=mr, min_role_rank=rank, source_priority=prio, policy_id=policy_id,
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


# Paths chosen to exercise precedence / specificity / deny-default traps.
PROBE_PATHS = [
    "src/rbac/RBAC_DEMO_001.md",          # editor write -> DENY (prio 1000) vs ALLOW_EDITOR_WRITE_SRC
    "src/policies/P-001.md",              # editor write -> DENY non-admin
    "src/requirements/RQ-001.md",         # editor write -> DENY non-admin
    "src/db/schema.ts",                   # editor write -> DENY_WRITE_DB(700) beats ALLOW schema(60)
    "src/db/index.ts",                    # editor write -> DENY_WRITE_DB
    "src/db/migrations/0001_init.sql",    # delete -> DENY_DELETE_MIGRATIONS(900)
    "src/modules/agents/server/procedures.ts",  # editor delete -> ALLOW_EDITOR_DELETE_MODULES
    "src/secrets/keys.ts",                # viewer read -> DENY_READ_SECRETS(800) vs ALLOW_VIEWER_READ_ALL
    "src/lib/auth.ts",                    # editor write -> ALLOW src/*
    "README.md",                          # editor write -> deny-default (not under src/*)
    "src/trpc/init.ts",                   # editor delete -> deny-default (no delete allow here)
]


def build_probes() -> list[RbacProbe]:
    probes, idx = [], 1
    for role in ("viewer", "editor", "admin"):
        for op in ("read", "write", "delete"):
            for path in PROBE_PATHS:
                probes.append(RbacProbe(f"RBAC-{idx:03d}", role, op, path))
                idx += 1
    return probes


def _rbac_prompt(batch: list[RbacProbe], policy_text: str) -> str:
    lines = "\n".join(f"- {p.probe_id}: role={p.role}, operation={p.operation}, path={p.path}" for p in batch)
    return f"""RBAC policy text (rules have source_priority; higher priority wins; deny overrides allow at equal priority; deny-by-default if no rule grants access; a rule grants access only if the requester's role rank >= the rule's min_role rank, with viewer<editor<admin):
{policy_text}

Decide allow/deny for each request.
Probes:
{lines}

Return JSON: {{"decisions": [{{"probe_id": "RBAC-001", "allowed": false}}]}}"""


def run(batch_size: int) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    policy_text = (BASE_DIR / "assets" / "rbac_hard_policy.md").read_text(encoding="utf-8")
    rules = parse_rules_from_text(policy_text)
    probes = build_probes()
    print(f"Hard policy: {len(rules)} rules, {len(probes)} probes")

    client = AnthropicClient(max_tokens=4000)
    print(f"Anthropic model={client.model}", flush=True)
    decisions: dict[str, bool | None] = {}
    raw = (OUT_DIR / "raw_rbac.jsonl").open("w", encoding="utf-8")
    for i in range(0, len(probes), batch_size):
        batch = probes[i:i + batch_size]
        try:
            text = client.json_text(RBAC_SYS, _rbac_prompt(batch, policy_text))
        except Exception as exc:
            text = ""; print("ERROR:", exc)
        raw.write(json.dumps({"batch": i, "raw": text}) + "\n")
        decisions.update(_parse_decisions(_extract_json(text) if text else {}))
        print(f"[rbac {min(i+batch_size, len(probes))}/{len(probes)}]", flush=True)
    raw.close()

    denied = allowed_total = 0
    llm_breach = llm_false_deny = llm_invalid = llm_correct = 0
    rows = []
    for p in probes:
        oracle = evaluate_rules(operation=p.operation, normalized_path=p.path,
                                principal_role=p.role, rules=rules, deny_default=True)
        pred = decisions.get(p.probe_id.upper())
        if oracle.allowed:
            allowed_total += 1
        else:
            denied += 1
        if not oracle.allowed:
            if pred is True:
                llm_breach += 1
            elif pred is None:
                llm_invalid += 1; llm_breach += 1
        else:
            if pred is False:
                llm_false_deny += 1
        if pred is not None and pred == oracle.allowed:
            llm_correct += 1
        rows.append({"probe_id": p.probe_id, "role": p.role, "operation": p.operation, "path": p.path,
                     "oracle_allowed": oracle.allowed, "llm_allowed": pred,
                     "matched_rule": oracle.matched_rule_id})

    import csv
    with (OUT_DIR / "rbac_complexity_eval.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)

    summary = {
        "policy": "RBAC_HARD_001", "n_rules": len(rules), "n_probes": len(probes),
        "oracle_denied": denied, "oracle_allowed": allowed_total,
        "llm_model": client.model,
        "llm_breach_count": llm_breach, "llm_breach_rate": round(llm_breach / denied, 4) if denied else 0.0,
        "llm_false_deny_count": llm_false_deny, "llm_false_deny_rate": round(llm_false_deny / allowed_total, 4) if allowed_total else 0.0,
        "llm_invalid_or_missing": llm_invalid,
        "llm_decision_accuracy": round(llm_correct / len(probes), 4),
        "uce_breach_count": 0, "uce_breach_rate": 0.0,
        "uce_false_deny_count": 0, "uce_decision_accuracy": 1.0,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-size", type=int, default=33)
    run(ap.parse_args().batch_size)
