"""Tests for templates/hooks/blind_tdd_audit.py — closes the audit gap.

The PostToolUse audit hook records every tool call during a blind
session. If it silently fails, the forensic record is worthless.

Covers:
- Passthrough when no active session
- Writes one JSONL record per tool call
- Summarizes Bash commands verbatim
- Summarizes file paths but NOT content (no old_string/new_string leak)
- Records response exit codes and errors
- Never blocks on audit failures (always exits 0)

Run with:
    python -m blind_tdd.test_audit_hook
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[1]
_HOOK = _REPO / "templates" / "hooks" / "blind_tdd_audit.py"


def _run_hook(*, cwd: Path, input_json: dict) -> int:
    result = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(input_json),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=30,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode


def _write_session(cwd: Path, session: dict) -> None:
    d = cwd / ".themis" / "blind_tdd"
    d.mkdir(parents=True, exist_ok=True)
    (d / "active_session.json").write_text(json.dumps(session), encoding="utf-8")


SESSION = {
    "session_id": "audit-test-1",
    "agent_role": "test_writer",
    "task_id": "t1",
}


def _read_audit(cwd: Path) -> list[dict]:
    audit = cwd / ".themis" / "blind_audit" / "audit-test-1.jsonl"
    if not audit.exists():
        return []
    return [
        json.loads(line)
        for line in audit.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Passthrough
# ---------------------------------------------------------------------------

def test_passthrough_when_no_session():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        rc = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "foo.py"},
            "tool_response": {"exit_code": 0},
        })
        assert rc == 0
        # No audit dir created when no session
        assert not (cwd / ".themis" / "blind_audit").exists()


def test_passthrough_with_empty_stdin():
    rc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    ).returncode
    assert rc == 0


def test_passthrough_with_malformed_stdin():
    rc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input="{garbage",
        capture_output=True,
        text=True,
        timeout=10,
    ).returncode
    assert rc == 0


# ---------------------------------------------------------------------------
# Record writes
# ---------------------------------------------------------------------------

def test_writes_one_record_per_call():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, SESSION)
        rc = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "tests/foo.py"},
            "tool_response": {"output": "file contents here"},
        })
        assert rc == 0
        records = _read_audit(cwd)
        assert len(records) == 1
        assert records[0]["tool_name"] == "Read"
        assert records[0]["session_id"] == "audit-test-1"
        assert records[0]["agent_role"] == "test_writer"
        assert records[0]["task_id"] == "t1"
        assert records[0]["source"] == "posttooluse_hook"


def test_multiple_calls_append_to_same_file():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, SESSION)
        for i in range(3):
            _run_hook(cwd=cwd, input_json={
                "tool_name": "Read",
                "tool_input": {"file_path": f"tests/foo_{i}.py"},
                "tool_response": {"output": "x"},
            })
        records = _read_audit(cwd)
        assert len(records) == 3


# ---------------------------------------------------------------------------
# Input summarization — no content leakage
# ---------------------------------------------------------------------------

def test_edit_input_summary_excludes_old_new_strings():
    """CRITICAL: old_string/new_string must NOT appear in the audit log."""
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, SESSION)
        secret = "SECRET_IMPLEMENTATION_DETAIL"
        _run_hook(cwd=cwd, input_json={
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "tests/foo.py",
                "old_string": f"def foo(): return {secret}",
                "new_string": "def foo(): return 'new'",
            },
            "tool_response": {"output": "edited"},
        })
        records = _read_audit(cwd)
        raw = json.dumps(records[0])
        assert secret not in raw, (
            "old_string content leaked into audit log — blind implementation "
            "details could be captured"
        )
        # But file_path should be present
        assert "tests/foo.py" in raw


def test_write_input_summary_excludes_content():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, SESSION)
        secret = "ANOTHER_SECRET_CONTENT_STRING"
        _run_hook(cwd=cwd, input_json={
            "tool_name": "Write",
            "tool_input": {
                "file_path": "tests/new.py",
                "content": f"huge content with {secret} in it",
            },
            "tool_response": {"output": "written"},
        })
        records = _read_audit(cwd)
        raw = json.dumps(records[0])
        assert secret not in raw
        assert "tests/new.py" in raw


def test_bash_command_captured_verbatim():
    """Bash commands should be captured fully for forensic review."""
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, SESSION)
        cmd = "pytest tests/contracts/test_foo.py -v"
        _run_hook(cwd=cwd, input_json={
            "tool_name": "Bash",
            "tool_input": {"command": cmd, "description": "run tests"},
            "tool_response": {"output": "pass", "exit_code": 0},
        })
        records = _read_audit(cwd)
        assert records[0]["tool_input_summary"]["command"] == cmd


# ---------------------------------------------------------------------------
# Response summarization
# ---------------------------------------------------------------------------

def test_response_exit_code_captured():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, SESSION)
        _run_hook(cwd=cwd, input_json={
            "tool_name": "Bash",
            "tool_input": {"command": "false"},
            "tool_response": {"exit_code": 1, "error": "command failed"},
        })
        records = _read_audit(cwd)
        assert records[0]["tool_response_summary"]["exit_code"] == 1
        assert "command failed" in records[0]["tool_response_summary"]["error"]


def test_response_content_length_captured_not_content():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, SESSION)
        secret_output = "SECRET_OUTPUT_THAT_SHOULD_NOT_LEAK"
        _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "tests/foo.py"},
            "tool_response": {"output": secret_output},
        })
        records = _read_audit(cwd)
        raw = json.dumps(records[0])
        assert secret_output not in raw
        # Length should be captured
        assert records[0]["tool_response_summary"].get("output_length") == len(secret_output)


# ---------------------------------------------------------------------------
# Never blocks
# ---------------------------------------------------------------------------

def test_always_exits_zero_even_on_unknown_tool():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, SESSION)
        rc = _run_hook(cwd=cwd, input_json={
            "tool_name": "WeirdTool",
            "tool_input": {"x": 1},
            "tool_response": {"y": 2},
        })
        assert rc == 0
        records = _read_audit(cwd)
        assert len(records) == 1
        assert records[0]["tool_name"] == "WeirdTool"


def test_malformed_session_file_passes_through():
    """Corrupt session JSON should not crash the audit hook."""
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        d = cwd / ".themis" / "blind_tdd"
        d.mkdir(parents=True)
        (d / "active_session.json").write_text("{garbage", encoding="utf-8")
        rc = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "x.py"},
            "tool_response": {"output": "y"},
        })
        # Audit hook treats corrupt session as no session
        assert rc == 0


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
