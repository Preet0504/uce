from __future__ import annotations

import logging
import os
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from core.config import UceConfig, resolve_paths
from runtime.updater import GraphUpdater
from ingestion.code_parser import LANGUAGE_BY_EXTENSION


def _existing_watch_root(path: str) -> str | None:
    candidate = os.path.abspath(path)
    while candidate and not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return None
        candidate = parent
    return candidate if candidate and os.path.exists(candidate) else None


class UceEventHandler(FileSystemEventHandler):
    def __init__(self, config: UceConfig, updater: GraphUpdater, logger: logging.Logger | None = None):
        self.config = config
        self.updater = updater
        self.logger = logger or logging.getLogger("uce.watcher")
        self.paths = resolve_paths(config)
        self.code_exts = set(ext for ext, lang in LANGUAGE_BY_EXTENSION.items() if lang in config.languages)

    def _is_in_paths(self, path: str, paths: tuple[str, ...]) -> bool:
        for root in paths:
            if path.startswith(root):
                return True
        return False

    def _is_schema_file(self, path: str) -> bool:
        return path.endswith((".sql", ".ts", ".js", ".json", ".yaml", ".yml"))

    def on_modified(self, event):
        if event.is_directory:
            return
        self._handle_change(event.src_path)

    def on_created(self, event):
        if event.is_directory:
            return
        self._handle_change(event.src_path)

    def on_deleted(self, event):
        if event.is_directory:
            return
        self._handle_delete(event.src_path)

    def _handle_change(self, path: str) -> None:
        abs_path = os.path.abspath(path)
        if self._is_in_paths(abs_path, self.paths["code"]):
            ext = os.path.splitext(abs_path)[1].lower()
            if ext in self.code_exts:
                self.logger.info("Code change detected: %s", abs_path)
                self.updater.update_code_file(abs_path)
            return

        if self._is_in_paths(abs_path, self.paths["schema"]) and self._is_schema_file(abs_path):
            self.logger.info("Schema change detected: %s", abs_path)
            self.updater.update_schema_file(abs_path)
            return

        if self._is_in_paths(abs_path, self.paths["requirements"]):
            self.logger.info("Requirements change detected (LLM-only): %s", abs_path)
            self.updater.llm_ingest_requirements_dir(os.path.dirname(abs_path))
            return

        if self._is_in_paths(abs_path, self.paths["policies"]):
            self.logger.info("Policies change detected (LLM-only): %s", abs_path)
            self.updater.llm_ingest_policies_dir(os.path.dirname(abs_path))
            return

        if self._is_in_paths(abs_path, self.paths.get("rbac", tuple())):
            self.logger.info("RBAC change detected (LLM-only): %s", abs_path)
            self.updater.llm_ingest_rbac_dir(os.path.dirname(abs_path))

    def _handle_delete(self, path: str) -> None:
        abs_path = os.path.abspath(path)
        if self._is_in_paths(abs_path, self.paths["code"]):
            ext = os.path.splitext(abs_path)[1].lower()
            if ext in self.code_exts:
                self.logger.info("Code deletion detected: %s", abs_path)
                self.updater.delete_code_file(abs_path)


def start_watcher(config: UceConfig, updater: GraphUpdater) -> Observer:
    observer = Observer()
    handler = UceEventHandler(config, updater)

    paths = resolve_paths(config)
    watch_groups = ("code", "schema", "requirements", "policies", "rbac", "identifiers")
    watched = set()
    for group in watch_groups:
        path_group = paths.get(group, tuple())
        for path in path_group:
            watch_root = path
            if not os.path.exists(watch_root):
                watch_root = _existing_watch_root(watch_root)
            if watch_root and watch_root not in watched:
                observer.schedule(handler, watch_root, recursive=True)
                watched.add(watch_root)

    observer.start()
    return observer
