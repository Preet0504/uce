"""
Runnable demo of the UCE propose_change gate -- no external repos needed.

Runs against THIS project's own graph (whatever config.yaml points at), calling the exact
functions the live MCP server exposes (uce.server.mcp_server), in-process -- not a mock, not
canned output. Anyone who self-hosts UCE can run this immediately after the Quick Start.

Prerequisites:
  - A running Neo4j instance reachable via config.yaml (see README "Quick Start").
  - This project ingested at least once: `python -m uce.cli --config config.yaml --skip-llm-ingestion`
    (or just let the full `uce` CLI run once; deterministic code+schema ingestion is enough).

What it demonstrates, all live against the real graph:
  1. propose_change() BLOCKING an incomplete plan -- declaring only one file when the real
     dependency graph knows others are affected -- and showing exactly why (missed files, literal
     evidence).
  2. propose_change() ALLOWING a trivial, fully-declared plan and minting a gate_token.
  3. write_file() refusing to run WITHOUT a gate_token -- the hard failure that makes the gate
     mandatory rather than a convention an agent could skip.
  4. The same write succeeding once a valid gate_token is presented, then delete_file() cleaning
     up (each mutation needs its own token -- the demo scratch file is removed at the end so
     re-running is idempotent).

Usage:
  python demo/run_demo.py [--config config.yaml]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastmcp.exceptions import AuthorizationError

from uce.core.config import load_config
from uce.core.graph_db import GraphDB
import uce.server.mcp_server as srv

SCRATCH_FILE = "demo/.gate_demo_scratch.txt"


def _rule(char: str = "-", width: int = 72) -> None:
    print(char * width)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(REPO_ROOT / "config.yaml"))
    ap.add_argument("--target-file", default="uce/core/rbac.py",
                     help="A real file in this repo with known dependents, used for the BLOCK demo.")
    args = ap.parse_args()

    config = load_config(args.config)
    graph = GraphDB(config.neo4j.uri, config.neo4j.user, config.neo4j.password)
    srv._CONFIG = config
    srv._DB = graph

    print("UCE propose_change GATE -- live demo")
    print(f"project_root={config.project_root}  gate_enforcement={config.gate.enforcement}")
    _rule("=")

    # ------------------------------------------------------------------
    # 1) BLOCK: declare only one file when the real graph knows there's more.
    # ------------------------------------------------------------------
    print(f"\n[1] propose_change: rewrite {args.target_file}, declaring ONLY that one file")
    resp = srv.propose_change(
        operation="write",
        entity_type="file",
        entity_name=args.target_file,
        files_to_edit=[args.target_file],
    )
    print(f"  decision: {resp['decision'].upper()}")
    print(f"  declared: {resp['blast_radius']['declared_files']}")
    print(f"  actual blast radius: {resp['blast_radius']['missed_count'] + 1} files "
          f"({resp['blast_radius']['missed_count']} missed by the declared plan)")
    if resp["blast_radius"]["missed_files"]:
        sample = resp["blast_radius"]["missed_files"][:5]
        more = len(resp["blast_radius"]["missed_files"]) - len(sample)
        print(f"  missed (sample): {sample}" + (f"  (+{more} more)" if more > 0 else ""))
    print(f"  gate_token issued: {resp['gate_token'] is not None}")
    _rule()

    # ------------------------------------------------------------------
    # 2) ALLOW: a trivial, fully self-contained scratch file.
    # ------------------------------------------------------------------
    print(f"\n[2] propose_change: create {SCRATCH_FILE} (a new, harmless demo scratch file)")
    resp = srv.propose_change(
        operation="write",
        entity_type="file",
        entity_name=SCRATCH_FILE,
        files_to_edit=[SCRATCH_FILE],
    )
    print(f"  decision: {resp['decision'].upper()}")
    token = resp["gate_token"]
    print(f"  gate_token issued: {token is not None}")
    _rule()

    # ------------------------------------------------------------------
    # 3) write_file WITHOUT a token -- must fail hard, no filesystem write happens.
    # ------------------------------------------------------------------
    print(f"\n[3] write_file WITHOUT a gate_token (skipping propose_change on purpose)")
    try:
        srv.write_file(SCRATCH_FILE, "this should never be written\n")
        print("  UNEXPECTED: write succeeded without a token!")
    except AuthorizationError as exc:
        print(f"  REJECTED (as expected): {exc}")
    exists = os.path.exists(os.path.join(config.project_root, SCRATCH_FILE))
    print(f"  file actually exists on disk: {exists}")
    _rule()

    # ------------------------------------------------------------------
    # 4) write_file WITH the valid token from step 2 -- succeeds. Then delete_file cleans up.
    # ------------------------------------------------------------------
    print(f"\n[4] write_file WITH the gate_token from step [2]")
    resp = srv.write_file(
        SCRATCH_FILE,
        "Written by demo/run_demo.py after a valid propose_change() allow decision.\n",
        gate_token=token,
    )
    print(f"  written: {resp['written']}  bytes: {resp['bytes_written']}")

    print(f"\n[4b] cleaning up: propose_change(delete) -> delete_file with a fresh token")
    del_resp = srv.propose_change(
        operation="delete", entity_type="file", entity_name=SCRATCH_FILE,
        files_to_edit=[SCRATCH_FILE],
    )
    del_result = srv.delete_file(SCRATCH_FILE, gate_token=del_resp["gate_token"])
    print(f"  deleted: {del_result['deleted']}")
    _rule("=")

    print("\nDone. Steps [1]-[2] never touched the filesystem (propose_change is read-only).")
    print("Step [3] proves write_file cannot be called successfully without the gate.")
    print("Step [4] proves it works, and is cleaned up, once the gate allows it.")

    graph.close()


if __name__ == "__main__":
    main()
