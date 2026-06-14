"""Tests for blind_tdd.session — closes the audit gap.

The session manager writes/removes `.themis/blind_tdd/active_session.json`
— the contract file between Python and the path-guard hook. If its
format breaks, the hook can't enforce blindness. These tests pin the
contract.

Covers:
- activate() writes the expected JSON shape
- activate() refuses to clobber an existing session
- deactivate() removes the file and returns the prior session
- current_session() round-trip
- blind_session() context manager cleanup on success and exception
- default_allowed_paths() per role
- audit log parsing (get_audit_log, audit_violations)

Run with:
    python -m blind_tdd.test_session
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

from blind_tdd import session as sess  # noqa: E402


def _chdir(path: Path):
    class _Ctx:
        def __enter__(self_):
            self_.prev = os.getcwd()
            os.chdir(path)
            return path

        def __exit__(self_, *a):
            os.chdir(self_.prev)
    return _Ctx()


# ---------------------------------------------------------------------------
# activate / deactivate
# ---------------------------------------------------------------------------

def test_activate_writes_session_file():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            session = sess.activate(agent_role="test_writer", task_id="t1")
            sf = Path(".themis/blind_tdd/active_session.json")
            assert sf.exists()
            data = json.loads(sf.read_text(encoding="utf-8"))
            assert data["session_id"] == session["session_id"]
            assert data["agent_role"] == "test_writer"
            assert data["task_id"] == "t1"
            assert isinstance(data["allowed_paths"], list)
            assert isinstance(data["blocked_paths"], list)
            assert "created_at" in data
            sess.deactivate()


def test_activate_uses_default_allowed_paths_per_role():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            session = sess.activate(agent_role="test_writer", task_id="t1")
            # Writer defaults include tests/** and public_api.md
            assert "tests/**" in session["allowed_paths"]
            assert "public_api.md" in session["allowed_paths"]
            sess.deactivate()

            session = sess.activate(agent_role="test_runner", task_id="t2")
            # Runner defaults include test config files
            assert "pyproject.toml" in session["allowed_paths"]
            assert "pytest.ini" in session["allowed_paths"]
            sess.deactivate()


def test_activate_uses_default_blocked_paths():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            session = sess.activate(agent_role="test_writer", task_id="t1")
            blocked = session["blocked_paths"]
            assert "src/**" in blocked
            assert "examples/**" in blocked
            assert ".git/**" in blocked
            sess.deactivate()


def test_activate_custom_paths_override_defaults():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            session = sess.activate(
                agent_role="test_writer",
                task_id="t1",
                allowed_paths=["only_this.md"],
                blocked_paths=["nothing_else/**"],
            )
            assert session["allowed_paths"] == ["only_this.md"]
            assert session["blocked_paths"] == ["nothing_else/**"]
            sess.deactivate()


def test_activate_refuses_to_clobber_existing_session():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            sess.activate(agent_role="test_writer", task_id="t1")
            try:
                sess.activate(agent_role="test_runner", task_id="t2")
            except RuntimeError as e:
                assert "already active" in str(e)
                sess.deactivate()
                return
            assert False, "should have raised RuntimeError"


def test_deactivate_removes_file():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            sess.activate(agent_role="test_writer", task_id="t1")
            sf = Path(".themis/blind_tdd/active_session.json")
            assert sf.exists()
            result = sess.deactivate()
            assert result is not None
            assert result["agent_role"] == "test_writer"
            assert not sf.exists()


def test_deactivate_noop_when_no_session():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            assert sess.deactivate() is None


def test_current_session_returns_active():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            assert sess.current_session() is None
            sess.activate(agent_role="test_writer", task_id="t1")
            current = sess.current_session()
            assert current is not None
            assert current["task_id"] == "t1"
            sess.deactivate()
            assert sess.current_session() is None


def test_activate_explicit_session_id():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            session = sess.activate(
                agent_role="test_writer", task_id="t1",
                session_id="custom-id-123",
            )
            assert session["session_id"] == "custom-id-123"
            sess.deactivate()


# ---------------------------------------------------------------------------
# blind_session context manager
# ---------------------------------------------------------------------------

def test_blind_session_cleans_up_on_success():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            with sess.blind_session(agent_role="test_writer", task_id="t1") as s:
                assert sess.current_session() is not None
                assert s["task_id"] == "t1"
            # Exited — session file should be gone
            assert sess.current_session() is None


def test_blind_session_cleans_up_on_exception():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            try:
                with sess.blind_session(agent_role="test_writer", task_id="t1"):
                    assert sess.current_session() is not None
                    raise RuntimeError("simulated failure")
            except RuntimeError:
                pass
            # Session must be cleaned up even on exception
            assert sess.current_session() is None


def test_blind_session_nested_raises():
    """Nested sessions should be rejected by the inner activate()."""
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            with sess.blind_session(agent_role="test_writer", task_id="t1"):
                try:
                    with sess.blind_session(agent_role="test_runner", task_id="t2"):
                        assert False, "inner should have raised"
                except RuntimeError as e:
                    assert "already active" in str(e)


# ---------------------------------------------------------------------------
# default_allowed_paths / default_blocked_paths
# ---------------------------------------------------------------------------

def test_default_allowed_paths_per_role():
    writer = sess.default_allowed_paths("test_writer")
    runner = sess.default_allowed_paths("test_runner")
    arbiter = sess.default_allowed_paths("arbiter")
    unknown = sess.default_allowed_paths("bogus")

    assert "tests/**" in writer
    assert "pyproject.toml" in runner
    assert ".themis/blind_tdd/challenges/**" in arbiter
    assert unknown == []

    # Runner is a superset of writer
    for p in writer:
        assert p in runner


def test_default_blocked_paths_includes_src_and_git():
    blocked = sess.default_blocked_paths()
    assert "src/**" in blocked
    assert ".git/**" in blocked
    assert "examples/**" in blocked
    assert "data/generated/**" in blocked


# ---------------------------------------------------------------------------
# Audit log parsing
# ---------------------------------------------------------------------------

def test_get_audit_log_empty_when_no_file():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            assert sess.get_audit_log("nonexistent-session") == []


def test_get_audit_log_parses_jsonl():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            audit_dir = Path(".themis/blind_audit")
            audit_dir.mkdir(parents=True)
            (audit_dir / "sess-1.jsonl").write_text(
                '{"tool_name": "Read", "source": "pretooluse_hook", "allowed": true}\n'
                '{"tool_name": "Read", "source": "pretooluse_hook", "allowed": false, "path": "src/foo.py"}\n'
                '\n'  # empty line should be skipped
                '{not valid json\n'  # malformed should be skipped
                '{"tool_name": "Write", "source": "posttooluse_hook"}\n',
                encoding="utf-8",
            )
            records = sess.get_audit_log("sess-1")
            assert len(records) == 3
            assert records[0]["tool_name"] == "Read"
            assert records[2]["tool_name"] == "Write"


def test_audit_violations_filters_denied_pretooluse():
    with tempfile.TemporaryDirectory() as td:
        with _chdir(Path(td)):
            audit_dir = Path(".themis/blind_audit")
            audit_dir.mkdir(parents=True)
            (audit_dir / "sess-2.jsonl").write_text(
                '{"source": "pretooluse_hook", "allowed": true, "path": "tests/a.py"}\n'
                '{"source": "pretooluse_hook", "allowed": false, "path": "src/foo.py"}\n'
                '{"source": "pretooluse_hook", "allowed": false, "path": "src/bar.py"}\n'
                '{"source": "posttooluse_hook", "allowed": false, "path": "src/baz.py"}\n',
                encoding="utf-8",
            )
            violations = sess.audit_violations("sess-2")
            # Only pretooluse + allowed=False should count
            assert len(violations) == 2
            assert all(v["source"] == "pretooluse_hook" for v in violations)
            assert all(v["allowed"] is False for v in violations)


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
