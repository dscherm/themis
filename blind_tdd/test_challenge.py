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
            )
            assert ruling is None


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
            )
            assert result.success
            assert result.action == "escalated_to_human"
            assert result.human_request_id is not None
            assert test_path.exists()


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
        test_apply_ruling_upheld_deletes_test_file,
        test_apply_ruling_rejected_keeps_test_file,
        test_apply_ruling_ambiguous_writes_human_request,
        test_process_challenge_upheld_happy_path,
        test_process_challenge_cap_exceeded_short_circuits,
        test_process_challenge_records_observations,
        test_process_challenge_arbiter_failure_reports_cleanly,
        test_process_challenge_ambiguous_escalates,
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
