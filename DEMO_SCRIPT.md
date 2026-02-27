# UCE 5-Minute Demo Script

## 0:00-0:30 — Problem
"Agentic systems often change code without deterministic context. Traditional RAG is probabilistic and not auditable. Enterprises need deterministic governance."

## 0:30-1:30 — Ingestion
Show ingestion commands:
```
python ingest/file_graph.py
python ingest/function_graph.py
python ingest/db_schema.py
python ingest/requirements.py
python ingest/policies.py
python ingest/api_graph.py
```
Explain that this creates a multi-layer graph across code, schema, requirements, policies, APIs, and services.

## 1:30-3:00 — Explain Change
Run:
```
python -c "from impact import explain_change; print(explain_change('table','meetings'))"
```
Highlight:
- Backend-only file filtering
- API and service linkage
- Requirement and policy traces
- Risk breakdown

## 3:00-4:00 — Risk Scoring
Explain the calibrated formula and how governance layers influence risk:
- Requirements and policies increase risk
- APIs add risk due to exposure

## 4:00-5:00 — Enterprise Contrast
Contrast with vanilla agent behavior:
- No deterministic impact
- No governance trace
- No auditable reasoning
UCE adds pre-execution validation and trust.
