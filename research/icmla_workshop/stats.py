"""
Statistical analysis of the independent evaluation results.

Adds the rigor that distinguishes a publishable empirical claim from an anecdote:
  - Paired bootstrap 95% confidence intervals on micro-averaged F1 (resampling scenarios).
  - Wilcoxon signed-rank test on paired per-scenario F1 differences (UCE vs each baseline).
  - McNemar's exact test on the paired binary outcome "caught >=1 governed requirement".
  - Cliff's delta effect size for the paired F1 differences.

Reads:  results/independent_scenario_results.csv
Writes: results/tables/independent_significance.csv
        results/independent_stats.json
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "results"
TABLES_DIR = RESULTS_DIR / "tables"

RNG = np.random.default_rng(20260603)
N_BOOT = 10000


def micro_f1(df: pd.DataFrame, prefix: str) -> float:
    tp = df[f"{prefix}_tp"].sum()
    fp = df[f"{prefix}_fp"].sum()
    fn = df[f"{prefix}_fn"].sum()
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    return (2 * p * r / (p + r)) if (p + r) else 0.0


def bootstrap_f1_ci(df_system: pd.DataFrame, prefix: str, n_boot: int = N_BOOT):
    """Bootstrap a 95% CI for micro-F1 by resampling scenarios with replacement."""
    rows = df_system.reset_index(drop=True)
    n = len(rows)
    tp = rows[f"{prefix}_tp"].to_numpy()
    fp = rows[f"{prefix}_fp"].to_numpy()
    fn = rows[f"{prefix}_fn"].to_numpy()
    boots = np.empty(n_boot)
    for b in range(n_boot):
        idx = RNG.integers(0, n, n)
        TP, FP, FN = tp[idx].sum(), fp[idx].sum(), fn[idx].sum()
        p = TP / (TP + FP) if (TP + FP) else 0.0
        r = TP / (TP + FN) if (TP + FN) else 0.0
        boots[b] = (2 * p * r / (p + r)) if (p + r) else 0.0
    point = micro_f1(rows, prefix)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return point, float(lo), float(hi)


def cliffs_delta(a: np.ndarray, b: np.ndarray) -> float:
    """Cliff's delta effect size for paired/independent samples (a vs b)."""
    n, m = len(a), len(b)
    if n == 0 or m == 0:
        return 0.0
    gt = sum(1 for x in a for y in b if x > y)
    lt = sum(1 for x in a for y in b if x < y)
    return (gt - lt) / (n * m)


def wilcoxon_paired(df: pd.DataFrame, system_a: str, system_b: str, metric: str):
    a = df[df["system"] == system_a].sort_values("scenario_id")[metric].to_numpy()
    b = df[df["system"] == system_b].sort_values("scenario_id")[metric].to_numpy()
    diff = a - b
    if np.allclose(diff, 0):
        return {"a": system_a, "b": system_b, "metric": metric,
                "median_diff": 0.0, "statistic": None, "p_value": 1.0,
                "cliffs_delta": 0.0, "n": int(len(diff))}
    try:
        statw, p = stats.wilcoxon(a, b, zero_method="wilcox", alternative="two-sided")
        statw = float(statw)
    except ValueError:
        statw, p = None, 1.0
    return {
        "a": system_a, "b": system_b, "metric": metric,
        "median_diff": float(np.median(diff)),
        "mean_diff": float(np.mean(diff)),
        "statistic": statw, "p_value": float(p),
        "cliffs_delta": cliffs_delta(a, b),
        "n": int(len(diff)),
    }


def mcnemar_caught(df: pd.DataFrame, system_a: str, system_b: str):
    """McNemar exact test on paired binary 'caught >=1 governed requirement'."""
    a = df[(df["system"] == system_a) & (df["req_caught_any"] >= 0)].sort_values("scenario_id")
    b = df[(df["system"] == system_b) & (df["req_caught_any"] >= 0)].sort_values("scenario_id")
    merged = a[["scenario_id", "req_caught_any"]].merge(
        b[["scenario_id", "req_caught_any"]], on="scenario_id", suffixes=("_a", "_b")
    )
    n01 = int(((merged["req_caught_any_a"] == 0) & (merged["req_caught_any_b"] == 1)).sum())
    n10 = int(((merged["req_caught_any_a"] == 1) & (merged["req_caught_any_b"] == 0)).sum())
    n11 = int(((merged["req_caught_any_a"] == 1) & (merged["req_caught_any_b"] == 1)).sum())
    n00 = int(((merged["req_caught_any_a"] == 0) & (merged["req_caught_any_b"] == 0)).sum())
    # exact binomial test on discordant pairs
    n = n01 + n10
    if n == 0:
        p = 1.0
    else:
        p = float(stats.binomtest(min(n01, n10), n, 0.5, alternative="two-sided").pvalue)
    return {
        "a": system_a, "b": system_b,
        "a_only_caught": n10, "b_only_caught": n01,
        "both_caught": n11, "neither": n00,
        "discordant": n, "p_value": p,
    }


def main() -> None:
    df = pd.read_csv(RESULTS_DIR / "independent_scenario_results.csv")
    systems = ["naive_edit", "lexical", "uce"]

    # 1) Bootstrap CIs
    ci_rows = []
    for system in systems:
        sub = df[df["system"] == system]
        for prefix, label in [("file", "file_f1"), ("req", "requirement_f1"), ("pol", "policy_f1")]:
            point, lo, hi = bootstrap_f1_ci(sub, prefix)
            ci_rows.append({"system": system, "metric": label,
                            "f1": round(point, 4), "ci95_low": round(lo, 4), "ci95_high": round(hi, 4)})
    ci_df = pd.DataFrame(ci_rows)

    # 2) Paired significance: UCE vs each baseline
    sig_rows = []
    for baseline in ["naive_edit", "lexical"]:
        for metric in ["file_f1", "req_f1", "pol_f1"]:
            sig_rows.append(wilcoxon_paired(df, "uce", baseline, metric))
    sig_df = pd.DataFrame(sig_rows)

    # 3) McNemar on requirement-catch
    mc_rows = [mcnemar_caught(df, "uce", "naive_edit"), mcnemar_caught(df, "uce", "lexical")]
    mc_df = pd.DataFrame(mc_rows)

    TABLES_DIR.mkdir(parents=True, exist_ok=True)
    ci_df.to_csv(TABLES_DIR / "independent_bootstrap_ci.csv", index=False)
    sig_df.to_csv(TABLES_DIR / "independent_wilcoxon.csv", index=False)
    mc_df.to_csv(TABLES_DIR / "independent_mcnemar.csv", index=False)

    out = {
        "bootstrap_ci": ci_rows,
        "wilcoxon_uce_vs_baselines": sig_df.to_dict(orient="records"),
        "mcnemar_requirement_catch": mc_df.to_dict(orient="records"),
        "n_boot": N_BOOT,
    }
    (RESULTS_DIR / "independent_stats.json").write_text(json.dumps(out, indent=2), encoding="utf-8")

    pd.set_option("display.width", 160)
    print("=== Bootstrap 95% CIs (micro-F1, resampling scenarios) ===")
    print(ci_df.to_string(index=False))
    print("\n=== Wilcoxon signed-rank (UCE vs baseline, per-scenario F1) ===")
    print(sig_df[["a", "b", "metric", "mean_diff", "p_value", "cliffs_delta"]].to_string(index=False))
    print("\n=== McNemar exact (caught >=1 governed requirement) ===")
    print(mc_df.to_string(index=False))


if __name__ == "__main__":
    main()
