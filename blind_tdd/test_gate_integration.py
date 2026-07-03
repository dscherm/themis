"""Self-contained tests for gate_integration.

Runs without pytest. Exercises the blind-TDD smart-gate adapter against
in-memory tasks and a stub orchestrator that bypasses agent spawning.

Run with:
    python -m blind_tdd.test_gate_integration
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

from blind_tdd import gate_integration as gi  # noqa: E402
from blind_tdd.orchestrator import RedPhaseResult, GreenPhaseResult  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_TASK = {
    "id": "task-test-1",
    "title": "Do the thing",
    "passes": False,
    "public_surface": {
        "module": "src.foo",
        "adds": ["do_thing"],
    },
    "acceptance_criteria": [
        {
            "id": "AC-1",
            "given": "a foo object",
            "when": "do_thing is called",
            "then": "do_thing returns True",
        },
    ],
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


def _write_plan(dir_: Path, tasks: list[dict]) -> None:
    blob = {"tasks": tasks}
    (dir_ / "plan.md").write_text(
        "# Plan\n\n```json\n" + json.dumps(blob, indent=2) + "\n```\n",
        encoding="utf-8",
    )


class _StubOrchestrator:
    """Pluggable stub that mimics BlindTddOrchestrator's two methods."""

    def __init__(self, *, red_result=None, green_result=None):
        self.red_result = red_result
        self.green_result = green_result
        self.red_called = False
        self.green_called = False

    def run_red_phase(self, task):
        self.red_called = True
        return self.red_result

    def run_green_phase(self, task, red_result):
        self.green_called = True
        return self.green_result


def _install_stub_orch(monkey_orch):
    """Replace _make_orchestrator with a factory that returns the stub."""
    original = gi._make_orchestrator
    gi._make_orchestrator = lambda cfg: monkey_orch
    return original


def _restore(original):
    gi._make_orchestrator = original


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_get_blind_tdd_config_defaults():
    cfg = gi.get_blind_tdd_config({})
    assert cfg["enabled"] is False
    assert cfg["enforcement"] == "strict"
    assert "tests/contracts/" in cfg["test_dirs"]
    assert cfg["spawner"] == "claude_code"
    assert cfg["spawn_auth"] == "subscription"  # strip API key by default


def test_get_blind_tdd_config_overrides():
    cfg = gi.get_blind_tdd_config({
        "gate": {
            "blind_tdd": {
                "enabled": True,
                "enforcement": "warn",
                "test_dirs": ["custom/"],
                "spawner": "manual",
                "spawn_auth": "api",
            }
        }
    })
    assert cfg["enabled"] is True
    assert cfg["enforcement"] == "warn"
    assert cfg["test_dirs"] == ["custom/"]
    assert cfg["spawner"] == "manual"
    assert cfg["spawn_auth"] == "api"


def test_run_disabled_is_skipped():
    result = gi.run_blind_tdd_gate({})
    assert result.passed
    assert result.phase == "skipped"
    assert "disabled" in result.message


def test_run_enabled_no_task_is_skipped_with_pass():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            result = gi.run_blind_tdd_gate(
                {"gate": {"blind_tdd": {"enabled": True}}}
            )
            assert result.passed
            assert result.phase == "skipped"
            assert "no current task" in result.message


def test_load_current_task_from_env(monkeypatch=None):
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        _write_plan(p, [VALID_TASK])
        with _chdir(p):
            os.environ["RALPH_BLIND_TDD_TASK"] = "task-test-1"
            try:
                task, source = gi.load_current_task({})
                assert task is not None
                assert task["id"] == "task-test-1"
                assert "env RALPH_BLIND_TDD_TASK" in source
            finally:
                del os.environ["RALPH_BLIND_TDD_TASK"]


def test_load_current_task_from_state_file():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        _write_plan(p, [VALID_TASK])
        (p / ".themis").mkdir()
        (p / ".themis" / "current_task.json").write_text(
            json.dumps({"id": "task-test-1"}), encoding="utf-8"
        )
        with _chdir(p):
            task, source = gi.load_current_task({})
            assert task is not None
            assert task["id"] == "task-test-1"
            assert "current_task.json" in source


def test_load_current_task_fallback_to_plan_md_incomplete():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        _write_plan(p, [
            {"id": "done-1", "title": "done", "passes": True},
            VALID_TASK,
        ])
        with _chdir(p):
            task, source = gi.load_current_task({})
            assert task is not None
            assert task["id"] == "task-test-1"
            assert "first incomplete" in source


def test_red_phase_pass_saves_state():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        _write_plan(p, [VALID_TASK])
        with _chdir(p):
            stub = _StubOrchestrator(
                red_result=RedPhaseResult(
                    passed=True,
                    reason="red ok",
                    test_file_hashes={"tests/contracts/foo.py": "abc"},
                    triage_report={"task": "task-test-1", "triage": []},
                ),
            )
            original = _install_stub_orch(stub)
            try:
                os.environ["RALPH_BLIND_TDD_TASK"] = "task-test-1"
                result = gi.run_blind_tdd_gate(
                    {"gate": {"blind_tdd": {"enabled": True}}}
                )
            finally:
                os.environ.pop("RALPH_BLIND_TDD_TASK", None)
                _restore(original)

            assert result.passed
            assert result.phase == "red"
            assert stub.red_called
            assert not stub.green_called

            # Red state persisted
            assert (p / ".themis" / "blind_tdd" / "red_state" / "task-test-1.json").exists()


def test_second_run_goes_to_green_phase():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        _write_plan(p, [VALID_TASK])
        with _chdir(p):
            # Pre-populate red state
            gi.save_red_state(
                "task-test-1",
                RedPhaseResult(
                    passed=True,
                    test_file_hashes={"tests/contracts/foo.py": "abc"},
                    triage_report={"task": "task-test-1", "triage": []},
                ),
            )

            stub = _StubOrchestrator(
                green_result=GreenPhaseResult(
                    passed=True,
                    reason="green ok",
                    green_report={
                        "overall": "pass",
                        "tests_passed": 3,
                        "tests_failed": 0,
                    },
                    hash_match=True,
                ),
            )
            original = _install_stub_orch(stub)
            try:
                os.environ["RALPH_BLIND_TDD_TASK"] = "task-test-1"
                result = gi.run_blind_tdd_gate(
                    {"gate": {"blind_tdd": {"enabled": True}}}
                )
            finally:
                os.environ.pop("RALPH_BLIND_TDD_TASK", None)
                _restore(original)

            assert result.passed
            assert result.phase == "green"
            assert not stub.red_called
            assert stub.green_called

            # Red state cleared after green-pass
            assert not (p / ".themis" / "blind_tdd" / "red_state" / "task-test-1.json").exists()


def test_green_fail_strict_blocks_commit():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        _write_plan(p, [VALID_TASK])
        with _chdir(p):
            gi.save_red_state(
                "task-test-1",
                RedPhaseResult(passed=True, test_file_hashes={}, triage_report={}),
            )
            stub = _StubOrchestrator(
                green_result=GreenPhaseResult(
                    passed=False,
                    reason="runner reported overall=fail",
                    green_report={"overall": "fail", "tests_passed": 1, "tests_failed": 2},
                    hash_match=True,
                ),
            )
            original = _install_stub_orch(stub)
            try:
                os.environ["RALPH_BLIND_TDD_TASK"] = "task-test-1"
                result = gi.run_blind_tdd_gate(
                    {"gate": {"blind_tdd": {"enabled": True, "enforcement": "strict"}}}
                )
            finally:
                os.environ.pop("RALPH_BLIND_TDD_TASK", None)
                _restore(original)

            assert not result.passed
            assert result.phase == "green"
            assert "runner reported" in result.message
            # Red state NOT cleared on failure
            assert (p / ".themis" / "blind_tdd" / "red_state" / "task-test-1.json").exists()


def test_green_fail_warn_does_not_block():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        _write_plan(p, [VALID_TASK])
        with _chdir(p):
            gi.save_red_state(
                "task-test-1",
                RedPhaseResult(passed=True, test_file_hashes={}, triage_report={}),
            )
            stub = _StubOrchestrator(
                green_result=GreenPhaseResult(
                    passed=False, reason="x", green_report={"overall": "fail"},
                ),
            )
            original = _install_stub_orch(stub)
            try:
                os.environ["RALPH_BLIND_TDD_TASK"] = "task-test-1"
                result = gi.run_blind_tdd_gate(
                    {"gate": {"blind_tdd": {"enabled": True, "enforcement": "warn"}}}
                )
            finally:
                os.environ.pop("RALPH_BLIND_TDD_TASK", None)
                _restore(original)

            assert result.passed  # warn never blocks
            assert result.phase == "green"


def test_invalid_task_fails_strict():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        broken = {"id": "bad-1", "title": "Broken", "passes": False}  # no criteria
        _write_plan(p, [broken])
        with _chdir(p):
            os.environ["RALPH_BLIND_TDD_TASK"] = "bad-1"
            try:
                result = gi.run_blind_tdd_gate(
                    {"gate": {"blind_tdd": {"enabled": True, "enforcement": "strict"}}}
                )
            finally:
                os.environ.pop("RALPH_BLIND_TDD_TASK", None)
            assert not result.passed
            assert result.phase == "error"
            assert result.reason == "schema_invalid"


def test_invalid_task_warns_but_passes():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        broken = {"id": "bad-2", "title": "Broken", "passes": False}
        _write_plan(p, [broken])
        with _chdir(p):
            os.environ["RALPH_BLIND_TDD_TASK"] = "bad-2"
            try:
                result = gi.run_blind_tdd_gate(
                    {"gate": {"blind_tdd": {"enabled": True, "enforcement": "warn"}}}
                )
            finally:
                os.environ.pop("RALPH_BLIND_TDD_TASK", None)
            assert result.passed
            assert result.phase == "error"


def test_red_state_round_trip():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            r = RedPhaseResult(
                passed=True,
                test_file_hashes={"a": "1", "b": "2"},
                triage_report={"task": "t", "triage": [{"criterion": "AC-1"}]},
                spawn_result={"agent_id": "sess-7"},
            )
            gi.save_red_state("t", r)
            loaded = gi.load_red_state("t")
            assert loaded is not None
            assert loaded["task_id"] == "t"
            assert loaded["test_file_hashes"] == {"a": "1", "b": "2"}
            assert loaded["spawn_agent_id"] == "sess-7"
            gi.clear_red_state("t")
            assert gi.load_red_state("t") is None


def _pay_task(task_id: str, module: str, files: list[str], tags: list[str] | None = None) -> dict:
    task = {
        "id": task_id,
        "title": f"Task {task_id}",
        "passes": False,
        "files": files,
        "public_surface": {"module": module, "adds": ["do_thing"]},
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "given": "a foo object",
                "when": "do_thing is called",
                "then": "do_thing returns True",
            },
        ],
    }
    if tags:
        task["tags"] = tags
    return task


def test_hash_break_feeds_escalation_for_next_run():
    """Detection on run N feeds the prediction for run N+1: a hash break on a
    gated task escalates a similar task that selective routing would skip."""
    tampered = _pay_task("task-pay-1", "src.pay.charge", ["src/pay/charge.py"], tags=["gate-me"])
    neighbor = _pay_task("task-pay-2", "src.pay.refund", ["src/pay/refund.py"])
    unrelated = _pay_task("task-docs-1", "docs_tools.gen", ["docs_tools/gen.py"])
    config = {"gate": {"blind_tdd": {"enabled": True, "routing": {"tags": ["gate-me"]}}}}

    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        _write_plan(p, [tampered, neighbor, unrelated])
        with _chdir(p):
            # Green phase for the gated task catches a broken seal.
            gi.save_red_state(
                "task-pay-1",
                RedPhaseResult(passed=True, test_file_hashes={"tests/contracts/t.py": "abc"},
                               triage_report={}),
            )
            stub = _StubOrchestrator(
                green_result=GreenPhaseResult(
                    passed=False,
                    reason="test file integrity compromised: 1 file(s) modified",
                    green_report={"overall": "fail"},
                    hash_match=False,
                    hash_break=True,
                ),
            )
            original = _install_stub_orch(stub)
            try:
                os.environ["RALPH_BLIND_TDD_TASK"] = "task-pay-1"
                result = gi.run_blind_tdd_gate(config)
            finally:
                os.environ.pop("RALPH_BLIND_TDD_TASK", None)
                _restore(original)
            assert not result.passed
            assert result.details.get("hash_break") is True
            assert (p / ".themis" / "blind_tdd" / "tamper_ledger.jsonl").exists()

            # The neighbor task carries no routing tag — without the ledger it
            # would be skipped. Tamper history escalates it into the gate.
            stub = _StubOrchestrator(
                red_result=RedPhaseResult(
                    passed=True, reason="red ok",
                    test_file_hashes={"tests/contracts/t2.py": "def"},
                    triage_report={},
                ),
            )
            original = _install_stub_orch(stub)
            try:
                os.environ["RALPH_BLIND_TDD_TASK"] = "task-pay-2"
                result = gi.run_blind_tdd_gate(config)
            finally:
                os.environ.pop("RALPH_BLIND_TDD_TASK", None)
                _restore(original)
            assert result.phase == "red"
            assert stub.red_called
            assert result.details.get("tamper_escalation")

            # An unrelated task is untouched by the ledger and still skips.
            try:
                os.environ["RALPH_BLIND_TDD_TASK"] = "task-docs-1"
                result = gi.run_blind_tdd_gate(config)
            finally:
                os.environ.pop("RALPH_BLIND_TDD_TASK", None)
            assert result.phase == "skipped"
            assert result.reason == "routing_excluded"


def test_seal_record_tamper_is_recorded():
    """A forged red-state record (HMAC mismatch) also lands in the ledger."""
    task = _pay_task("task-pay-3", "src.pay.audit", ["src/pay/audit.py"])
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        _write_plan(p, [task])
        with _chdir(p):
            os.environ["THEMIS_SEAL_KEY"] = "test-key"
            os.environ["RALPH_BLIND_TDD_TASK"] = "task-pay-3"
            try:
                gi.save_red_state(
                    "task-pay-3",
                    RedPhaseResult(passed=True, test_file_hashes={"t.py": "abc"},
                                   triage_report={}),
                )
                # Forge the baseline the way a Bash-capable implementer would.
                state_path = p / ".themis" / "blind_tdd" / "red_state" / "task-pay-3.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["test_file_hashes"] = {"t.py": "forged"}
                state_path.write_text(json.dumps(state), encoding="utf-8")

                result = gi.run_blind_tdd_gate({"gate": {"blind_tdd": {"enabled": True}}})
            finally:
                os.environ.pop("THEMIS_SEAL_KEY", None)
                os.environ.pop("RALPH_BLIND_TDD_TASK", None)

            assert not result.passed
            assert result.reason == "seal_tampered"
            ledger = (p / ".themis" / "blind_tdd" / "tamper_ledger.jsonl")
            assert ledger.exists()
            record = json.loads(ledger.read_text(encoding="utf-8").strip())
            assert record["kind"] == "seal_record_tampered"
            assert record["task_id"] == "task-pay-3"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [
        test_get_blind_tdd_config_defaults,
        test_get_blind_tdd_config_overrides,
        test_run_disabled_is_skipped,
        test_run_enabled_no_task_is_skipped_with_pass,
        test_load_current_task_from_env,
        test_load_current_task_from_state_file,
        test_load_current_task_fallback_to_plan_md_incomplete,
        test_red_phase_pass_saves_state,
        test_second_run_goes_to_green_phase,
        test_green_fail_strict_blocks_commit,
        test_green_fail_warn_does_not_block,
        test_invalid_task_fails_strict,
        test_invalid_task_warns_but_passes,
        test_red_state_round_trip,
        test_hash_break_feeds_escalation_for_next_run,
        test_seal_record_tamper_is_recorded,
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
