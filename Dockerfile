FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md MANIFEST.in run.py run_uce.py ./
COPY core ./core
COPY ingestion ./ingestion
COPY neo4j_mcp ./neo4j_mcp
COPY reasoning ./reasoning
COPY runtime ./runtime
COPY server ./server
COPY scripts ./scripts
COPY .env.example ./.env.example

RUN python -m pip install --upgrade pip && \
    python -m pip install .

CMD ["uce", "--config", "/workspace/config.yaml"]
