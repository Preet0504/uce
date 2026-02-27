# Unified Context Engine (UCE)

## What It Does
UCE is a deterministic semantic governance engine for agentic systems. It builds a Neo4j knowledge graph across code, schema, requirements, policies, APIs, and services, then performs multi-hop reasoning to produce explainable impact analysis and risk scoring.

## Why It Matters
Traditional RAG is probabilistic and not auditable. UCE provides deterministic, traceable reasoning paths that enterprises can validate before allowing agents to execute changes.

## Architecture Overview
- File graph and import dependencies
- Function graph and call edges
- Table/column schema layer
- Requirement and policy governance
- API exposure and service ownership
- Deterministic risk scoring

## Quick Start
```
python ingest/file_graph.py
python ingest/function_graph.py
python ingest/db_schema.py
python ingest/requirements.py
python ingest/policies.py
python ingest/api_graph.py
python -c "from impact import explain_change; print(explain_change('table','meetings'))"
```

## Example Output
```json
{
  "entity": "meetings",
  "affected_files": ["app/api/webhook/route.ts"],
  "affected_functions": ["POST@app/api/webhook/route.ts"],
  "affected_apis": ["POST /api/webhook"],
  "affected_services": ["webhook"],
  "violated_requirements": ["RQ-003"],
  "enforced_policies": ["P-001"],
  "risk_breakdown": {
    "backend_files": 1,
    "violated_requirements": 1,
    "enforced_policies": 1,
    "affected_apis": 1,
    "risk_score": 15
  },
  "risk_score": 15,
  "trace_paths": [
    "Table meetings -> File app/api/webhook/route.ts -> Function POST -> API POST /api/webhook -> Service webhook",
    "Table meetings -> Requirement RQ-003 -> Policy P-001"
  ]
}
```

## Risk Model
```
risk_score =
  2 * backend_files
  4 * violated_requirements
  6 * enforced_policies
  3 * affected_apis
```

## Roadmap
1. Cross-repository dependency linking
2. Temporal graph snapshots
3. CI integration for pre-merge governance
4. Schema change impact automation

## License
Apache 2.0
