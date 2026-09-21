"""Keeps diagnostics.KNOWN_DIAGNOSTIC_MESSAGES honest against the real source.

A message can only ever be logged verbatim when it is a plain string literal
passed directly to `raise ValueError(...)` / `TypeError(...)` / `KeyError(...)`
in curator/recommendation/ or curator/contracts/: an f-string or a
concatenation can carry a wrapped vendor message or request-derived text, and
must never enter the allowlist. This scans both trees with the ast module and
asserts every such literal is already registered, so adding a new raise with
a new constant message needs a matching allowlist entry or this test fails.
"""
import ast
from pathlib import Path

from curator.recommendation.diagnostics import KNOWN_DIAGNOSTIC_MESSAGES

ROOT = Path(__file__).resolve().parents[1]
SCANNED_ROOTS = ("curator/recommendation", "curator/contracts")
TARGET_EXCEPTIONS = {"ValueError", "TypeError", "KeyError"}


def _exception_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _constant_raise_messages() -> set[str]:
    """Every constant-string message raised via ValueError/TypeError/KeyError.

    Only a bare `ast.Constant` string argument counts: an f-string is a
    `JoinedStr` node and a concatenation is a `BinOp`, neither of which match,
    so both are correctly excluded from this set (and must stay excluded from
    the allowlist).
    """
    messages: set[str] = set()
    for root in SCANNED_ROOTS:
        for path in sorted((ROOT / root).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                    continue
                if _exception_name(node.exc.func) not in TARGET_EXCEPTIONS or not node.exc.args:
                    continue
                arg = node.exc.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    messages.add(arg.value)
    return messages


def test_known_diagnostic_messages_is_a_superset_of_every_constant_curator_raise():
    found = _constant_raise_messages()
    assert found, "the scan itself found nothing: it is broken, not the allowlist"
    missing = found - KNOWN_DIAGNOSTIC_MESSAGES
    assert not missing, (
        "constant raise message(s) not registered in "
        "curator/recommendation/diagnostics.py KNOWN_DIAGNOSTIC_MESSAGES: "
        f"{sorted(missing)}"
    )
