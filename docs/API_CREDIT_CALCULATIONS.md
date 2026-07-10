# Claude API Credit Calculations

This file records the working estimate used for UCE Claude API experiments.

## Assumptions

- Main model: `claude-sonnet-4-5`
- Bulk/cheap model option: `claude-haiku-4-5`
- High-quality comparison option: Opus, used sparingly
- Workload includes LLM ingestion plus benchmark/evaluation reruns

## Current Measured Baseline

From saved UCE benchmark prompt/response logs:

- Scenario baseline batches: about 4,068 input tokens and 2,035 output tokens
- RBAC baseline batches: about 1,402 input tokens and 1,162 output tokens
- Tool-assisted latency batches: about 5,876 input tokens and 1,894 output tokens

Measured experiment subtotal:

- Input: about 11,346 tokens
- Output: about 5,091 tokens

## Estimated LLM Ingestion Cycle

One full LLM ingestion pass over requirements, policies, and RBAC docs is estimated at:

- Input: 150,000 to 350,000 tokens
- Output: 20,000 to 70,000 tokens

Planning point:

- Input: 200,000 tokens
- Output: 40,000 tokens

## Research Cycle Estimate

One research cycle equals one ingestion pass plus one benchmark/evaluation pass:

- Input: about 211,000 tokens
- Output: about 45,000 tokens

## Budget Planning

Approximate usage for repeated experimentation:

- 50 cycles: about 10.6M input tokens and 2.25M output tokens
- 100 cycles: about 21.1M input tokens and 4.5M output tokens

Recommended credit request:

- Minimum demo budget: about $20
- Practical research budget: about $200
- Heavy Opus or many reruns: $300+

Use Haiku for cheap iteration, Sonnet for main results, and Opus only for small high-quality comparison runs.
