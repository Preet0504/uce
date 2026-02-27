## Problem Statement
Vanilla agent execution can introduce unsafe changes because it does not deterministically quantify downstream impact across schema and code. This project evaluates whether a deterministic semantic grounding layer (UCE) reduces missed dependencies and improves explainability.

## Method
UCE builds a deterministic graph of files, tables, and columns and exposes impact analysis based on explicit graph traversal (no embeddings, no probabilistic retrieval). We compare UCE impact detection against a naive baseline (vanilla) that only detects the schema file for schema changes, or only the directly edited file for file changes.

## Experimental Setup
- Project: TalkAI (TypeScript full-stack)
- Graph inputs: file dependencies, database schema (tables + columns), and deterministic linking
- Scenarios: 18 controlled change tasks in `evaluation/scenarios.json`
- Evaluation runner: `evaluation/run_evaluation.py`
- Outputs: `evaluation/results.json`, `evaluation/summary.json`, charts in `evaluation/plots/`

## Results
Populate this section after running the evaluation.
- Average UCE coverage: see `evaluation/summary.json`
- Average vanilla coverage: see `evaluation/summary.json`
- Safety improvement: see `evaluation/summary.json`
- Risk score distribution: `evaluation/plots/risk_hist.png`
- Scenario comparison: `evaluation/plots/scenario_comparison_table.png`

## Discussion
Interpret the magnitude of coverage and safety improvements. Highlight that UCE provides deterministic risk scoring and explainability via known graph traversal and Cypher queries, while vanilla provides no structured impact analysis.

## Limitations
- Coverage is computed using UCE output as ground truth.
- Column linking is string-based and may miss camelCase references.
- UI and presentation layers are not part of the analysis scope.
