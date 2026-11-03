"""GDScript support in the stages that used to read Python only.

See docs/gdscript-pipeline.md. Each stage here used to drop `.gd` files
silently: the `Bead:` reader, the quality review's collector and scanner,
and the suppression scan. These tests pin that each one now reads GDScript,
and that a test file in a language with no scanner is reported as not
scanned instead of passing as clean.
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path

import pytest

from blind_tdd import gate_integration as gi
from blind_tdd import lang
from blind_tdd.orchestrator import _new_file_scope
from blind_tdd.quality_gd import scan_test_file
from blind_tdd.quality_review import scan
from blind_tdd.suppression import LEGACY_SOURCE_SUFFIXES, scan_repo, scan_text


# --- Bead: lines --------------------------------------------------------------

GD_HEADER = (
    "extends GutTest\n"
    "## Contract tests for PB-2.\n"
    "## Bead: PB-2\n"
    "\n"
    "const R = preload(\"res://addons/x/r.gd\")\n"
)


def test_gdscript_bead_is_read_from_the_header():
    assert lang.bead_of_text("t/test_x.gd", GD_HEADER) == "PB-2"
    assert lang.bead_of_text("t/test_x.gd", "# Bead: PB-3\nextends GutTest\n") == "PB-3"
    assert lang.bead_of_text("t/test_x.gd", "@tool\nclass_name T\nextends GutTest\n## Bead: A.1\n") == "A.1"


def test_a_bead_comment_below_the_header_does_not_count():
    text = "extends GutTest\n\nvar x = 1\n## Bead: PB-9\n"
    assert lang.bead_of_text("t/test_x.gd", text) is None


def test_python_bead_reader_is_unchanged():
    assert lang.bead_of_text("t/test_x.py", '"""Tests.\n\nBead: TD10\n"""\n') == "TD10"
    assert lang.bead_of_text("t/test_x.py", "# Bead: TD10\n") is None


def test_an_unknown_language_has_no_bead():
    assert lang.bead_of_text("t/test_x.txt", "Bead: X\n") is None


def test_seal_attributes_a_new_gd_file_by_its_bead_line(tmp_path):
    mine = tmp_path / "test_new.gd"
    mine.write_text(GD_HEADER, encoding="utf-8")
    other = tmp_path / "test_other.gd"
    other.write_text(GD_HEADER.replace("PB-2", "PB-7"), encoding="utf-8")
    task = {"id": "PB-2", "acceptance_criteria": []}
    assert _new_file_scope(str(mine), task=task, known_task_ids=set()) == "new file declaring `Bead: PB-2`"
    assert _new_file_scope(str(other), task=task, known_task_ids=set()).startswith("out of scope")


# --- GDScript quality scanner ---------------------------------------------------

def _kinds(tmp_path: Path, body: str, public_module: str | None = None) -> list[tuple[str, str]]:
    p = tmp_path / "test_q.gd"
    p.write_text("extends GutTest\n\n" + body, encoding="utf-8")
    return [(c.kind, c.test_name) for c in scan_test_file(p, public_module)]


def test_a_real_assertion_raises_nothing(tmp_path):
    assert _kinds(tmp_path,
        "func test_sum() -> void:\n"
        "\tassert_eq(add(2, 3), 5)\n"
        "\tassert_true(f(1) > 0, \"positive\")\n") == []


def test_empty_test(tmp_path):
    assert _kinds(tmp_path, "func test_nothing() -> void:\n\tvar x = make()\n\tx.go()\n") == [
        ("empty_test", "test_nothing")]


def test_pending_test(tmp_path):
    assert _kinds(tmp_path, "func test_later():\n\tpending(\"todo\")\n") == [
        ("pending_test", "test_later")]


@pytest.mark.parametrize("line", [
    "assert_true(true)",
    "assert_not_null(make())",
    "assert_eq(x, x)",
    "assert_gte(items.size(), 0)",
    "assert_true(node != null)",
    "pass_test(\"ok\")",
])
def test_trivial_assertions(tmp_path, line):
    assert _kinds(tmp_path, f"func test_t():\n\t{line}\n") == [("trivial_assertion", "test_t")]


def test_one_trivial_and_one_real_assertion_is_fine(tmp_path):
    assert _kinds(tmp_path, "func test_t():\n\tassert_not_null(r)\n\tassert_eq(r.n, 3)\n") == []


def test_a_project_assert_helper_counts_as_an_assertion(tmp_path):
    assert _kinds(tmp_path, "func test_t():\n\t_assert_written(path, 2)\n") == []


def test_one_line_body_and_inner_class_tests(tmp_path):
    body = (
        "func test_inline() -> void: assert_eq(f(), 2)\n"
        "\n"
        "class TestInner:\n"
        "\textends GutTest\n"
        "\tfunc test_inner():\n"
        "\t\tvar a = 1\n"
        "\n"
        "func test_after():\n"
        "\tassert_eq(g(), 1)\n"
    )
    assert _kinds(tmp_path, body) == [("empty_test", "test_inner")]


def test_comment_and_string_contents_are_not_assertions(tmp_path):
    body = (
        "func test_t():\n"
        "\t# assert_eq(a, b)\n"
        "\tprint(\"assert_eq(a, b)\") # assert_true(x)\n"
    )
    assert _kinds(tmp_path, body) == [("empty_test", "test_t")]


def test_doubling_the_script_under_test_is_flagged(tmp_path):
    body = (
        "const Panel = preload(\"res://addons/x/panel.gd\")\n"
        "func test_t():\n"
        "\tvar p = partial_double(Panel).new()\n"
        "\tvar q = double(\"res://addons/x/panel.gd\").new()\n"
        "\tvar other = double(Helper).new()\n"
        "\tassert_eq(p.n, 1)\n"
    )
    kinds = _kinds(tmp_path, body, public_module="addons/x/panel.gd")
    assert kinds.count(("mock_shadows_uut", "(double)")) == 2


# --- quality review: dispatch and "not scanned" --------------------------------

def test_scan_reads_gd_and_reports_an_unknown_language_as_not_scanned(tmp_path):
    gd = tmp_path / "test_a.gd"
    gd.write_text("extends GutTest\nfunc test_x():\n\tpass\n", encoding="utf-8")
    rs = tmp_path / "test_b.rs"
    rs.write_text("#[test] fn x() {}\n", encoding="utf-8")
    result = scan("T", [gd, rs], [])
    assert [c.kind for c in result.candidates] == ["empty_test"]
    assert result.not_scanned == [str(rs)]
    assert not result.is_clean()


def test_a_not_scanned_file_alone_does_not_auto_pass(tmp_path):
    rs = tmp_path / "test_b.rs"
    rs.write_text("#[test] fn x() { assert_eq!(1, 1); }\n", encoding="utf-8")
    result = scan("T", [rs], [])
    assert result.candidates == [] and not result.is_clean()


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("THEMIS_HOME", raising=False)
    monkeypatch.delenv("RALPH_HOME", raising=False)
    t = tmp_path / "tests" / "contracts"
    t.mkdir(parents=True)
    (t / "test_pb2.gd").write_text(GD_HEADER + "\nfunc test_x():\n\tvar a = 1\n", encoding="utf-8")
    (t / "test_pb3.gd").write_text(
        GD_HEADER.replace("PB-2", "PB-3") + "\nfunc test_y():\n\tassert_eq(f(), 2)\n",
        encoding="utf-8")


def _brief_json(tmp_path: Path, task_id: str) -> dict:
    text = (tmp_path / ".themis/blind_tdd/quality_review_pending" / f"{task_id}.md").read_text(encoding="utf-8")
    return json.loads(re.search(r"```json\n(.*?)\n```", text, re.S).group(1))


def test_gate_scopes_the_review_to_the_gd_file_with_this_tasks_bead(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    r = gi._run_quality_review_phase({}, "PB-2", ["tests"])
    assert r is not None and r.phase == "quality_review_pending"
    data = _brief_json(tmp_path, "PB-2")
    assert {c["file"] for c in data["candidates"]} == {"tests/contracts/test_pb2.gd"}


def test_gate_does_not_auto_pass_when_a_test_file_was_not_scanned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("THEMIS_HOME", raising=False)
    monkeypatch.delenv("RALPH_HOME", raising=False)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.rs").write_text("fn x() {}\n", encoding="utf-8")
    r = gi._run_quality_review_phase({}, "R1", ["tests"])
    assert r is not None and r.phase == "quality_review_pending"
    assert r.details["not_scanned"]
    assert not (tmp_path / ".themis/blind_tdd/quality_review/R1.md").exists()
    assert _brief_json(tmp_path, "R1")["not_scanned"] == ["tests/test_x.rs"]


# --- suppression scan -------------------------------------------------------------

def test_gdscript_warning_ignore_is_a_suppression_marker():
    assert scan_text("@warning_ignore(\"unused_variable\")\nvar x\n") == {"gd-warning-ignore": 1}
    assert scan_text("@warning_ignore_start(\"x\")\n") == {"gd-warning-ignore": 1}


def test_scan_repo_covers_gd_but_a_legacy_rescan_does_not(tmp_path):
    (tmp_path / "a.gd").write_text("@warning_ignore(\"x\")\nfunc f(): pass\n", encoding="utf-8")
    assert "a.gd" in scan_repo(tmp_path)
    assert scan_repo(tmp_path, suffixes=LEGACY_SOURCE_SUFFIXES) == {}


def _red(hashes):
    return types.SimpleNamespace(test_file_hashes=hashes, triage_report={}, spawn_result=None)


def test_red_state_records_and_signs_the_baseline_suffixes(tmp_path, monkeypatch):
    monkeypatch.setenv(gi.SEAL_KEY_ENV, "k")
    monkeypatch.chdir(tmp_path)
    gi.save_red_state("t", _red({}), suppression_baseline={},
                      suppression_suffixes=[".gd", ".py"])
    state = gi.load_red_state("t")
    assert state["suppression_suffixes"] == [".gd", ".py"]
    assert gi.verify_red_state_seal(state)[0]
    # Deleting the field to shrink the green rescan breaks the seal.
    del state["suppression_suffixes"]
    assert not gi.verify_red_state_seal(state)[0]


def test_a_red_state_without_recorded_suffixes_still_verifies(tmp_path, monkeypatch):
    """Records sealed before this change carry no suffix list; they must
    keep verifying, and their green rescan uses the legacy set."""
    monkeypatch.setenv(gi.SEAL_KEY_ENV, "k")
    monkeypatch.chdir(tmp_path)
    gi.save_red_state("t", _red({}), suppression_baseline={})
    state = gi.load_red_state("t")
    assert "suppression_suffixes" not in state
    assert gi.verify_red_state_seal(state)[0]


def test_asserting_a_boolean_property_is_not_trivial(tmp_path):
    """From godot-prompt-bridge test_pb8d_prompt_panel.gd:335, a real check."""
    assert _kinds(tmp_path, "func test_t():\n\tassert_true(send.disabled, \"disabled\")\n") == []


def test_a_test_that_calls_an_asserting_helper_is_not_empty(tmp_path):
    """Soapbox-2D test_gw06_big_stream.gd:79: `await _check(1)` asserts inside."""
    body = (
        "func _inner(n):\n\tassert_eq(n, 1)\n\n"
        "func _check(n):\n\t_inner(n)\n\n"
        "func test_1p() -> void:\n\tawait _check(1)\n"
    )
    assert _kinds(tmp_path, body) == []


def test_assert_null_is_a_real_claim(tmp_path):
    """Soapbox-2D test_boost_flames.gd:221: no regex match is the requirement."""
    assert _kinds(tmp_path, "func test_t():\n\tassert_null(re.search(src), \"no Time\")\n") == []


def test_a_string_spanning_lines_does_not_end_the_body(tmp_path):
    """Soapbox-2D test_kart_layout.gd:353 splits on a literal newline."""
    body = (
        "func test_t():\n"
        "\tfor line in text.split(\"\n\"):\n"
        "\t\tassert_true(line.begins_with(\"x\"))\n"
    )
    assert _kinds(tmp_path, body) == []


@pytest.mark.parametrize("line", [
    "assert_true(flush, \"flush_stdout_on_print\")",
    "assert_eq(_draws(Rng.new(42), 100), _draws(Rng.new(42), 100))",
])
def test_idiomatic_gdscript_checks_are_not_trivial(tmp_path, line):
    """Soapbox-2D test_project_settings.gd:80 and test_race_rng.gd:14."""
    assert _kinds(tmp_path, "func test_t():\n\t" + line + "\n") == []
