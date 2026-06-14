"""Self-contained tests for BlindTddOrchestrator.resume_red_phase_after_ruling.

Exercises Phase B2: after an upheld challenge, the orchestrator must
re-spawn Agent #1 with arbiter reasoning in the prompt, merge the new
triage report with the previous one, re-hash the test tree, and return
a fresh RedPhaseResult.

Run with:
    python -m blind_tdd.test_resume_red_phase
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[1]
sys.path.insert(0, str(_REPO))

from blind_tdd import orchestrator as orch_mod  # noqa: E402
from blind_tdd.orchestrator import (  # noqa: E402
    BlindTddOrchestrator,
    RedPhaseResult,
    _build_resume_context_block,
    _merge_triage,
)


VALID_TASK = {
    "id": "task-resume-1",
    "title": "Do thing",
    "acceptance_criteria": [
        {"id": "AC-1", "given": "a", "when": "b", "then": "c"},
        {"id": "AC-2", "given": "d", "when": "e", "then": "f"},
    ],
    "public_surface": {"module": "src.foo", "adds": ["do_thing()"]},
}


def _chdir(path: Path):
    class _Ctx:
        def __enter__(self_):
            self_.prev = os.getcwd()
            os.chdir(path)
            return path

        def __exit__(self_, *a):
            os.chdir(self_.prev)
    return _Ctx()


def _write_writer_prompt(ralph_home: Path) -> Path:
    d = ralph_home / "templates" / "blind_tdd" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "test_writer.md"
    p.write_text("# Stub writer prompt\nBe blind.\n", encoding="utf-8")
    # Also create runner/arbiter stubs so orchestrator init doesn't fail
    (d / "test_runner.md").write_text("# Runner\n", encoding="utf-8")
    (d / "arbiter.md").write_text("# Arbiter\n", encoding="utf-8")
    return p


class FakeWriterSpawner:
    """Spawner that writes a test file and triage report for a given role.

    On each spawn, it creates:
      - tests/contracts/test_ac1.py covering AC-1
      - tests/contracts/test_ac2.py covering AC-2
      - .themis/blind_tdd/triage/<task_id>.json with empty triage list
    """

    def __init__(self, *, triage_extra: list | None = None):
        self.triage_extra = triage_extra or []
        self.spawned_count = 0
        self.last_prompt: str = ""
        self.last_inputs: dict = {}

    def spawn(self, *, role: str, prompt_template: str, inputs: dict) -> dict:
        self.spawned_count += 1
        self.last_prompt = prompt_template
        self.last_inputs = inputs
        if role != "test_writer":
            return {"success": False, "error": f"unexpected role {role}"}

        task_id = inputs["task_id"]
        # Write tests covering both criteria
        tests_dir = Path("tests/contracts")
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_ac1.py").write_text(
            'def test_ac1():\n    """Covers: AC-1"""\n    assert True\n',
            encoding="utf-8",
        )
        (tests_dir / "test_ac2.py").write_text(
            'def test_ac2():\n    """Covers: AC-2"""\n    assert True\n',
            encoding="utf-8",
        )
        # Write triage report
        triage_dir = Path(".themis/blind_tdd/triage")
        triage_dir.mkdir(parents=True, exist_ok=True)
        triage_doc = {
            "task": task_id,
            "agent_id": f"fake-writer-{self.spawned_count}",
            "phase": "red",
            "criteria_tested": ["AC-1", "AC-2"],
            "triage": list(self.triage_extra),
        }
        (triage_dir / f"{task_id}.json").write_text(
            json.dumps(triage_doc, indent=2), encoding="utf-8"
        )
        return {"success": True, "agent_id": f"fake-writer-{self.spawned_count}"}


def _make_orch(ralph_home: Path) -> BlindTddOrchestrator:
    base = ralph_home / "templates" / "blind_tdd" / "prompts"
    return BlindTddOrchestrator(
        spawner=FakeWriterSpawner(),
        test_dirs=["tests/contracts/"],
        writer_prompt_path=str(base / "test_writer.md"),
        runner_prompt_path=str(base / "test_runner.md"),
        arbiter_prompt_path=str(base / "arbiter.md"),
    )


# ---------------------------------------------------------------------------
# Helpers: _build_resume_context_block
# ---------------------------------------------------------------------------

def test_build_resume_context_block_formats_ruling():
    ruling = {
        "criterion_affected": "AC-3",
        "reasoning": "The test uses time.sleep which is flaky.",
        "resolution": "rewrite_with_mocked_clock",
    }
    block = _build_resume_context_block(ruling)
    assert "Resume context" in block
    assert "AC-3" in block
    assert "flaky" in block
    assert "rewrite_with_mocked_clock" in block
    assert "UPHELD" in block


def test_build_resume_context_block_handles_missing_fields():
    block = _build_resume_context_block({})
    assert "?" in block  # criterion placeholder
    assert "(no reasoning provided)" in block
    assert "(none)" in block  # resolution placeholder


# ---------------------------------------------------------------------------
# Helpers: _merge_triage
# ---------------------------------------------------------------------------

def test_merge_triage_new_entries_override():
    prev = {"task": "t", "triage": [
        {"criterion": "AC-1", "status": "tested", "note": "old"},
        {"criterion": "AC-2", "status": "needs_human", "note": "keep this"},
    ]}
    new = {"task": "t", "triage": [
        {"criterion": "AC-1", "status": "tested", "note": "new"},
    ]}
    merged = _merge_triage(prev, new)
    entries = {e["criterion"]: e for e in merged["triage"]}
    assert entries["AC-1"]["note"] == "new"  # new wins
    assert entries["AC-2"]["note"] == "keep this"  # preserved


def test_merge_triage_handles_empty():
    assert _merge_triage({}, {"task": "t", "triage": []})["triage"] == []
    merged = _merge_triage({"task": "t", "triage": [{"criterion": "AC-1"}]}, {})
    assert len(merged["triage"]) == 1


def test_merge_triage_criterion_case_normalized():
    prev = {"task": "t", "triage": [{"criterion": "ac-1", "note": "a"}]}
    new = {"task": "t", "triage": [{"criterion": "AC-1", "note": "b"}]}
    merged = _merge_triage(prev, new)
    assert len(merged["triage"]) == 1
    assert merged["triage"][0]["note"] == "b"


# ---------------------------------------------------------------------------
# resume_red_phase_after_ruling
# ---------------------------------------------------------------------------

def test_resume_red_phase_happy_path():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = tmp / "ralph_home"
        _write_writer_prompt(ralph)
        project = tmp / "project"
        project.mkdir()

        with _chdir(project):
            orch = _make_orch(ralph)
            previous_red = RedPhaseResult(
                passed=True,
                test_file_hashes={"tests/contracts/old_ac1.py": "deadbeef"},
                triage_report={
                    "task": "task-resume-1", "triage": [
                        {"criterion": "AC-2", "status": "tested"},
                    ],
                },
            )
            ruling = {
                "criterion_affected": "AC-1",
                "reasoning": "previous test asserted wrong value",
                "resolution": "rewrite_with_correct_assertion",
            }

            result = orch.resume_red_phase_after_ruling(
                VALID_TASK, previous_red, ruling,
            )

            assert result.passed, f"expected pass, got {result.reason}"
            # Fresh hashes across the rewritten test tree
            assert len(result.test_file_hashes) == 2
            assert any("test_ac1.py" in k for k in result.test_file_hashes.keys())
            assert any("test_ac2.py" in k for k in result.test_file_hashes.keys())
            # Arbiter context was injected into the spawner's prompt
            spawner = orch.spawner
            assert "Resume context" in spawner.last_prompt
            assert "AC-1" in spawner.last_prompt
            assert "wrong value" in spawner.last_prompt
            # resume_context was passed through in inputs
            assert spawner.last_inputs["resume_context"]["affected_criterion"] == "AC-1"


def test_resume_red_phase_fails_on_invalid_schema():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = tmp / "ralph_home"
        _write_writer_prompt(ralph)
        project = tmp / "project"
        project.mkdir()

        with _chdir(project):
            orch = _make_orch(ralph)
            broken_task = {"id": "bad", "title": "bad"}  # no criteria
            result = orch.resume_red_phase_after_ruling(
                broken_task,
                RedPhaseResult(passed=True),
                {"criterion_affected": "AC-1", "reasoning": "x"},
            )
            assert not result.passed
            assert "schema validation failed on resume" in result.reason


def test_resume_red_phase_fails_when_spawner_errors():
    class FailingSpawner:
        def spawn(self, *, role, prompt_template, inputs):
            return {"success": False, "error": "API down"}

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = tmp / "ralph_home"
        _write_writer_prompt(ralph)
        project = tmp / "project"
        project.mkdir()

        with _chdir(project):
            base = ralph / "templates" / "blind_tdd" / "prompts"
            orch = BlindTddOrchestrator(
                spawner=FailingSpawner(),
                test_dirs=["tests/contracts/"],
                writer_prompt_path=str(base / "test_writer.md"),
                runner_prompt_path=str(base / "test_runner.md"),
                arbiter_prompt_path=str(base / "arbiter.md"),
            )
            result = orch.resume_red_phase_after_ruling(
                VALID_TASK,
                RedPhaseResult(passed=True),
                {"criterion_affected": "AC-1", "reasoning": "x"},
            )
            assert not result.passed
            assert "writer respawn failed" in result.reason
            assert "API down" in result.reason


def test_resume_red_phase_fails_on_coverage_gap():
    """Spawner writes a triage report missing one criterion — coverage should fail."""
    class IncompleteSpawner:
        def __init__(self):
            self.last_prompt = ""
            self.last_inputs = {}

        def spawn(self, *, role, prompt_template, inputs):
            self.last_prompt = prompt_template
            self.last_inputs = inputs
            task_id = inputs["task_id"]
            # Only AC-1 gets a test — AC-2 is uncovered and not triaged
            Path("tests/contracts").mkdir(parents=True, exist_ok=True)
            Path("tests/contracts/test_ac1.py").write_text(
                'def test_ac1():\n    """Covers: AC-1"""\n    assert True\n',
                encoding="utf-8",
            )
            Path(".themis/blind_tdd/triage").mkdir(parents=True, exist_ok=True)
            Path(f".themis/blind_tdd/triage/{task_id}.json").write_text(
                json.dumps({"task": task_id, "triage": []}), encoding="utf-8"
            )
            return {"success": True, "agent_id": "incomplete"}

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = tmp / "ralph_home"
        _write_writer_prompt(ralph)
        project = tmp / "project"
        project.mkdir()

        with _chdir(project):
            base = ralph / "templates" / "blind_tdd" / "prompts"
            orch = BlindTddOrchestrator(
                spawner=IncompleteSpawner(),
                test_dirs=["tests/contracts/"],
                writer_prompt_path=str(base / "test_writer.md"),
                runner_prompt_path=str(base / "test_runner.md"),
                arbiter_prompt_path=str(base / "arbiter.md"),
            )
            result = orch.resume_red_phase_after_ruling(
                VALID_TASK,
                RedPhaseResult(passed=True, triage_report={"task": "task-resume-1", "triage": []}),
                {"criterion_affected": "AC-1", "reasoning": "x"},
            )
            assert not result.passed
            assert "coverage gap after resume" in result.reason
            assert "AC-2" in str(result.coverage.missing)


def test_resume_red_phase_merges_previous_triage_for_unaffected_criteria():
    class OnlyAC1Spawner:
        def __init__(self):
            self.last_inputs = {}
            self.last_prompt = ""

        def spawn(self, *, role, prompt_template, inputs):
            self.last_inputs = inputs
            self.last_prompt = prompt_template
            task_id = inputs["task_id"]
            Path("tests/contracts").mkdir(parents=True, exist_ok=True)
            Path("tests/contracts/test_ac1.py").write_text(
                'def test_ac1():\n    """Covers: AC-1"""\n    assert True\n',
                encoding="utf-8",
            )
            # New triage report only covers AC-1; AC-2 remains in previous
            Path(".themis/blind_tdd/triage").mkdir(parents=True, exist_ok=True)
            Path(f".themis/blind_tdd/triage/{task_id}.json").write_text(
                json.dumps({
                    "task": task_id,
                    "triage": [
                        {"criterion": "AC-1", "status": "tested"},
                    ],
                }),
                encoding="utf-8",
            )
            return {"success": True, "agent_id": "only-ac1"}

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = tmp / "ralph_home"
        _write_writer_prompt(ralph)
        project = tmp / "project"
        project.mkdir()

        with _chdir(project):
            base = ralph / "templates" / "blind_tdd" / "prompts"
            orch = BlindTddOrchestrator(
                spawner=OnlyAC1Spawner(),
                test_dirs=["tests/contracts/"],
                writer_prompt_path=str(base / "test_writer.md"),
                runner_prompt_path=str(base / "test_runner.md"),
                arbiter_prompt_path=str(base / "arbiter.md"),
            )
            previous_red = RedPhaseResult(
                passed=True,
                triage_report={
                    "task": "task-resume-1",
                    "triage": [
                        {"criterion": "AC-2", "status": "needs_human",
                         "reason": "subjective visual check"},
                    ],
                },
            )
            result = orch.resume_red_phase_after_ruling(
                VALID_TASK, previous_red,
                {"criterion_affected": "AC-1", "reasoning": "x"},
            )
            assert result.passed
            # AC-2 preserved from previous triage, so coverage passes
            triage_by_crit = {
                e["criterion"]: e
                for e in result.triage_report["triage"]
            }
            assert "AC-2" in triage_by_crit
            assert triage_by_crit["AC-2"]["status"] == "needs_human"
            assert "AC-1" in triage_by_crit
            assert triage_by_crit["AC-1"]["status"] == "tested"


def test_resume_green_phase_alias_calls_run_green_phase():
    """resume_green_phase should delegate to run_green_phase."""
    class RecordingOrch(BlindTddOrchestrator):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.run_green_calls = 0

        def run_green_phase(self, task, red_result):
            self.run_green_calls += 1
            return "delegated"

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = tmp / "ralph_home"
        _write_writer_prompt(ralph)
        project = tmp / "project"
        project.mkdir()
        with _chdir(project):
            base = ralph / "templates" / "blind_tdd" / "prompts"
            orch = RecordingOrch(
                spawner=FakeWriterSpawner(),
                test_dirs=["tests/contracts/"],
                writer_prompt_path=str(base / "test_writer.md"),
                runner_prompt_path=str(base / "test_runner.md"),
                arbiter_prompt_path=str(base / "arbiter.md"),
            )
            result = orch.resume_green_phase(VALID_TASK, RedPhaseResult(passed=True))
            assert result == "delegated"
            assert orch.run_green_calls == 1


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [
        test_build_resume_context_block_formats_ruling,
        test_build_resume_context_block_handles_missing_fields,
        test_merge_triage_new_entries_override,
        test_merge_triage_handles_empty,
        test_merge_triage_criterion_case_normalized,
        test_resume_red_phase_happy_path,
        test_resume_red_phase_fails_on_invalid_schema,
        test_resume_red_phase_fails_when_spawner_errors,
        test_resume_red_phase_fails_on_coverage_gap,
        test_resume_red_phase_merges_previous_triage_for_unaffected_criteria,
        test_resume_green_phase_alias_calls_run_green_phase,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
        else:
            print(f"  PASS  {t.__name__}")
    print()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
