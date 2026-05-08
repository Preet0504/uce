# Supplemental Benchmarks / Report Reproducibility (UCE)

This guide documents exactly how to regenerate the empirical results and report artifacts shipped in this repo.

Important:

- The canonical accurate report is `research/final_report/CS540_Final_Project_Report_UCE_Preet_Patel.docx`.
- The `research/supplemental_benchmarks/` directory is supplemental experiment material and intermediate publication-style artifacts.

## Scope

Reproducible artifacts live under:

- `research/supplemental_benchmarks/results/`
- `research/supplemental_benchmarks/results/tables/`
- `research/supplemental_benchmarks/results/figures/`
- `research/supplemental_benchmarks/results/real_llm_baseline/`
- `research/final_report/CS540_Final_Project_Report_UCE_Preet_Patel.docx`

## 1) Environment Setup

From repo root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
pip install pandas matplotlib python-docx
```

Why the extra install:

- `pandas` + `matplotlib` are used by benchmark/eval scripts.
- `python-docx` is used to generate the final report DOCX.

## 2) Prerequisites for Full Regeneration

You need:

- Neo4j running and reachable from `config.yaml`.
- A valid `config.yaml` that points `project_root` to the evaluated codebase.
- The target project must contain:
  - `src/db/schema.ts`
  - `src/requirements/*.md`
  - `src/policies/*.md`
  - `src/rbac/*.md`

If your setup differs, update `config.yaml` first.

## 3) Regenerate Deterministic UCE Benchmark Outputs

From repo root:

```powershell
python research\supplemental_benchmarks\run_benchmark.py --config config.yaml
```

This regenerates:

- `research/supplemental_benchmarks/results/scenario_results.csv`
- `research/supplemental_benchmarks/results/rbac_probe_results.csv`
- `research/supplemental_benchmarks/results/summary.json`
- `research/supplemental_benchmarks/results/tables/overall_metrics.csv`
- `research/supplemental_benchmarks/results/tables/metrics_by_entity_type.csv`
- `research/supplemental_benchmarks/results/tables/violation_metrics.csv`
- `research/supplemental_benchmarks/results/tables/rbac_breach_metrics.csv`
- `research/supplemental_benchmarks/results/figures/*.png`

## 4) Regenerate Tables/Figures Only (No Neo4j Rerun)

If `scenario_results.csv` already exists and you only want refreshed aggregates/plots:

```powershell
python research\supplemental_benchmarks\run_benchmark.py --config config.yaml --postprocess-only
```

## 5) Regenerate Real LLM Baseline Comparison

This re-runs the no-tool LLM baseline and compares it against existing MCP-UCE benchmark outputs.

### Option A: OpenAI

```powershell
$env:OPENAI_API_KEY="<your_key>"
$env:OPENAI_MODEL="gpt-4o-mini"
python research\supplemental_benchmarks\run_real_llm_baseline.py --config config.yaml --provider openai
```

### Option B: Local OpenAI-compatible endpoint

```powershell
$env:LOCAL_LLM_BASE_URL="http://127.0.0.1:11434/v1"
$env:LOCAL_LLM_MODEL="llama3:instruct"
python research\supplemental_benchmarks\run_real_llm_baseline.py --config config.yaml --provider local
```

Outputs:

- `research/supplemental_benchmarks/results/real_llm_baseline/scenario_predictions.csv`
- `research/supplemental_benchmarks/results/real_llm_baseline/scenario_eval.csv`
- `research/supplemental_benchmarks/results/real_llm_baseline/scenario_comparison_summary.csv`
- `research/supplemental_benchmarks/results/real_llm_baseline/rbac_eval.csv`
- `research/supplemental_benchmarks/results/real_llm_baseline/rbac_comparison_summary.csv`
- `research/supplemental_benchmarks/results/real_llm_baseline/raw_scenario_responses.jsonl`
- `research/supplemental_benchmarks/results/real_llm_baseline/raw_rbac_responses.jsonl`
- `research/supplemental_benchmarks/results/real_llm_baseline/summary.json`
- `research/supplemental_benchmarks/results/figures/real_llm_requirement_policy_violation.png`
- `research/supplemental_benchmarks/results/figures/real_llm_rbac_breach_rate.png`

## 6) Regenerate Final Course Report DOCX

From repo root:

```powershell
python research\final_report\create_cs540_final_report.py
```

Output:

- `research/final_report/CS540_Final_Project_Report_UCE_Preet_Patel.docx`

Important: this script expects benchmark outputs from Steps 3 and 5 to exist first.

## 7) Fast End-to-End Reproduction Command Sequence

```powershell
python research\supplemental_benchmarks\run_benchmark.py --config config.yaml
python research\supplemental_benchmarks\run_real_llm_baseline.py --config config.yaml --provider local
python research\final_report\create_cs540_final_report.py
```

## 8) Sanity Checks

After regeneration, verify these files were updated recently:

- `research/supplemental_benchmarks/results/summary.json`
- `research/supplemental_benchmarks/results/tables/overall_metrics.csv`
- `research/supplemental_benchmarks/results/real_llm_baseline/summary.json`
- `research/final_report/CS540_Final_Project_Report_UCE_Preet_Patel.docx`


