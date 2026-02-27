# Unified Context Engine (UCE) Developer Tutorial

## 1. Installation

### Python Setup
Create and activate a virtual environment, then install dependencies required by UCE and Neo4j.

### Neo4j Setup
Start Neo4j locally and set credentials in `config.py`:
- `NEO4J_URI`
- `NEO4J_USER`
- `NEO4J_PASS`
- `PROJECT_ROOT`

## 2. Ingestion Pipeline

Run ingestion in this order:

### File Graph
```
python ingest/file_graph.py
```

### Function Graph
```
python ingest/function_graph.py
```

### Database Schema
```
python ingest/db_schema.py
```

### Requirements
```
python ingest/requirements.py
```

### Policies
```
python ingest/policies.py
```

### API Layer and Service Linking
```
python ingest/api_graph.py
```

## 3. Running Impact Analysis

Example:
```
python -c "from impact import explain_change; print(explain_change('table','meetings'))"
```

This returns a structured JSON response with:
- affected_files
- affected_functions
- affected_apis
- affected_services
- violated_requirements
- enforced_policies
- risk_score
- trace_paths

## 4. Understanding Trace Output

Trace paths are deterministic and hierarchical:
- `Table -> File -> Function -> API -> Service`
- `Table -> Requirement -> Policy`

Example:
```
Table meetings -> File app/api/webhook/route.ts -> Function POST -> API POST /api/webhook -> Service webhook
Table meetings -> Requirement RQ-003 -> Policy P-001
```

## 5. Extending the Graph

### Add Requirements
Create a new markdown file in `requirements/` with:
- `ID: RQ-XXX`
- `Title: ...`
- `Description: ...`

Run:
```
python ingest/requirements.py
```

### Add Policies
Create a markdown file in `policies/` with:
- `ID: P-XXX`
- `Description: ...`
- `Enforces: RQ-XXX, RQ-YYY`

Run:
```
python ingest/policies.py
```

### Add Service Layers
Create service-related code in `modules/<service>/server/` or an API route in `app/api/`.
Run:
```
python ingest/api_graph.py
```
