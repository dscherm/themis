"""Tests for the green-phase hash integrity check — closes the audit gap.

THE audit found that test_gate_integration uses a stub orchestrator
that replaces run_green_phase entirely, so the real hash-comparison
code has zero coverage. This file tests the real thing: run a real
red phase, run a real green phase, and verify the hash check fires
when a test file is modified between them.

Uses a fake spawner (no claude -p), but the orchestrator's own code
path — red phase, file hashing, green phase, coverage verification,
hash comparison — is exercised end-to-end with real file I/O.

Covers:
- Real green phase passes when test files are untouched
- Real green phase FAILS when a test file is modified between phases
- Real green phase FAILS when a test file is deleted between phases
- Real green phase FAILS when a new test file is added between phases
  (catches the "sneak in a trivially-passing test" attack)
- Coverage verification actually fires (every AC must be in per_test)
- Hash set comparison is strict (order-independent but content-strict)

Run with:
    python -m blind_tdd.test_hash_integrity
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[1]
sys.path.insert(0, str(_REPO))

from blind_tdd.orchestrator import (  # noqa: E402
    BlindTddOrchestrator,
    RedPhaseResult,
    _hash_test_files,
)


def _chdir(path: Path):
    class _Ctx:
        def __enter__(self_):
            self_.prev = os.getcwd()
            os.chdir(path)
            return path

        def __exit__(self_, *a):
            os.chdir(self_.prev)
    return _Ctx()


def _write_prompts(ralph_home: Path) -> Path:
    d = ralph_home / "templates" / "blind_tdd" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("test_writer.md", "test_runner.md", "arbiter.md"):
        (d / name).write_text(f"# Stub {name}\n", encoding="utf-8")
    return d


VALID_TASK = {
    "id": "task-hash",
    "acceptance_criteria": [
        {"id": "AC-1", "given": "a", "when": "b", "then": "c"},
        {"id": "AC-2", "given": "d", "when": "e", "then": "f"},
    ],
    "public_surface": {"module": "src.foo", "adds": ["foo()"]},
}


class WriterSpawner:
    """Fake spawner that writes two test files + a triage report."""

    def spawn(self, *, role, prompt_template, inputs):
        if role != "test_writer":
            return {"success": False, "error": f"unexpected role {role}"}
        task_id = inputs["task_id"]
        Path("tests/contracts").mkdir(parents=True, exist_ok=True)
        Path("tests/contracts/test_ac1.py").write_text(
            'def test_ac1():\n    """Covers: AC-1"""\n    assert 1 == 1\n',
            encoding="utf-8",
        )
        Path("tests/contracts/test_ac2.py").write_text(
            'def test_ac2():\n    """Covers: AC-2"""\n    assert 1 == 1\n',
            encoding="utf-8",
        )
        Path(".themis/blind_tdd/triage").mkdir(parents=True, exist_ok=True)
        Path(f".themis/blind_tdd/triage/{task_id}.json").write_text(
            json.dumps({"task": task_id, "triage": [],
                        "criteria_tested": ["AC-1", "AC-2"]}),
            encoding="utf-8",
        )
        return {"success": True, "agent_id": "writer-1"}


class RunnerSpawner:
    """Fake spawner that produces a passing green report for AC-1 and AC-2.

    Also re-reads the test files during spawn to simulate the real
    runner's per_test extraction.
    """

    def __init__(self, *, tests_passed: int = 2, overall: str = "pass"):
        self.tests_passed = tests_passed
        self.overall = overall

    def spawn(self, *, role, prompt_template, inputs):
        if role != "test_runner":
            return {"success": False, "error": f"unexpected role {role}"}
        task_id = inputs["task_id"]
        Path(".themis/blind_tdd/green_report").mkdir(parents=True, exist_ok=True)
        report = {
            "task": task_id,
            "agent_id": "runner-1",
            "phase": "green",
            "runner_command": "pytest tests/contracts/",
            "runner_exit_code": 0,
            "tests_collected": 2,
            "tests_passed": self.tests_passed,
            "tests_failed": 2 - self.tests_passed,
            "tests_skipped": 0,
            "per_test": [
                {"name": "test_ac1", "file": "tests/contracts/test_ac1.py",
                 "result": "pass" if self.tests_passed >= 1 else "fail",
                 "covers": ["AC-1"]},
                {"name": "test_ac2", "file": "tests/contracts/test_ac2.py",
                 "result": "pass" if self.tests_passed >= 2 else "fail",
                 "covers": ["AC-2"]},
            ],
            "overall": self.overall,
        }
        Path(f".themis/blind_tdd/green_report/{task_id}.json").write_text(
            json.dumps(report), encoding="utf-8",
        )
        return {"success": True, "agent_id": "runner-1"}


class DualSpawner:
    """Dispatches to the writer or runner spawner based on role."""

    def __init__(self, writer, runner):
        self.writer = writer
        self.runner = runner

    def spawn(self, *, role, prompt_template, inputs):
        if role == "test_writer":
            return self.writer.spawn(
                role=role, prompt_template=prompt_template, inputs=inputs)
        if role == "test_runner":
            return self.runner.spawn(
                role=role, prompt_template=prompt_template, inputs=inputs)
        return {"success": False, "error": f"unknown role {role}"}


def _make_orch(ralph_home: Path, runner_spawner=None) -> BlindTddOrchestrator:
    base = ralph_home / "templates" / "blind_tdd" / "prompts"
    runner = runner_spawner or RunnerSpawner()
    return BlindTddOrchestrator(
        spawner=DualSpawner(WriterSpawner(), runner),
        test_dirs=["tests/contracts/"],
        writer_prompt_path=str(base / "test_writer.md"),
        runner_prompt_path=str(base / "test_runner.md"),
        arbiter_prompt_path=str(base / "arbiter.md"),
    )


# ---------------------------------------------------------------------------
# Baseline: real red + green should pass when nothing is tampered
# ---------------------------------------------------------------------------

def test_untampered_red_then_green_passes():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = tmp / "ralph_home"
        _write_prompts(ralph)
        project = tmp / "project"
        project.mkdir()
        with _chdir(project):
            orch = _make_orch(ralph)
            red = orch.run_red_phase(VALID_TASK)
            assert red.passed, f"red phase should pass: {red.reason}"
            assert len(red.test_file_hashes) == 2

            green = orch.run_green_phase(VALID_TASK, red)
            assert green.passed, f"green phase should pass: {green.reason}"
            assert green.hash_match


# ---------------------------------------------------------------------------
# Modification — the core anti-tamper property
# ---------------------------------------------------------------------------

def test_green_fails_when_test_file_modified():
    """CRITICAL: if a test file is edited between red and green, the
    hash check must fire and green phase must fail."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = tmp / "ralph_home"
        _write_prompts(ralph)
        project = tmp / "project"
        project.mkdir()
        with _chdir(project):
            orch = _make_orch(ralph)
            red = orch.run_red_phase(VALID_TASK)
            assert red.passed

            # SIMULATED TAMPERING: the implementing agent edits the test
            # to weaken the assertion so their implementation passes.
            Path("tests/contracts/test_ac1.py").write_text(
                'def test_ac1():\n    """Covers: AC-1"""\n    assert True  # weakened\n',
                encoding="utf-8",
            )

            green = orch.run_green_phase(VALID_TASK, red)
            assert not green.passed, "green should detect the modification"
            assert not green.hash_match
            assert "integrity compromised" in green.reason
            assert "test_ac1.py" in green.reason


def test_green_fails_when_test_file_deleted():
    """If a test is deleted between phases, hash set changes → fail."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = tmp / "ralph_home"
        _write_prompts(ralph)
        project = tmp / "project"
        project.mkdir()
        with _chdir(project):
            orch = _make_orch(ralph)
            red = orch.run_red_phase(VALID_TASK)
            assert red.passed

            Path("tests/contracts/test_ac2.py").unlink()

            green = orch.run_green_phase(VALID_TASK, red)
            assert not green.passed
            assert not green.hash_match


def test_green_fails_when_new_test_file_added():
    """The 'sneak in a trivially-passing test' attack.

    An implementing agent could try to add a new test file with only
    passing assertions, hoping to dilute the criterion coverage or
    paper over a failing test. The hash set comparison should catch
    this because a new path appears in current_hashes that isn't in
    red_result.test_file_hashes.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = tmp / "ralph_home"
        _write_prompts(ralph)
        project = tmp / "project"
        project.mkdir()
        with _chdir(project):
            orch = _make_orch(ralph)
            red = orch.run_red_phase(VALID_TASK)
            assert red.passed

            Path("tests/contracts/test_sneaky.py").write_text(
                'def test_sneaky():\n    """Covers: AC-1"""\n    assert True\n',
                encoding="utf-8",
            )

            green = orch.run_green_phase(VALID_TASK, red)
            assert not green.passed
            assert not green.hash_match


# ---------------------------------------------------------------------------
# Coverage verification in green phase
# ---------------------------------------------------------------------------

def test_green_fails_when_runner_reports_test_failure():
    """Runner reports a test failure → green fails even if hashes match."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = tmp / "ralph_home"
        _write_prompts(ralph)
        project = tmp / "project"
        project.mkdir()
        with _chdir(project):
            # Runner reports only AC-1 passing, AC-2 failing
            orch = _make_orch(ralph, runner_spawner=RunnerSpawner(
                tests_passed=1, overall="fail"))
            red = orch.run_red_phase(VALID_TASK)
            assert red.passed

            green = orch.run_green_phase(VALID_TASK, red)
            assert not green.passed
            # Either coverage gap (AC-2 has no passing test) OR overall fail
            assert ("coverage gap" in green.reason
                    or "overall=fail" in green.reason)


# ---------------------------------------------------------------------------
# Direct hashing helper
# ---------------------------------------------------------------------------

def test_hash_test_files_is_deterministic():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            Path("tests").mkdir()
            Path("tests/a.py").write_text("def test(): pass\n", encoding="utf-8")
            Path("tests/b.py").write_text("def test(): pass\n", encoding="utf-8")
            h1 = _hash_test_files([Path("tests")])
            h2 = _hash_test_files([Path("tests")])
            assert h1 == h2
            assert len(h1) == 2


def test_hash_test_files_detects_single_byte_change():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            Path("tests").mkdir()
            Path("tests/a.py").write_text("x = 1", encoding="utf-8")
            h1 = _hash_test_files([Path("tests")])
            Path("tests/a.py").write_text("x = 2", encoding="utf-8")
            h2 = _hash_test_files([Path("tests")])
            assert h1 != h2
            # Same set of files, different hash values
            assert set(h1.keys()) == set(h2.keys())


def test_hash_test_files_ignores_non_test_extensions():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            Path("tests").mkdir()
            Path("tests/a.py").write_text("x", encoding="utf-8")
            Path("tests/notes.txt").write_text("ignore me", encoding="utf-8")
            Path("tests/data.json").write_text("{}", encoding="utf-8")
            h = _hash_test_files([Path("tests")])
            assert len(h) == 1
            assert any("a.py" in k for k in h.keys())


def test_hash_test_files_handles_missing_directory():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            h = _hash_test_files([Path("nonexistent")])
            assert h == {}


def test_hash_uses_sha256():
    """Regression: hash length should be 64 hex chars (sha256)."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            Path("tests").mkdir()
            Path("tests/a.py").write_text("x", encoding="utf-8")
            h = _hash_test_files([Path("tests")])
            for digest in h.values():
                assert len(digest) == 64
                # sha256 of 'x' in utf-8
                expected = hashlib.sha256(b"x").hexdigest()
                assert digest == expected


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [v for k, v in globals().items()
             if callable(v) and k.startswith("test_")]
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
