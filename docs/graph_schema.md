## UCE Graph Schema

This document describes the current graph schema. All nodes and relationships are created deterministically with `MERGE`.

## Node Types

- `File {path, id}`
- `Table {name}`
- `Column {name, table}`
- `Requirement {id, description}`
- `Policy {id, description}`
- `Function {name, file_path, id}`
- `Class {name, file_path, id}`
- `Method {name, file_path, class_name, id}`
- `Identifier {name}`
- `Role {name, rank}`
- `AuthorityRule {id, operation, path_pattern, min_role, effect, source_priority}`

## Relationships

- `(File)-[:IMPORTS]->(File)`
- `(File)-[:USES_TABLE]->(Table)`
- `(File)-[:REFERENCES_COLUMN]->(Column)`
- `(Table)-[:HAS_COLUMN]->(Column)`
- `(Requirement)-[:GOVERNS]->(Table|Column)`
- `(Policy)-[:ENFORCES]->(Requirement)`
- `(File)-[:DECLARES_FUNCTION]->(Function|Method)`
- `(File)-[:DECLARES_CLASS]->(Class)`
- `(Class)-[:HAS_METHOD]->(Method)`
- `(Function)-[:CALLS]->(Function)`
- `(File)-[:USES_IDENTIFIER]->(Identifier)`
- `(Policy)-[:DEFINES_RULE]->(AuthorityRule)`
- `(AuthorityRule)-[:REQUIRES_ROLE]->(Role)`

LLM-assisted relationships (optional when LLM ingestion is enabled):
- `(Requirement)-[:IMPLEMENTED_BY]->(File|Function|Class|Method)`
- `(Policy)-[:APPLIES_TO]->(File|Function|Class|Method)`
- `(Policy)-[:GOVERNS]->(Table|Column)`
