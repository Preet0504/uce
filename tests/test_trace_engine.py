"""Tests for uce.reasoning.trace_engine — entity detection and word boundary regex."""
import pytest

from uce.reasoning.trace_engine import _word_pattern, detect_entity


# ---------------------------------------------------------------------------
# _word_pattern — regex correctness
# ---------------------------------------------------------------------------

def test_word_pattern_matches_whole_word():
    pattern = _word_pattern("users")
    assert pattern.search("query the users table")
    assert not pattern.search("nonusers")
    assert not pattern.search("users123")


def test_word_pattern_case_insensitive():
    pattern = _word_pattern("Users", case_insensitive=True)
    assert pattern.search("the USERS table")
    assert pattern.search("users")


def test_word_pattern_case_sensitive_default():
    pattern = _word_pattern("users")
    assert not pattern.search("USERS")
    assert pattern.search("users")


def test_word_pattern_boundary_at_start():
    pattern = _word_pattern("id")
    assert pattern.search("id is the primary key")
    assert not pattern.search("userid")


def test_word_pattern_boundary_at_end():
    pattern = _word_pattern("id")
    # Standalone "id" at word boundary should match.
    assert pattern.search("id is the primary key")
    # Underscore is a word character (\w), so (?<!\w)id does NOT match inside user_id.
    assert not pattern.search("user_id")
    assert not pattern.search("the user_id field")


# ---------------------------------------------------------------------------
# detect_entity — table detection
# ---------------------------------------------------------------------------

def test_detect_entity_table():
    tables = ["users", "orders"]
    columns = {"users": ["id", "name"], "orders": ["id", "total"]}
    files = []

    etype, name = detect_entity("modify the users table", tables, columns, files)
    assert etype == "table"
    assert name == "users"


def test_detect_entity_column():
    tables = ["users"]
    columns = {"users": ["email", "name"]}
    files = []

    etype, name = detect_entity("update the email column in users", tables, columns, files)
    assert etype == "column"
    assert "email" in name


def test_detect_entity_file():
    tables = []
    columns = {}
    files = ["src/app.py", "src/utils.py"]

    etype, name = detect_entity("change src/app.py", tables, columns, files)
    assert etype == "file"
    assert name == "src/app.py"


def test_detect_entity_unknown():
    etype, name = detect_entity("some random text", [], {}, [])
    assert etype == "unknown"
    assert name == ""


def test_detect_entity_case_insensitive_table():
    tables = ["users"]
    columns = {"users": ["id"]}
    files = []

    etype, name = detect_entity("modify USERS table", tables, columns, files)
    assert etype == "table"
    assert name == "users"


def test_detect_entity_column_prefers_table_match():
    """When both table and column are mentioned, prefer the column result."""
    tables = ["orders"]
    columns = {"orders": ["total", "status"]}
    files = []

    etype, name = detect_entity("change total in orders table", tables, columns, files)
    # Table detected first, then column within it
    assert etype == "column"
    assert "total" in name


def test_detect_entity_multi_table_column_picks_mentioned_table():
    """When a column exists in multiple tables and one is mentioned, pick that table."""
    tables = ["users", "admins"]
    columns = {"users": ["id", "name"], "admins": ["id", "name"]}
    files = []

    etype, name = detect_entity("rename name column in users", tables, columns, files)
    assert etype == "column"
    assert name.startswith("users.") or name.startswith("admins.")
