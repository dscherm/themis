#!/usr/bin/env python3
"""PostToolUse hook for blind TDD audit logging.

This is Layer 3 of the three-layer defense from the Blind TDD RFC (OQ1).
Logs every tool call made during an active blind session — not just the
guarded ones — so any Bash escape, MCP call, or unusual tool use shows up
in the forensic record.

## Behavior

1. If `.themis/blind_tdd/active_session.json` does not exist, passthrough.
2. Otherwise, append one JSONL record per tool call to
   `.themis/blind_audit/<session_id>.jsonl`.
3. Always exit 0 — audit logging must never block tool execution.

## Input format

Reads JSON on stdin with the Claude Code PostToolUse hook schema:
```json
{
  "tool_name": "Bash",
  "tool_input": {"command": "pytest tests/foo.py", "description": "run tests"},
  "tool_response": {"output": "...", "exit_code": 0}
}
```

## Output

Audit records written to `.themis/blind_audit/<session_id>.jsonl`:
```json
{
  "timestamp": "2026-04-10T...",
  "session_id": "blind-writer-abc123",
  "agent_role": "test_writer",
  "task_id": "task-42",
  "tool_name": "Bash",
  "tool_input_summary": {"command": "pytest tests/foo.py"},
  "tool_response_summary": {"exit_code": 0},
  "source": "posttooluse_hook"
}
```

Tool inputs/responses are SUMMARIZED (not copied verbatim) to keep the
audit log small and redact any large content that might reveal implementation
details. Only structural metadata is recorded.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _repo_root() -> Path:
    return Path.cwd()


def _load_session() -> dict | None:
    p = _repo_root() / ".themis" / "blind_tdd" / "active_session.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _summarize_input(tool_name: str, tool_input: dict) -> dict:
    """Extract structural metadata from tool input without copying content."""
    if not isinstance(tool_input, dict):
        return {}
    summary: dict = {}
    # Paths
    for field in ("file_path", "path", "notebook_path", "pattern"):
        if field in tool_input:
            summary[field] = tool_input[field]
    # Bash command (full — we want to see every shell command)
    if tool_name == "Bash" and "command" in tool_input:
        summary["command"] = tool_input["command"]
    # Ranges and flags
    for field in ("offset", "limit", "glob", "type", "output_mode"):
        if field in tool_input:
            summary[field] = tool_input[field]
    # Don't copy: old_string, new_string, content — these could contain large data
    return summary


def _summarize_response(tool_response: dict) -> dict:
    """Extract non-content metadata from tool response."""
    if not isinstance(tool_response, dict):
        return {}
    summary: dict = {}
    if "exit_code" in tool_response:
        summary["exit_code"] = tool_response["exit_code"]
    if "error" in tool_response:
        # Keep first 500 chars of error for debugging
        err = str(tool_response["error"])[:500]
        summary["error"] = err
    # Estimate content size without copying it
    for field in ("output", "content", "stdout", "stderr"):
        if field in tool_response:
            val = tool_response[field]
            if isinstance(val, str):
                summary[f"{field}_length"] = len(val)
    return summary


def main() -> int:
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        return 0

    if not raw.strip():
        return 0

    try:
        hook_data = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    session = _load_session()
    if session is None:
        return 0  # no active blind session, skip audit

    tool_name = hook_data.get("tool_name", "unknown")
    tool_input = hook_data.get("tool_input", {}) or {}
    tool_response = hook_data.get("tool_response", {}) or {}

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session.get("session_id", "unknown"),
        "agent_role": session.get("agent_role"),
        "task_id": session.get("task_id"),
        "tool_name": tool_name,
        "tool_input_summary": _summarize_input(tool_name, tool_input),
        "tool_response_summary": _summarize_response(tool_response),
        "source": "posttooluse_hook",
    }

    audit_dir = _repo_root() / ".themis" / "blind_audit"
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        session_id = session.get("session_id", "unknown")
        audit_file = audit_dir / f"{session_id}.jsonl"
        with audit_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[blind-tdd] audit log write failed: {e}", file=sys.stderr)
        # Never block on audit failure
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
