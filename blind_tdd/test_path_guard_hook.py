"""Tests for templates/hooks/blind_tdd_path_guard.py — closes the audit gap.

This is the actual enforcement mechanism for blindness. If the hook has
bugs, blind agents can peek at src/. These tests invoke the hook as a
real subprocess with fake stdin and verify exit codes.

Covers:
- Passthrough when no active session
- Block on blocked_paths match
- Allow on allowed_paths match
- Deny when path is outside whitelist
- Deny wins over allow
- Fail-closed on corrupt session file
- Recursive ** pattern matching
- Single-segment patterns (plain "src")
- Path normalization (absolute → relative, Windows → POSIX)
- Audit log entry written on every check
- Unknown tool name passes through
- Malformed stdin passes through (fail open)

Run with:
    python -m blind_tdd.test_path_guard_hook
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
_HOOK = _REPO / "templates" / "hooks" / "blind_tdd_path_guard.py"


def _run_hook(*, cwd: Path, input_json: dict) -> tuple[int, str, str]:
    """Invoke the hook with JSON on stdin. Returns (exit_code, stdout, stderr)."""
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
    return result.returncode, result.stdout, result.stderr


def _write_session(cwd: Path, session: dict) -> None:
    d = cwd / ".themis" / "blind_tdd"
    d.mkdir(parents=True, exist_ok=True)
    (d / "active_session.json").write_text(
        json.dumps(session), encoding="utf-8"
    )


def _clear_session(cwd: Path) -> None:
    p = cwd / ".themis" / "blind_tdd" / "active_session.json"
    if p.exists():
        p.unlink()


WRITER_SESSION = {
    "session_id": "test-sess-1",
    "agent_role": "test_writer",
    "task_id": "t1",
    "allowed_paths": [
        "tests/**", "docs/**", "plan.md", "public_api.md",
        "CLAUDE.md", "README.md",
    ],
    "blocked_paths": [
        "src/**", "examples/**", ".git/**", "data/generated/**",
    ],
}


# ---------------------------------------------------------------------------
# Passthrough cases
# ---------------------------------------------------------------------------

def test_passthrough_when_no_session():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        # No session file written
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "src/anything.py"},
        })
        assert rc == 0, f"should passthrough when no session, got {rc}"


def test_unknown_tool_name_passthroughs():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Bash",  # not in guarded list
            "tool_input": {"command": "cat src/foo.py"},
        })
        assert rc == 0, f"unguarded tool should pass, got {rc}"


def test_empty_stdin_passthroughs():
    rc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    ).returncode
    assert rc == 0


def test_malformed_stdin_passthroughs():
    rc = subprocess.run(
        [sys.executable, str(_HOOK)],
        input="{not valid json",
        capture_output=True,
        text=True,
        timeout=10,
    ).returncode
    assert rc == 0


def test_tool_call_without_path_passthroughs():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Grep",
            "tool_input": {"pattern": "something"},  # no path field
        })
        # Grep pattern is extracted as the "path" (matches _PATH_FIELDS)
        # and "something" doesn't match any whitelist entry, so it denies.
        # Let's use a different tool without path fields instead.
        pass

    # Use Read with no file_path — shouldn't crash
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {},
        })
        assert rc == 0, f"Read with no path should pass, got {rc}"


# ---------------------------------------------------------------------------
# Blocking cases
# ---------------------------------------------------------------------------

def test_blocks_read_of_src():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        rc, _, stderr = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "src/engine/math.py"},
        })
        assert rc == 2, f"should block src/, got rc={rc} stderr={stderr}"
        assert "BLOCKED" in stderr
        assert "src/engine/math.py" in stderr or "src" in stderr


def test_blocks_grep_in_examples():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        rc, _, stderr = _run_hook(cwd=cwd, input_json={
            "tool_name": "Grep",
            "tool_input": {"path": "examples/pacman/", "pattern": "x"},
        })
        assert rc == 2


def test_blocks_git_directory():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        rc, _, stderr = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": ".git/HEAD"},
        })
        assert rc == 2


def test_blocks_data_generated():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "data/generated/pacman/Player.cs"},
        })
        assert rc == 2


def test_blocks_path_outside_whitelist():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        # Not in src/, not in tests/ — not in whitelist
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "scripts/deploy.sh"},
        })
        assert rc == 2


# ---------------------------------------------------------------------------
# Allowing cases
# ---------------------------------------------------------------------------

def test_allows_read_of_tests():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "tests/contracts/test_foo.py"},
        })
        assert rc == 0, f"should allow tests/, got {rc}"


def test_allows_read_of_public_api():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "public_api.md"},
        })
        assert rc == 0


def test_allows_read_of_plan_md():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "plan.md"},
        })
        assert rc == 0


def test_allows_write_to_tests():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Write",
            "tool_input": {"file_path": "tests/contracts/test_new.py"},
        })
        assert rc == 0


# ---------------------------------------------------------------------------
# Deny-wins
# ---------------------------------------------------------------------------

def test_deny_wins_when_path_matches_both_lists():
    """If a path appears in BOTH allowed and blocked, blocked wins."""
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, {
            "session_id": "s",
            "agent_role": "test_writer",
            "task_id": "t1",
            "allowed_paths": ["src/**"],  # explicitly allowed
            "blocked_paths": ["src/**"],  # but also blocked
        })
        rc, _, stderr = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "src/foo.py"},
        })
        assert rc == 2, "deny should win over allow"


# ---------------------------------------------------------------------------
# Fail-closed on corrupt session file
# ---------------------------------------------------------------------------

def test_corrupt_session_fails_closed():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        d = cwd / ".themis" / "blind_tdd"
        d.mkdir(parents=True)
        (d / "active_session.json").write_text("{not valid", encoding="utf-8")
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "tests/contracts/test_foo.py"},
        })
        # Corrupt session → blocked_paths = ["**"], blocks everything
        assert rc == 2, f"corrupt session should fail closed, got {rc}"


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------

def test_absolute_path_inside_repo_normalized():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td).resolve()
        _write_session(cwd, WRITER_SESSION)
        abs_path = str(cwd / "src" / "foo.py")
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": abs_path},
        })
        assert rc == 2, f"abs src path should block, got {rc}"


def test_path_outside_repo_treated_as_absolute():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td).resolve()
        _write_session(cwd, WRITER_SESSION)
        # Path outside the repo
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "/etc/passwd"},
        })
        # Not in whitelist → denied
        assert rc == 2


# ---------------------------------------------------------------------------
# Audit log writes
# ---------------------------------------------------------------------------

def test_audit_log_written_on_allow():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "tests/contracts/test_foo.py"},
        })
        audit = cwd / ".themis" / "blind_audit" / "test-sess-1.jsonl"
        assert audit.exists()
        lines = audit.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["allowed"] is True
        assert rec["tool_name"] == "Read"
        assert rec["source"] == "pretooluse_hook"
        assert "tests/contracts/test_foo.py" in rec["path"]


def test_audit_log_written_on_deny():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, WRITER_SESSION)
        _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "src/engine/foo.py"},
        })
        audit = cwd / ".themis" / "blind_audit" / "test-sess-1.jsonl"
        assert audit.exists()
        lines = audit.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["allowed"] is False
        assert "blocked pattern" in rec["reason"]


# ---------------------------------------------------------------------------
# Pattern coverage
# ---------------------------------------------------------------------------

def test_plain_directory_name_blocks_contents():
    """`src` without /** should still match src/anything."""
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, {
            "session_id": "s2",
            "agent_role": "test_writer",
            "task_id": "t",
            "allowed_paths": ["tests/**"],
            "blocked_paths": ["src"],  # no /** suffix
        })
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "src/engine/foo.py"},
        })
        assert rc == 2, "plain 'src' should block src/**"


def test_no_allowed_paths_means_blacklist_only():
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, {
            "session_id": "s3",
            "agent_role": "test_writer",
            "task_id": "t",
            "allowed_paths": [],
            "blocked_paths": ["src/**"],
        })
        # tests/ is not in the whitelist, but also not blocked → allowed
        rc, _, _ = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "tests/contracts/test_foo.py"},
        })
        assert rc == 0, "blacklist-only mode should allow non-blocked paths"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------- BT1: locked-test write rule


def _write_red_state(cwd: Path, task_id: str, hashes: dict) -> None:
    d = cwd / ".themis" / "blind_tdd" / "red_state"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "test_file_hashes": hashes}),
        encoding="utf-8",
    )


def _tamper_records(cwd: Path) -> list[dict]:
    f = cwd / ".themis" / "blind_tdd" / "tamper_attempts.jsonl"
    if not f.exists():
        return []
    return [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def test_runner_write_to_locked_test_denied_and_tamper_recorded():
    """BT1: a non-writer blind agent writing a hash-locked test is denied even
    though the runner's allowed_paths (tests/**) would have permitted it, and
    a tamper-attempt record is appended."""
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, {
            "session_id": "blind-runner-bt1",
            "agent_role": "test_runner",
            "task_id": "task-bt1",
            "allowed_paths": ["tests/**"],
            "blocked_paths": ["src/**"],
        })
        _write_red_state(cwd, "task-bt1",
                         {"tests/contracts/test_foo.py": "0" * 64})
        code, _, err = _run_hook(cwd=cwd, input_json={
            "tool_name": "Write",
            "tool_input": {"file_path": "tests/contracts/test_foo.py",
                           "content": "tampered"},
        })
        assert code == 2, f"expected deny (2), got {code}; stderr={err}"
        assert "hash-locked" in err
        records = _tamper_records(cwd)
        assert len(records) == 1, f"expected 1 tamper record, got {records}"
        assert records[0]["tool_name"] == "Write"
        assert records[0]["agent_role"] == "test_runner"
        assert records[0]["path"] == "tests/contracts/test_foo.py"


def test_locked_test_denied_with_windows_native_separators():
    """Regression: save_red_state stores keys with the host's native separator,
    so on Windows red_state keys look like 'tests\\contracts\\test_foo.py'. The
    deny-wins rule must still fire — the guard normalizes locked keys to POSIX
    before comparing. Before the fix this silently no-op'd on Windows."""
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, {
            "session_id": "blind-runner-bt1-win",
            "agent_role": "test_runner",
            "task_id": "task-bt1",
            "allowed_paths": ["tests/**"],
            "blocked_paths": ["src/**"],
        })
        # Backslash key, exactly as save_red_state writes it on Windows.
        _write_red_state(cwd, "task-bt1",
                         {"tests\\contracts\\test_foo.py": "0" * 64})
        code, _, err = _run_hook(cwd=cwd, input_json={
            "tool_name": "Write",
            "tool_input": {"file_path": "tests/contracts/test_foo.py",
                           "content": "tampered"},
        })
        assert code == 2, f"expected deny (2), got {code}; stderr={err}"
        assert "hash-locked" in err
        assert len(_tamper_records(cwd)) == 1


def test_writer_write_to_locked_test_allowed():
    """BT1: the test writer may modify locked tests (red-phase resume after an
    arbiter ruling re-hashes afterward) — no deny, no tamper record."""
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, {
            "session_id": "blind-writer-bt1",
            "agent_role": "test_writer",
            "task_id": "task-bt1",
            "allowed_paths": ["tests/**"],
            "blocked_paths": ["src/**"],
        })
        _write_red_state(cwd, "task-bt1",
                         {"tests/contracts/test_foo.py": "0" * 64})
        code, _, err = _run_hook(cwd=cwd, input_json={
            "tool_name": "Write",
            "tool_input": {"file_path": "tests/contracts/test_foo.py",
                           "content": "resumed red phase"},
        })
        assert code == 0, f"expected allow (0), got {code}; stderr={err}"
        assert _tamper_records(cwd) == []


def test_runner_write_to_unlocked_test_allowed():
    """BT1: writes to test files NOT in any red_state stay allowed — the rule
    is scoped to hash-locked files only."""
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, {
            "session_id": "blind-runner-bt1b",
            "agent_role": "test_runner",
            "task_id": "task-bt1",
            "allowed_paths": ["tests/**"],
            "blocked_paths": ["src/**"],
        })
        _write_red_state(cwd, "task-bt1",
                         {"tests/contracts/test_foo.py": "0" * 64})
        code, _, err = _run_hook(cwd=cwd, input_json={
            "tool_name": "Write",
            "tool_input": {"file_path": "tests/contracts/test_other.py",
                           "content": "fine"},
        })
        assert code == 0, f"expected allow (0), got {code}; stderr={err}"
        assert _tamper_records(cwd) == []


def test_runner_read_of_locked_test_allowed():
    """BT1: the rule only guards write tools — the runner must still READ
    locked tests to execute them."""
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, {
            "session_id": "blind-runner-bt1c",
            "agent_role": "test_runner",
            "task_id": "task-bt1",
            "allowed_paths": ["tests/**"],
            "blocked_paths": ["src/**"],
        })
        _write_red_state(cwd, "task-bt1",
                         {"tests/contracts/test_foo.py": "0" * 64})
        code, _, err = _run_hook(cwd=cwd, input_json={
            "tool_name": "Read",
            "tool_input": {"file_path": "tests/contracts/test_foo.py"},
        })
        assert code == 0, f"expected allow (0), got {code}; stderr={err}"
        assert _tamper_records(cwd) == []


def test_blocked_src_write_on_locked_path_records_tamper():
    """BT1: a deny that lands on a locked test path via the ordinary
    blocked/whitelist rules still produces a tamper record."""
    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        _write_session(cwd, {
            "session_id": "blind-arbiter-bt1",
            "agent_role": "arbiter",
            "task_id": "task-bt1",
            "allowed_paths": ["docs/**"],  # tests NOT whitelisted for this session
            "blocked_paths": ["src/**"],
        })
        _write_red_state(cwd, "task-bt1",
                         {"tests/contracts/test_foo.py": "0" * 64})
        code, _, err = _run_hook(cwd=cwd, input_json={
            "tool_name": "Edit",
            "tool_input": {"file_path": "tests/contracts/test_foo.py",
                           "old_string": "a", "new_string": "b"},
        })
        assert code == 2, f"expected deny (2), got {code}; stderr={err}"
        records = _tamper_records(cwd)
        assert len(records) == 1
        assert records[0]["agent_role"] == "arbiter"


def test_each_role_can_write_its_mandated_output():
    """Regression: every blind role's DEFAULT allowed_paths must permit the
    exact output file the orchestrator requires from it — the writer's triage,
    the runner's green report, the arbiter's ruling.

    This drives the real hook against `session.default_allowed_paths(role)` and
    `orchestrator._expected_manual_output(role, ...)`, so it catches the class
    of bug where a role is prompted/required to write a file its own whitelist
    forbids. (A live run caught exactly this: triage/** and green_report/**
    were missing, so the writer/runner could never signal completion.)
    """
    from blind_tdd.session import default_allowed_paths, default_blocked_paths
    from blind_tdd.orchestrator import _expected_manual_output

    cases = {
        "test_writer": {"task_id": "t1"},
        "test_runner": {"task_id": "t1"},
        "arbiter": {"task_id": "t1", "challenge_id": "c1"},
    }
    for role, inputs in cases.items():
        out_path = _expected_manual_output(role, inputs["task_id"], inputs)
        assert out_path is not None, f"no mandated output declared for {role}"
        session = {
            "session_id": f"reg-{role}",
            "agent_role": role,
            "task_id": "t1",
            "allowed_paths": default_allowed_paths(role),
            "blocked_paths": default_blocked_paths(),
        }
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            _write_session(cwd, session)
            rc, _, stderr = _run_hook(cwd=cwd, input_json={
                "tool_name": "Write",
                "tool_input": {"file_path": str(out_path).replace("\\", "/"),
                               "content": "{}"},
            })
            assert rc == 0, (
                f"{role} is required to write {out_path} but its default "
                f"allowed_paths forbid it (rc={rc}); stderr={stderr}"
            )


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
