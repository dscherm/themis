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


def test_membership_and_identity_are_mutable():
    """`in`, `not in`, `is`, `is not` are Compare operators and must be swappable.

    They were absent from `_CMP_SWAP`, and the cost was concentrated exactly where it
    hurts most. On this four-line validator the walker found ONE site — the `return True`
    — so the admissibility test, the whole decision the function exists to make, could not
    be mutated and no test suite could be shown to leave it unconstrained.

    For a gate whose validators are the trust boundary, that is the least useful place to
    be blind.
    """
    validator = (
        "def f(v, allowed):\n"
        "    if v not in allowed:\n"
        "        raise ValueError(v)\n"
        "    return True\n"
    )
    ops = {(m.before, m.after) for m in discover_mutants(validator)}
    assert ("not in", "in") in ops, (
        "the membership test is not mutable; mutants found: %s" % sorted(ops))

    for src, pair in (
        ("def g(x, s):\n    return x in s\n", ("in", "not in")),
        ("def h(a):\n    return a is None\n", ("is", "is not")),
        ("def i(a):\n    return a is not None\n", ("is not", "is")),
    ):
        ops = {(m.before, m.after) for m in discover_mutants(src)}
        assert pair in ops, "%r not mutated in %r; got %s" % (pair, src, sorted(ops))


def test_a_mutated_membership_operator_still_parses():
    """Every mutant is produced by `ast.unparse`, so it is valid by construction — but
    this asserts it rather than assuming, because a mutant that does not compile reads as
    a kill while proving only that the file is broken."""
    validator = "def f(v, allowed):\n    if v not in allowed:\n        return False\n    return True\n"
    for m in discover_mutants(validator):
        compile(m.source, "<mutant>", "exec")


def test_a_comparison_yields_both_an_inversion_and_a_boundary_shift():
    """`>=` must produce BOTH `<` and `>`. They catch different bugs.

    An inversion flips the entire result set and dies to almost any test. A boundary shift
    differs for exactly one input — the boundary itself — which is where suites are
    thinnest and why off-by-one survives review.
    """
    ops = {(m.before, m.after) for m in discover_mutants(
        "def clears(score, floor):\n    return score >= floor\n")}
    assert (">=", "<") in ops, "the inversion is missing: %s" % sorted(ops)
    assert (">=", ">") in ops, "the boundary shift is missing: %s" % sorted(ops)

    for src, pair in (
        ("def f(a, b):\n    return a > b\n", (">", ">=")),
        ("def f(a, b):\n    return a <= b\n", ("<=", "<")),
        ("def f(a, b):\n    return a < b\n", ("<", "<=")),
    ):
        got = {(m.before, m.after) for m in discover_mutants(src)}
        assert pair in got, "%r missing from %r: %s" % (pair, src, sorted(got))


def test_a_boundary_mutant_survives_a_suite_that_never_tests_the_boundary():
    """The whole point, asserted rather than assumed.

    A mutation operator nobody has watched SURVIVE is not known to be discriminating — it
    might be equivalent to the original, in which case it can never be killed and is pure
    noise. This runs the mutants against a weak suite and a strong one and requires the
    boundary mutant to survive the first and die to the second.
    """
    code = "def clears(score, floor):\n    return score >= floor\n"

    def survivors(checks):
        out = []
        for m in discover_mutants(code):
            ns = {}
            exec(m.source, ns)  # noqa: S102 — executing a generated mutant is the test
            if all(check(ns["clears"]) for check in checks):
                out.append((m.before, m.after))
        return out

    far_from_boundary = [lambda f: f(10, 5) is True, lambda f: f(1, 5) is False]
    on_the_boundary = far_from_boundary + [lambda f: f(5, 5) is True]

    assert (">=", ">") in survivors(far_from_boundary), (
        "the boundary mutant died to a suite that never tests the boundary, so it is not "
        "discriminating the way this operator claims to")
    assert survivors(on_the_boundary) == [], (
        "a suite testing the boundary still leaves a survivor: %s"
        % survivors(on_the_boundary))


def test_a_named_threshold_still_gets_a_boundary_mutant():
    """Why the operator is needed at all.

    `age >= 18` already got a boundary-equivalent mutant by accident, from the int-constant
    bump 18 -> 19. Spell the threshold as a name and there is no constant to bump — before
    this operator, that comparison had no boundary mutant whatsoever.
    """
    named = "LEGAL = 18\n\n\ndef is_adult(age):\n    return age >= LEGAL\n"
    ops = {(m.before, m.after) for m in discover_mutants(named)}
    assert (">=", ">") in ops, (
        "a named threshold has no boundary mutant, so boundary coverage depends on whether "
        "somebody wrote the number inline: %s" % sorted(ops))
