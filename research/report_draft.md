## Problem Statement
Vanilla agent execution can introduce unsafe changes because it does not deterministically quantify downstream impact across schema and code. This project evaluates whether a deterministic semantic grounding layer (UCE) reduces missed dependencies and improves explainability.

## Method
UCE builds a deterministic graph of files, tables, and columns and exposes impact analysis based on explicit graph traversal (no embeddings, no probabilistic retrieval). We compare UCE impact detection against a naive baseline (vanilla) that only detects the schema file for schema changes, or only the directly edited file for file changes.

## Experimental Setup
- Project: TalkAI (TypeScript full-stack)
- Graph inputs: file dependencies, database schema (tables + columns), and deterministic linking
- Scenarios: controlled change tasks (table, column, file)
- Evaluation runner: script MCP tool calls (impact/risk) and collect results
- Outputs: results.json, summary.json, optional plots

## Results
Populate this section after running the evaluation.
- Average UCE coverage: summary.json
- Average vanilla coverage: summary.json
- Safety improvement: summary.json
- Risk score distribution: plots (optional)
- Scenario comparison: plots (optional)

## Discussion
Interpret the magnitude of coverage and safety improvements. Highlight that UCE provides deterministic risk scoring and explainability via known graph traversal and Cypher queries, while vanilla provides no structured impact analysis.

## Limitations
- Coverage is computed using UCE output as ground truth.
- Column linking is string-based and may miss camelCase references.
- UI and presentation layers are not part of the analysis scope.
