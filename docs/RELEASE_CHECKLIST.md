# Release Checklist (PyPI Direct)

This checklist assumes the project is ready to publish and your PyPI credentials are configured.

## 1) Finalize Version

1. Update `version` in `pyproject.toml`.
2. Ensure CLI entry points are unchanged:
- `uce = "run:main"`
- `neo4j-mcp = "neo4j_mcp.server:cli"`

## 2) Build and Validate Artifacts

```bash
python -c "import shutil, pathlib; shutil.rmtree('dist', ignore_errors=True); pathlib.Path('dist').mkdir(exist_ok=True)"
python -m build --no-isolation
python -m twine check dist/uce_engine-0.2.1*
```

## 3) Publish to PyPI

```bash
python -m twine upload dist/uce_engine-0.2.1*
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
copy .env.docker.example .env.docker
# or: cp .env.docker.example .env.docker

docker compose --env-file .env.docker up -d --build
python scripts/bootstrap_keycloak.py --output-env-file .keycloak-secrets.env
```

Then verify:
- `http://localhost:8080/realms/uce-realm/.well-known/openid-configuration` is reachable.
- UCE MCP endpoint (`http://localhost:9001/mcp/`) is reachable.

## 6) Teardown (optional)

```bash
docker compose --env-file .env.docker down
```
