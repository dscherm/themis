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


def test_hash_test_files_seals_gdscript_but_not_json_fixtures():
    """sb2d-lha: a Godot project's blind contract tests are .gd files, not
    .py — `record-writer-handshake ... --test-dir tests/contracts` on such a
    project must seal them instead of reporting "no test files found"."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            Path("tests/contracts").mkdir(parents=True)
            Path("tests/contracts/test_sb2d_awa_kart_physics_blind.gd").write_text(
                "extends GutTest\nfunc test_x():\n\tassert_true(true)\n",
                encoding="utf-8",
            )
            Path("tests/contracts/fixture.json").write_text("{}", encoding="utf-8")
            h = _hash_test_files([Path("tests/contracts")])
            assert len(h) == 1
            assert any(k.endswith(".gd") for k in h.keys())
            assert not any(k.endswith(".json") for k in h.keys())


def test_hash_test_files_still_seals_python_regression():
    """Regression guard: unifying the extension set behind
    SEALABLE_TEST_EXTENSIONS must not stop sealing plain .py test files."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            Path("tests").mkdir()
            Path("tests/test_a.py").write_text("def test_a(): pass\n", encoding="utf-8")
            h = _hash_test_files([Path("tests")])
            assert len(h) == 1
            assert any(k.endswith("test_a.py") for k in h.keys())


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
# Post-ruling re-seal — the BL-2 / BM-1 scenario through the gate
#
# red → implement → challenge upheld (file deleted) → writer rewrites the
# file → green. Before 2026-10-24 the last step reported "test file
# integrity compromised" for a rewrite the engine itself ordered. These run
# the real orchestrator (fake spawners) through gate_integration end-to-end.
# ---------------------------------------------------------------------------

from blind_tdd import challenge as ch  # noqa: E402
from blind_tdd import gate_integration as gi  # noqa: E402

GATE_TASK = {**VALID_TASK, "title": "hash task", "passes": False}

AC1_ORIGINAL = 'def test_ac1():\n    """Covers: AC-1"""\n    assert 1 == 1\n'
AC1_REWRITTEN = (
    'def test_ac1():\n    """Covers: AC-1"""\n'
    '    # rewritten under the arbiter\'s ruling: compare against the bead id\n'
    '    assert "bid" == "bid"\n'
)


def _write_plan(dir_: Path, tasks: list[dict]) -> None:
    (dir_ / "plan.md").write_text(
        "# Plan\n\n```json\n" + json.dumps({"tasks": tasks}, indent=2) + "\n```\n",
        encoding="utf-8",
    )


def _gate_config(enforcement: str = "strict", spawner: str = "manual") -> dict:
    return {"gate": {"blind_tdd": {
        "enabled": True,
        "enforcement": enforcement,
        "spawner": spawner,
        "preflight": "warn",
        "test_dirs": ["tests/contracts/"],
    }}}


def _run_gate(config: dict, task_id: str = "task-hash"):
    os.environ["RALPH_BLIND_TDD_TASK"] = task_id
    try:
        return gi.run_blind_tdd_gate(config)
    finally:
        os.environ.pop("RALPH_BLIND_TDD_TASK", None)


class _ReturningFactory:
    """Stand-in for gi._make_orchestrator that hands back one real orchestrator."""

    def __init__(self, orch):
        self.orch = orch

    def __call__(self, config, btd_cfg):
        return self.orch


def _scenario(tmp: Path, *, orch=None):
    """Set up a project with a real red phase sealed, return (project, orch)."""
    ralph = tmp / "ralph_home"
    _write_prompts(ralph)
    project = tmp / "project"
    project.mkdir()
    _write_plan(project, [GATE_TASK])
    return project, (orch or _make_orch(ralph))


def _challenge_ac1_upheld() -> "ch.Challenge":
    chal = ch.file_challenge(
        task_id="task-hash", test_file="tests/contracts/test_ac1.py",
        test_name="test_ac1", criterion="AC-1",
        argument="the assertion contradicts the fixture it depends on",
    )
    ruling = ch.Ruling(
        challenge_id=chal.challenge_id, task_id="task-hash", ruling="upheld",
        reasoning="the fixture itself puts the string in plan.md", criterion_affected="AC-1",
        resolution="rewrite test_ac1 narrowly; keep every other assertion",
    )
    # The ruling document the arbiter would have left on disk.
    rulings = Path(".themis/blind_tdd/rulings")
    rulings.mkdir(parents=True, exist_ok=True)
    (rulings / f"{chal.challenge_id}.json").write_text(
        json.dumps(ruling.to_dict()), encoding="utf-8",
    )
    res = ch.apply_ruling(challenge=chal, ruling=ruling)
    assert res.action == "test_deleted" and res.reseal_recorded, res.reseal_note
    return chal


def test_upheld_ruling_rewrite_is_resealed_and_cited():
    """Done-criterion 1: red, implement, challenge upheld, rewrite → integrity
    OK and the gate result cites the ruling."""
    with tempfile.TemporaryDirectory() as td:
        project, orch = _scenario(Path(td))
        original = gi._make_orchestrator
        gi._make_orchestrator = _ReturningFactory(orch)
        try:
            with _chdir(project):
                cfg = _gate_config("strict")
                r1 = _run_gate(cfg)
                assert r1.passed and r1.phase == "red", r1.message
                # Bytes on disk, not the string: write_text translates newlines.
                sealed_ac1 = hashlib.sha256(Path("tests/contracts/test_ac1.py").read_bytes()).hexdigest()
                assert sealed_ac1 in gi.load_red_state("task-hash")["test_file_hashes"].values()

                # "implement"
                Path("src").mkdir()
                Path("src/foo.py").write_text("def foo():\n    return True\n", encoding="utf-8")

                chal = _challenge_ac1_upheld()
                assert not Path("tests/contracts/test_ac1.py").exists()

                # Gate before the rewrite exists: not green, not tampering.
                r2 = _run_gate(cfg)
                assert r2.reason == "ruling_rewrite_pending", r2.message
                assert not r2.passed  # strict blocks until the rewrite lands
                assert chal.challenge_id in r2.message
                assert "not yet rewritten" in r2.message
                assert not Path(".themis/blind_tdd/tamper_ledger.jsonl").exists()

                # The writer rewrites the file per the ruling.
                Path("tests/contracts/test_ac1.py").write_text(AC1_REWRITTEN, encoding="utf-8")

                r3 = _run_gate(cfg)
                assert r3.passed and r3.phase == "green", r3.message
                assert chal.challenge_id in r3.message
                applied = r3.details["rulings_applied"]
                assert len(applied) == 1
                assert applied[0]["challenge_id"] == chal.challenge_id
                assert applied[0]["test_file"] == "tests/contracts/test_ac1.py"
                assert applied[0]["criterion_affected"] == "AC-1"
                assert applied[0]["sealed_hash"] == sealed_ac1
                assert applied[0]["resealed_hash"] == hashlib.sha256(
                    Path("tests/contracts/test_ac1.py").read_bytes()).hexdigest()
                assert not Path(".themis/blind_tdd/tamper_ledger.jsonl").exists()
                # Green pass clears the seal as always.
                assert gi.load_red_state("task-hash") is None
        finally:
            gi._make_orchestrator = original


def test_unrelated_edit_still_breaks_integrity_after_a_reseal():
    """Done-criterion 2: the ruling licenses ONE file. Any other sealed file
    that changed is still reported compromised, and the tamper ledger records it."""
    with tempfile.TemporaryDirectory() as td:
        project, orch = _scenario(Path(td))
        original = gi._make_orchestrator
        gi._make_orchestrator = _ReturningFactory(orch)
        try:
            with _chdir(project):
                cfg = _gate_config("strict")
                assert _run_gate(cfg).phase == "red"
                chal = _challenge_ac1_upheld()
                Path("tests/contracts/test_ac1.py").write_text(AC1_REWRITTEN, encoding="utf-8")
                # Unrelated tampering, no ruling behind it.
                Path("tests/contracts/test_ac2.py").write_text(
                    'def test_ac2():\n    """Covers: AC-2"""\n    assert True  # weakened\n',
                    encoding="utf-8",
                )

                r = _run_gate(cfg)
                assert not r.passed and r.reason == "green_failed", r.message
                assert r.details["hash_break"] is True
                assert "integrity compromised" in r.message
                assert "test_ac2.py" in r.message
                assert "test_ac1.py" not in r.message
                # The accepted rewrite is still cited, so a reader can tell the
                # two apart.
                assert chal.challenge_id in r.message
                assert r.details["rulings_applied"][0]["challenge_id"] == chal.challenge_id
                assert Path(".themis/blind_tdd/tamper_ledger.jsonl").exists()
                # Red state survives a failed green, with the re-seal in it.
                state = gi.load_red_state("task-hash")
                assert state["rulings_applied"][0]["challenge_id"] == chal.challenge_id
                assert "pending_rulings" not in state
        finally:
            gi._make_orchestrator = original


def test_restoring_the_deleted_test_unchanged_is_not_a_rewrite():
    """`git checkout` of the deleted file is not the ordered rewrite: byte-identical
    to the sealed version → still pending, with a message that says so."""
    with tempfile.TemporaryDirectory() as td:
        project, orch = _scenario(Path(td))
        original = gi._make_orchestrator
        gi._make_orchestrator = _ReturningFactory(orch)
        try:
            with _chdir(project):
                cfg = _gate_config("strict")
                assert _run_gate(cfg).phase == "red"
                chal = _challenge_ac1_upheld()
                Path("tests/contracts/test_ac1.py").write_text(AC1_ORIGINAL, encoding="utf-8")

                r = _run_gate(cfg)
                assert r.reason == "ruling_rewrite_pending", r.message
                assert "byte-identical" in r.message
                assert r.details["pending_rulings"][0]["status"] == "unchanged"
                assert r.details["pending_rulings"][0]["challenge_id"] == chal.challenge_id
                assert gi.load_red_state("task-hash")["pending_rulings"]
        finally:
            gi._make_orchestrator = original


def test_warn_mode_pending_rewrite_passes_but_still_names_the_ruling():
    with tempfile.TemporaryDirectory() as td:
        project, orch = _scenario(Path(td))
        original = gi._make_orchestrator
        gi._make_orchestrator = _ReturningFactory(orch)
        try:
            with _chdir(project):
                cfg = _gate_config("warn")
                assert _run_gate(cfg).phase == "red"
                chal = _challenge_ac1_upheld()
                r = _run_gate(cfg)
                assert r.passed  # warn never blocks
                assert r.reason == "ruling_rewrite_pending"
                assert chal.challenge_id in r.message
        finally:
            gi._make_orchestrator = original


class _ResumingOrchestrator:
    """Wraps the real orchestrator; stands in for a claude_code spawner whose
    resumed blind writer rewrites the ordered file."""

    def __init__(self, inner, content: str):
        self._inner = inner
        self._content = content
        self.resume_calls: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def resume_red_phase_after_ruling(self, task, previous_red, ruling):
        self.resume_calls.append(ruling)
        Path("tests/contracts/test_ac1.py").write_text(self._content, encoding="utf-8")
        merged = dict(previous_red.triage_report)
        merged["resumed_for"] = ruling.get("criterion_affected")
        return RedPhaseResult(
            passed=True, reason="red phase resumed after upheld ruling",
            test_file_hashes=_hash_test_files([Path("tests/contracts/")]),
            triage_report=merged,
        )


def test_claude_code_spawner_resumes_the_writer_for_a_missing_rewrite():
    """With an automated spawner the gate does not wait for a human: it resumes
    the red phase under the ruling, then re-seals only the ordered file."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ralph = tmp / "ralph_home"
        _write_prompts(ralph)
        project = tmp / "project"
        project.mkdir()
        _write_plan(project, [GATE_TASK])
        orch = _ResumingOrchestrator(_make_orch(ralph), AC1_REWRITTEN)
        original = gi._make_orchestrator
        gi._make_orchestrator = _ReturningFactory(orch)
        try:
            with _chdir(project):
                cfg = _gate_config("strict", spawner="claude_code")
                assert _run_gate(cfg).phase == "red"
                chal = _challenge_ac1_upheld()
                # An unrelated file also drifts — the resume's full-tree hashes
                # must NOT launder it.
                Path("tests/contracts/test_ac2.py").write_text(
                    'def test_ac2():\n    """Covers: AC-2"""\n    assert True  # drift\n',
                    encoding="utf-8",
                )

                r = _run_gate(cfg)
                assert len(orch.resume_calls) == 1
                assert orch.resume_calls[0]["challenge_id"] == chal.challenge_id
                assert not r.passed and r.reason == "green_failed", r.message
                assert "test_ac2.py" in r.message and "test_ac1.py" not in r.message
                state = gi.load_red_state("task-hash")
                assert state["rulings_applied"][0]["challenge_id"] == chal.challenge_id
                assert state["triage_report"]["resumed_for"] == "AC-1"

                # Put the unrelated file back; now the task goes green.
                Path("tests/contracts/test_ac2.py").write_text(
                    'def test_ac2():\n    """Covers: AC-2"""\n    assert 1 == 1\n',
                    encoding="utf-8",
                )
                r2 = _run_gate(cfg)
                assert r2.passed and r2.phase == "green", r2.message
                assert chal.challenge_id in r2.message
        finally:
            gi._make_orchestrator = original


def test_reseal_refuses_to_unsign_a_signed_record():
    """Direct check of the fail-closed branch: a signed record whose key is gone
    cannot be re-sealed; the rewrite stays reported, nothing is written."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            os.environ[gi.SEAL_KEY_ENV] = "unit-test-key"
            try:
                Path("tests/contracts").mkdir(parents=True)
                Path("tests/contracts/test_ac1.py").write_text(AC1_ORIGINAL, encoding="utf-8")
                gi.save_red_state("task-hash", RedPhaseResult(
                    passed=True, test_file_hashes=_hash_test_files([Path("tests/contracts/")]),
                    triage_report={},
                ))
                ok, note = gi.record_pending_ruling(
                    "task-hash", challenge_id="chal-x", test_file="tests/contracts/test_ac1.py",
                    criterion_affected="AC-1",
                )
                assert ok, note
            finally:
                os.environ.pop(gi.SEAL_KEY_ENV, None)
            Path("tests/contracts/test_ac1.py").write_text(AC1_REWRITTEN, encoding="utf-8")
            before = gi.load_red_state("task-hash")
            state, applied, pending = gi.reseal_after_rulings("task-hash", before)
            assert applied == []
            assert pending[0]["status"] == "unsigned_record_refused"
            assert gi.load_red_state("task-hash") == before

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
