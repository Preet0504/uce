from __future__ import annotations

import logging
import os
from typing import Iterable

from uce.core.config import UceConfig, resolve_paths
from uce.core.graph_db import GraphDB
from uce.ingestion import code_parser, schema_parser, requirement_parser, policy_parser
from uce.ingestion.graph_builder import (
    is_ignored,
    ensure_relative,
    normalize_path,
    upsert_code_file,
    upsert_schema,
    upsert_requirements,
    upsert_policies,
    load_tables,
    load_columns,
    link_tables_for_file,
)


class GraphUpdater:
    def __init__(self, config: UceConfig, graph: GraphDB, logger: logging.Logger | None = None):
        self.config = config
        self.graph = graph
        self.paths = resolve_paths(config)
        self.logger = logger or logging.getLogger("uce.updater")
        self.extensions = self._extensions_for_languages(config.languages)

    def _extensions_for_languages(self, languages: Iterable[str]) -> tuple[str, ...]:
        exts = []
        for ext, lang in code_parser.LANGUAGE_BY_EXTENSION.items():
            if lang in languages:
                exts.append(ext)
        return tuple(sorted(set(exts)))

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

    def _iter_requirement_dirs(self):
        for path in self.paths["requirements"]:
            if os.path.isdir(path):
                yield path

    def _iter_policy_dirs(self):
        for path in self.paths["policies"]:
            if os.path.isdir(path):
                yield path

    def full_refresh(self) -> None:
        self.logger.info("Starting full ingestion")
        self.refresh_schema()
        self.refresh_requirements()
        self.refresh_policies()
        self.refresh_code()
        self.logger.info("Full ingestion completed")

    def refresh_schema(self) -> None:
        tables = []
        for schema_file in self._iter_schema_files():
            tables.extend(schema_parser.parse_schema_file(schema_file))
        if tables:
            upsert_schema(self.graph, tables)
            self.logger.info("Schema ingestion: %s tables", len(tables))
        else:
            self.logger.info(
                "Schema ingestion: no tables detected (paths=%s)",
                ", ".join(self.paths["schema"]),
            )

    def refresh_requirements(self) -> None:
        all_requirements = []
        for directory in self._iter_requirement_dirs():
            all_requirements.extend(requirement_parser.parse_requirements(directory))
        tables = load_tables(self.graph)
        columns_by_table = load_columns(self.graph)
        if all_requirements:
            upsert_requirements(self.graph, all_requirements, tables, columns_by_table)
            self.logger.info("Requirements ingestion: %s requirements", len(all_requirements))
        else:
            self.logger.info(
                "Requirements ingestion: none detected (paths=%s)",
                ", ".join(self.paths["requirements"]),
            )

    def refresh_policies(self) -> None:
        all_policies = []
        for directory in self._iter_policy_dirs():
            all_policies.extend(policy_parser.parse_policies(directory))
        if all_policies:
            upsert_policies(self.graph, all_policies)
            self.logger.info("Policies ingestion: %s policies", len(all_policies))
        else:
            self.logger.info(
                "Policies ingestion: none detected (paths=%s)",
                ", ".join(self.paths["policies"]),
            )

    def refresh_code(self) -> None:
        tables = load_tables(self.graph)
        columns_by_table = load_columns(self.graph)

        total_files = 0
        parsed_files = 0
        for full_path, rel_path in self._iter_code_files():
            total_files += 1
            try:
                parsed = code_parser.parse_file(full_path)
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.error("Failed to parse %s: %s", full_path, exc)
                continue

            if not parsed:
                continue

            parsed_files += 1
            upsert_code_file(
                self.graph,
                rel_path,
                parsed,
                self.config.project_root,
                self.config.aliases,
                self.extensions,
                self.config.ignore,
            )

            with open(full_path, "r", encoding="utf-8", errors="ignore") as handle:
                content = handle.read()
            link_tables_for_file(self.graph, rel_path, content, tables, columns_by_table)

        self.graph.cleanup_orphan_apis()
        self.logger.info("Code ingestion: parsed %s/%s files", parsed_files, total_files)

    def update_code_file(self, full_path: str) -> None:
        if not os.path.isfile(full_path):
            return
        rel_path = ensure_relative(full_path, self.config.project_root)
        if is_ignored(rel_path, self.config.ignore):
            return

        try:
            parsed = code_parser.parse_file(full_path)
        except Exception as exc:  # pragma: no cover - defensive logging
            self.logger.error("Failed to parse %s: %s", full_path, exc)
            return

        if not parsed:
            return

        upsert_code_file(
            self.graph,
            rel_path,
            parsed,
            self.config.project_root,
            self.config.aliases,
            self.extensions,
            self.config.ignore,
        )

        tables = load_tables(self.graph)
        columns_by_table = load_columns(self.graph)
        with open(full_path, "r", encoding="utf-8", errors="ignore") as handle:
            content = handle.read()
        link_tables_for_file(self.graph, rel_path, content, tables, columns_by_table)

        self.graph.cleanup_orphan_apis()

    def delete_code_file(self, full_path: str) -> None:
        rel_path = ensure_relative(full_path, self.config.project_root)
        self.graph.delete_file(normalize_path(rel_path))
        self.graph.cleanup_orphan_apis()

    def update_schema_file(self, full_path: str) -> None:
        tables = schema_parser.parse_schema_file(full_path)
        if tables:
            upsert_schema(self.graph, tables)
            self.logger.info("Schema updated: %s", full_path)

    def update_requirements_dir(self, directory: str) -> None:
        requirements = requirement_parser.parse_requirements(directory)
        if requirements:
            tables = load_tables(self.graph)
            columns_by_table = load_columns(self.graph)
            upsert_requirements(self.graph, requirements, tables, columns_by_table)
            self.logger.info("Requirements updated: %s", directory)

    def update_policies_dir(self, directory: str) -> None:
        policies = policy_parser.parse_policies(directory)
        if policies:
            upsert_policies(self.graph, policies)
            self.logger.info("Policies updated: %s", directory)
