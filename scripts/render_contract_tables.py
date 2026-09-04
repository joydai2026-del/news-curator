"""Render canonical contract tables from the frozen ownership tuples.

Run from the repository root after changing the tuples in
``curator/contracts/__init__.py``. The renderer updates only the text between
the three generated marker pairs and refuses missing or duplicate markers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from curator.contracts import (
    RECEIPT_KIND_TIERS,
    RECEIPT_WRAPPER_KINDS,
    SUBJECT_BOUND_RECEIPT_KINDS,
    SUBJECT_BOUND_RECORDS,
    SUBJECTLESS_RECEIPT_KINDS,
    SUBJECTLESS_RECORDS,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _code_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"`{value}`" for value in values)


def _record_list(records: tuple[type, ...], kinds: tuple[str, ...]) -> str:
    names = tuple(record.__name__ for record in records)
    return (
        f"{_code_list(names)}, and a `ReceiptEnvelope` whose `kind` is one of "
        f"{_code_list(kinds)}"
    )


def render_ownership_classification() -> str:
    """Render the two ownership tiers from their frozen class and kind tuples."""
    rows = (
        (
            "SUBJECT-BOUND (yes)",
            "REQUIRED non-blank, whatever wrote the row",
            _record_list(SUBJECT_BOUND_RECORDS, SUBJECT_BOUND_RECEIPT_KINDS),
        ),
        (
            "SUBJECTLESS (no)",
            "May be null, and then ONLY when `actor_kind` is `system`",
            _record_list(SUBJECTLESS_RECORDS, SUBJECTLESS_RECEIPT_KINDS),
        ),
    )
    lines = ["| Tier | `user_id` | Records |", "|---|---|---|"]
    lines.extend(f"| {tier} | {user_rule} | {records} |" for tier, user_rule, records in rows)
    return "\n" + "\n".join(lines) + "\n"


def render_receipt_kind_tiers() -> str:
    """Render the closed receipt-kind vocabulary and its ownership tier."""
    lines = [
        "| `kind` (wire value) | Tier | `user_id` |",
        "|---|---|---|",
    ]
    for kind, tier in RECEIPT_KIND_TIERS:
        user_rule = (
            "required non-blank"
            if tier == "subject_bound"
            else "may be null under a `system` actor"
        )
        lines.append(f"| `{kind}` | {tier.replace('_', '-')} | {user_rule} |")
    return "\n" + "\n".join(lines) + "\n"


def render_receipt_wrapper_kinds() -> str:
    """Render each frozen wrapper, its envelope field, and permitted kind."""
    lines = ["| Wrapper | Envelope field | Envelope `kind` |", "|---|---|---|"]
    lines.extend(
        f"| `{wrapper.__name__}` | `{field_name}` | `{kind}` |"
        for wrapper, field_name, kind in RECEIPT_WRAPPER_KINDS
    )
    return "\n" + "\n".join(lines) + "\n"


GENERATED_TABLES: dict[str, tuple[Path, Callable[[], str]]] = {
    "ownership-classification": (
        Path("docs/contracts/tenant.md"),
        render_ownership_classification,
    ),
    "receipt-kind-tiers": (
        Path("docs/contracts/receipt.md"),
        render_receipt_kind_tiers,
    ),
    "receipt-wrapper-kinds": (
        Path("docs/contracts/receipt.md"),
        render_receipt_wrapper_kinds,
    ),
}


def generated_markers(name: str) -> tuple[str, str]:
    return f"<!-- generated: {name} -->", "<!-- end generated -->"


def replace_generated_table(text: str, name: str, rendered: str) -> str:
    """Replace exactly one named generated block and preserve its markers."""
    start, end = generated_markers(name)
    if text.count(start) != 1:
        raise ValueError(f"{name}: expected exactly one generated marker pair")
    before, remainder = text.split(start, 1)
    if end not in remainder:
        raise ValueError(f"{name}: generated block has no closing marker")
    _, after = remainder.split(end, 1)
    return before + start + rendered + end + after


def render_contract_tables(repo_root: Path = REPO_ROOT) -> tuple[Path, ...]:
    """Update all canonical tables and return the changed document paths."""
    by_path: dict[Path, list[tuple[str, Callable[[], str]]]] = {}
    for name, (relative_path, renderer) in GENERATED_TABLES.items():
        by_path.setdefault(repo_root / relative_path, []).append((name, renderer))

    changed: list[Path] = []
    for path, tables in by_path.items():
        original = path.read_text(encoding="utf-8")
        rendered = original
        for name, renderer in tables:
            rendered = replace_generated_table(rendered, name, renderer())
        if rendered != original:
            path.write_text(rendered, encoding="utf-8")
            changed.append(path)
    return tuple(changed)


if __name__ == "__main__":
    for changed_path in render_contract_tables():
        print(changed_path.relative_to(REPO_ROOT))
