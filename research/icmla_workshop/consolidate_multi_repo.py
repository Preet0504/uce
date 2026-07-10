"""Merge baseline (summary.json) + UCE (uce_per_repo.json) into a consolidated multi-repo table."""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "results" / "multi_repo"


def f(x):
    return f"{x:.3f}" if isinstance(x, (int, float)) else "  -  "


def main() -> None:
    base = json.loads((OUT / "summary.json").read_text(encoding="utf-8"))
    uce = json.loads((OUT / "uce_per_repo.json").read_text(encoding="utf-8"))
    per_repo = [r for r in base["per_repo"] if r["system"] != "uce"]
    for repo, row in uce.items():
        per_repo.append(row)

    repos = ["talkai", "melodi", "expenses", "spark"]
    systems = ["naive_edit", "lexical", "static", "uce"]

    lines = ["# Multi-repo external validity (file impact over full import graph; governance where present)\n"]
    lines.append("| repo | system | file P | file R | file F1 | req F1 | pol F1 |")
    lines.append("|------|--------|--------|--------|---------|--------|--------|")
    pooled = {s: {"f": [0.0, 0, 0], "rr": [], "pp": []} for s in systems}
    for repo in repos:
        for s in systems:
            row = next((r for r in per_repo if r["repo"] == repo and r["system"] == s), None)
            if not row:
                continue
            lines.append(f"| {repo} | {s} | {f(row['file_precision'])} | {f(row['file_recall'])} | "
                         f"{f(row['file_f1'])} | {f(row.get('req_f1'))} | {f(row.get('pol_f1'))} |")

    # Macro-average across repos (file F1/recall over all 4; req/pol over governed repos only).
    lines.append("\n## Macro-average across repos\n")
    lines.append("| system | mean file F1 | mean file recall | mean req F1 (gov) | mean pol F1 (gov) |")
    lines.append("|--------|--------------|------------------|-------------------|-------------------|")
    for s in systems:
        rows = [r for r in per_repo if r["system"] == s]
        ff = [r["file_f1"] for r in rows]
        fr = [r["file_recall"] for r in rows]
        rq = [r["req_f1"] for r in rows if r.get("req_f1") is not None]
        po = [r["pol_f1"] for r in rows if r.get("pol_f1") is not None]
        lines.append(f"| {s} | {sum(ff)/len(ff):.3f} | {sum(fr)/len(fr):.3f} | "
                     f"{(sum(rq)/len(rq) if rq else 0):.3f} | {(sum(po)/len(po) if po else 0):.3f} |")

    md = "\n".join(lines)
    (OUT / "consolidated.md").write_text(md, encoding="utf-8")
    (OUT / "consolidated.json").write_text(json.dumps(per_repo, indent=2), encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
