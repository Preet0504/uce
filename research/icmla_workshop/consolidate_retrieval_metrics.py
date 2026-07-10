"""Generate RETRIEVAL_METRICS.md (F1 / precision / recall) from multi_repo/summary.json."""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "results" / "multi_repo"


def pct(x):
    return f"{x:.3f}" if x is not None else "n/a"


def main() -> None:
    data = json.loads((OUT / "summary.json").read_text(encoding="utf-8"))
    per_repo = data["per_repo"]
    pooled = data["pooled"]

    lines = [
        "# Retrieval metrics (impact prediction vs independent oracle)\n",
        "Micro-averaged precision / recall / F1 over all scenarios. "
        "UCE uses unfiltered `direct + transitive + call_chain` files from Neo4j.\n",
        "",
        "## Per repository\n",
        "| repo | system | file P | file R | file F1 | req P | req R | req F1 | pol P | pol R | pol F1 |",
        "|------|--------|--------|--------|---------|-------|-------|--------|-------|-------|--------|",
    ]
    for row in per_repo:
        lines.append(
            f"| {row['repo']} | {row['system']} | {pct(row['file_precision'])} | {pct(row['file_recall'])} | "
            f"{pct(row['file_f1'])} | {pct(row.get('req_precision'))} | {pct(row.get('req_recall'))} | "
            f"{pct(row.get('req_f1'))} | {pct(row.get('pol_precision'))} | {pct(row.get('pol_recall'))} | "
            f"{pct(row.get('pol_f1'))} |"
        )

    lines += [
        "",
        "## Pooled (micro over all scenarios, all repos)\n",
        "| system | file P | file R | file F1 | req P | req R | req F1 | pol P | pol R | pol F1 |",
        "|--------|--------|--------|---------|-------|-------|--------|-------|-------|--------|",
    ]
    for row in pooled:
        lines.append(
            f"| {row['system']} | {pct(row['file_precision'])} | {pct(row['file_recall'])} | "
            f"{pct(row['file_f1'])} | {pct(row['req_precision'])} | {pct(row['req_recall'])} | "
            f"{pct(row['req_f1'])} | {pct(row['pol_precision'])} | {pct(row['pol_recall'])} | "
            f"{pct(row['pol_f1'])} |"
        )

    # Macro mean of per-repo F1 (for comparison to prior tables)
    lines += [
        "",
        "## Macro-average of per-repo F1 (unweighted by scenario count)\n",
        "| system | mean file F1 | mean file R | mean req F1 | mean pol F1 |",
        "|--------|--------------|-------------|-------------|-------------|",
    ]
    for sysname in ("naive_edit", "lexical", "static", "uce"):
        rows = [r for r in per_repo if r["system"] == sysname]
        if not rows:
            continue
        mf = sum(r["file_f1"] for r in rows) / len(rows)
        mr = sum(r["file_recall"] for r in rows) / len(rows)
        rq = [r["req_f1"] for r in rows if r.get("req_f1") is not None]
        po = [r["pol_f1"] for r in rows if r.get("pol_f1") is not None]
        lines.append(
            f"| {sysname} | {mf:.3f} | {mr:.3f} | "
            f"{(sum(rq)/len(rq) if rq else 0):.3f} | {(sum(po)/len(po) if po else 0):.3f} |"
        )

    md = "\n".join(lines)
    (OUT / "RETRIEVAL_METRICS.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
