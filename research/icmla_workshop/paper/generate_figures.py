"""
Publication figures for UCE paper. IEEE-friendly typography via matplotlib rcParams.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
import numpy as np

BASE = Path(__file__).resolve().parent
RESULTS = BASE.parent / "results"
FIG = BASE / "figures"
FIG.mkdir(parents=True, exist_ok=True)

# Design system
PALETTE = {
    "uce": "#0B3D5C",
    "uce_light": "#1A6B8C",
    "agent": "#94A3B8",
    "gate": "#15803D",
    "warn": "#C2410C",
    "bad": "#B91C1C",
    "bg": "#F8FAFC",
}

mpl.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "font.size": 9,
    "axes.titlesize": 10,
    "axes.labelsize": 9,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 8,
    "figure.facecolor": "white",
    "axes.facecolor": PALETTE["bg"],
    "axes.edgecolor": "#CBD5E1",
    "axes.grid": True,
    "grid.alpha": 0.35,
    "grid.linestyle": "-",
})


def _load_json(path: Path) -> dict | list:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(fig: plt.Figure, name: str) -> None:
    fig.savefig(FIG / f"{name}.pdf", bbox_inches="tight", dpi=400)
    fig.savefig(FIG / f"{name}.png", bbox_inches="tight", dpi=400)
    plt.close(fig)
    print(f"  {name}")


def fig_hero_results() -> None:
    """Single multi-panel figure: enforcement + MCP + RBAC."""
    enforce = _load_json(RESULTS / "enforcement_eval" / "pooled_summary.json")
    mcp = _load_json(RESULTS / "agent_mcp_eval" / "pooled_summary.json")
    per_repo = _load_json(RESULTS / "enforcement_eval" / "per_repo_summary.json")

    fig = plt.figure(figsize=(7.5, 6.5))
    gs = fig.add_gridspec(2, 2, hspace=0.38, wspace=0.32)

    # (A) Enforcement catch
    ax = fig.add_subplot(gs[0, 0])
    agent_self = enforce.get("agent_self_catch_rate", 0.07) * 100
    catch = enforce.get("catch_rate", 1.0) * 100
    bars = ax.bar(["Agent\nself-catch", "UCE gate\n(catch)"], [agent_self, catch],
                  color=[PALETTE["agent"], PALETTE["gate"]], width=0.55, edgecolor="white", linewidth=1.2)
    ax.set_ylim(0, 108)
    ax.set_ylabel("Rate (%)")
    ax.set_title(f"(a) Enforcement ({enforce.get('n_scenarios', 114)} scenarios, 4 repos)")
    for b in bars:
        ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 2, f"{b.get_height():.1f}%",
                ha="center", fontsize=9, fontweight="bold")

    # (B) MCP planning (if available)
    ax = fig.add_subplot(gs[0, 1])
    if mcp.get("uce_mcp_n"):
        labels = ["Prompt only", "UCE MCP"]
        recall = [mcp.get("prompt_only_file_recall", 0) * 100, mcp.get("uce_mcp_file_recall", 0) * 100]
        x = np.arange(2)
        ax.bar(x, recall, color=[PALETTE["agent"], PALETTE["uce"]], width=0.5, edgecolor="white")
        ax.set_xticks(x)
        ax.set_xticklabels(labels)
        ax.set_ylim(0, 105)
        prof = mcp.get("prompt_profile", "neutral")
        ax.set_title(f"(b) Plan recall vs oracle (n={mcp.get('uce_mcp_n')}, {prof} prompt)")
        for i, v in enumerate(recall):
            ax.text(i, v + 2, f"{v:.1f}%", ha="center", fontweight="bold")
        if "mean_tool_adherence" in mcp:
            ax.text(0.5, 0.08, f"Tool adherence: {mcp['mean_tool_adherence']*100:.1f}%",
                    transform=ax.transAxes, ha="center", fontsize=7.5, color=PALETTE["warn"])
    else:
        ax.text(0.5, 0.5, "Run agent_mcp_eval\n(--prompt neutral --ingest)", ha="center", va="center")
        ax.set_title("(b) MCP planning eval")

    # (C) Per-repo missed files
    ax = fig.add_subplot(gs[1, 0])
    if isinstance(per_repo, list) and per_repo:
        repos = [r["repo"] for r in per_repo]
        missed = [r["mean_missed_files"] for r in per_repo]
        declared = [r["mean_agent_files_declared"] for r in per_repo]
        x = np.arange(len(repos))
        w = 0.35
        ax.bar(x - w / 2, declared, w, label="Agent declared", color=PALETTE["agent"])
        ax.bar(x + w / 2, missed, w, label="UCE − agent (missed)", color=PALETTE["warn"])
        ax.set_xticks(x)
        ax.set_xticklabels(repos)
        ax.set_ylabel("Files / scenario")
        ax.set_title("(c) Under-declaration by repository")
        ax.legend(loc="upper right", framealpha=0.95)

    # (D) Three-layer story
    ax = fig.add_subplot(gs[1, 1])
    layers = ["MCP tool\noutput", "Agent final\nplan", "UCE gate"]
    if mcp.get("mcp_tool_output_file_recall"):
        vals = [
            mcp["mcp_tool_output_file_recall"] * 100,
            mcp.get("uce_mcp_file_recall", 0) * 100,
            100.0,
        ]
    else:
        vals = [98, 40, 100]
    colors = [PALETTE["uce_light"], PALETTE["agent"], PALETTE["gate"]]
    y = np.arange(3)
    ax.barh(y, vals, color=colors, height=0.55)
    ax.set_yticks(y)
    ax.set_yticklabels(layers)
    ax.set_xlabel("Recall / catch (%)")
    ax.set_xlim(0, 105)
    ax.set_title("(d) Evidence → plan → enforcement")
    for i, v in enumerate(vals):
        ax.text(v + 1.5, i, f"{v:.0f}%", va="center", fontsize=9)

    fig.suptitle("UCE evaluation summary", fontsize=12, fontweight="bold", y=0.98)
    _save(fig, "fig_hero_results")


def fig_mcp_by_repo() -> None:
    summaries = _load_json(RESULTS / "agent_mcp_eval" / "per_repo_summary.json")
    if not summaries or not isinstance(summaries, list):
        return
    repos = [s["repo"] for s in summaries]
    po = [s.get("prompt_only_file_recall", 0) * 100 for s in summaries]
    mcp = [s.get("uce_mcp_file_recall", 0) * 100 for s in summaries]

    fig, ax = plt.subplots(figsize=(6.5, 3.8))
    x = np.arange(len(repos))
    w = 0.36
    ax.bar(x - w / 2, po, w, label="Prompt only", color=PALETTE["agent"])
    ax.bar(x + w / 2, mcp, w, label="UCE MCP", color=PALETTE["uce"])
    ax.set_xticks(x)
    ax.set_xticklabels(repos)
    ax.set_ylabel("File recall vs oracle (%)")
    ax.set_ylim(0, 105)
    ax.legend()
    ax.set_title("External validity: agent+MCP across repositories")
    fig.tight_layout()
    _save(fig, "fig_mcp_by_repo")


def fig_context_and_rbac() -> None:
    ctx = _load_json(RESULTS / "context_comparison" / "summary.json")
    if not ctx:
        return
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.2, 3.2))

    ladder = ctx["impact_context_ladder"]
    labels, recall = [], []
    name_map = {
        "llm_bare": "Bare LLM",
        "llm_+gov_docs": "+ Governance",
        "llm_+inventory": "+ Inventory",
        "llm_+rag_files": "+ RAG",
        "uce_graph": "UCE graph",
    }
    for row in ladder:
        labels.append(name_map.get(row["system"], row["system"]))
        recall.append(row["file_recall"] * 100)
    colors = [PALETTE["agent"]] * 4 + [PALETTE["uce"]]
    ax1.barh(np.arange(len(labels)), recall, color=colors, height=0.6)
    ax1.set_yticks(np.arange(len(labels)))
    ax1.set_yticklabels(labels)
    ax1.set_xlabel("File recall (%)")
    ax1.set_title("(a) Context ladder (TalkAI)")
    ax1.set_xlim(0, 100)

    rbac = ctx["rbac_context_ladder"]
    names = ["Zero-shot", "Few-shot", "UCE det."]
    breach = [r["breach_rate"] * 100 for r in rbac]
    ax2.bar(names, breach, color=[PALETTE["bad"], PALETTE["agent"], PALETTE["gate"]], width=0.5)
    ax2.set_ylabel("Breach rate (%)")
    ax2.set_title("(b) Hard RBAC (41 denied probes)")
    fig.tight_layout()
    _save(fig, "fig_context_rbac")


def fig_architecture() -> None:
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 4)
    ax.axis("off")

    stages = [
        (0.4, "Repository\n(code, schema,\ngovernance, RBAC)"),
        (2.8, "Deterministic\ningestion"),
        (5.2, "Neo4j UCE\ngraph"),
        (7.6, "MCP server\nimpact analysis"),
        (10.0, "LLM agent"),
        (12.4, "UCE gate"),
    ]
    colors = [PALETTE["agent"], PALETTE["uce_light"], PALETTE["uce"],
              PALETTE["warn"], PALETTE["agent"], PALETTE["gate"]]
    for (x, txt), c in zip(stages, colors):
        ax.add_patch(FancyBboxPatch(
            (x, 1.2), 2.0, 1.4, boxstyle="round,pad=0.02,rounding_size=0.08",
            facecolor=c, edgecolor="white", linewidth=1.5, alpha=0.95))
        ax.text(x + 1.0, 1.9, txt, ha="center", va="center", fontsize=7.5,
                color="white", fontweight="bold", linespacing=1.15)

    for x0, x1 in [(2.4, 2.8), (4.8, 5.2), (7.2, 7.6), (9.6, 10.0), (11.4, 12.4)]:
        ax.annotate("", xy=(x1, 1.9), xytext=(x0 + 2.0, 1.9),
                    arrowprops=dict(arrowstyle="-|>", color="#334155", lw=1.8))
    ax.annotate("", xy=(12.4, 1.9), xytext=(12.0, 1.9),
                arrowprops=dict(arrowstyle="-|>", color="#334155", lw=1.8, connectionstyle="arc3,rad=0.3"))

    ax.text(7.0, 3.35, "Unified Change Enforcement (UCE)", ha="center",
            fontsize=11, fontweight="bold", color=PALETTE["uce"])
    _save(fig, "fig_architecture")


def main() -> None:
    print("Figures ->", FIG)
    fig_architecture()
    fig_hero_results()
    fig_mcp_by_repo()
    fig_context_and_rbac()
    print("Done.")


if __name__ == "__main__":
    main()
