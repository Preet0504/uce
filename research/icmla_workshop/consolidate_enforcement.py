"""Print canonical enforcement metrics table from per-repo + pooled summaries."""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "results" / "enforcement_eval"


def main() -> None:
    per_repo = json.loads((OUT / "per_repo_summary.json").read_text(encoding="utf-8"))
    pooled = json.loads((OUT / "pooled_summary.json").read_text(encoding="utf-8"))

    lines = [
        "# Enforcement evaluation (canonical metrics)\n",
        "Agent: Claude (no tools). UCE: live Neo4j graph after deterministic ingest.\n",
        "Gate fires if: agent missed files UCE flagged, OR silent requirement, OR RBAC breach.\n",
        "",
        "## Per repository\n",
        "| repo | n | catch_rate | agent_self_catch | false_gate | mean_missed_files | mean_agent_files | mean_uce_files | silent_req_rate |",
        "|------|---|------------|------------------|------------|-------------------|------------------|----------------|-----------------|",
    ]
    for s in per_repo:
        sr = s.get("silent_requirement_scenario_rate")
        sr_s = f"{sr:.1%}" if sr is not None else "n/a"
        lines.append(
            f"| {s['repo']} | {s['n_scenarios']} | {s['catch_rate']:.1%} | {s['agent_self_catch_rate']:.1%} | "
            f"{s['false_gate_rate']:.1%} | {s['mean_missed_files']:.0f} | {s['mean_agent_files_declared']:.1f} | "
            f"{s['mean_uce_files_flagged']:.1f} | {sr_s} |"
        )

    lines += [
        "",
        "## Pooled (all scenarios, all repos)\n",
        f"- **n_scenarios**: {pooled['n_scenarios']}",
        f"- **UCE catch_rate**: {pooled['catch_rate']:.1%}",
        f"- **agent_self_catch_rate**: {pooled['agent_self_catch_rate']:.1%}",
        f"- **false_gate_rate**: {pooled['false_gate_rate']:.1%}",
        f"- **mean_missed_files** (UCE \\ agent per scenario): {pooled['mean_missed_files']}",
        f"- **median_missed_files**: {pooled['median_missed_files']}",
        f"- **mean files agent declared**: {pooled['mean_agent_files_declared']}",
        f"- **mean files UCE flagged**: {pooled['mean_uce_files_flagged']}",
        f"- **permission_breach_count** (agent allowed, RBAC denied): {pooled['permission_breach_count']}",
    ]
    if pooled.get("silent_requirement_scenario_rate") is not None:
        lines.append(f"- **silent_requirement_scenario_rate**: {pooled['silent_requirement_scenario_rate']:.1%}")

    md = "\n".join(lines)
    (OUT / "METRICS.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
