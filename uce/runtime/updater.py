import logging
import os
from contextlib import contextmanager
from typing import Iterable

from uce.core.config import UceConfig, resolve_paths
from uce.core.graph_db import GraphDB
from uce.ingestion import code_parser, schema_parser
from uce.ingestion.graph_builder import (
    is_ignored,
    ensure_relative,
    normalize_path,
    upsert_code_file,
    upsert_schema,
    load_tables,
    load_columns,
    link_tables_for_file,
    resolve_import,
)


class GraphUpdater:
    def __init__(self, config: UceConfig, graph: GraphDB, logger: logging.Logger | None = None):
        self.config = config
        self.graph = graph
        self.paths = resolve_paths(config)
        self.logger = logger or logging.getLogger("uce.updater")
        self.extensions = self._extensions_for_languages(config.languages)
        self.identifier_paths = self._resolve_identifier_paths()
        self.identifier_max_bytes = self._env_int("UCE_IDENTIFIER_MAX_BYTES", 200000)
        self.identifier_max_count = self._env_int("UCE_IDENTIFIER_MAX_COUNT", 5000)

    def _extensions_for_languages(self, languages: Iterable[str]) -> tuple[str, ...]:
        exts = []
        for ext, lang in code_parser.LANGUAGE_BY_EXTENSION.items():
            if lang in languages:
                exts.append(ext)
        return tuple(sorted(set(exts)))

    def _env_int(self, name: str, default: int) -> int:
        value = os.getenv(name)
        if value is None:
            return default
        value = value.strip()
        if not value:
            return default
        try:
            return int(value)
        except ValueError:
            return default

    def _resolve_identifier_paths(self) -> tuple[str, ...]:
        env_value = os.getenv("UCE_IDENTIFIER_PATHS")
        if env_value:
            normalized = env_value.replace(",", os.pathsep)
            parts = [p.strip() for p in normalized.split(os.pathsep) if p.strip()]
            resolved = []
            for part in parts:
                if os.path.isabs(part):
                    resolved.append(os.path.normpath(part))
                else:
                    resolved.append(os.path.normpath(os.path.join(self.config.project_root, part)))
            return tuple(sorted(set(resolved)))

        paths = self.paths.get("identifiers")
        if paths:
            return tuple(sorted(set(paths)))
        return tuple()

    def _should_index_identifiers(self, full_path: str) -> bool:
        if not self.identifier_paths:
            return False
        abs_path = os.path.abspath(full_path)
        in_scope = False
        for root in self.identifier_paths:
            try:
                if os.path.commonpath([abs_path, root]) == root:
                    in_scope = True
                    break
            except ValueError:
                continue
        if not in_scope:
            return False
        if self.identifier_max_bytes > 0:
            try:
                size = os.path.getsize(full_path)
            except OSError:
                return False
            if size > self.identifier_max_bytes:
                self.logger.info("Skipping identifier indexing for %s: size %d exceeds limit %d", full_path, size, self.identifier_max_bytes)
                return False
        return True

    def _resolve_imports(self, rel_path: str, imports: Iterable[str]) -> tuple[str, ...]:
        resolved: list[str] = []
        for imported in imports:
            resolved_path = resolve_import(
                rel_path,
                imported,
                self.config.project_root,
                self.config.aliases,
                self.extensions,
            )
            if not resolved_path:
                continue
            if is_ignored(resolved_path, self.config.ignore):
                continue
            resolved.append(resolved_path)
        return tuple(sorted(set(resolved)))

    def _link_cross_file_calls(
        self,
        records: Iterable[tuple[str, tuple[tuple[str, str], ...], tuple[str, ...]]],
    ) -> None:
        for rel_path, call_pairs, import_paths in records:
            if not call_pairs or not import_paths:
                continue
            for caller, callee in call_pairs:
                self.graph.run(
                    """
                    MATCH (caller:Function {name: $caller, file_path: $file_path})
                    MATCH (f:File)-[:DECLARES_FUNCTION]->(callee:Function {name: $callee})
                    WHERE f.path IN $imports
                    MERGE (caller)-[:CALLS]->(callee)
                    """,
                    caller=caller,
                    file_path=rel_path,
                    callee=callee,
                    imports=list(import_paths),
                )

    def _identifier_names_for_file(self, full_path: str, parsed: code_parser.ParsedCode) -> tuple[str, ...]:
        if not self._should_index_identifiers(full_path):
            return tuple()
        if self.identifier_max_count > 0 and len(parsed.identifiers) > self.identifier_max_count:
            self.logger.info(
                "Skipping identifier indexing for %s: %d identifiers exceeds limit %d",
                full_path,
                len(parsed.identifiers),
                self.identifier_max_count,
            )
            return tuple()
        return parsed.identifiers

    def _iter_code_files(self):
        for root_path in self.paths["code"]:
            if not os.path.isdir(root_path):
                continue
            for root, _, files in os.walk(root_path):
                for filename in files:
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in self.extensions:
                        continue
                    full_path = os.path.join(root, filename)
                    rel_path = ensure_relative(full_path, self.config.project_root)
                    if is_ignored(rel_path, self.config.ignore):
                        continue
                    yield full_path, rel_path

    def _schema_dir_prefixes(self) -> tuple[str, ...]:
        """Repo-relative schema files + their containing directories, used to gate USES_TABLE."""
        prefixes: set[str] = set()
        for full in self._iter_schema_files():
            rel = normalize_path(ensure_relative(full, self.config.project_root))
            prefixes.add(rel)
            parent = os.path.dirname(rel)
            if parent:
                prefixes.add(parent.rstrip("/") + "/")
        return tuple(sorted(prefixes))

    def _imports_schema(self, resolved_imports: Iterable[str], schema_prefixes: tuple[str, ...]) -> bool:
        for imp in resolved_imports:
            n = normalize_path(imp)
            for pre in schema_prefixes:
                if n == pre or (pre.endswith("/") and n.startswith(pre)):
                    return True
        return False

    def _iter_schema_files(self):
        for root_path in self.paths["schema"]:
            if os.path.isdir(root_path):
                for root, _, files in os.walk(root_path):
                    for filename in files:
                        if not filename.endswith((".sql", ".ts", ".js", ".json", ".yaml", ".yml")):
                            continue
                        yield os.path.join(root, filename)
            elif os.path.isfile(root_path):
                yield root_path

    @contextmanager
    def _temp_env(self, overrides: dict[str, str]):
        original: dict[str, str | None] = {}
        for key, value in overrides.items():
            original[key] = os.environ.get(key)
            os.environ[key] = value
        try:
            yield
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _llm_env_overrides(self) -> dict[str, str]:
        return {
            "NEO4J_URI": str(self.config.neo4j.uri),
            "NEO4J_USER": str(self.config.neo4j.user),
            "NEO4J_USERNAME": str(self.config.neo4j.user),
            "NEO4J_PASSWORD": str(self.config.neo4j.password),
        }

    def _llm_ingest_requirements(self, directories: Iterable[str]) -> None:
        try:
            from uce.ingestion import llm_ingest
        except Exception as exc:
            self.logger.error("Failed to import LLM requirements ingestion: %s", exc)
            return

        existing_dirs = [path for path in directories if os.path.isdir(path)]
        if not existing_dirs:
            self.logger.warning("LLM requirements ingestion skipped: no requirement directories found")
            return

        self.logger.info("Starting LLM requirements ingestion (%d director(ies))", len(existing_dirs))
        with self._temp_env(self._llm_env_overrides()):
            try:
                llm_ingest.ingest_requirements_with_graph(existing_dirs, self.graph)
                self.logger.info("LLM requirements ingestion completed")
            except Exception as exc:
                self.logger.warning("LLM requirements ingestion failed: %s", exc)

    def _llm_ingest_policies(self, directories: Iterable[str]) -> None:
        try:
            from uce.ingestion import llm_ingest
        except Exception as exc:
            self.logger.error("Failed to import LLM policies ingestion: %s", exc)
            return

        existing_dirs = [path for path in directories if os.path.isdir(path)]
        if not existing_dirs:
            self.logger.warning("LLM policies ingestion skipped: no policy directories found")
            return

        self.logger.info("Starting LLM policies ingestion (%d director(ies))", len(existing_dirs))
        with self._temp_env(self._llm_env_overrides()):
            try:
                llm_ingest.ingest_policies_with_graph(existing_dirs, self.graph)
                self.logger.info("LLM policies ingestion completed")
            except Exception as exc:
                self.logger.warning("LLM policies ingestion failed: %s", exc)

    def _llm_ingest_rbac(self, directories: Iterable[str]) -> None:
        try:
            from uce.ingestion import llm_rbac
        except Exception as exc:
            self.logger.error("Failed to import LLM RBAC ingestion: %s", exc)
            return

        existing_dirs = [path for path in directories if os.path.isdir(path)]
        if not existing_dirs:
            self.logger.warning("LLM RBAC ingestion skipped: no RBAC directories found")
            self.graph.replace_authority_rules([])
            return

        self.logger.info("Starting LLM RBAC ingestion (%d director(ies))", len(existing_dirs))
        try:
            rules = llm_rbac.ingest_rbac_rules(existing_dirs)
            self.graph.replace_authority_rules(rules)
            self.logger.info("LLM RBAC ingestion completed (%d rule(s))", len(rules))
            # Invalidate the MCP server's in-process RBAC cache after rule updates
            try:
                from uce.server.mcp_server import invalidate_rbac_cache
                invalidate_rbac_cache()
            except Exception:
                pass
        except Exception as exc:
            self.logger.warning("LLM RBAC ingestion failed: %s", exc)

    def run_llm_ingestion(self) -> None:
        self._llm_ingest_requirements(self.paths["requirements"])
        self._llm_ingest_policies(self.paths["policies"])
        self._llm_ingest_rbac(self.paths["rbac"])

    def llm_ingest_requirements_dir(self, directory: str) -> None:
        self._llm_ingest_requirements([directory])

    def llm_ingest_policies_dir(self, directory: str) -> None:
        self._llm_ingest_policies([directory])

    def llm_ingest_rbac_dir(self, directory: str) -> None:
        self._llm_ingest_rbac([directory])

    def full_refresh(self) -> None:
        self.logger.info("Starting full ingestion")
        self.refresh_schema()
        self.refresh_code()
        self.logger.info("Full ingestion completed")

    def refresh_schema(self) -> None:
        tables = []
        for schema_file in self._iter_schema_files():
            tables.extend(schema_parser.parse_schema_file(schema_file))
        if tables:
            upsert_schema(self.graph, tables)
            live_names = [t["name"] for t in tables]
            self.graph.cleanup_stale_schema(live_names)
            self.logger.info("Schema ingestion: %s tables (stale nodes pruned)", len(tables))
        else:
            self.logger.info(
                "Schema ingestion: no tables detected (paths=%s)",
                ", ".join(self.paths["schema"]),
            )

    def refresh_code(self) -> None:
        tables = load_tables(self.graph)
        columns_by_table = load_columns(self.graph)
        schema_prefixes = self._schema_dir_prefixes()

        total_files = 0
        parsed_files = 0
        call_records: list[tuple[str, tuple[tuple[str, str], ...], tuple[str, ...]]] = []
        for full_path, rel_path in self._iter_code_files():
            total_files += 1
            try:
                parsed = code_parser.parse_file(
                    full_path,
                    collect_identifiers=self._should_index_identifiers(full_path),
                )
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.error("Failed to parse %s: %s", full_path, exc)
                continue

            if not parsed:
                continue

            parsed_files += 1
            resolved_imports = self._resolve_imports(rel_path, parsed.imports)
            call_records.append(
                (
                    rel_path,
                    parsed.calls,
                    resolved_imports,
                )
            )

            try:
                last_modified = os.path.getmtime(full_path)
            except OSError:
                last_modified = None

            upsert_code_file(
                self.graph,
                rel_path,
                parsed,
                self.config.project_root,
                self.config.aliases,
                self.extensions,
                self.config.ignore,
                identifier_names=self._identifier_names_for_file(full_path, parsed),
                language=parsed.language,
                last_modified=last_modified,
            )

            with open(full_path, "r", encoding="utf-8", errors="ignore") as handle:
                content = handle.read()
            link_tables_for_file(
                self.graph,
                rel_path,
                content,
                tables,
                columns_by_table,
                imports_schema=self._imports_schema(resolved_imports, schema_prefixes),
                has_resolved_imports=bool(resolved_imports),
            )

        self._link_cross_file_calls(call_records)

        self.graph.cleanup_orphan_identifiers()
        self.logger.info("Code ingestion: parsed %s/%s files", parsed_files, total_files)

    def update_code_file(self, full_path: str) -> None:
        if not os.path.isfile(full_path):
            return
        rel_path = ensure_relative(full_path, self.config.project_root)
        if is_ignored(rel_path, self.config.ignore):
            return

        try:
            parsed = code_parser.parse_file(
                full_path,
                collect_identifiers=self._should_index_identifiers(full_path),
            )
        except Exception as exc:  # pragma: no cover - defensive logging
            self.logger.error("Failed to parse %s: %s", full_path, exc)
            return

        if not parsed:
            return

        try:
            last_modified = os.path.getmtime(full_path)
        except OSError:
            last_modified = None

        upsert_code_file(
            self.graph,
            rel_path,
            parsed,
            self.config.project_root,
            self.config.aliases,
            self.extensions,
            self.config.ignore,
            identifier_names=self._identifier_names_for_file(full_path, parsed),
            language=parsed.language,
            last_modified=last_modified,
        )

        resolved_imports = self._resolve_imports(rel_path, parsed.imports)
        self._link_cross_file_calls(
            [(rel_path, parsed.calls, resolved_imports)]
        )

        tables = load_tables(self.graph)
        columns_by_table = load_columns(self.graph)
        schema_prefixes = self._schema_dir_prefixes()
        with open(full_path, "r", encoding="utf-8", errors="ignore") as handle:
            content = handle.read()
        link_tables_for_file(
            self.graph,
            rel_path,
            content,
            tables,
            columns_by_table,
            imports_schema=self._imports_schema(resolved_imports, schema_prefixes),
            has_resolved_imports=bool(resolved_imports),
        )

        self.graph.cleanup_orphan_identifiers()

    def delete_code_file(self, full_path: str) -> None:
        rel_path = ensure_relative(full_path, self.config.project_root)
        self.graph.delete_file(normalize_path(rel_path))

        self.graph.cleanup_orphan_identifiers()

    def update_schema_file(self, full_path: str) -> None:
        tables = schema_parser.parse_schema_file(full_path)
        if tables:
            upsert_schema(self.graph, tables)
            self.logger.info("Schema updated: %s", full_path)

