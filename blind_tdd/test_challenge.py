"""Self-contained tests for blind_tdd.challenge.

Exercises filing, cap enforcement, arbiter spawning (via stub spawner),
ruling application, and the end-to-end `process_challenge` pipeline.

Run with:
    python -m blind_tdd.test_challenge
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

from blind_tdd import challenge as ch  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chdir(path: Path):
    class _Ctx:
        def __enter__(self_):
            self_.prev = os.getcwd()
            os.chdir(path)
            return path

        def __exit__(self_, *a):
            os.chdir(self_.prev)
    return _Ctx()


class StubSpawner:
    """Fake spawner that pretends to invoke the arbiter.

    Writes a ruling document at the expected path and returns
    success=True. Takes a `ruling` dict to control the verdict.
    """

    def __init__(self, ruling: dict | None, *, fail: bool = False):
        self.ruling = ruling
        self.fail = fail
        self.spawned_inputs: dict | None = None

    def spawn(self, *, role: str, prompt_template: str, inputs: dict) -> dict:
        self.spawned_inputs = inputs
        if self.fail:
            return {"success": False, "error": "stub failure"}
        if self.ruling is None:
            return {"success": True}  # success but no output file
        challenge_id = inputs["challenge_id"]
        out_dir = Path(".themis") / "blind_tdd" / "rulings"
        out_dir.mkdir(parents=True, exist_ok=True)
        ruling_doc = dict(self.ruling)
        ruling_doc.setdefault("challenge_id", challenge_id)
        ruling_doc.setdefault("task", inputs.get("task", {}).get("id", ""))
        (out_dir / f"{challenge_id}.json").write_text(
            json.dumps(ruling_doc, indent=2), encoding="utf-8"
        )
        return {"success": True, "agent_id": "stub-arbiter"}


def _write_test_file(path: Path, content: str = "def test_foo(): assert 1 == 1\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


VALID_TASK = {
    "id": "task-c1",
    "acceptance_criteria": [
        {"id": "AC-1", "given": "x", "when": "y", "then": "z"},
    ],
    "public_surface": {"module": "src.foo", "adds": ["foo()"]},
}

# TD26: spawn_arbiter/process_challenge now require the caller to hand in
# already-derived roots (blind_tdd.roots.derive_blocked_paths(...).to_record())
# rather than running the arbiter unsealed. Tests below use a fixed stand-in
# record — the derivation itself is blind_tdd.roots's job and is covered by
# test_roots.py.
SEALED_ROOTS = {
    "blocked_paths": ["src/**", "examples/**", ".git/**", "data/generated/**"],
    "roots": ["src", "examples"],
    "languages": ["node"],
    "method": "js-convention",
}


# ---------------------------------------------------------------------------
# file_challenge
# ---------------------------------------------------------------------------

def test_file_challenge_creates_doc_and_log_entry():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            chal = ch.file_challenge(
                task_id="task-c1",
                test_file="tests/contracts/test_foo.py",
                test_name="test_foo_bar",
                criterion="AC-1",
                argument="the test uses flaky timing",
                proposed_fix="use mocked clock",
                challenger="impl-agent-x",
            )
            assert chal.challenge_id.startswith("chal-task-c1-")
            assert chal.criterion == "AC-1"

            doc = json.loads(
                (Path(".themis") / "blind_tdd" / "challenges" / f"{chal.challenge_id}.json")
                .read_text(encoding="utf-8")
            )
            assert doc["challenge_id"] == chal.challenge_id
            assert doc["task"] == "task-c1"
            assert doc["argument"] == "the test uses flaky timing"

            log_entries = ch._iter_log()
            assert len(log_entries) == 1
            assert log_entries[0]["challenge_id"] == chal.challenge_id


def test_file_challenge_rejects_empty_argument():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            try:
                ch.file_challenge(
                    task_id="t", test_file="tests/a.py", test_name="t1",
                    criterion="AC-1", argument="   ",
                )
            except ValueError:
                return
            assert False, "should have raised ValueError for empty argument"


def test_file_challenge_normalizes_criterion_case():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            chal = ch.file_challenge(
                task_id="t", test_file="tests/a.py", test_name="t1",
                criterion="ac-3", argument="wrong",
            )
            assert chal.criterion == "AC-3"


# ---------------------------------------------------------------------------
# Cap enforcement
# ---------------------------------------------------------------------------

def test_check_caps_allows_first_challenge():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            allowed, reason = ch.check_challenge_caps(task_id="t", criterion="AC-1")
            assert allowed
            assert reason == ""


def test_per_criterion_cap_enforced():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            ch.file_challenge(task_id="t", test_file="a.py", test_name="n",
                              criterion="AC-1", argument="first")
            allowed, reason = ch.check_challenge_caps(
                task_id="t", criterion="AC-1",
                max_per_criterion=1,
            )
            assert not allowed
            assert "per-criterion cap" in reason


def test_per_task_cap_enforced():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            for i, crit in enumerate(["AC-1", "AC-2", "AC-3"]):
                ch.file_challenge(
                    task_id="t", test_file="a.py", test_name=f"n{i}",
                    criterion=crit, argument=f"arg{i}",
                )
            allowed, reason = ch.check_challenge_caps(
                task_id="t", criterion="AC-4",
                max_per_task=3, max_per_criterion=1,
            )
            assert not allowed
            assert "per-task cap" in reason


def test_count_challenges_filters_by_task_and_criterion():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            ch.file_challenge(task_id="t1", test_file="a.py", test_name="a",
                              criterion="AC-1", argument="x")
            ch.file_challenge(task_id="t1", test_file="b.py", test_name="b",
                              criterion="AC-2", argument="x")
            ch.file_challenge(task_id="t2", test_file="c.py", test_name="c",
                              criterion="AC-1", argument="x")
            assert ch.count_challenges(task_id="t1") == 2
            assert ch.count_challenges(task_id="t1", criterion="AC-1") == 1
            assert ch.count_challenges(task_id="t2") == 1
            assert ch.count_challenges(task_id="t3") == 0


# ---------------------------------------------------------------------------
# spawn_arbiter
# ---------------------------------------------------------------------------

def test_spawn_arbiter_parses_ruling():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            chal = ch.file_challenge(
                task_id="t", test_file="tests/a.py", test_name="n",
                criterion="AC-1", argument="flaky",
            )
            spawner = StubSpawner(ruling={
                "ruling": "upheld",
                "reasoning": "the test uses time.sleep which flakes",
                "criterion_affected": "AC-1",
                "resolution": "rewrite_with_mocked_clock",
                "arbiter_id": "arb-xyz",
            })
            ruling = ch.spawn_arbiter(
                challenge=chal,
                spawner=spawner,
                arbiter_prompt="<arbiter prompt>",
                task_spec=VALID_TASK,
                sealed_roots=SEALED_ROOTS,
            )
            assert ruling is not None
            assert ruling.ruling == "upheld"
            assert ruling.criterion_affected == "AC-1"
            assert ruling.arbiter_id == "arb-xyz"
            assert spawner.spawned_inputs["challenge_id"] == chal.challenge_id


def test_spawn_arbiter_returns_none_when_spawn_fails():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            chal = ch.file_challenge(
                task_id="t", test_file="a.py", test_name="n",
                criterion="AC-1", argument="x",
            )
            spawner = StubSpawner(ruling=None, fail=True)
            ruling = ch.spawn_arbiter(
                challenge=chal, spawner=spawner,
                arbiter_prompt="", task_spec=VALID_TASK,
                sealed_roots=SEALED_ROOTS,
            )
            assert ruling is None


def test_spawn_arbiter_returns_none_when_no_output_file():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            chal = ch.file_challenge(
                task_id="t", test_file="a.py", test_name="n",
                criterion="AC-1", argument="x",
            )
            spawner = StubSpawner(ruling=None)  # success but no file
            ruling = ch.spawn_arbiter(
                challenge=chal, spawner=spawner,
                arbiter_prompt="", task_spec=VALID_TASK,
                sealed_roots=SEALED_ROOTS,
            )
            assert ruling is None


def test_spawn_arbiter_rejects_unknown_verdict():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            chal = ch.file_challenge(
                task_id="t", test_file="a.py", test_name="n",
                criterion="AC-1", argument="x",
            )
            spawner = StubSpawner(ruling={
                "ruling": "maybe",  # not a valid verdict
                "reasoning": "idk",
                "criterion_affected": "AC-1",
            })
            ruling = ch.spawn_arbiter(
                challenge=chal, spawner=spawner,
                arbiter_prompt="", task_spec=VALID_TASK,
                sealed_roots=SEALED_ROOTS,
            )
            assert ruling is None


# ---------------------------------------------------------------------------
# TD26 — the arbiter is now blind: spawn_arbiter opens a real blind_session
# using the caller's derived roots (TD21), instead of spawning with no
# session at all (the bug arbiter.md's own briefing never disclosed).
# ---------------------------------------------------------------------------

class _SessionCapturingSpawner:
    """Records blind_tdd.session.current_session() at spawn time, then
    behaves like StubSpawner (writes the ruling doc, returns success)."""

    def __init__(self, ruling: dict):
        self.ruling = ruling
        self.captured_session: dict | None = None

    def spawn(self, *, role: str, prompt_template: str, inputs: dict) -> dict:
        from blind_tdd.session import current_session
        self.captured_session = current_session()

        challenge_id = inputs["challenge_id"]
        out_dir = Path(".themis") / "blind_tdd" / "rulings"
        out_dir.mkdir(parents=True, exist_ok=True)
        ruling_doc = dict(self.ruling)
        ruling_doc.setdefault("challenge_id", challenge_id)
        (out_dir / f"{challenge_id}.json").write_text(
            json.dumps(ruling_doc, indent=2), encoding="utf-8"
        )
        return {"success": True, "agent_id": "stub-arbiter"}


def test_spawn_arbiter_activates_a_real_blind_session_with_derived_roots():
    """AC-2: the arbiter spawns under a session using TD21's derived roots —
    not the previous no-session-at-all behavior."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            chal = ch.file_challenge(
                task_id="t", test_file="tests/a.py", test_name="n",
                criterion="AC-1", argument="flaky",
            )
            spawner = _SessionCapturingSpawner(
                {"ruling": "rejected", "reasoning": "fine", "criterion_affected": "AC-1"}
            )
            ruling = ch.spawn_arbiter(
                challenge=chal, spawner=spawner,
                arbiter_prompt="", task_spec=VALID_TASK,
                sealed_roots=SEALED_ROOTS,
            )
            assert ruling is not None
            # A session WAS active while spawner.spawn() ran.
            assert spawner.captured_session is not None
            assert spawner.captured_session["agent_role"] == "arbiter"
            assert spawner.captured_session["blocked_paths"] == SEALED_ROOTS["blocked_paths"]

            # And it is cleaned up afterwards, same as every other phase.
            from blind_tdd.session import current_session
            assert current_session() is None


def test_spawn_arbiter_session_actually_refuses_an_implementation_read():
    """AC-1/AC-2 end-to-end: while the arbiter's session is active, the real
    path-guard hook refuses a read of a file under the derived roots — the
    same claim arbiter.md makes in its own briefing, now actually true."""
    import importlib.util

    guard_path = _REPO / "templates" / "hooks" / "blind_tdd_path_guard.py"
    spec = importlib.util.spec_from_file_location("_td26_guard", guard_path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    checked: dict = {}

    class _CheckingSpawner:
        def spawn(self, *, role, prompt_template, inputs):
            from blind_tdd.session import current_session
            session = current_session()
            allowed, reason = guard._check_path("src/widget.js", session)
            checked["allowed"] = allowed
            checked["reason"] = reason

            challenge_id = inputs["challenge_id"]
            out_dir = Path(".themis") / "blind_tdd" / "rulings"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{challenge_id}.json").write_text(
                json.dumps({"ruling": "rejected", "reasoning": "x",
                            "criterion_affected": "AC-1"}),
                encoding="utf-8",
            )
            return {"success": True}

    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            chal = ch.file_challenge(
                task_id="t", test_file="tests/a.py", test_name="n",
                criterion="AC-1", argument="x",
            )
            ruling = ch.spawn_arbiter(
                challenge=chal, spawner=_CheckingSpawner(),
                arbiter_prompt="", task_spec=VALID_TASK,
                sealed_roots=SEALED_ROOTS,
            )
            assert ruling is not None
            assert checked["allowed"] is False, checked.get("reason")
            assert "blocked pattern" in checked["reason"]


def test_spawn_arbiter_requires_nonempty_sealed_roots():
    """No silent unsealed fallback: an empty/missing blocked_paths raises
    rather than spawning the arbiter with nothing enforced."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            chal = ch.file_challenge(
                task_id="t", test_file="a.py", test_name="n",
                criterion="AC-1", argument="x",
            )
            spawner = StubSpawner(ruling={"ruling": "rejected", "reasoning": "x",
                                           "criterion_affected": "AC-1"})
            try:
                ch.spawn_arbiter(
                    challenge=chal, spawner=spawner,
                    arbiter_prompt="", task_spec=VALID_TASK,
                    sealed_roots={"blocked_paths": []},
                )
            except ValueError:
                pass
            else:
                assert False, "expected ValueError for empty sealed_roots"
            # Nothing was spawned — the fail-loud check happens before spawn.
            assert spawner.spawned_inputs is None


def test_spawn_arbiter_records_sealed_roots_on_the_ruling():
    """AC-4: the produced Ruling records which roots were sealed."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            chal = ch.file_challenge(
                task_id="t", test_file="a.py", test_name="n",
                criterion="AC-1", argument="x",
            )
            spawner = StubSpawner(ruling={"ruling": "rejected", "reasoning": "x",
                                           "criterion_affected": "AC-1"})
            ruling = ch.spawn_arbiter(
                challenge=chal, spawner=spawner,
                arbiter_prompt="", task_spec=VALID_TASK,
                sealed_roots=SEALED_ROOTS,
            )
            assert ruling.sealed_roots == SEALED_ROOTS
            assert ruling.to_dict()["sealed_roots"] == SEALED_ROOTS


def test_spawn_arbiter_returns_none_if_a_session_is_already_active():
    """If a writer/runner session is already active (should not normally
    overlap with an arbiter spawn), spawn_arbiter fails rather than running
    the arbiter unsealed alongside it."""
    from blind_tdd.session import activate, deactivate
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            chal = ch.file_challenge(
                task_id="t", test_file="a.py", test_name="n",
                criterion="AC-1", argument="x",
            )
            activate(agent_role="test_writer", task_id="other-task")
            try:
                spawner = StubSpawner(ruling={"ruling": "rejected", "reasoning": "x",
                                               "criterion_affected": "AC-1"})
                ruling = ch.spawn_arbiter(
                    challenge=chal, spawner=spawner,
                    arbiter_prompt="", task_spec=VALID_TASK,
                    sealed_roots=SEALED_ROOTS,
                )
                assert ruling is None
                assert spawner.spawned_inputs is None
            finally:
                deactivate()


# ---------------------------------------------------------------------------
# apply_ruling
# ---------------------------------------------------------------------------

def test_apply_ruling_upheld_deletes_test_file():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            test_path = Path("tests/contracts/test_foo.py")
            _write_test_file(test_path)
            chal = ch.file_challenge(
                task_id="t", test_file=str(test_path), test_name="test_foo",
                criterion="AC-1", argument="x",
            )
            ruling = ch.Ruling(
                challenge_id=chal.challenge_id, task_id="t",
                ruling="upheld", reasoning="flaky",
                criterion_affected="AC-1", resolution="rewrite",
            )
            result = ch.apply_ruling(challenge=chal, ruling=ruling)
            assert result.success
            assert result.action == "test_deleted"
            assert not test_path.exists()


def test_apply_ruling_rejected_keeps_test_file():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            test_path = Path("tests/contracts/test_foo.py")
            _write_test_file(test_path)
            chal = ch.file_challenge(
                task_id="t", test_file=str(test_path), test_name="test_foo",
                criterion="AC-1", argument="x",
            )
            ruling = ch.Ruling(
                challenge_id=chal.challenge_id, task_id="t",
                ruling="rejected", reasoning="test is correct",
                criterion_affected="AC-1",
            )
            result = ch.apply_ruling(challenge=chal, ruling=ruling)
            assert result.success
            assert result.action == "rejected"
            assert test_path.exists()


def test_apply_ruling_ambiguous_writes_human_request():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            test_path = Path("tests/contracts/test_foo.py")
            _write_test_file(test_path)
            chal = ch.file_challenge(
                task_id="t", test_file=str(test_path), test_name="test_foo",
                criterion="AC-1", argument="x",
            )
            ruling = ch.Ruling(
                challenge_id=chal.challenge_id, task_id="t",
                ruling="ambiguous", reasoning="the spec is unclear on timing",
                criterion_affected="AC-1",
            )
            result = ch.apply_ruling(challenge=chal, ruling=ruling)
            assert result.success
            assert result.action == "escalated_to_human"
            assert test_path.exists()  # ambiguous does NOT delete
            assert result.human_request_id is not None
            req_path = Path(".themis/human_requests") / f"{result.human_request_id}.json"
            assert req_path.exists()
            req = json.loads(req_path.read_text(encoding="utf-8"))
            assert req["category"] == "blind_tdd_ambiguous_ruling"
            assert req["blocking"] is True
            values = [o["value"] for o in req["options"]]
            assert "uphold" in values and "reject" in values


# ---------------------------------------------------------------------------
# process_challenge end-to-end
# ---------------------------------------------------------------------------

def test_process_challenge_upheld_happy_path():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            test_path = Path("tests/contracts/test_timing.py")
            _write_test_file(test_path, "def test_timing(): import time; time.sleep(3.0)\n")

            spawner = StubSpawner(ruling={
                "ruling": "upheld",
                "reasoning": "wall-clock timing flakes on Windows",
                "criterion_affected": "AC-1",
                "resolution": "rewrite_with_mocked_clock",
                "arbiter_id": "stub-1",
            })
            result = ch.process_challenge(
                task_id="t1",
                test_file=str(test_path),
                test_name="test_timing",
                criterion="AC-1",
                argument="time.sleep(3) is flaky on Windows",
                proposed_fix="use freezegun",
                task_spec=VALID_TASK,
                spawner=spawner,
                arbiter_prompt="",
                sealed_roots=SEALED_ROOTS,
            )
            assert result.success
            assert result.action == "test_deleted"
            assert not test_path.exists()
            assert result.ruling is not None
            assert result.ruling.ruling == "upheld"


def test_process_challenge_cap_exceeded_short_circuits():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            # Fill per-criterion cap
            ch.file_challenge(
                task_id="t1", test_file="a.py", test_name="a",
                criterion="AC-1", argument="first",
            )
            spawner = StubSpawner(ruling={"ruling": "upheld", "reasoning": "x",
                                           "criterion_affected": "AC-1"})
            result = ch.process_challenge(
                task_id="t1", test_file="a.py", test_name="a",
                criterion="AC-1", argument="second attempt",
                task_spec=VALID_TASK,
                spawner=spawner, arbiter_prompt="",
                max_per_criterion=1,
                sealed_roots=SEALED_ROOTS,
            )
            assert not result.success
            assert result.action == "cap_exceeded"
            assert "per-criterion" in result.cap_reason
            # Spawner should NOT have been invoked
            assert spawner.spawned_inputs is None


def test_process_challenge_records_observations():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            test_path = Path("tests/contracts/test_x.py")
            _write_test_file(test_path)
            spawner = StubSpawner(ruling={
                "ruling": "rejected", "reasoning": "test is fine",
                "criterion_affected": "AC-1",
            })
            ch.process_challenge(
                task_id="t1", test_file=str(test_path), test_name="test_x",
                criterion="AC-1", argument="I don't like it",
                task_spec=VALID_TASK, spawner=spawner, arbiter_prompt="",
                sealed_roots=SEALED_ROOTS,
            )
            obs_path = Path(".themis/observations.jsonl")
            assert obs_path.exists()
            types = [
                json.loads(line)["type"]
                for line in obs_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert "challenge_filed" in types
            assert "arbiter_ruling" in types
            assert "challenge_resolved" in types


def test_process_challenge_arbiter_failure_reports_cleanly():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            spawner = StubSpawner(ruling=None, fail=True)
            result = ch.process_challenge(
                task_id="t1", test_file="a.py", test_name="a",
                criterion="AC-1", argument="x",
                task_spec=VALID_TASK, spawner=spawner, arbiter_prompt="",
                sealed_roots=SEALED_ROOTS,
            )
            assert not result.success
            assert result.action == "arbiter_failed"


def test_process_challenge_ambiguous_escalates():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            test_path = Path("tests/contracts/test_x.py")
            _write_test_file(test_path)
            spawner = StubSpawner(ruling={
                "ruling": "ambiguous",
                "reasoning": "the spec's timing requirement is unclear",
                "criterion_affected": "AC-1",
            })
            result = ch.process_challenge(
                task_id="t1", test_file=str(test_path), test_name="test_x",
                criterion="AC-1", argument="unclear",
                task_spec=VALID_TASK, spawner=spawner, arbiter_prompt="",
                sealed_roots=SEALED_ROOTS,
            )
            assert result.success
            assert result.action == "escalated_to_human"
            assert result.human_request_id is not None
            assert test_path.exists()


def test_process_challenge_carries_sealed_roots_at_top_level():
    """AC-4: a caller inspecting only the ChallengeResult (not the nested
    Ruling) can still tell the challenge was arbitrated under a seal."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            test_path = Path("tests/contracts/test_x.py")
            _write_test_file(test_path)
            spawner = StubSpawner(ruling={
                "ruling": "rejected", "reasoning": "fine",
                "criterion_affected": "AC-1",
            })
            result = ch.process_challenge(
                task_id="t1", test_file=str(test_path), test_name="test_x",
                criterion="AC-1", argument="x",
                task_spec=VALID_TASK, spawner=spawner, arbiter_prompt="",
                sealed_roots=SEALED_ROOTS,
            )
            assert result.sealed_roots == SEALED_ROOTS
            assert result.to_dict()["sealed_roots"] == SEALED_ROOTS


# ---------------------------------------------------------------------------
# apply_ruling → red state (post-ruling re-seal, see gate_integration docstring)
# ---------------------------------------------------------------------------

def _seal_red_state(task_id: str, test_file: Path, content: str) -> str:
    """Write `test_file`, seal a red state over it, return the sealed hash."""
    import hashlib
    from blind_tdd import gate_integration as gi
    from blind_tdd.orchestrator import RedPhaseResult, _hash_test_files

    _write_test_file(test_file, content)
    hashes = _hash_test_files([test_file.parent])
    gi.save_red_state(task_id, RedPhaseResult(
        passed=True, test_file_hashes=hashes, triage_report={"triage": []},
    ))
    # Hash the bytes on disk, not the string: write_text translates newlines.
    return hashlib.sha256(test_file.read_bytes()).hexdigest()


def _upheld(chal: "ch.Challenge", resolution: str = "rewrite narrowly") -> "ch.Ruling":
    return ch.Ruling(
        challenge_id=chal.challenge_id, task_id=chal.task_id,
        ruling="upheld", reasoning="assertion contradicts its own fixture",
        criterion_affected="AC-1", resolution=resolution,
    )


def test_apply_ruling_upheld_records_pending_ruling_in_red_state():
    """BL-2 / BM-1: the deleted file's sealed hash must stay recoverable, keyed
    by the ruling, so the next gate run can re-seal the ordered rewrite."""
    from blind_tdd import gate_integration as gi

    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            test_path = Path("tests/contracts/test_foo.py")
            sealed = _seal_red_state("t", test_path, "def test_foo(): assert 1 == 2\n")
            # The challenge names the file with forward slashes; the seal key
            # is str(Path) (backslashes on Windows). Both must resolve to one.
            chal = ch.file_challenge(
                task_id="t", test_file="tests/contracts/test_foo.py",
                test_name="test_foo", criterion="AC-1", argument="x",
            )
            result = ch.apply_ruling(challenge=chal, ruling=_upheld(chal, "fix line 2"))
            assert result.success and result.action == "test_deleted"
            assert result.reseal_recorded, result.reseal_note
            assert chal.challenge_id in result.message

            state = gi.load_red_state("t")
            pending = state["pending_rulings"]
            assert len(pending) == 1
            entry = pending[0]
            assert entry["challenge_id"] == chal.challenge_id
            assert entry["test_file"] == "tests/contracts/test_foo.py"
            assert entry["criterion_affected"] == "AC-1"
            assert entry["resolution"] == "fix line 2"
            assert entry["sealed_hash"] == sealed
            # The seal itself is untouched: the OLD hash still stands until a
            # rewrite exists for the gate to re-hash.
            assert list(state["test_file_hashes"].values()) == [sealed]


def test_apply_ruling_upheld_without_red_state_reports_nothing_to_reseal():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            test_path = Path("tests/contracts/test_foo.py")
            _write_test_file(test_path)
            chal = ch.file_challenge(
                task_id="t", test_file=str(test_path), test_name="test_foo",
                criterion="AC-1", argument="x",
            )
            result = ch.apply_ruling(challenge=chal, ruling=_upheld(chal))
            assert result.success and result.action == "test_deleted"
            assert not test_path.exists()
            assert result.reseal_recorded is False
            assert "no red state" in result.reseal_note


def test_apply_ruling_upheld_resigns_a_signed_red_state():
    """With THEMIS_SEAL_KEY present the record is re-signed over the new
    pending entry, so green's seal verification still passes."""
    from blind_tdd import gate_integration as gi

    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            os.environ[gi.SEAL_KEY_ENV] = "unit-test-key"
            try:
                test_path = Path("tests/contracts/test_foo.py")
                _seal_red_state("t", test_path, "def test_foo(): assert 1 == 2\n")
                before = gi.load_red_state("t")["seal_hmac"]
                chal = ch.file_challenge(
                    task_id="t", test_file=str(test_path), test_name="test_foo",
                    criterion="AC-1", argument="x",
                )
                result = ch.apply_ruling(challenge=chal, ruling=_upheld(chal))
                assert result.reseal_recorded, result.reseal_note
                state = gi.load_red_state("t")
                assert state["seal_hmac"] != before
                ok, reason = gi.verify_red_state_seal(state)
                assert ok, reason
                # And the pending entry is under the HMAC: forging one fails.
                state["pending_rulings"].append({"challenge_id": "chal-forged"})
                ok, _ = gi.verify_red_state_seal(state)
                assert not ok
            finally:
                os.environ.pop(gi.SEAL_KEY_ENV, None)


def test_apply_ruling_upheld_refuses_to_unsign_a_signed_red_state():
    """A key-less process (an implementer with Bash) must not be able to turn
    a signed record into an unsigned one by routing a forged ruling through
    apply_ruling: the test is still deleted, but the seal is left alone and
    the refusal is reported."""
    from blind_tdd import gate_integration as gi

    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            test_path = Path("tests/contracts/test_foo.py")
            os.environ[gi.SEAL_KEY_ENV] = "unit-test-key"
            try:
                _seal_red_state("t", test_path, "def test_foo(): assert 1 == 2\n")
            finally:
                os.environ.pop(gi.SEAL_KEY_ENV, None)
            before = gi.load_red_state("t")

            chal = ch.file_challenge(
                task_id="t", test_file=str(test_path), test_name="test_foo",
                criterion="AC-1", argument="x",
            )
            result = ch.apply_ruling(challenge=chal, ruling=_upheld(chal))
            assert result.success and result.action == "test_deleted"
            assert not test_path.exists()
            assert result.reseal_recorded is False
            assert "unavailable" in result.reseal_note
            assert gi.load_red_state("t") == before

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [
        test_file_challenge_creates_doc_and_log_entry,
        test_file_challenge_rejects_empty_argument,
        test_file_challenge_normalizes_criterion_case,
        test_check_caps_allows_first_challenge,
        test_per_criterion_cap_enforced,
        test_per_task_cap_enforced,
        test_count_challenges_filters_by_task_and_criterion,
        test_spawn_arbiter_parses_ruling,
        test_spawn_arbiter_returns_none_when_spawn_fails,
        test_spawn_arbiter_returns_none_when_no_output_file,
        test_spawn_arbiter_rejects_unknown_verdict,
        test_spawn_arbiter_activates_a_real_blind_session_with_derived_roots,
        test_spawn_arbiter_session_actually_refuses_an_implementation_read,
        test_spawn_arbiter_requires_nonempty_sealed_roots,
        test_spawn_arbiter_records_sealed_roots_on_the_ruling,
        test_spawn_arbiter_returns_none_if_a_session_is_already_active,
        test_apply_ruling_upheld_deletes_test_file,
        test_apply_ruling_rejected_keeps_test_file,
        test_apply_ruling_ambiguous_writes_human_request,
        test_process_challenge_upheld_happy_path,
        test_process_challenge_cap_exceeded_short_circuits,
        test_process_challenge_records_observations,
        test_process_challenge_arbiter_failure_reports_cleanly,
        test_process_challenge_ambiguous_escalates,
        test_process_challenge_carries_sealed_roots_at_top_level,
        test_apply_ruling_upheld_records_pending_ruling_in_red_state,
        test_apply_ruling_upheld_without_red_state_reports_nothing_to_reseal,
        test_apply_ruling_upheld_resigns_a_signed_red_state,
        test_apply_ruling_upheld_refuses_to_unsign_a_signed_red_state,
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
