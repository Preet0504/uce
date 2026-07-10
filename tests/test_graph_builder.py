"""Tests for uce.ingestion.graph_builder.resolve_import — path resolution correctness,
including Python absolute dotted-module imports (e.g. "uce.core.rbac")."""
import os

from uce.ingestion.graph_builder import resolve_import

PY_EXTENSIONS = (".py",)


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("")


def test_python_dotted_absolute_import_resolves_to_file(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "pkg", "core", "rbac.py"))

    resolved = resolve_import(
        source_rel="pkg/server/mcp_server.py",
        import_path="pkg.core.rbac",
        project_root=root,
        aliases={},
        extensions=PY_EXTENSIONS,
    )
    assert resolved == "pkg/core/rbac.py"


def test_python_dotted_package_import_resolves_to_init(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "pkg", "reasoning", "__init__.py"))

    resolved = resolve_import(
        source_rel="pkg/server/mcp_server.py",
        import_path="pkg.reasoning",
        project_root=root,
        aliases={},
        extensions=PY_EXTENSIONS,
    )
    assert resolved == "pkg/reasoning/__init__.py"


def test_bare_single_segment_import_is_not_resolved(tmp_path):
    """A single-segment name ("os", "logging") must not be resolved even if a same-named
    local file happens to exist — it's indistinguishable from a stdlib/third-party import
    at this point, and resolving it would risk a false IMPORTS edge."""
    root = str(tmp_path)
    _touch(os.path.join(root, "os.py"))  # deliberately adversarial local file

    resolved = resolve_import(
        source_rel="pkg/app.py",
        import_path="os",
        project_root=root,
        aliases={},
        extensions=PY_EXTENSIONS,
    )
    assert resolved is None


def test_dotted_import_with_no_matching_file_resolves_to_none(tmp_path):
    root = str(tmp_path)
    resolved = resolve_import(
        source_rel="pkg/app.py",
        import_path="numpy.core.multiarray",
        project_root=root,
        aliases={},
        extensions=PY_EXTENSIONS,
    )
    assert resolved is None


def test_package_directory_import_prefers_existing_init_over_missing_index(tmp_path):
    root = str(tmp_path)
    _touch(os.path.join(root, "pkg", "utils", "__init__.py"))

    resolved = resolve_import(
        source_rel="pkg/app.py",
        import_path="pkg.utils",
        project_root=root,
        aliases={},
        extensions=(".py", ".ts"),
    )
    assert resolved == "pkg/utils/__init__.py"


def test_js_style_relative_import_still_resolves(tmp_path):
    """Regression guard: the new Python-dotted branch must not interfere with the
    existing relative-import resolution used by JS/TS repos."""
    root = str(tmp_path)
    _touch(os.path.join(root, "src", "lib", "utils.ts"))

    resolved = resolve_import(
        source_rel="src/app.ts",
        import_path="./lib/utils",
        project_root=root,
        aliases={},
        extensions=(".ts", ".tsx", ".js"),
    )
    assert resolved == "src/lib/utils.ts"


def test_js_bare_package_import_still_unresolved(tmp_path):
    """A bare npm-style package name must not be mistaken for a Python dotted import."""
    root = str(tmp_path)
    resolved = resolve_import(
        source_rel="src/app.ts",
        import_path="react",
        project_root=root,
        aliases={},
        extensions=(".ts", ".tsx", ".js"),
    )
    assert resolved is None
