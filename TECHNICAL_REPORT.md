# Unified Context Engine (UCE): Deterministic Semantic Governance for Agentic Systems

## Abstract
Agentic systems often operate with fragmented context and limited impact awareness, which undermines enterprise trust. Probabilistic RAG pipelines can retrieve relevant text but do not provide deterministic, auditable reasoning paths or governance guarantees. This report presents the Unified Context Engine (UCE), a deterministic semantic grounding system that builds a Neo4j knowledge graph across code, database schema, requirements, policies, APIs, and services. UCE performs exact-match linking and multi-hop graph traversal to generate explainable impact analysis and calibrated risk scoring without embeddings or LLM-based retrieval. The system is designed to serve as a governance layer for agentic systems via an MCP server, enabling pre-execution validation with traceable reasoning.

## 1. Introduction
Enterprises face a trust gap when deploying autonomous or semi-autonomous agents because typical agent workflows lack explainability, determinism, and governance controls. When changes are proposed, agents often lack precise knowledge of downstream dependencies across domains such as code, database schemas, and policy constraints.

UCE addresses this gap by providing a deterministic, cross-domain semantic grounding layer. It links:
- Code artifacts (files and functions)
- Database schema (tables and columns)
- Requirements and policies
- API routes and service ownership

This enables deterministic reasoning across:
- Code to Schema
- Schema to Requirements to Policies
- Code to API to Service

The result is a verifiable reasoning trace and risk score suitable for enterprise validation.

## 2. System Architecture
UCE builds a graph in Neo4j and exposes deterministic analysis via a Python MCP server.

### 2.1 Node Types
- File {path}
- Function {name, file_path}
- Table {name}
- Column {name, table}
- Requirement {id, title, description}
- Policy {id, description}
- API {route, method}
- Service {name}

### 2.2 Relationships
- (File)-[:IMPORTS]->(File)
- (File)-[:DECLARES_FUNCTION]->(Function)
- (File)-[:USES_TABLE]->(Table)
- (Table)-[:HAS_COLUMN]->(Column)
- (Requirement)-[:GOVERNS]->(Table|Column)
- (Policy)-[:ENFORCES]->(Requirement)
- (Function)-[:EXPOSED_AS]->(API)
- (API)-[:BELONGS_TO]->(Service)

### 2.3 Deterministic Traversal
UCE performs bounded, multi-hop traversal with explicit relationship constraints. All linking is deterministic and exact-match, with no embeddings or fuzzy resolution. Trace paths are recorded as ordered node sequences.

## 3. Deterministic Reasoning Model
UCE’s reasoning model is strictly structural:
- Extract nodes via static parsing and deterministic ingestion
- Link entities using exact string matches or file path derivations
- Traverse graph with explicit Cypher patterns
- Return trace paths that reflect actual graph edges

This model guarantees:
- Reproducible results
- Auditable trace paths
- No nondeterministic retrieval components

## 4. Risk Model
UCE uses a deterministic risk score for explainable governance.

### 4.1 Formula
```
risk_score =
  2 * backend_files
  4 * violated_requirements
  6 * enforced_policies
  3 * affected_apis
```

### 4.2 Risk Bands
- Low: 0-9
- Moderate: 10-24
- High: 25-39
- Critical: 40+

These bands reflect enterprise expectations: governance violations and API exposure are weighted higher than raw file counts.

## 5. Evaluation Methodology
UCE is evaluated against a vanilla agent baseline that performs no deterministic impact analysis.

### 5.1 Baseline
- No requirement or policy detection
- No deterministic traversal
- No trace output or risk scoring

### 5.2 UCE
- Detects requirement and policy violations
- Produces deterministic trace paths
- Computes calibrated risk score
- Provides reproducible impact results

### 5.3 Metrics
- Violation detection rate
- Trace completeness
- Deterministic reproducibility
- Governance coverage

This report does not fabricate numeric results. The evaluation protocol is defined and can be executed with the provided runner.

## 6. Limitations
- No fuzzy linking across synonyms or aliases
- Requires structured requirements and policies
- Dependent on schema naming consistency
- Deterministic but not predictive of runtime behavior

## 7. Future Work
- Cross-repository graph linking
- Temporal versioned graphs for change history
- Automated requirement extraction from structured documents
- Integration with CI pipelines for pre-merge governance checks
