# Enforcement evaluation (canonical metrics)

Agent: Claude (no tools). UCE: live Neo4j graph after deterministic ingest.

Gate fires if: agent missed files UCE flagged, OR silent requirement, OR RBAC breach.


## Per repository

| repo | n | catch_rate | agent_self_catch | false_gate | mean_missed_files | mean_agent_files | mean_uce_files | silent_req_rate |
|------|---|------------|------------------|------------|-------------------|------------------|----------------|-----------------|
| talkai | 24 | 100.0% | 8.3% | 0.0% | 64 | 3.3 | 66.7 | 29.2% |
| melodi | 33 | 100.0% | 6.1% | 0.0% | 20 | 4.5 | 23.3 | 30.3% |
| expenses | 9 | 100.0% | 33.3% | 0.0% | 3 | 2.6 | 6.0 | 0.0% |
| spark | 48 | 100.0% | 2.1% | 2.1% | 58 | 3.7 | 61.3 | n/a |

## Pooled (all scenarios, all repos)

- **n_scenarios**: 114
- **UCE catch_rate**: 100.0%
- **agent_self_catch_rate**: 7.0%
- **false_gate_rate**: 0.9%
- **mean_missed_files** (UCE \ agent per scenario): 43.89
- **median_missed_files**: 55
- **mean files agent declared**: 3.73
- **mean files UCE flagged**: 47.08
- **permission_breach_count** (agent allowed, RBAC denied): 0
- **silent_requirement_scenario_rate**: 14.9%