"""GDScript mutation runner (blind_tdd/mutate_gd.py), without Godot.

The real-Godot trial is recorded in docs/gdscript-pipeline.md. These tests
pin the parts that decide a verdict: which tokens are mutated, how a parse
break is told apart from a kill, and how GUT's output names the killer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from blind_tdd import mutate_gd as mg


def _ops(source: str) -> list[tuple[int, str, str, str]]:
    return [(m.lineno, m.operator, m.before, m.after) for m in mg.discover_mutants(source)]


def test_each_operator_family():
    src = (
        "func f(a, b):\n"
        "\tif a < b and a != 0:\n"
        "\t\ta += 2\n"
        "\treturn a - b == true\n"
    )
    assert _ops(src) == [
        (2, "comparison", "<", ">="),
        (2, "boundary", "<", "<="),
        (2, "boolean", "and", "or"),
        (2, "comparison", "!=", "=="),
        (2, "int-constant", "0", "1"),
        (3, "aug-assign", "+=", "-="),
        (3, "int-constant", "2", "3"),
        (4, "arithmetic", "-", "+"),
        (4, "comparison", "==", "!="),
        (4, "bool-constant", "true", "false"),
    ]


def test_strings_comments_and_type_arrows_are_not_mutated():
    src = (
        "func f(x: int) -> bool:  # a < b and true\n"
        "\tprint(\"a < b == 1 and true\")\n"
        "\treturn x\n"
    )
    assert _ops(src) == []


def test_unary_minus_and_return_minus_are_not_arithmetic():
    src = "func f(x):\n\tvar y = -x\n\treturn -1\n\tcall(a, -b)\n"
    assert [o for o in _ops(src) if o[1] == "arithmetic"] == []


def test_declaration_lines_are_skipped():
    src = (
        "@export_range(0, 10) var n := 3\n"
        "extends Node\n"
        "enum E { A = 1, B = 2 }\n"
        "const S = preload(\"res://x.gd\")\n"
        "var k := 5\n"
    )
    assert _ops(src) == [(5, "int-constant", "5", "6")]


def test_floats_and_hex_are_not_int_constants():
    assert _ops("var a := 1.5\nvar b := 0xFF\nvar c := 1e3\n") == []


def test_a_multiline_string_is_not_mutated():
    src = 'var s := """\nif a < b and true\n"""\nvar n := 1\n'
    assert _ops(src) == [(4, "int-constant", "1", "2")]


def test_each_mutant_changes_exactly_one_token():
    src = "func f(a):\n\treturn a == 1 or a == 2\n"
    for m in mg.discover_mutants(src):
        changed = [i for i, (x, y) in enumerate(zip(src.split("\n"), m.source.split("\n"))) if x != y]
        assert changed == [m.lineno - 1]


def test_lines_limit_the_mutants():
    src = "var a := 1\nvar b := 2\nvar c := 3\n"
    assert [m.lineno for m in mg.discover_mutants(src, {2})] == [2]
    assert mg.parse_line_spec("1-3, 7") == {1, 2, 3, 7}


# --- verdicts from Godot output ------------------------------------------------

# Printed on every run by GUT 9.6 under Godot 4.6 (godot-prompt-bridge, 2026-11-03).
GUT_ADDON_NOISE = (
    'SCRIPT ERROR: Parse Error: Identifier "AccessibilityServer" not declared in the current scope.\n'
    'ERROR: Failed to load script "res://addons/gut/godot_singletons.gd" with error "Parse error".\n'
)


def test_gut_addon_parse_errors_do_not_make_a_mutant_invalid():
    assert not mg._broke_parse(GUT_ADDON_NOISE, "res://addons/x/r.gd")


@pytest.mark.parametrize("line", [
    'SCRIPT ERROR: Parse Error: Could not preload resource script "res://addons/x/r.gd".',
    'SCRIPT ERROR: Parse Error: Could not resolve script "res://addons/x/r.gd".',
    'ERROR: Failed to load script "res://addons/x/r.gd" with error "Parse error".',
    "   at: GDScript::reload (res://addons/x/r.gd:39)",
])
def test_a_parse_break_in_the_mutated_file_is_invalid(line):
    assert mg._broke_parse(GUT_ADDON_NOISE + line + "\n", "res://addons/x/r.gd")


def test_a_parse_break_in_another_file_is_not():
    assert not mg._broke_parse(
        'ERROR: Failed to load script "res://addons/x/r2.gd" with error "Parse error".\n',
        "res://addons/x/r.gd",
    )


def test_gut_killers_are_read_from_the_failure_listing():
    out = (
        "res://tests/contracts/test_pb2.gd\n"
        "* test_ok\n"
        "* test_a\n"
        "    [Failed]:  [0] expected to equal [1]\n"
        "    [Failed]:  [0] expected to equal [1]\n"
        "res://tests/contracts/test_pb3.gd\n"
        "* test_b\n"
        "    \x1b[31m[Failed]\x1b[0m:  nope\n"
    )
    assert mg._gut_killers(out) == [
        "res://tests/contracts/test_pb2.gd::test_a",
        "res://tests/contracts/test_pb3.gd::test_b",
    ]


# --- the run loop, with a stand-in test command -------------------------------

# Stands in for GUT: `a <= b` and `return 3` fail a test, `return 2` breaks
# parsing of lib.gd, anything else passes.
CHECKER = """\
import sys, pathlib
src = pathlib.Path('lib.gd').read_text()
if 'return 2' in src:
    print('ERROR: Failed to load script "res://lib.gd" with error "Parse error".')
    sys.exit(1)
sys.exit(1 if 'a <= b' in src or 'return 3' in src else 0)
"""


def _project(tmp_path: Path, source: str) -> Path:
    (tmp_path / "project.godot").write_text("", encoding="utf-8")
    (tmp_path / "check.py").write_text(CHECKER, encoding="utf-8")
    src = tmp_path / "lib.gd"
    src.write_text(source, encoding="utf-8")
    return src


def test_run_counts_each_verdict_and_restores_the_file(tmp_path):
    source = "func f(a, b):\n\tif a < b:\n\t\treturn 1\n"
    src = _project(tmp_path, source)
    r = mg.run_mutation(src, [sys.executable, "check.py"], timeout=60)
    verdicts = {(rec["before"], rec["after"]): rec["verdict"] for rec in r.records}
    assert verdicts == {("<", ">="): "survived", ("<", "<="): "killed", ("1", "2"): "invalid"}
    assert (r.total, r.killed, r.survived, r.invalid) == (3, 1, 1, 1)
    assert r.score == 0.5  # the invalid mutant is left out: 1 of 2 valid survived
    assert src.read_text(encoding="utf-8") == source


def test_a_red_baseline_stops_the_run(tmp_path):
    src = _project(tmp_path, "func f():\n\treturn 3\n")
    with pytest.raises(mg.BaselineNotGreen):
        mg.run_mutation(src, [sys.executable, "check.py"], timeout=60)


def test_resolve_shell_never_picks_the_wsl_launcher(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows path search only")
    cmd = mg.resolve_shell(["bash", "x.sh"])
    assert "system32" not in cmd[0].lower() and "windowsapps" not in cmd[0].lower()
