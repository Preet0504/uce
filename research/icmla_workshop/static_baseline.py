"""
Static-analysis baseline: a real, off-the-shelf module dependency tool (madge).

madge (https://github.com/pahen/madge) builds the JS/TS import graph independently of UCE and of
our hand-written oracle. We use it two ways:
  1. As an impact predictor: reverse-reachability over madge's dependency graph from the seed files.
  2. As an oracle-validation signal: agreement between madge's import graph and our oracle's
     import resolver (reported separately) shows our oracle is not idiosyncratic.

Requires Node.js + npx (madge is fetched on first use via `npx --yes madge`).
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from independent_oracle import normalize_repo_path


def madge_reverse_graph(
    project_root: Path,
    code_dirs: tuple[str, ...],
    ts_config: str | None = None,
) -> dict[str, set[str]] | None:
    """Return reverse dependency map {file: set(files that import it)}, repo-relative paths.

    Returns None if madge could not be run (so callers can skip the static baseline gracefully).
    """
    dirs = [d for d in code_dirs if (project_root / d).exists()] or ["."]
    parts = ["npx", "--yes", "madge", "--json", "--extensions", "ts,tsx,js,jsx"]
    if ts_config and (project_root / ts_config).exists():
        parts += ["--ts-config", ts_config]
    parts += list(dirs)
    # npx resolves to npx.cmd on Windows; run via the shell so the launcher is found.
    cmd_str = " ".join(parts)

    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tf:
        out_path = Path(tf.name)
    try:
        with out_path.open("w", encoding="utf-8") as fh:
            proc = subprocess.run(
                cmd_str, cwd=str(project_root), stdout=fh, stderr=subprocess.PIPE,
                text=True, timeout=300, shell=True,
            )
        raw = out_path.read_text(encoding="utf-8")
        if not raw.strip():
            print("  [madge stderr]", (proc.stderr or "")[:300])
            return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        print("  [madge error]", exc)
        return None
    finally:
        out_path.unlink(missing_ok=True)

    try:
        fwd_raw: dict[str, list[str]] = json.loads(raw)
    except json.JSONDecodeError:
        return None

    code_dir_tops = {normalize_repo_path(d).split("/")[0] for d in dirs}
    single_prefix = normalize_repo_path(dirs[0]) if len(dirs) == 1 else None

    def repo_rel(p: str) -> str:
        p = normalize_repo_path(p)
        top = p.split("/")[0]
        if top in code_dir_tops:
            return p
        if single_prefix:
            return f"{single_prefix}/{p}"
        return p

    reverse: dict[str, set[str]] = {}
    for importer, deps in fwd_raw.items():
        imp = repo_rel(importer)
        reverse.setdefault(imp, set())
        for dep in deps:
            d = repo_rel(dep)
            reverse.setdefault(d, set()).add(imp)
    return reverse


def reverse_reachable_static(reverse: dict[str, set[str]], seeds: set[str]) -> set[str]:
    result = set(seeds)
    frontier = list(seeds)
    while frontier:
        node = frontier.pop()
        for importer in reverse.get(node, ()):
            if importer not in result:
                result.add(importer)
                frontier.append(importer)
    return result
