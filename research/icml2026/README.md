# ICML 2026 Paper Artifact (UCE)

This directory contains a fully reproducible paper artifact for the UCE project:

- Benchmark runner: `run_benchmark.py`
- Metrics and plots: `results/`
- Manuscript source: `paper/uce_icml2026.tex`
- Compiled PDF: `paper/uce_icml2026.pdf`

## 1) Re-run Benchmarks

From repo root:

```powershell
.\.venv-release\Scripts\python.exe research\icml2026\run_benchmark.py --config config.yaml
```

This regenerates:

- `research/icml2026/results/scenario_results.csv`
- `research/icml2026/results/tables/overall_metrics.csv`
- `research/icml2026/results/tables/metrics_by_entity_type.csv`
- `research/icml2026/results/figures/*.png`
- `research/icml2026/results/summary.json`

## 2) Build PDF

From `research/icml2026/paper`:

```powershell
pdflatex -interaction=nonstopmode uce_icml2026.tex
bibtex uce_icml2026
pdflatex -interaction=nonstopmode uce_icml2026.tex
pdflatex -interaction=nonstopmode uce_icml2026.tex
```

Output:

- `research/icml2026/paper/uce_icml2026.pdf`
