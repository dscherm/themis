#!/usr/bin/env python3
"""PreToolUse hook for blind TDD shell enforcement (Layer 2, in-session model).

Governs every shell tool Claude Code exposes: `Bash` and `PowerShell`. They are
the same capability -- run a program, read its output -- so they get the same
deny-by-default policy. Registering this hook on `Bash` alone left PowerShell as
an unguarded second shell: found 2026-10-24 while dispatching the OS-1 blind
writer, where nothing but the brief's instructions kept the writer from
`Get-Content src/...`. The settings matcher must name both (`Bash|PowerShell`).

The path-guard hook deliberately does NOT guard Bash (its docstring notes it
relies on "Layer 2: SDK tool scoping or settings.json Bash(...) allow/deny").
That settings-based deny-list worked for the `claude -p` spawner, which swapped
in a role-specific settings.local.json per spawn. The in-session model (blind
agents run as Agent-tool subagents) drops that per-spawn settings swap, so Bash
would be unguarded — a blind runner could `cat src/foo.py` and defeat blindness.

This hook closes that hole. When a blind session is active it enforces, per role:

  - arbiter               : Bash is denied entirely (it has no Bash need).
  - test_runner           : Bash is allowed ONLY for test-runner commands
                            (pytest, npm test, dotnet test, ...). Inspection
                            commands (cat/grep/find/...) and command
                            substitution are blocked. Fail-closed: anything not
                            recognized as a test command is denied.
  - test_writer           : the same test-runner commands, but ONLY while the
                            task's implementation is still absent. See
                            `_impl_state` and `evaluate` for why the two cases
                            differ.

Session schema note (TD140): the writer rule reads an optional `impl_paths`
list off the session — the files the task is going to create. When it is
missing the guard falls back to the glob-free entries of `blocked_paths`, and
if that yields nothing the implementation state is unknown and Bash is denied,
exactly as before.

No active session => passthrough (allow all), exactly like the path guard, so it
is safe to ship always-on in the plugin.

## PowerShell is parsed more strictly than Bash, on purpose

PowerShell evaluates more of its argument text than a POSIX shell does. Beyond
`$(...)`, an argument-mode `( ... )` runs a pipeline (`pytest (gc src/x.py)`),
`@( ... )` and `{ ... }` run code, and `${C:/src/x.py}` is provider syntax that
READS A FILE through variable expansion. So under PowerShell any `$`, `(`, `{`
or `@` in the command is denied, on top of the Bash rules. That costs the
runner `$env:GODOT` and parenthesised `-k` expressions; quoting a `-k` without
parentheses still works. An allowance this guard cannot parse is one it cannot
clear.

## scope_root does NOT apply here, and that is the honest answer (TD144 AC-4)

The path guard scopes by `scope_root`: a path outside the tree the session
declared passes through untouched. This guard deliberately does not, and the
asymmetry is not an oversight or a TODO.

A guarded tool call NAMES the file it is about, and the tool contract
guarantees it touches that file and no other — so "is this call inside the
governed tree?" has an answer the guard can rely on. A Bash command is a
program, and its text does not bound the files it reads. `cd` moves the ground
under every relative path in the next segment; `$HOME`, `$OLDPWD` and any other
variable expand to trees the guard never saw; `git -C`, `--file=`, a wrapper
script, a symlink and a wildcard all reach outside whatever the visible
arguments suggest. A command whose every visible argument sits in another
repository can still open this one by absolute path on its second segment.

So any scope check over a command line would be a GUESS, and it would guess in
the allowing direction — which is the one that matters, because this guard is
the only thing standing between a blind agent and `cat core/schemas.py`. It is
already deny-by-default (only recognized test-runner commands pass), so an
unparsed command is denied rather than waved through; a path-shaped exemption
would convert that into a bypass wearing a safety label. That false assurance
would be worse than the cost it buys.

The cost it declines to buy is real and stays on the table: while a session is
armed here, Bash stays governed for every agent in the session, including one
whose work is in another repository. That is a genuine remaining limit of this
control, not something `scope_root` quietly fixed.

## What none of this fixes

Two writers in the SAME repository remain unsafe — one-at-a-time, for the
git-index reason in
`lessons/parallel-agents-in-one-working-tree-race-the-git-index.md`.

Exit codes: 0 allow, 2 block (stderr shown to the agent). Pure stdlib, 3.9+.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


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


def _session_file() -> Path:
    return _repo_root() / ".themis" / "blind_tdd" / "active_session.json"


def _load_session() -> dict | None:
    p = _session_file()
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        # Corrupt session => fail closed: deny Bash until fixed.
        return {"session_id": "corrupt", "agent_role": "unknown"}
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"session_id": "corrupt", "agent_role": "unknown"}
    # An empty object or an explicit `"active": false` ends the session. The
    # path guard keeps the session file writable for exactly this purpose, so
    # whoever armed a session can always end it (TD140 AC-1); both guards have
    # to agree on what "ended" looks like or Bash would stay denied after the
    # disarm.
    if not isinstance(data, dict) or not data:
        return None
    if data.get("active") is False:
        return None
    return data


# Path prefixes that are never implementation, however they appear in
# `blocked_paths`: the guard's own state and the VCS internals.
_NON_IMPL_PREFIXES = (".themis/", ".git/", ".schermness/")


def _impl_state(session: dict) -> str:
    """'absent', 'present', or 'unknown' for the task's implementation files.

    Prefers the session's explicit `impl_paths`. Falls back to the glob-free
    entries of `blocked_paths` — when a session is armed by hand for one task
    those are the task's own files, which is how the 2026-08-18 TD125 session
    was written — minus the guard-state and VCS paths, which always exist and
    would otherwise report every session as 'present'.
    """
    raw = session.get("impl_paths")
    candidates: list[str] = []
    if isinstance(raw, list):
        candidates = [str(p) for p in raw if isinstance(p, str) and p.strip()]
    if not candidates:
        for p in (session.get("blocked_paths") or []):
            if not isinstance(p, str) or not p.strip():
                continue
            norm = p.replace("\\", "/")
            if any(ch in norm for ch in "*?[") or norm.endswith("/"):
                continue
            if norm.startswith(_NON_IMPL_PREFIXES):
                continue
            candidates.append(norm)
    if not candidates:
        return "unknown"

    root = _repo_root()
    for p in candidates:
        try:
            if (root / p).exists():
                return "present"
        except OSError:
            return "unknown"
    return "absent"


# Test-runner commands a blind runner is allowed to execute. Anchored at the
# start of each shell segment. Kept in sync with settings.blind-runner.json.
_ALLOWED_TEST = [
    r"pytest\b",
    r"python3?\s+-m\s+pytest\b",
    r"py\s+-m\s+pytest\b",
    r"coverage\s+run\b",
    r"python3?\s+-m\s+coverage\b",
    r"tox\b",
    r"npm\s+(run\s+)?test\b",
    r"yarn\s+(run\s+)?test\b",
    r"pnpm\s+(run\s+)?test\b",
    r"jest\b",
    r"vitest\b",
    r"dotnet\s+test\b",
    r"cargo\s+test\b",
    r"go\s+test\b",
    r"mvn\s+test\b",
    r"gradle\s+test\b",
    # GDScript/Godot headless test runs. The target must be a test SCENE
    # (.tscn) or GUT's cmdline runner (gut_cmdln.gd), or a cache-warming
    # --import pass. A bare `-s <arbitrary>.gd` is deliberately NOT allowed:
    # godot executes any GDScript, so an unconstrained `-s` would let the
    # runner read source and defeat its src-blindness. The binary may be
    # `godot`/`godot4`, the `$GODOT`/`${GODOT}` env override, or a quoted
    # absolute path to a Godot .exe; each match stays within one shell segment.
    r'"?(?:godot\w*(?:\.exe)?|\$\{?GODOT\}?|[^"|;&]*[Gg]odot[^"|;&]*\.exe)"?[^|;&]*--headless[^|;&]*(?:\.tscn|gut_cmdln\.gd)\b',
    r'"?(?:godot\w*(?:\.exe)?|\$\{?GODOT\}?|[^"|;&]*[Gg]odot[^"|;&]*\.exe)"?[^|;&]*--headless[^|;&]*--import\b',
]
_ALLOWED_RE = [re.compile(r"^\s*" + p, re.IGNORECASE) for p in _ALLOWED_TEST]

# Split a command line into independently-executed segments.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n|&")


def _segments(command: str) -> list[str]:
    return [s.strip() for s in _SEGMENT_SPLIT.split(command) if s.strip()]


# The shell tools this guard governs. Anything else is not its business.
_SHELL_TOOLS = ("Bash", "PowerShell")

# PowerShell characters that make argument text execute or read a file. See
# "PowerShell is parsed more strictly than Bash" in the module docstring.
_PS_EVAL_CHARS = ("$", "(", "{", "@")


def evaluate(command: str, role: str, impl_state: str = "unknown",
             shell: str = "Bash") -> tuple[bool, str]:
    """Return (allowed, reason) for a shell command under an active blind session.

    `impl_state` is 'absent', 'present' or 'unknown' (see `_impl_state`) and
    only affects the `test_writer` role. `shell` is the tool name, `Bash` or
    `PowerShell`; PowerShell additionally denies its evaluating syntax.
    """
    if shell not in _SHELL_TOOLS:
        return False, f"unknown shell tool {shell!r} during a blind session"

    if role == "arbiter":
        return False, f"role {role!r} may not use {shell} during a blind session"

    if role not in ("test_writer", "test_runner"):
        # Unknown/other role with an active session -> fail closed.
        return False, f"{shell} denied for role {role!r} during a blind session"

    if role == "test_writer":
        # TD140 AC-2/AC-3. The writer's Bash access turns on ONE fact: does the
        # implementation exist yet?
        #
        # It does not (the red phase): running the tests can only produce
        # ImportError/AttributeError, because there is nothing to consult. That
        # is not a leak, it is the point — a test suite that was never executed
        # is indistinguishable from real coverage, and twenty unrunnable tests
        # look exactly like twenty passing contracts until someone runs them.
        #
        # It does (a retrofit, as TD125 was): the same command becomes a side
        # channel INTO the implementation. Failure output carries the rule and
        # its wording — `ValidationError: lesson_end must be set together with
        # lesson_start` hands over the contract the writer was hired to specify
        # independently. A writer that can iterate against real messages until
        # they pass is not specifying a contract, it is reverse-engineering one
        # through stdout, and the blindness the path guard enforces over files
        # is defeated without a single denied Read.
        #
        # A PreToolUse hook can only allow or deny; it cannot strip assertion
        # text out of the output. So the withholding is a denial. Do not
        # "simplify" this to a flat "let the writer run tests" — that erases
        # the distinction the two cases are built on.
        if impl_state == "present":
            return False, (
                "the task's implementation already exists, so test output "
                "would report assertion-level results from it — a side channel "
                "into the code this session is blind to"
            )
        if impl_state != "absent":
            return False, (
                "cannot tell whether the task's implementation exists (no "
                "impl_paths on the session); denying rather than guessing"
            )

    if not command or not command.strip():
        return False, f"empty {shell} command"

    # Command substitution can smuggle a file read into an allowed command.
    if "$(" in command or "`" in command:
        return False, "command substitution ($(...) / backticks) is not allowed"

    if shell == "PowerShell":
        bad = sorted({ch for ch in _PS_EVAL_CHARS if ch in command})
        if bad:
            return False, (
                f"PowerShell syntax {' '.join(bad)} can execute code or read a "
                f"file inside an allowed command; it is not allowed during a "
                f"blind session"
            )

    segments = _segments(command)
    for seg in segments:
        if not any(rx.match(seg) for rx in _ALLOWED_RE):
            return False, (
                f"segment {seg!r} is not an allowed test command; the blind "
                f"runner may run only test-runner commands (pytest, npm test, "
                f"dotnet test, ...)"
            )
    return True, "all segments are allowed test commands"


def _write_audit(session: dict, command: str, allowed: bool, reason: str,
                 tool_name: str = "Bash") -> None:
    audit_dir = _repo_root() / ".themis" / "blind_audit"
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        sid = session.get("session_id", "unknown")
        rec = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": sid,
            "agent_role": session.get("agent_role"),
            "task_id": session.get("task_id"),
            "tool_name": tool_name,
            "command": command[:500],
            "allowed": allowed,
            "reason": reason,
            "source": "pretooluse_hook",
        }
        with (audit_dir / f"{sid}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # audit failure never blocks the call


def main() -> int:
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        return 0  # fail open on hook I/O error
    if not raw.strip():
        return 0
    try:
        hook_data = json.loads(raw)
        _set_root_from_payload(hook_data)
    except json.JSONDecodeError:
        return 0

    if hook_data.get("tool_name") not in _SHELL_TOOLS:
        return 0

    session = _load_session()
    if session is None:
        return 0  # no active blind session => passthrough

    # A session IS active from here, so the failure direction inverts. Above
    # this line the hook fails OPEN (no session, nothing to protect, and a
    # guard bug must not wedge every Bash call in every enrolled project);
    # below it, failing open lets a blind agent read implementation through
    # the shell -- the exact bypass this guard exists to close. An unhandled
    # raise exits 1, and Claude Code treats any non-2 exit as a NON-blocking
    # error, so the command runs. `_load_session` already applies this rule to
    # its own branch: a corrupt session file denies rather than passes through.
    try:
        return _decide(hook_data, session)
    except Exception as e:  # noqa: BLE001 -- deliberate catch-all, see above
        print(
            f"[blind-tdd] BLOCKED {hook_data.get('tool_name')}: guard raised "
            f"{type(e).__name__}: {e} "
            f"while a blind-TDD session is active (session "
            f"{session.get('session_id', '?')}). Blocking rather than allowing "
            f"-- a guard that cannot decide must not grant access.",
            file=sys.stderr,
        )
        return 2


def _decide(hook_data: dict, session: dict) -> int:
    """Allow (0) or block (2) one shell call against an ACTIVE blind session."""
    shell = hook_data.get("tool_name", "Bash")
    command = (hook_data.get("tool_input") or {}).get("command", "") or ""
    role = session.get("agent_role", "unknown")
    # No `scope_root` check here on purpose. A command line does not bound the
    # files it touches, so scoping one would guess in the allowing direction --
    # see the "scope_root does NOT apply here" section of the module docstring.
    allowed, reason = evaluate(command, role, _impl_state(session), shell)
    _write_audit(session, command, allowed, reason, shell)

    if allowed:
        return 0

    print(
        f"[blind-tdd] BLOCKED {shell}: {reason}. Session {session.get('session_id', '?')}, "
        f"role {role}. Blind agents must not inspect implementation via the shell. "
        f"Use the Read tool for allowed paths. Test-runner commands are "
        f"permitted for the test_runner role, and for the test_writer role only "
        f"while the task's implementation does not exist yet.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
