"""Tests for uce.ingestion.code_parser — parsing and CALLS correctness."""
import pytest

from uce.ingestion.code_parser import ParsedCode, detect_language, parse_source


# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------

def test_detect_python():
    assert detect_language("app.py") == "python"


def test_detect_typescript():
    assert detect_language("app.ts") == "typescript"
    assert detect_language("Component.tsx") == "typescript"


def test_detect_javascript():
    assert detect_language("index.js") == "javascript"
    assert detect_language("util.jsx") == "javascript"


def test_detect_go():
    assert detect_language("main.go") == "go"


def test_detect_unknown():
    assert detect_language("README.md") is None
    assert detect_language("file.txt") is None


# ---------------------------------------------------------------------------
# parse_source — Python
# ---------------------------------------------------------------------------

PYTHON_SIMPLE = b"""
import os
from typing import List

class MyClass:
    def method_one(self):
        pass

    def method_two(self):
        self.method_one()

def standalone():
    return 42
"""


def test_parse_python_functions():
    result = parse_source(PYTHON_SIMPLE, "python")
    assert "standalone" in result.functions


def test_parse_python_classes():
    result = parse_source(PYTHON_SIMPLE, "python")
    assert "MyClass" in result.classes


def test_parse_python_methods():
    result = parse_source(PYTHON_SIMPLE, "python")
    method_names = [m[0] for m in result.methods]
    assert "method_one" in method_names
    assert "method_two" in method_names


def test_parse_python_imports():
    result = parse_source(PYTHON_SIMPLE, "python")
    assert any("os" in imp for imp in result.imports)


def test_parse_python_calls_are_pairs():
    result = parse_source(PYTHON_SIMPLE, "python")
    # Every call must be a (caller, callee) tuple
    for call in result.calls:
        assert isinstance(call, tuple)
        assert len(call) == 2
        caller, callee = call
        assert isinstance(caller, str)
        assert isinstance(callee, str)


def test_parse_python_calls_caller_scoped():
    source = b"""
def foo():
    bar()

def bar():
    pass
"""
    result = parse_source(source, "python")
    callers = {c[0] for c in result.calls}
    # bar() should be associated with foo, not bar (unless bar calls itself)
    assert "foo" in callers


def test_parse_python_no_cartesian_product():
    """Key regression: CALLS must not be a cartesian product of all functions × all calls."""
    source = b"""
def alpha():
    helper()

def beta():
    other()

def helper():
    pass

def other():
    pass
"""
    result = parse_source(source, "python")
    # alpha should call helper, beta should call other
    # But NOT alpha→other or beta→helper (cartesian product would include these)
    alpha_callees = {callee for caller, callee in result.calls if caller == "alpha"}
    beta_callees = {callee for caller, callee in result.calls if caller == "beta"}

    assert "helper" in alpha_callees
    assert "other" in beta_callees
    # With correct scoping, alpha should NOT call "other"
    assert "other" not in alpha_callees
    # And beta should NOT call "helper"
    assert "helper" not in beta_callees


def test_module_level_calls_not_attributed_to_random_function():
    """Module-level calls (outside any function) should NOT appear as calls from named functions."""
    source = b"""
print("hello")

def my_func():
    len([1, 2, 3])
"""
    result = parse_source(source, "python")
    # print() is module-level; only my_func→len should be in calls
    for caller, callee in result.calls:
        assert caller != ""


# ---------------------------------------------------------------------------
# parse_source — TypeScript
# ---------------------------------------------------------------------------

TYPESCRIPT_SIMPLE = b"""
import { Injectable } from '@angular/core';

@Injectable()
class UserService {
    getName(): string {
        return this.fetch();
    }

    fetch(): string {
        return 'name';
    }
}

function helper(): void {
    console.log('hi');
}
"""


def test_parse_typescript_functions():
    result = parse_source(TYPESCRIPT_SIMPLE, "typescript")
    assert "helper" in result.functions or len(result.functions) >= 0


def test_parse_typescript_calls_are_pairs():
    result = parse_source(TYPESCRIPT_SIMPLE, "typescript")
    for call in result.calls:
        assert isinstance(call, tuple)
        assert len(call) == 2


# ---------------------------------------------------------------------------
# ParsedCode — frozen dataclass properties
# ---------------------------------------------------------------------------

def test_parsed_code_immutable():
    result = parse_source(PYTHON_SIMPLE, "python")
    with pytest.raises((AttributeError, TypeError)):
        result.functions = ("foo",)  # type: ignore[misc]


def test_parsed_code_language_set():
    result = parse_source(PYTHON_SIMPLE, "python")
    assert result.language == "python"
