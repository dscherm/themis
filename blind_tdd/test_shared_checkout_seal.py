"""The green seal check in a checkout several sessions merge into.

ralph-universal OS-1, 2026-10-24: the red phase sealed all 76 test files under
tests/contracts/; three other tasks' test files landed during the implementation
window; the green check called that "test file integrity compromised", wrote a
false test_hash_break to the tamper ledger, and under strict enforcement would
have blocked the commit. OS-1's own test was unchanged.

What these tests pin (docs/shared-checkout-seal.md):
- a NEW file that is not this task's passes green, is reported, and is never
  counted toward this task's coverage;
- a new file tagging this task's criteria still breaks the seal (the "sneak in
  a passing test" attack), as does one named for this task or a new conftest.py;
- any sealed file modified or deleted still breaks the seal.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))

from blind_tdd import gate_integration as gi  # noqa: E402
from blind_tdd.orchestrator import _new_file_scope  # noqa: E402
from blind_tdd.test_hash_integrity import (  # noqa: E402
    GATE_TASK,
    VALID_TASK,
    RunnerSpawner,
    _ReturningFactory,
    _chdir,
    _gate_config,
    _make_orch,
    _run_gate,
    _scenario,
    _write_plan,
    _write_prompts,
)

FOREIGN_UNTAGGED = (
    '"""DR-1: bead_drift reads each bead\'s real status."""\n'
    "def test_reads_real_status():\n    assert True\n"
)
# Another task's blind test: AC ids are per-task, so it tags AC-1 too.
FOREIGN_BLIND = 'def test_other_ac1():\n    """Covers: AC-1"""\n    assert True\n'
SNEAKY = 'def test_sneaky():\n    """Covers: AC-1"""\n    assert True\n'
OTHER_TASK = {"id": "COV-2", "title": "another task", "passes": False,
              "acceptance_criteria": [{"id": "AC-1", "given": "a", "when": "b", "then": "c"}]}


def _red(tmp: Path):
    ralph = tmp / "ralph_home"
    _write_prompts(ralph)
    project = tmp / "project"
    project.mkdir()
    return project, ralph


# ---------------------------------------------------------------------------
# Orchestrator: run_red_phase -> foreign file lands -> run_green_phase
# ---------------------------------------------------------------------------

def test_unrelated_new_file_mid_window_passes_green():
    with tempfile.TemporaryDirectory() as td:
        project, ralph = _red(Path(td))
        with _chdir(project):
            orch = _make_orch(ralph)
            red = orch.run_red_phase(VALID_TASK)
            assert red.passed
            Path("tests/contracts/test_bead_drift_reads_real_status.py").write_text(
                FOREIGN_UNTAGGED, encoding="utf-8")

            green = orch.run_green_phase(VALID_TASK, red)
            assert green.passed, green.reason
            assert green.hash_match and not green.hash_break
            [(path, why)] = green.out_of_scope_files.items()
            assert path.endswith("test_bead_drift_reads_real_status.py")
            assert why.startswith("out of scope")


def test_new_file_tagged_with_this_tasks_ac_still_fails():
    with tempfile.TemporaryDirectory() as td:
        project, ralph = _red(Path(td))
        with _chdir(project):
            orch = _make_orch(ralph)
            red = orch.run_red_phase(VALID_TASK)
            Path("tests/contracts/test_sneaky.py").write_text(SNEAKY, encoding="utf-8")

            green = orch.run_green_phase(VALID_TASK, red)
            assert not green.passed and green.hash_break
            assert "test_sneaky.py" in green.reason
            assert "AC-1" in green.reason


def test_modified_sealed_file_still_fails_alongside_a_foreign_one():
    with tempfile.TemporaryDirectory() as td:
        project, ralph = _red(Path(td))
        with _chdir(project):
            orch = _make_orch(ralph)
            red = orch.run_red_phase(VALID_TASK)
            Path("tests/contracts/test_other.py").write_text(FOREIGN_UNTAGGED, encoding="utf-8")
            Path("tests/contracts/test_ac2.py").write_text(
                'def test_ac2():\n    """Covers: AC-2"""\n    assert True  # weakened\n',
                encoding="utf-8")

            green = orch.run_green_phase(VALID_TASK, red)
            assert not green.passed and green.hash_break
            assert "test_ac2.py (modified)" in green.reason
            assert "test_other.py" not in green.reason


def test_deleted_sealed_file_still_fails():
    with tempfile.TemporaryDirectory() as td:
        project, ralph = _red(Path(td))
        with _chdir(project):
            orch = _make_orch(ralph)
            red = orch.run_red_phase(VALID_TASK)
            Path("tests/contracts/test_ac1.py").unlink()

            green = orch.run_green_phase(VALID_TASK, red)
            assert not green.passed and green.hash_break
            assert "test_ac1.py (deleted)" in green.reason


def test_preexisting_foreign_sealed_file_modified_still_fails():
    """Sealing stays whole-tree: another task's PRE-EXISTING test edited
    mid-window is still a break (re-seal it by hand, per post-ruling-reseal.md)."""
    with tempfile.TemporaryDirectory() as td:
        project, ralph = _red(Path(td))
        with _chdir(project):
            Path("tests/contracts").mkdir(parents=True)
            Path("tests/contracts/test_older_task.py").write_text(FOREIGN_UNTAGGED, encoding="utf-8")
            orch = _make_orch(ralph)
            red = orch.run_red_phase(VALID_TASK)
            Path("tests/contracts/test_older_task.py").write_text(
                FOREIGN_UNTAGGED + "# edited by another session\n", encoding="utf-8")

            green = orch.run_green_phase(VALID_TASK, red)
            assert not green.passed and green.hash_break
            assert "test_older_task.py (modified)" in green.reason


def test_new_conftest_is_in_scope():
    with tempfile.TemporaryDirectory() as td:
        project, ralph = _red(Path(td))
        with _chdir(project):
            orch = _make_orch(ralph)
            red = orch.run_red_phase(VALID_TASK)
            Path("tests/contracts/conftest.py").write_text(
                "import builtins\n", encoding="utf-8")

            green = orch.run_green_phase(VALID_TASK, red)
            assert not green.passed and green.hash_break
            assert "conftest.py" in green.reason


def test_new_file_named_for_this_task_is_in_scope_even_untagged():
    with tempfile.TemporaryDirectory() as td:
        project, ralph = _red(Path(td))
        with _chdir(project):
            orch = _make_orch(ralph)
            red = orch.run_red_phase(VALID_TASK)
            Path("tests/contracts/test_task-hash_extra.py").write_text(
                "def test_extra():\n    assert True\n", encoding="utf-8")

            green = orch.run_green_phase(VALID_TASK, red)
            assert not green.passed and green.hash_break


class _AC1FailsForeignPasses(RunnerSpawner):
    """Runner whose report has this task's AC-1 test failing and another
    task's AC-1-tagged test passing."""

    def spawn(self, *, role, prompt_template, inputs):
        out = super().spawn(role=role, prompt_template=prompt_template, inputs=inputs)
        import json
        p = Path(f".themis/blind_tdd/green_report/{inputs['task_id']}.json")
        rep = json.loads(p.read_text(encoding="utf-8"))
        rep["per_test"][0]["result"] = "fail"
        rep["per_test"].append({"name": "test_other_ac1",
                                "file": "tests/contracts/test_cov-2_blind.py",
                                "result": "pass", "covers": ["AC-1"]})
        rep["overall"] = "pass"
        p.write_text(json.dumps(rep), encoding="utf-8")
        return out


def test_another_tasks_blind_file_is_out_of_scope_and_never_covers():
    """A new file named for another known task, tagging AC-1, is that task's
    test: the seal holds, but its passing AC-1 test cannot stand in for this
    task's failing one."""
    with tempfile.TemporaryDirectory() as td:
        project, ralph = _red(Path(td))
        with _chdir(project):
            orch = _make_orch(ralph, runner_spawner=_AC1FailsForeignPasses())
            orch.known_task_ids = ["task-hash", "COV-2"]
            red = orch.run_red_phase(VALID_TASK)
            Path("tests/contracts/test_cov-2_blind.py").write_text(FOREIGN_BLIND, encoding="utf-8")

            green = orch.run_green_phase(VALID_TASK, red)
            assert not green.hash_break, green.reason
            assert any(p.endswith("test_cov-2_blind.py") for p in green.out_of_scope_files)
            assert not green.passed
            assert "AC-1" in green.coverage.not_passing


def test_without_known_task_ids_another_tasks_tagged_file_breaks():
    """No plan to attribute against: a new file tagging this task's AC ids is
    treated as this task's. Fail closed."""
    with tempfile.TemporaryDirectory() as td:
        project, ralph = _red(Path(td))
        with _chdir(project):
            orch = _make_orch(ralph)
            red = orch.run_red_phase(VALID_TASK)
            Path("tests/contracts/test_cov-2_blind.py").write_text(FOREIGN_BLIND, encoding="utf-8")

            green = orch.run_green_phase(VALID_TASK, red)
            assert not green.passed and green.hash_break


# ---------------------------------------------------------------------------
# Through the gate, strict enforcement, real orchestrator
# ---------------------------------------------------------------------------

def _gate_scenario(td: str, extra_tasks=()):
    project, orch = _scenario(Path(td))
    _write_plan(project, [GATE_TASK, *extra_tasks])
    return project, orch


def test_gate_strict_passes_with_foreign_files_and_writes_no_tamper_record():
    with tempfile.TemporaryDirectory() as td:
        project, orch = _gate_scenario(td, [OTHER_TASK])
        original = gi._make_orchestrator
        gi._make_orchestrator = _ReturningFactory(orch)
        try:
            with _chdir(project):
                cfg = _gate_config("strict")
                assert _run_gate(cfg).phase == "red"
                Path("tests/contracts/test_bead_drift_reads_real_status.py").write_text(
                    FOREIGN_UNTAGGED, encoding="utf-8")
                Path("tests/contracts/test_cov-2_blind.py").write_text(
                    FOREIGN_BLIND, encoding="utf-8")

                r = _run_gate(cfg)
                assert r.passed and r.phase == "green", r.message
                assert len(r.details["out_of_scope_files"]) == 2
                assert "judged another task's" in r.message
                assert not Path(".themis/blind_tdd/tamper_ledger.jsonl").exists()
        finally:
            gi._make_orchestrator = original


def test_gate_strict_still_blocks_a_sneaked_tagged_file():
    with tempfile.TemporaryDirectory() as td:
        project, orch = _gate_scenario(td, [OTHER_TASK])
        original = gi._make_orchestrator
        gi._make_orchestrator = _ReturningFactory(orch)
        try:
            with _chdir(project):
                cfg = _gate_config("strict")
                assert _run_gate(cfg).phase == "red"
                Path("tests/contracts/test_sneaky.py").write_text(SNEAKY, encoding="utf-8")

                r = _run_gate(cfg)
                assert not r.passed and r.reason == "green_failed", r.message
                assert r.details["hash_break"] is True
                assert "test_sneaky.py" in r.message
                assert Path(".themis/blind_tdd/tamper_ledger.jsonl").exists()
        finally:
            gi._make_orchestrator = original


# ---------------------------------------------------------------------------
# The attribution rule itself
# ---------------------------------------------------------------------------

def test_file_naming_both_this_and_another_task_is_in_scope():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "test_cov-2_task-hash.py"
        f.write_text(SNEAKY, encoding="utf-8")
        why = _new_file_scope(str(f), task=VALID_TASK, known_task_ids=["task-hash", "COV-2"])
        assert not why.startswith("out of scope"), why


def test_tag_for_a_criterion_this_task_lacks_is_out_of_scope():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "test_x.py"
        f.write_text('def test_x():\n    """Covers: AC-9"""\n', encoding="utf-8")
        why = _new_file_scope(str(f), task=VALID_TASK, known_task_ids=[])
        assert why.startswith("out of scope"), why
