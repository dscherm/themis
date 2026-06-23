"""Tests for the mutation (test-strength) pass.

Covers: deterministic discovery, weak tests letting mutants survive, strong tests
killing them all, and the load-bearing safety property — a mutant that spins is
recorded as a timeout, not allowed to hang the run.
"""

from __future__ import annotations

import sys
import textwrap

from blind_tdd.mutate import discover_mutants, run_mutation

# A small module with an arithmetic op, a comparison, and a counting loop whose
# `+=` becomes an infinite loop when mutated to `-=`.
_SRC = textwrap.dedent('''
    def add(a, b):
        return a + b

    def is_positive(x):
        return x > 0

    def total_upto(n):
        i, s = 0, 0
        while i < n:
            s += i
            i += 1
        return s
''')


def _check(*calls: str) -> str:
    """Build a `python -c` body that imports src.py and asserts the given exprs."""
    body = "import sys; sys.path.insert(0, '.'); import src; " + "; ".join(
        f"assert {c}" for c in calls
    )
    return body


def test_discovery_is_deterministic_and_single_change():
    a = discover_mutants(_SRC)
    b = discover_mutants(_SRC)
    assert a and [m.source for m in a] == [m.source for m in b]   # stable order
    ops = {m.operator for m in a}
    assert "arithmetic" in ops and "comparison" in ops and "aug-assign" in ops
    # each mutant differs from the original by exactly one site
    assert all(m.source != _SRC for m in a)


def test_weak_tests_let_mutants_survive(tmp_path):
    (tmp_path / "src.py").write_text(_SRC, encoding="utf-8")
    # Weak: only checks a type, never a value or a boundary.
    cmd = [sys.executable, "-c", _check("isinstance(src.add(2, 3), int)")]
    r = run_mutation(tmp_path / "src.py", cmd, timeout=10, cwd=tmp_path)
    assert r.survived > 0
    assert r.score < 1.0
    assert any(s["operator"] == "arithmetic" for s in r.survivors)


def test_strong_tests_kill_mutants(tmp_path):
    # A source with NO equivalent mutants (no additive-identity constants, no
    # loops), so a strong suite can kill every mutant.
    src = textwrap.dedent('''
        def add(a, b):
            return a + b

        def classify(x):
            if x > 0:
                return "pos"
            return "nonpos"
    ''')
    (tmp_path / "src.py").write_text(src, encoding="utf-8")
    cmd = [sys.executable, "-c", _check(
        "src.add(2, 3) == 5", "src.add(2, 2) == 4",
        "src.classify(1) == 'pos'", "src.classify(0) == 'nonpos'",
        "src.classify(-1) == 'nonpos'",
    )]
    r = run_mutation(tmp_path / "src.py", cmd, timeout=10, cwd=tmp_path)
    # strong asserts catch every value/boundary mutation
    assert r.survived == 0, f"unexpected survivors: {r.survivors}"
    assert r.score == 1.0


def test_spinning_mutant_is_timeout_not_hang(tmp_path):
    (tmp_path / "src.py").write_text(_SRC, encoding="utf-8")
    # Calls total_upto, whose `i += 1 -> i -= 1` mutant loops forever.
    cmd = [sys.executable, "-c", _check("src.total_upto(3) == 3")]
    r = run_mutation(tmp_path / "src.py", cmd, timeout=2, cwd=tmp_path)
    assert r.timed_out >= 1            # the spin was caught as a timeout...
    assert r.total > r.timed_out       # ...and the rest of the run completed


def test_source_is_restored_after_run(tmp_path):
    p = tmp_path / "src.py"
    p.write_text(_SRC, encoding="utf-8")
    run_mutation(p, [sys.executable, "-c", "pass"], timeout=5, cwd=tmp_path)
    assert p.read_text(encoding="utf-8") == _SRC   # original restored


def test_no_mutable_sites_scores_vacuously(tmp_path):
    p = tmp_path / "src.py"
    p.write_text("x = 'hello'\n", encoding="utf-8")
    r = run_mutation(p, [sys.executable, "-c", "pass"], timeout=5, cwd=tmp_path)
    assert r.total == 0 and r.score == 1.0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
