"""
Context-augmentation comparison: does prompt-stuffing close the gap to UCE's structured graph?

The key reviewer question: "Why a Neo4j graph + tools? Just paste the policy/requirements/files
into the LLM context." This script answers it empirically by running the SAME frontier LLM (Claude)
with an increasing amount of in-context information, and comparing each rung to UCE on the SAME
independent oracle.

IMPACT task context ladder (all Claude, scored vs independent_oracle):
  bare          : schema (tables/columns) only
  +gov_docs     : + full requirements + policies text
  +inventory    : + backend file path inventory
  +rag_files    : + retrieved CONTENTS of top-K lexically-relevant backend files (a RAG agent)
  [uce]         : structured graph traversal (from independent_summary.json)

RBAC task context ladder (hard policy with precedence traps):
  zero_shot     : policy text + probes
  few_shot      : policy text + worked precedence-resolution examples + probes
  [uce]         : deterministic evaluate_rules (0% breach by construction)

Requires ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
REPO_ROOT = BASE_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from uce.core.config import load_config
from uce.core.rbac import evaluate_rules

from independent_oracle import (
    build_import_graph, governance_oracle, independent_file_oracle,
    is_backend_file, normalize_repo_path, parse_policies, parse_requirements, parse_schema,
)
from run_independent_eval import build_scenarios, prf, _tokens
from run_anthropic_baseline import (
    AnthropicClient, SCENARIO_SYS, RBAC_SYS, _extract_json, _collect_ids, _collect_files,
    REQ_ID_RE, POL_ID_RE, _parse_decisions,
)
from run_rbac_complexity import parse_rules_from_text, build_probes, _rbac_prompt

RESULTS_DIR = BASE_DIR / "results"
OUT_DIR = RESULTS_DIR / "context_comparison"

IMPACT_VARIANTS = ("bare", "+gov_docs", "+inventory", "+rag_files")


def _schema_ctx(schema) -> str:
    return "\n".join(f"- {t.sql_name}: {', '.join(sorted(t.sql_to_prop))}" for t in schema.values())


def _req_ctx(reqs) -> str:
    return "\n".join(f"- {r.req_id}: {r.description}" for r in reqs)


def _pol_ctx(pols) -> str:
    return "\n".join(f"- {p.policy_id}: enforces {', '.join(sorted(p.enforces))}" for p in pols)


def _retrieve_files(scenario, import_graph, backend_prefixes, k=4, max_chars=900) -> str:
    toks = _tokens(scenario)
    scored = []
    for rel in import_graph.files:
        if not is_backend_file(rel, backend_prefixes):
            continue
        text = import_graph.text(rel).lower()
        s = sum(text.count(t) for t in toks)
        if s > 0:
            scored.append((rel, s))
    scored.sort(key=lambda x: (-x[1], x[0]))
    blocks = []
    for rel, _ in scored[:k]:
        body = import_graph.text(rel)
        if len(body) > max_chars:
            body = body[:max_chars] + "\n... [truncated]"
        blocks.append(f"FILE {rel}:\n```\n{body}\n```")
    return "\n\n".join(blocks) if blocks else "(no candidate files retrieved)"


def _impact_prompt(variant, batch, schema, requirements, policies, import_graph, backend_prefixes):
    parts = [
        "A developer wants to change each entity below. Identify the blast radius:",
        "- affected_files: backend source files (src/...) that may need changes or could break.",
        "- violated_requirements: requirement IDs that could be regressed.",
        "- enforced_policies: policy IDs whose enforced requirements are implicated.",
        "",
        "Database schema:",
        _schema_ctx(schema),
    ]
    if variant in ("+gov_docs", "+inventory", "+rag_files"):
        parts += ["", "Requirements:", _req_ctx(requirements), "", "Policies:", _pol_ctx(policies)]
    if variant in ("+inventory", "+rag_files"):
        inv = "\n".join(f"- {f}" for f in import_graph.files if is_backend_file(f, backend_prefixes))
        parts += ["", "Backend file inventory (paths only):", inv]
    if variant == "+rag_files":
        for sc in batch:
            parts += ["", f"Retrieved source for scenario {sc['id']} ({sc['name']}):",
                      _retrieve_files(sc, import_graph, backend_prefixes)]
    lines = "\n".join(f"- {s['id']}: entity_type={s['type']}, entity_name={s['name']}" for s in batch)
    ids = ", ".join(f'"{s["id"]}"' for s in batch)
    parts += ["", "Scenarios:", lines, "",
              f'Return JSON: {{"predictions": [{{"scenario_id": <one of {ids}>, '
              '"affected_files": ["src/..."], "violated_requirements": ["RQ-001"], '
              '"enforced_policies": ["P-001"]}]}}']
    return "\n".join(parts)


def run_impact(client, schema, requirements, policies, import_graph, backend_prefixes, scenario_batch):
    scenarios = build_scenarios(schema, import_graph, backend_prefixes)
    schema_rel_candidates = {"src/db/schema.ts", "src/db/index.ts"}
    rows = []
    raw = (OUT_DIR / "raw_impact.jsonl").open("w", encoding="utf-8")
    for variant in IMPACT_VARIANTS:
        agg = {"file": [0, 0, 0], "req": [0, 0, 0], "pol": [0, 0, 0]}
        rec_files = [0, 0]  # tp, tp+fn for recall
        for i in range(0, len(scenarios), scenario_batch):
            batch = scenarios[i:i + scenario_batch]
            prompt = _impact_prompt(variant, batch, schema, requirements, policies, import_graph, backend_prefixes)
            t0 = time.perf_counter()
            try:
                text = client.json_text(SCENARIO_SYS, prompt)
            except Exception as exc:
                text = ""; print("ERROR:", exc)
            lat = (time.perf_counter() - t0) * 1000.0
            parsed = _extract_json(text) if text else {}
            raw.write(json.dumps({"variant": variant, "batch": i, "raw": text[:4000]}) + "\n")
            preds = {}
            for it in (parsed.get("predictions") or []):
                if isinstance(it, dict):
                    preds[str(it.get("scenario_id") or "").strip()] = it
            for sc in batch:
                tf = independent_file_oracle(import_graph, schema, schema_rel_candidates, sc["type"], sc["name"], backend_prefixes)
                tr, tp_ = governance_oracle(sc["type"], sc["name"], requirements, policies)
                pred = preds.get(sc["id"], {})
                pf = _collect_files(pred); pr = _collect_ids(pred, REQ_ID_RE); pp = _collect_ids(pred, POL_ID_RE)
                for key, ps, tr_set in (("file", pf, tf), ("req", pr, tr), ("pol", pp, tp_)):
                    a, b, c, *_ = prf(ps, tr_set)
                    agg[key][0] += a; agg[key][1] += b; agg[key][2] += c
                rec_files[0] += len(pf & tf); rec_files[1] += len(tf)
            print(f"[impact {variant} {min(i+scenario_batch,len(scenarios))}/{len(scenarios)}] lat={lat:.0f}", flush=True)

        def micro(k):
            tp, fp, fn = agg[k]
            p = tp/(tp+fp) if tp+fp else 0.0; r = tp/(tp+fn) if tp+fn else 0.0
            return round(p, 4), round(r, 4), round(2*p*r/(p+r), 4) if p+r else 0.0
        fp_, fr_, ff_ = micro("file"); rp_, rr_, rf_ = micro("req"); pp_, pr_, pf_ = micro("pol")
        rows.append({"system": f"llm_{variant}", "file_precision": fp_, "file_recall": fr_, "file_f1": ff_,
                     "requirement_f1": rf_, "policy_f1": pf_})
    raw.close()
    return rows


FEWSHOT = """Worked examples (apply the same reasoning):
1. role=editor, write, src/db/schema.ts -> DENY. Reason: DENY_WRITE_DB (src/db/*, prio 700) outranks
   ALLOW_EDITOR_WRITE_DB_SCHEMA (prio 60); higher priority wins, so the deny applies.
2. role=editor, write, README.md -> DENY. Reason: deny-by-default; no allow rule matches a path
   outside src/* for editor.
3. role=editor, write, src/rbac/x.md -> DENY. Reason: DENY_NON_ADMIN_RBAC_WRITE (prio 1000) outranks
   ALLOW_EDITOR_WRITE_SRC (prio 100).
4. role=editor, write, src/lib/auth.ts -> ALLOW. Reason: ALLOW_EDITOR_WRITE_SRC matches and no
   higher-priority deny applies.
"""


def run_rbac(client, project_root, rbac_batch):
    policy_text = (BASE_DIR / "assets" / "rbac_hard_policy.md").read_text(encoding="utf-8")
    rules = parse_rules_from_text(policy_text)
    probes = build_probes()
    rows = []
    raw = (OUT_DIR / "raw_rbac_ctx.jsonl").open("w", encoding="utf-8")
    for variant in ("zero_shot", "few_shot"):
        decisions = {}
        for i in range(0, len(probes), rbac_batch):
            batch = probes[i:i + rbac_batch]
            prompt = _rbac_prompt(batch, policy_text)
            if variant == "few_shot":
                prompt = FEWSHOT + "\n" + prompt
            try:
                text = client.json_text(RBAC_SYS, prompt)
            except Exception as exc:
                text = ""; print("ERROR:", exc)
            raw.write(json.dumps({"variant": variant, "batch": i, "raw": text[:4000]}) + "\n")
            decisions.update(_parse_decisions(_extract_json(text) if text else {}))
            print(f"[rbac {variant} {min(i+rbac_batch,len(probes))}/{len(probes)}]", flush=True)
        denied = breach = correct = 0
        for p in probes:
            o = evaluate_rules(operation=p.operation, normalized_path=p.path, principal_role=p.role, rules=rules, deny_default=True)
            pred = decisions.get(p.probe_id.upper())
            if not o.allowed:
                denied += 1
                if pred is not False:
                    breach += 1
            if pred is not None and pred == o.allowed:
                correct += 1
        rows.append({"system": f"llm_{variant}", "breach_rate": round(breach/denied, 4) if denied else 0.0,
                     "breach_count": breach, "oracle_denied": denied,
                     "decision_accuracy": round(correct/len(probes), 4)})
    raw.close()
    rows.append({"system": "uce_deterministic", "breach_rate": 0.0, "breach_count": 0,
                 "oracle_denied": rows[0]["oracle_denied"], "decision_accuracy": 1.0})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="F:/UIC/CS540/Projects/talkai-main/config.yaml")
    ap.add_argument("--scenario-batch", type=int, default=6)
    ap.add_argument("--rbac-batch", type=int, default=33)
    ap.add_argument("--task", choices=["impact", "rbac", "both"], default="both")
    args = ap.parse_args()

    from run_anthropic_baseline import _load_env
    _load_env()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    project_root = Path(config.project_root)
    backend_prefixes = tuple(normalize_repo_path(p).lower() for p in (config.paths.backend or ()) if normalize_repo_path(p))
    schema = parse_schema(project_root / "src" / "db" / "schema.ts")
    alias_map = dict(config.aliases) if config.aliases else {"@/": "src/"}
    alias_map.setdefault("@/", "src/")
    import_graph = build_import_graph(project_root, config.paths.code or ("src",), alias_map, config.ignore)
    requirements = parse_requirements(project_root / "src" / "requirements", schema)
    policies = parse_policies(project_root / "src" / "policies")

    client = AnthropicClient(max_tokens=4000)
    print(f"Anthropic model={client.model}", flush=True)

    summary = {"model": client.model}
    if args.task in ("impact", "both"):
        impact_rows = run_impact(client, schema, requirements, policies, import_graph, backend_prefixes, args.scenario_batch)
        # append UCE row from prior independent eval
        try:
            indep = json.loads((RESULTS_DIR / "independent_summary.json").read_text(encoding="utf-8"))
            uce = next(s for s in indep["overall"] if s["system"] == "uce")
            impact_rows.append({"system": "uce_graph", "file_precision": round(uce["file_precision"], 4),
                                "file_recall": round(uce["file_recall"], 4), "file_f1": round(uce["file_f1"], 4),
                                "requirement_f1": round(uce["requirement_f1"], 4), "policy_f1": round(uce["policy_f1"], 4)})
        except Exception:
            pass
        summary["impact_context_ladder"] = impact_rows
        print("\n=== IMPACT context ladder (file metrics + req/pol F1, vs independent oracle) ===")
        print(f"{'system':18s} {'file_P':>7s} {'file_R':>7s} {'file_F1':>8s} {'req_F1':>7s} {'pol_F1':>7s}")
        for r in impact_rows:
            print(f"{r['system']:18s} {r['file_precision']:>7.3f} {r['file_recall']:>7.3f} "
                  f"{r['file_f1']:>8.3f} {r['requirement_f1']:>7.3f} {r['policy_f1']:>7.3f}")

    if args.task in ("rbac", "both"):
        rbac_rows = run_rbac(client, project_root, args.rbac_batch)
        summary["rbac_context_ladder"] = rbac_rows
        print("\n=== RBAC context ladder (hard policy, vs deterministic oracle) ===")
        print(f"{'system':18s} {'breach_rate':>12s} {'accuracy':>9s}")
        for r in rbac_rows:
            print(f"{r['system']:18s} {r['breach_rate']:>12.3f} {r['decision_accuracy']:>9.3f}")

    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("\nWrote", OUT_DIR / "summary.json")


if __name__ == "__main__":
    main()
