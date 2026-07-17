"""Regression tests for GDScript AC-tag coverage extraction.

`coverage.extract_covers` dispatches by file suffix; a missing `.gd` branch
silently dropped GDScript coverage (the tags were invisible, so a fully-tagged
suite read as a coverage gap). These pin the `.gd` extractor:
`# Covers: AC-N` comments — inside a `func _test_*` body or on the line above
it — are attributed to the enclosing test method.
"""
from __future__ import annotations

from blind_tdd.coverage import extract_covers, _extract_from_gdscript_file

_TESTBASE_STYLE = """class_name TestXpCurve
extends TestBase

func _init() -> void:
\t_test_name = "XpCurve"

func run_tests() -> void:
\tvar c := XpCurve.new()
\t_test_zero(c)
\t_test_incr(c)

func _test_zero(c) -> void:
\t# Covers: AC-1 — a fresh XpCurve returns 0 for level 1
\t_check_eq("l1", c.xp_for_level(1), 0)

func _test_incr(c) -> void:
\t# Covers: AC-2, AC-3 — strictly increasing and clamped below 1
\t_check("incr", c.xp_for_level(3) > c.xp_for_level(2))
"""


def test_gdscript_tags_inside_body_are_extracted(tmp_path):
    d = tmp_path / "tests"
    d.mkdir()
    (d / "test_xp_curve.gd").write_text(_TESTBASE_STYLE, encoding="utf-8")

    anns = extract_covers(d)
    covered = {ac for a in anns for ac in a.covers}
    assert covered == {"AC-1", "AC-2", "AC-3"}

    # Tags attach to the enclosing test method, not to run_tests()/_init().
    tagged = {a.test_name for a in anns if a.covers}
    assert tagged == {"_test_zero", "_test_incr"}


def test_gdscript_tag_on_line_above_func_attaches_to_it(tmp_path):
    d = tmp_path / "tests"
    d.mkdir()
    (d / "test_above.gd").write_text(
        "# Covers: AC-9\nfunc _test_thing() -> void:\n\tpass\n",
        encoding="utf-8",
    )
    anns = _extract_from_gdscript_file(d / "test_above.gd")
    assert any(a.test_name == "_test_thing" and a.covers == ["AC-9"] for a in anns)


def test_gdscript_untagged_helper_funcs_carry_no_coverage(tmp_path):
    d = tmp_path / "tests"
    d.mkdir()
    (d / "test_plain.gd").write_text(
        "func run_tests() -> void:\n\tpass\n"
        "func _helper() -> void:\n\tpass\n",
        encoding="utf-8",
    )
    # Neither is a test_* method; nothing is claimed as covered.
    covered = {ac for a in extract_covers(d) for ac in a.covers}
    assert covered == set()
