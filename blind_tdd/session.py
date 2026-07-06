"""Blind TDD session manager.

Orchestrates the lifecycle of a blind agent session by writing/removing
the `.themis/blind_tdd/active_session.json` file that the path-guard and
audit hooks check on every tool call.

Usage:

    from blind_tdd.session import blind_session

    with blind_session(
        agent_role="test_writer",
        task_id="task-42",
        allowed_paths=["tests/**", "docs/**", "plan.md", "public_api.md"],
        blocked_paths=["src/**", "examples/**", ".git/**"],
    ) as session:
        # Spawn blind agent here. All tool calls are guarded.
        print(f"Spawned blind agent with session_id={session['session_id']}")
    # Session file is deleted on context exit, audit log persists.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def _session_file() -> Path:
    d = Path(".themis") / "blind_tdd"
    d.mkdir(parents=True, exist_ok=True)
    return d / "active_session.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Default blocked paths — blind agents should never read these.
_DEFAULT_BLOCKED = [
    "src/**",
    "examples/**",
    ".git/**",
    ".themis/observations.jsonl",
    ".themis/blind_audit/**",  # prevent the agent from reading its own audit log
    "data/generated/**",       # translator output is also "implementation"
]


# Default allowed paths for Agent #1 (test writer).
_DEFAULT_WRITER_ALLOWED = [
    "tests/**",
    "docs/**",
    "plan.md",
    "fix_plan.md",
    "public_api.md",
    "CLAUDE.md",
    "README.md",
    ".themis/blind_tdd/active_session.json",  # agent needs to know it's blind
    ".themis/blind_tdd/triage/**",  # the writer's MANDATED output — its triage report
]


# Default allowed paths for Agent #2 (test runner). Same as writer but
# also allows reading test config files needed to execute tests, and writing
# its own mandated output (the green report).
_DEFAULT_RUNNER_ALLOWED = _DEFAULT_WRITER_ALLOWED + [
    ".themis/blind_tdd/green_report/**",  # the runner's MANDATED output
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "package.json",
    "jest.config.js",
    "vitest.config.ts",
    "Cargo.toml",
    "*.csproj",
    "conftest.py",
]


# Default allowed paths for Agent #3 (arbiter). Same as writer plus the
# specific challenge document location.
_DEFAULT_ARBITER_ALLOWED = _DEFAULT_WRITER_ALLOWED + [
    ".themis/blind_tdd/challenges/**",
    ".themis/blind_tdd/rulings/**",
]


def default_allowed_paths(agent_role: str) -> list[str]:
    """Return the default allowed_paths list for the given agent role."""
    if agent_role == "test_writer":
        return list(_DEFAULT_WRITER_ALLOWED)
    if agent_role == "test_runner":
        return list(_DEFAULT_RUNNER_ALLOWED)
    if agent_role == "arbiter":
        return list(_DEFAULT_ARBITER_ALLOWED)
    return []


def default_blocked_paths() -> list[str]:
    return list(_DEFAULT_BLOCKED)


def activate(
    *,
    agent_role: str,
    task_id: str,
    allowed_paths: list[str] | None = None,
    blocked_paths: list[str] | None = None,
    session_id: str | None = None,
) -> dict:
    """Write the active_session.json file to enable blind mode.

    Returns the session dict. Raises RuntimeError if a session is already active.
    """
    sf = _session_file()
    if sf.exists():
        existing = {}
        try:
            existing = json.loads(sf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
        raise RuntimeError(
            f"Blind TDD session already active: {existing.get('session_id', '?')} "
            f"(role={existing.get('agent_role', '?')}, task={existing.get('task_id', '?')}). "
            f"Call deactivate() before activating a new session."
        )

    if session_id is None:
        session_id = f"blind-{agent_role}-{uuid.uuid4().hex[:8]}"

    if allowed_paths is None:
        allowed_paths = default_allowed_paths(agent_role)
    if blocked_paths is None:
        blocked_paths = default_blocked_paths()

    session = {
        "session_id": session_id,
        "agent_role": agent_role,
        "task_id": task_id,
        "allowed_paths": allowed_paths,
        "blocked_paths": blocked_paths,
        "created_at": _now_iso(),
    }
    sf.write_text(
        json.dumps(session, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return session


def deactivate() -> dict | None:
    """Remove the active_session.json file. Returns the session that was active,
    or None if there was no active session."""
    sf = _session_file()
    if not sf.exists():
        return None
    try:
        session = json.loads(sf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        session = None
    try:
        sf.unlink()
    except OSError:
        pass
    return session


def current_session() -> dict | None:
    """Return the currently active session, if any."""
    sf = _session_file()
    if not sf.exists():
        return None
    try:
        return json.loads(sf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


@contextlib.contextmanager
def blind_session(
    *,
    agent_role: str,
    task_id: str,
    allowed_paths: list[str] | None = None,
    blocked_paths: list[str] | None = None,
    session_id: str | None = None,
) -> Iterator[dict]:
    """Context manager that activates a blind session and deactivates on exit."""
    session = activate(
        agent_role=agent_role,
        task_id=task_id,
        allowed_paths=allowed_paths,
        blocked_paths=blocked_paths,
        session_id=session_id,
    )
    try:
        yield session
    finally:
        deactivate()


def get_audit_log(session_id: str) -> list[dict]:
    """Read and parse the audit log for a completed session."""
    audit_file = Path(".themis") / "blind_audit" / f"{session_id}.jsonl"
    if not audit_file.exists():
        return []
    records = []
    with audit_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def audit_violations(session_id: str) -> list[dict]:
    """Return only the violation attempts (blocked tool calls) from a session's audit log."""
    return [
        r for r in get_audit_log(session_id)
        if r.get("source") == "pretooluse_hook" and r.get("allowed") is False
    ]
