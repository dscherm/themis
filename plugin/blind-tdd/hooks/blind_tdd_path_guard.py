#!/usr/bin/env python3
"""PreToolUse hook for blind TDD path enforcement.

This is Layer 1 of the three-layer defense from the Blind TDD RFC (OQ1).
Installed as a PreToolUse hook on Read/Grep/Glob/Edit/Write/NotebookEdit/
NotebookRead, and separately on `mcp__.*`.

## MCP tools are denied outright while a session is armed

An MCP server is a second filesystem and a second shell the built-in tools
never see: desktop-commander's `read_file` and `start_process` reach `src/`
as easily as `Read` and `Bash` do. Found 2026-10-24 while dispatching the OS-1
blind writer -- the audit log showed it did not use them, but only its brief
asked it not to.

There is no path to check: every server names its arguments differently, and
some take a command rather than a path. A guard that guessed which argument is
the path would guess in the allowing direction for every server it did not
know. So while a session is armed, every `mcp__*` call is denied for every role,
regardless of `scope_root`, and audited. No session armed => passthrough, as
for every other tool. The cost is that the orchestrator loses its MCP tools
for the life of the session too, since nothing in the payload tells the two
apart -- the same limit the Bash guard documents.

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

Two optional fields extend the schema (TD140):

  "impl_paths": ["core/schemas.py"],   # read by the BASH guard, not this one
  "active": false                      # an explicit off switch, see below

A session file whose JSON is `{}`, empty, or carries `"active": false` counts
as NO session at all — that is how a blind session is ended. The file itself is
always readable and writable regardless of `allowed_paths`/`blocked_paths`, so
whoever armed the session can always end it (TD140 AC-1).

A third extends it (TD144):

  "scope_root": "."                    # the tree this session governs

## scope_root — the session governs one tree, not the whole machine

Without it an armed session is EXCLUSIVE, and not on purpose. `allowed_paths`
is a whitelist: non-empty means anything not matching is denied. A path in a
DIFFERENT repository matches nothing, so on 2026-08-19 an agent working in
`mod-my-week-platform` was blocked from its own files by a session armed here,
and its reads were filed under this session's audit log as well.

`scope_root` names the tree the session is about, relative to the repo root
(`"."` for the whole repo) or absolute. A path resolving OUTSIDE that tree is
not this session's business: it passes through untouched and is not audited
under this session. A path INSIDE is checked exactly as before.

This works without any identity claim, which is why it is implementable where
TD140's per-agent scoping was not: nothing in the PreToolUse payload separates
a subagent from its orchestrator, but every guarded tool call names the path it
is about, and the tool contract guarantees it touches that path and no other.

When `scope_root` is absent the guard behaves exactly as it did before the
field existed — fail closed, every path in scope. Existing sessions are not
silently widened.

### What this does NOT fix

Two writers in the SAME repository remain unsafe, and this change must never be
described as if it made them safe. That case is still one-at-a-time, for the
git-index reason in
`lessons/parallel-agents-in-one-working-tree-race-the-git-index.md`. What
`scope_root` removes is the accidental cost to agents working in a DIFFERENT
tree, who were never the ones this control was protecting anything from.

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
import re
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


# Project root for this invocation. None until the payload is read, or when
# the hook is invoked outside Claude Code (direct run, tests).
_ROOT: Path | None = None


def _set_root_from_payload(payload: object) -> None:
    """Adopt the `cwd` Claude Code sent in the hook payload.

    Claude Code reports the session's working directory in every payload.
    Trusting the hook PROCESS's cwd instead is a silent-corruption bug: state
    lands wherever the shell happened to be, and nothing reports the mistake.
    That is not hypothetical -- ralph-universal accumulated a stray
    tools/.schermness/ (handoff.md, memories.md, bash_telemetry.jsonl,
    reflection_state.json) on 2026-07-16 from hooks that ran with cwd=tools/.

    Falls back to process cwd when the payload has no usable `cwd`, so direct
    invocation and tests keep working unchanged.
    """
    global _ROOT
    if not isinstance(payload, dict):
        return
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return
    try:
        candidate = Path(cwd)
        if candidate.is_dir():
            _ROOT = candidate
    except (OSError, ValueError):
        pass


def _git_root(start: Path) -> Path:
    """Nearest ancestor holding `.git`, else `start` unchanged.

    `.exists()` rather than `.is_dir()`: in a worktree or submodule `.git` is
    a FILE containing a gitdir pointer, and treating that as "not a repo"
    would walk straight past the root it was looking for.
    """
    try:
        start = start.resolve()
    except OSError:
        return start
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def _repo_root() -> Path:
    """Project root: the git root at or above the session cwd.

    Anchoring to the git root, not to the cwd itself. The cwd is where the
    session happens to be standing, which is not the same thing: `cd tools`
    inside this repo made hooks write state to `tools/.schermness/` -- four files
    that then got committed. Trusting the payload cwd (2026-07-16) fixed
    hooks running from an unrelated directory; it does not fix a cwd that is
    a genuine SUBDIRECTORY of the project, which is the common case.

    Worse for the guards than for telemetry: a guard resolving to the wrong
    root reads no active session and fails OPEN, silently.

    Falls back to the unanchored path outside a repo, so tests and direct
    invocation behave as before.
    """
    return _git_root(_ROOT if _ROOT is not None else Path.cwd())


# Repo-relative POSIX path of the file that arms — and disarms — a session.
_SESSION_RELPATH = ".themis/blind_tdd/active_session.json"


def _session_file() -> Path:
    return _repo_root() / _SESSION_RELPATH


def _load_session() -> dict | None:
    p = _session_file()
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return _corrupt_session()
    if not raw.strip():
        # An emptied file is a disarmed session, not a corrupt one. Writing
        # the file empty is one of the two ways to end a session; see
        # `_SESSION_RELPATH` handling in `_decide`.
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # If the session file is corrupt, fail closed: block all guarded
        # tool calls until the session is fixed or removed.
        return _corrupt_session()
    if not isinstance(data, dict) or not data:
        return None
    if data.get("active") is False:
        return None
    return data


def _corrupt_session() -> dict:
    return {
        "session_id": "corrupt",
        "agent_role": "unknown",
        "allowed_paths": [],
        "blocked_paths": ["**"],
    }


def _scope_root(session: dict) -> Path | None:
    """Absolute root of the tree this session governs, or None if unscoped.

    None means "every path is this session's business", which is how the guard
    behaved before `scope_root` existed and how it still behaves for a session
    that does not declare one (TD144 AC-3). A `scope_root` the guard cannot
    resolve is treated the same way — an unreadable declaration must widen the
    guard's reach, never narrow it.
    """
    raw = session.get("scope_root")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        p = Path(raw.strip())
        if not p.is_absolute():
            p = _repo_root() / p
        return p.resolve()
    except (OSError, ValueError):
        return None


def _in_scope(raw: str, scope: Path | None) -> bool:
    """True when `raw` names a path inside the governed tree.

    Relative paths resolve against the repo root, which is where Claude Code's
    own tool calls resolve them. A path the guard cannot place at all counts as
    IN scope: the whole point of scoping is to stop denying calls the session
    provably has no stake in, and "I could not tell" is not proof.
    """
    if scope is None or not raw:
        return True
    try:
        p = Path(raw)
        if not p.is_absolute():
            p = _repo_root() / p
        p = p.resolve()
    except (OSError, ValueError):
        return True
    try:
        p.relative_to(scope)
    except ValueError:
        return False
    return True


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


# Bounds on the Grep leak scan (see `_grep_leak_sources`). A scope the scan
# cannot cover is reported as unscannable, and an unscannable scope is denied.
_LEAK_SCAN_MAX_FILES = 4000
_LEAK_SCAN_MAX_BYTES = 2_000_000
_LEAK_SCAN_SKIP_DIRS = {
    ".git", ".hg", "node_modules", ".venv", "venv", "env", "dist", "build",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next",
}


def _grep_leak_sources(tool_input: dict, session: dict) -> list[str] | None:
    """Blocked files inside a Grep's scope whose CONTENT matches its pattern.

    The guard used to check only the Grep's `path` argument, so a search
    scoped to an allowed directory happily returned matching lines out of the
    blocked files sitting inside it. On 2026-08-18 the TD125 verifier grepped
    `tests/` for `ValidationError` and read back two assertion strings from
    `tests/test_lesson_spans.py` — the one file it was forbidden to open.
    Denying the invocation by directory was never the same thing as keeping
    the file's lines out of the results.

    Denying every Grep whose scope merely *contains* a blocked file would
    block the writer's legitimate `tests/**` searches, and that friction is
    what got this control switched off once already (2026-08-17). So the scan
    asks the narrower question the leak actually turns on — would any blocked
    file have contributed a line? — and only then denies. The blocked lines
    are absent from the results because ripgrep never runs.

    Returns the blocked files that would have matched (empty list = no leak
    possible), or None when the scope could not be scanned reliably; an
    unreadable scope is not a safe one, so the caller denies.
    """
    pattern = tool_input.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return []

    flags = re.MULTILINE
    if tool_input.get("-i"):
        flags |= re.IGNORECASE
    try:
        rx = re.compile(pattern, flags)
    except re.error:
        # ripgrep's dialect is not Python's. A pattern this guard cannot
        # reason about is one it cannot clear.
        return None

    root = _repo_root()
    raw_scope = tool_input.get("path")
    if isinstance(raw_scope, str) and raw_scope.strip():
        scope = Path(raw_scope)
        if not scope.is_absolute():
            scope = root / scope
    else:
        scope = root

    try:
        if scope.is_file():
            candidates = [scope]
        elif scope.is_dir():
            candidates = _scan_candidates(scope)
            if candidates is None:
                return None
        else:
            return []
    except OSError:
        return None

    scope = _scope_root(session)
    hits: list[str] = []
    for f in candidates:
        # A file the session does not govern cannot be a leak from it, and
        # counting it as one would deny a Grep over a tree this session has no
        # opinion about.
        if not _in_scope(str(f), scope):
            continue
        rel = _normalize_path(str(f))
        ok, _ = _check_path(rel, session)
        if ok:
            continue
        try:
            if f.stat().st_size > _LEAK_SCAN_MAX_BYTES:
                return None
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        if rx.search(text):
            hits.append(rel)
    return hits


def _scan_candidates(scope: Path) -> list[Path] | None:
    """Files under `scope`, or None if there are more than the scan bounds."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(scope):
        dirnames[:] = [d for d in dirnames if d not in _LEAK_SCAN_SKIP_DIRS]
        for name in filenames:
            found.append(Path(dirpath) / name)
            if len(found) > _LEAK_SCAN_MAX_FILES:
                return None
    return found


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
                          reason: str, source: str = "path-guard-deny") -> None:
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
        "source": source,
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
        _set_root_from_payload(hook_data)
    except json.JSONDecodeError:
        # Not valid JSON → not a hook we understand → allow
        return 0

    tool_name = hook_data.get("tool_name", "")
    tool_input = hook_data.get("tool_input", {}) or {}

    if tool_name not in _GUARDED_TOOLS and not _is_mcp_tool(tool_name):
        return 0

    session = _load_session()
    if session is None:
        # No active blind session → passthrough
        return 0

    # A session IS active from here, so the failure direction inverts.
    #
    # Above this line the hook fails OPEN: with no session there is nothing to
    # protect, and a bug in the guard must not wedge every file read in every
    # enrolled project. Below it, failing open means a blind agent reads `src/`
    # -- which is the one thing this guard exists to prevent, and the gate has
    # no other way to notice. An unhandled raise here used to exit 1, and
    # Claude Code treats any non-2 exit as a NON-blocking error: the tool call
    # proceeds. A crash silently became permission.
    #
    # `_load_session` already applies the same rule to its own branch: a
    # session file that exists but will not parse yields blocked_paths ["**"]
    # rather than None.
    try:
        if _is_mcp_tool(tool_name):
            return _deny_mcp(tool_name, session)
        return _decide(tool_name, tool_input, session)
    except Exception as e:  # noqa: BLE001 -- deliberate catch-all, see above
        print(
            f"[blind-tdd] BLOCKED: guard raised {type(e).__name__}: {e} while a "
            f"blind-TDD session is active (session "
            f"{session.get('session_id', '?')}, role "
            f"{session.get('agent_role', '?')}). Blocking rather than allowing "
            f"-- a guard that cannot decide must not grant access. Fix the "
            f"guard or end the session.",
            file=sys.stderr,
        )
        return 2


def _is_mcp_tool(tool_name: object) -> bool:
    return isinstance(tool_name, str) and tool_name.startswith("mcp__")


def _deny_mcp(tool_name: str, session: dict) -> int:
    """Block (2) any MCP tool call against an ACTIVE blind session.

    See "MCP tools are denied outright" in the module docstring.
    """
    reason = ("MCP tools are denied while a blind-TDD session is armed; an MCP "
              "server can read or run anything, and its arguments name no path "
              "this guard can check")
    _write_audit_attempt(session, tool_name, "", False, reason)
    print(
        f"[blind-tdd] BLOCKED: {tool_name}: {reason}. Session "
        f"{session.get('session_id', '?')}, role "
        f"{session.get('agent_role', '?')}. Use Read/Grep/Glob for allowed "
        f"paths.",
        file=sys.stderr,
    )
    return 2


def _decide(tool_name: str, tool_input: dict, session: dict) -> int:
    """Allow (0) or block (2) one tool call against an ACTIVE blind session.

    Split out of main() so the caller can convert any unexpected failure into
    a block. Every `return 0` below is a deliberate allow.
    """
    path_raw = _extract_path(tool_name, tool_input)
    if not path_raw:
        # Tool call doesn't carry a path → allow (e.g. Grep with no --path)
        return 0

    # TD144: a path outside the tree this session declared is not this
    # session's business. Returning before the audit write is deliberate --
    # filing another project's work under this session is what made
    # .themis/blind_audit/unknown.jsonl unreadable (AC-5).
    if not _in_scope(path_raw, _scope_root(session)):
        return 0

    path_norm = _normalize_path(path_raw)

    # TD140 AC-1: the session file that arms this guard is ALWAYS reachable.
    #
    # The session is project-global and carries a single `agent_role`, so the
    # guard cannot tell the orchestrator that armed it from the blind agent it
    # constrains — nothing in the PreToolUse payload separates them. Without
    # this bypass the off switch sits behind the switch it turns off: on
    # 2026-08-18 a hand-armed session (whose allowed_paths did not include
    # `.themis/**`) made the session file unwritable to every actor, Bash was
    # denied by role, and the only way out was a human running `rm`. That trap
    # is almost certainly why nobody armed a session for months, while every
    # staged brief kept promising the hook was enforcing.
    #
    # The cost is that a blind agent can disarm itself. That is deliberate and
    # bounded: the write is recorded in the Layer-3 audit log AND appended to
    # tamper_attempts.jsonl, which pre_commit_gate folds into the observation
    # stream — so a self-disarm is loud, not silent. An unrecoverable session
    # is the worse failure, because its cure is to stop arming sessions at all.
    if path_norm == _SESSION_RELPATH:
        _write_audit_attempt(
            session, tool_name, path_norm, True,
            "blind-TDD session file is always reachable so the session can be ended",
        )
        if tool_name in _WRITE_TOOLS:
            _write_tamper_attempt(
                session, tool_name, path_norm,
                "wrote the blind-TDD session file while a session was active",
                source="path-guard-session-write",
            )
        return 0

    allowed, reason = _check_path(path_norm, session)

    # TD140 AC-4: a Grep scoped to an allowed directory must not return lines
    # out of the blocked files inside it. See `_grep_leak_sources`.
    if allowed and tool_name == "Grep":
        leaks = _grep_leak_sources(tool_input, session)
        if leaks is None:
            allowed = False
            reason = ("Grep scope could not be scanned for blocked files; "
                      "narrow the search to a specific file or directory")
        elif leaks:
            allowed = False
            reason = ("Grep results would have included lines from blocked "
                      "file(s): " + ", ".join(sorted(leaks)[:5]))

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
