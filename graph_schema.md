## UCE Graph Schema

This document describes the current graph schema and the planned ontology extensions.
All nodes and relationships are created deterministically with `MERGE`.

## Node Types

Existing:
- `File {path}`
- `Table {name}`
- `Column {name, table}`
- `Requirement {id, title, description}`

Grant-Level Extensions:
- `Function {name, file_path}`
- `API {route, method}`
- `Service {name}`
- `Policy {id, description}`
- `Migration {name}`

## Relationships

Existing:
- `(File)-[:IMPORTS]->(File)`
- `(File)-[:USES_TABLE]->(Table)`
- `(File)-[:REFERENCES_COLUMN]->(Column)`
- `(Table)-[:HAS_COLUMN]->(Column)`
- `(Requirement)-[:GOVERNS]->(Table|Column)`

Grant-Level Extensions:
- `(File)-[:DECLARES_FUNCTION]->(Function)`
- `(Function)-[:CALLS]->(Function)`
- `(Function)-[:EXPOSED_AS]->(API)`
- `(API)-[:BELONGS_TO]->(Service)`
- `(Policy)-[:ENFORCES]->(Requirement)`
- `(Migration)-[:MODIFIES]->(Table|Column)`

## Helper Functions

Helper functions are provided in `graph.py` for deterministic creation and linking:
- `create_function(graph, name, file_path)`
- `link_function_call(graph, caller_name, caller_file, callee_name, callee_file)`
- `link_function_to_api(graph, function_name, function_file, route, method, service_name=None)`
- `create_service(graph, name)`
- `create_policy(graph, policy_id, description, requirement_id=None)`
- `create_migration(graph, name, table_name=None, column_name=None)`

These helpers use `MERGE` for idempotency and do not alter existing ingestion logic.
