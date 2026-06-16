#!/usr/bin/env python3
"""PreToolUse hook for blind TDD path enforcement.

This is Layer 1 of the three-layer defense from the Blind TDD RFC (OQ1).
Installed as a PreToolUse hook on Read/Grep/Glob/Edit/Write/NotebookEdit.

## Behavior

1. If `.themis/blind_tdd/active_session.json` does not exist, passthrough (allow all).
2. If it exists, load the session config with its `allowed_paths` and `blocked_paths`
   patterns (gitignore-style).
3. Extract the target path from the tool input.
4. Check the path against blocked_paths first (deny wins), then allowed_paths
   (whitelist enforcement if set).
5. Exit 0 to allow, exit 2 with stderr to block.

## Active session schema

```json
{
  "session_id": "blind-writer-<uuid>",
  "agent_role": "test_writer | test_runner | arbiter",
  "task_id": "task-42",
  "allowed_paths": ["tests/**", "docs/**", "plan.md", "public_api.md"],
  "blocked_paths": ["src/**", "examples/**", ".git/**"],
  "created_at": "2026-04-10T..."
}
```

If `allowed_paths` is empty, no whitelist enforcement (only blocked_paths apply).
If `blocked_paths` is empty, no blacklist enforcement (only allowed_paths apply).
If both are set, blocked_paths takes precedence.

## Input format

Reads JSON on stdin with the Claude Code PreToolUse hook schema:
```json
{
  "tool_name": "Read",
  "tool_input": {"file_path": "src/foo.py"}
}
```

## Exit codes

- 0: allow the tool call
- 2: block with the stderr message shown to the agent
- Any other nonzero: treated as a hook error, tool call is denied by claude-code

## Cross-platform

Pure stdlib Python 3.9+. Uses pathlib.PurePosixPath for consistent pattern matching
across Windows and POSIX.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


# Tools that take a path argument we need to guard.
# NOTE: Bash is NOT guarded here — we rely on Layer 2 (SDK tool scoping or
# settings.json Bash(...) allow/deny) to prevent shell-based file access.
_GUARDED_TOOLS = {
    "Read", "Grep", "Glob", "Edit", "Write", "NotebookEdit", "NotebookRead",
}

# Mapping of tool name → list of tool_input field names that contain the path.
# Tried in order; first match wins.
_PATH_FIELDS = {
    "Read":         ["file_path", "path"],
    "Edit":         ["file_path"],
    "Write":        ["file_path"],
    "NotebookEdit": ["notebook_path"],
    "NotebookRead": ["notebook_path"],
    "Glob":         ["pattern", "path"],
    "Grep":         ["path", "pattern"],
}


def _repo_root() -> Path:
    """Return the current working directory as the repo root.

    claude-code hooks run in the project's working directory, so CWD is
    the right reference for relative path resolution.
    """
    return Path.cwd()


def _session_file() -> Path:
    return _repo_root() / ".themis" / "blind_tdd" / "active_session.json"


def _load_session() -> dict | None:
    p = _session_file()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # If the session file is corrupt, fail closed: block all guarded
        # tool calls until the session is fixed or removed.
        return {
            "session_id": "corrupt",
            "agent_role": "unknown",
            "allowed_paths": [],
            "blocked_paths": ["**"],
        }


def _normalize_path(raw: str) -> str:
    """Normalize a path to a repo-relative POSIX form for pattern matching."""
    if not raw:
        return ""
    p = Path(raw)
    # Resolve relative to CWD if relative
    try:
        if p.is_absolute():
            rel = p.resolve().relative_to(_repo_root().resolve())
        else:
            rel = p
    except ValueError:
        # Path is outside the repo — treat as absolute, use as-is
        return str(PurePosixPath(*p.parts))
    # Convert to POSIX with forward slashes
    return str(PurePosixPath(*rel.parts))


def _pattern_matches(path: str, pattern: str) -> bool:
    """Check if `path` matches `pattern` using gitignore-ish semantics.

    Supports:
      - Exact path: "plan.md" → matches "plan.md" only
      - Glob: "tests/*.py" → matches one level
      - Recursive: "src/**" → matches src/ and anything underneath
      - Double-star anywhere: "**/test_*.py"
    """
    if not pattern or not path:
        return False

    # Exact match
    if path == pattern:
        return True

    # Directory prefix: "src/**" or "src/"
    if pattern.endswith("/**") or pattern.endswith("/"):
        prefix = pattern.rstrip("/").rstrip("*").rstrip("/")
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            return True

    # "src" alone should match src/ and src/**
    if "/" not in pattern and "*" not in pattern:
        if path == pattern or path.startswith(pattern + "/"):
            return True

    # Convert ** to a placeholder, use fnmatch for the rest
    # fnmatch doesn't know **, so we handle it manually
    if "**" in pattern:
        # Replace ** with a sentinel, then use regex
        import re
        regex_parts = []
        i = 0
        while i < len(pattern):
            if pattern[i:i+2] == "**":
                regex_parts.append(".*")
                i += 2
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
            elif pattern[i] == "*":
                regex_parts.append("[^/]*")
                i += 1
            elif pattern[i] == "?":
                regex_parts.append("[^/]")
                i += 1
            elif pattern[i] == ".":
                regex_parts.append(r"\.")
                i += 1
            else:
                regex_parts.append(re.escape(pattern[i]))
                i += 1
        regex = "^" + "".join(regex_parts) + "$"
        return bool(re.match(regex, path))

    # Plain fnmatch for single-segment globs
    return fnmatch.fnmatchcase(path, pattern)


def _check_path(path: str, session: dict) -> tuple[bool, str]:
    """Return (allowed, reason).

    Order:
      1. If blocked_paths matches → deny (deny wins).
      2. If allowed_paths is non-empty and path matches → allow.
      3. If allowed_paths is non-empty and path doesn't match → deny.
      4. If allowed_paths is empty → allow (blacklist-only mode).
    """
    blocked = session.get("blocked_paths") or []
    allowed = session.get("allowed_paths") or []

    for pattern in blocked:
        if _pattern_matches(path, pattern):
            return False, f"path matches blocked pattern {pattern!r}"

    if allowed:
        for pattern in allowed:
            if _pattern_matches(path, pattern):
                return True, f"path matches allowed pattern {pattern!r}"
        return False, "path not in allowed_paths whitelist"

    return True, "no whitelist restrictions"


def _extract_path(tool_name: str, tool_input: dict) -> str | None:
    """Pull the path argument out of a tool_input dict."""
    fields = _PATH_FIELDS.get(tool_name, ["file_path", "path", "pattern"])
    for f in fields:
        if f in tool_input and isinstance(tool_input[f], str):
            return tool_input[f]
    return None


# Tools that can modify a file. Used by the BT1 locked-test write rule.
_WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}


def _locked_test_files() -> set[str]:
    """Repo-relative POSIX paths of every test file hash-locked by a red phase.

    Read from `.themis/blind_tdd/red_state/*.json` (written by
    gate_integration.save_red_state). Empty set when no red phase is pending.
    """
    red_dir = _repo_root() / ".themis" / "blind_tdd" / "red_state"
    locked: set[str] = set()
    if not red_dir.is_dir():
        return locked
    for state_file in red_dir.glob("*.json"):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for rel in (state.get("test_file_hashes") or {}):
            # red_state keys may use the host's native separator (backslashes
            # on Windows). Normalize to POSIX so they match the forward-slash
            # form produced by _normalize_path for incoming tool-call paths —
            # otherwise the deny-wins rule silently no-ops on Windows.
            locked.add(str(PurePosixPath(str(rel).replace("\\", "/"))))
    return locked


def _write_tamper_attempt(session: dict, tool_name: str, path: str,
                          reason: str) -> None:
    """Append a BT1 tamper-attempt record for a denied touch of a locked test.

    Stdlib-only on purpose — this hook cannot import project tools. The records
    are folded into the observation stream by pre_commit_gate.py at the next
    commit attempt and the file is then truncated.
    """
    out_dir = _repo_root() / ".themis" / "blind_tdd"
    out_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session.get("session_id"),
        "agent_role": session.get("agent_role"),
        "task_id": session.get("task_id"),
        "tool_name": tool_name,
        "path": path,
        "reason": reason,
        "source": "path-guard-deny",
    }
    try:
        with (out_dir / "tamper_attempts.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        print("[blind-tdd] warning: could not write tamper_attempts.jsonl",
              file=sys.stderr)


def _write_audit_attempt(session: dict, tool_name: str, path: str,
                         allowed: bool, reason: str) -> None:
    """Append a line to the blind audit log (Layer 3)."""
    audit_dir = _repo_root() / ".themis" / "blind_audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    session_id = session.get("session_id", "unknown")
    audit_file = audit_dir / f"{session_id}.jsonl"
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "agent_role": session.get("agent_role"),
        "task_id": session.get("task_id"),
        "tool_name": tool_name,
        "path": path,
        "allowed": allowed,
        "reason": reason,
        "source": "pretooluse_hook",
    }
    try:
        with audit_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # Audit failure should not block the tool call; log to stderr and continue.
        print(f"[blind-tdd] warning: could not write audit log {audit_file}",
              file=sys.stderr)


def main() -> int:
    # Read hook input from stdin
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError) as e:
        print(f"[blind-tdd] hook error reading stdin: {e}", file=sys.stderr)
        return 0  # fail open — don't block due to hook bugs

    if not raw.strip():
        return 0  # no input, nothing to guard

    try:
        hook_data = json.loads(raw)
    except json.JSONDecodeError:
        # Not valid JSON → not a hook we understand → allow
        return 0

    tool_name = hook_data.get("tool_name", "")
    tool_input = hook_data.get("tool_input", {}) or {}

    if tool_name not in _GUARDED_TOOLS:
        return 0

    session = _load_session()
    if session is None:
        # No active blind session → passthrough
        return 0

    path_raw = _extract_path(tool_name, tool_input)
    if not path_raw:
        # Tool call doesn't carry a path → allow (e.g. Grep with no --path)
        return 0

    path_norm = _normalize_path(path_raw)
    allowed, reason = _check_path(path_norm, session)

    # BT1: a hash-locked test file must never be modified by a blind agent
    # other than the test writer (the writer legitimately edits tests when a
    # red phase resumes after an arbiter ruling, and is re-hashed afterward).
    # This deny wins even when allowed_paths (e.g. the runner's `tests/**`)
    # would have permitted the write.
    locked: set[str] | None = None
    if (allowed and tool_name in _WRITE_TOOLS
            and session.get("agent_role") != "test_writer"):
        locked = _locked_test_files()
        if path_norm in locked:
            allowed = False
            reason = "write to hash-locked test file (red_state)"

    _write_audit_attempt(session, tool_name, path_norm, allowed, reason)

    if allowed:
        return 0

    # Denied touches of locked test files are tamper attempts — record them
    # for the observation stream (folded in by pre_commit_gate at commit).
    if locked is None:
        locked = _locked_test_files()
    if path_norm in locked:
        _write_tamper_attempt(session, tool_name, path_norm, reason)

    # Block the tool call
    session_id = session.get("session_id", "?")
    agent_role = session.get("agent_role", "?")
    msg = (
        f"[blind-tdd] BLOCKED: {tool_name} access to {path_norm!r} "
        f"denied by blind-TDD policy ({reason}). "
        f"Session: {session_id}, role: {agent_role}. "
        f"Blind agents must not read implementation code; rely only on the "
        f"task spec, acceptance_criteria, public_api.md, and external docs."
    )
    print(msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
