# Release Checklist (PyPI Direct)

This checklist assumes the project is ready to publish and your PyPI credentials are configured.

## 1) Finalize Version

1. Update `version` in `pyproject.toml`.
2. Ensure CLI entry points are unchanged (`pyproject.toml` `[project.scripts]`):
- `uce = "uce.cli:main"`
- `neo4j-mcp = "uce.neo4j_mcp.server:cli"`

## 2) Build and Validate Artifacts

```bash
python -c "import shutil, pathlib; shutil.rmtree('dist', ignore_errors=True); pathlib.Path('dist').mkdir(exist_ok=True)"
python -m build --no-isolation
python -m twine check dist/uce_engine-*
```

## 3) Publish to PyPI

```bash
python -m twine upload dist/uce_engine-*
```

## 4) Post-Publish Verification

In a fresh virtual environment:

```bash
python -m venv .venv-release
# Windows
.\.venv-release\Scripts\activate
# Linux/macOS
# source .venv-release/bin/activate

pip install --upgrade pip
pip install uce-engine
uce --help
neo4j-mcp --help
```

## 5) Smoke Check Docker Docs

Run the Docker quickstart exactly as documented:

```bash
copy docker\configs\client.env.example docker\configs\client.env
# or: cp docker/configs/client.env.example docker/configs/client.env
# edit docker/configs/client.env: set UCE_TARGET_REPO and an LLM provider key

docker compose -f docker/compose/client/docker-compose.client.yml --env-file docker/configs/client.env up -d --build
python scripts/bootstrap_keycloak.py --output-env-file .keycloak-secrets.env
```

Then verify:
- `http://localhost:8080/realms/uce-realm/.well-known/openid-configuration` is reachable.
- UCE MCP endpoint (`http://localhost:9001/mcp/`) is reachable.
- `propose_change` and `write_file` round-trip correctly with a freshly minted `gate_token` (see
  [CONNECTING_AI_ASSISTANTS.md](CONNECTING_AI_ASSISTANTS.md)) — not just that the endpoint answers.

## 6) Teardown (optional)

```bash
docker compose -f docker/compose/client/docker-compose.client.yml --env-file docker/configs/client.env down
```
