"""Regressions-Guard: kein append_bets() ohne User-Confirm-Gate.

Rule (feedback_no_auto_log): jeder append_bets/ledger.add_bet-Aufruf in
scripts/ + src/scanner/ MUSS in einem If-Block stehen, der auf einen
Confirmation-Marker prüft (auto_log, confirmed, chosen).

Test failt hart mit File:Line + Kontext, wenn ein neues Script unter dem
Radar auto-loggen würde.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCAN_ROOTS = [ROOT / "scripts", ROOT / "src" / "scanner"]

# Namen die als "gated" gelten wenn sie in einem enclosing If-Test vorkommen.
CONFIRM_MARKERS = {"auto_log", "confirmed", "chosen"}

# Files die aus dem Scan ausgenommen sind (Utility ohne CLI-Semantik).
EXEMPT_FILES = {"_bet_confirm.py", "__init__.py"}


def _iter_target_files():
    for root in SCAN_ROOTS:
        for p in root.rglob("*.py"):
            if p.name in EXEMPT_FILES:
                continue
            yield p


def _is_ledger_write_call(node: ast.Call) -> bool:
    """Erkennt append_bets(...) und ledger.add_bet(...)."""
    f = node.func
    if isinstance(f, ast.Name) and f.id == "append_bets":
        return True
    if isinstance(f, ast.Attribute) and f.attr in ("append_bets", "add_bet"):
        return True
    return False


def _names_in_expr(expr: ast.AST) -> set[str]:
    """Sammelt Name-IDs und Attribute-attrs (a.auto_log → {'a','auto_log'})."""
    out = set()
    for n in ast.walk(expr):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


class _Auditor(ast.NodeVisitor):
    """Trackt enclosing Control-Flow-Markers; sammelt ungegatete Aufrufe."""

    def __init__(self, path: Path):
        self.path = path
        self.enclosing_tests: list[set[str]] = []
        self.violations: list[tuple[int, str]] = []

    def visit_If(self, node: ast.If):
        markers = _names_in_expr(node.test)
        self.enclosing_tests.append(markers)
        for stmt in node.body:
            self.visit(stmt)
        self.enclosing_tests.pop()
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_For(self, node: ast.For):
        # `for s in confirmed:` gilt als Gate (Iteration nur über bestätigte Liste).
        markers = _names_in_expr(node.iter)
        self.enclosing_tests.append(markers)
        for stmt in node.body:
            self.visit(stmt)
        self.enclosing_tests.pop()
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Call(self, node: ast.Call):
        if _is_ledger_write_call(node):
            all_markers: set[str] = set().union(*self.enclosing_tests) if self.enclosing_tests else set()
            if not (all_markers & CONFIRM_MARKERS):
                self.violations.append((node.lineno, ast.unparse(node)[:120]))
        self.generic_visit(node)


def test_no_ungated_append_bets():
    all_violations: list[tuple[Path, int, str]] = []
    for path in _iter_target_files():
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        auditor = _Auditor(path)
        auditor.visit(tree)
        for line, code in auditor.violations:
            all_violations.append((path, line, code))

    if all_violations:
        lines = ["Ungeschützte append_bets/add_bet-Aufrufe gefunden:"]
        for p, ln, code in all_violations:
            lines.append(f"  {p.relative_to(ROOT)}:{ln}  →  {code}")
        lines.append(
            "\nJeder Aufruf MUSS in einem If-Block stehen, dessen Test einen "
            f"Confirmation-Marker enthält: {sorted(CONFIRM_MARKERS)}. "
            "Rule: feedback_no_auto_log — Wetten NIEMALS ohne Nutzerbestätigung."
        )
        pytest.fail("\n".join(lines))
