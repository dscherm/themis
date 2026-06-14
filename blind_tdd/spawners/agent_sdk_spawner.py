"""AgentSdkSpawner — spawn blind agents via the Claude Agent SDK.

Implements the `AgentSpawner` protocol from `orchestrator.py` for
standalone (non-Claude-Code) automation. Use this when:

  - Running the blind-TDD gate from a shell script or CI runner
  - Integrating into a long-lived agent process that's not itself
    a Claude Code session
  - You want programmatic control over the agent lifecycle without
    spawning a subprocess

For interactive Claude Code sessions, `ClaudeCodeSpawner` or the
`Agent` tool is usually more convenient. This module is the
SDK-based alternative listed as A2 in the blind-TDD RFC.

## Dependency

`claude_agent_sdk` is imported lazily — the module loads even when
the SDK isn't installed. `spawn()` raises `ImportError` with a
helpful message if the SDK isn't available at call time.

    pip install claude-agent-sdk

## Blindness enforcement (three layers)

The SDK supports scoping tools at spawn time via `AgentDefinition`,
which covers Layer 2 of the three-layer defense. We also install the
path-guard and audit hooks via the SDK's `hooks` option (Layer 1 +
Layer 3) so the enforcement matches `ClaudeCodeSpawner`'s guarantees.

## Synchronous shim

The Agent SDK's `query()` returns an async iterator, but the
`AgentSpawner.spawn()` protocol is synchronous. `spawn()` wraps the
async call with `asyncio.run()` internally, so callers see a normal
blocking invocation.

## Output verification

Same as `ClaudeCodeSpawner`: after the SDK call returns, we verify
the expected output file exists (triage report, green report, or
ruling) based on role and task id.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Role → tool scoping (Layer 2 of the three-layer defense)
# ---------------------------------------------------------------------------

# Writer: Read for context files, Grep/Glob for test discovery, Write/Edit
# for new test files. NO Bash — runners only.
_WRITER_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Write", "Edit", "WebFetch"]

# Runner: Read for test files + configs, Bash restricted to pytest etc.
# (enforced via settings or Layer 3), no Write/Edit (tests are read-only).
_RUNNER_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash"]

# Arbiter: Read-only judge. No Write, no Bash.
_ARBITER_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "WebFetch"]

_ROLE_TO_TOOLS = {
    "test_writer": _WRITER_ALLOWED_TOOLS,
    "test_runner": _RUNNER_ALLOWED_TOOLS,
    "arbiter": _ARBITER_ALLOWED_TOOLS,
}


@dataclass
class SpawnDiagnostics:
    """Diagnostic info about one SDK spawn attempt."""
    role: str = ""
    tools_allowed: list[str] = field(default_factory=list)
    message_count: int = 0
    sdk_error: str = ""
    duration_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Spawner
# ---------------------------------------------------------------------------

class AgentSdkSpawner:
    """Spawns blind agents via `claude_agent_sdk.query()`.

    Args:
        sdk: optional pre-imported `claude_agent_sdk` module. If None,
            the SDK is imported lazily on first `spawn()` call. Tests
            inject a mock by passing a fake module here.
        project_root: cwd the SDK call should use. Defaults to current.
        model: optional model override (e.g. "claude-opus-4-6").
        permission_mode: one of the SDK's permission modes. Defaults
            to "acceptEdits" so the blind agent can write test files.
        install_hooks: if True, the blind-TDD path guard and audit
            hook scripts are registered via the SDK's `hooks` option.
        themis_home: path to the Themis checkout (for locating hook
            template scripts). Defaults to the THEMIS_HOME env var (or legacy
            RALPH_HOME), falling back to the module's computed location.
            `ralph_home` is accepted as a deprecated alias.
    """

    def __init__(
        self,
        *,
        sdk: Any | None = None,
        project_root: str | Path | None = None,
        model: str | None = None,
        permission_mode: str = "acceptEdits",
        install_hooks: bool = True,
        themis_home: str | Path | None = None,
        ralph_home: str | Path | None = None,  # deprecated alias for themis_home
    ) -> None:
        self._sdk = sdk
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.model = model
        self.permission_mode = permission_mode
        self.install_hooks = install_hooks
        _home = themis_home if themis_home is not None else ralph_home
        self.themis_home = (
            Path(_home) if _home else _default_themis_home()
        )
        self._last_diagnostics: SpawnDiagnostics | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def spawn(self, *, role: str, prompt_template: str, inputs: dict) -> dict:
        """Spawn a blind agent via the SDK and wait for completion."""
        task_id = inputs.get("task_id", "unknown")
        session_id = inputs.get("session_id", f"{role}-{task_id}")

        if role not in _ROLE_TO_TOOLS:
            return self._failure(
                f"unknown role {role!r}",
                agent_id=session_id,
            )

        diag = SpawnDiagnostics(role=role, tools_allowed=list(_ROLE_TO_TOOLS[role]))
        self._last_diagnostics = diag

        try:
            sdk = self._get_sdk()
        except ImportError as e:
            return self._failure(
                f"claude_agent_sdk not available: {e}. "
                f"Install with `pip install claude-agent-sdk`.",
                agent_id=session_id,
                diagnostics=diag,
            )

        expected_outputs = _expected_outputs_for(role, inputs)
        full_prompt = _build_prompt(prompt_template, inputs, expected_outputs)

        options = self._build_options(sdk, role)

        start = datetime.now(timezone.utc)
        try:
            asyncio.run(self._run_query(sdk, full_prompt, options, diag))
        except Exception as e:  # SDK call errors must not propagate raw
            diag.sdk_error = f"{type(e).__name__}: {e}"
            return self._failure(
                f"SDK query raised: {diag.sdk_error}",
                agent_id=session_id,
                diagnostics=diag,
            )
        finally:
            diag.duration_seconds = (
                datetime.now(timezone.utc) - start
            ).total_seconds()

        # Verify expected output files exist under project_root
        def _resolve(p: str) -> Path:
            return self.project_root / p

        missing = [p for p in expected_outputs if not _resolve(p).exists()]
        if missing:
            return self._failure(
                f"agent did not produce expected output files: {missing}",
                agent_id=session_id,
                diagnostics=diag,
                output_files=[p for p in expected_outputs if _resolve(p).exists()],
            )

        return {
            "success": True,
            "agent_id": session_id,
            "output_files": expected_outputs,
            "diagnostics": _diag_to_dict(diag),
            "timestamp": _now_iso(),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_sdk(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        import claude_agent_sdk  # noqa: F401 — imported lazily
        self._sdk = claude_agent_sdk
        return self._sdk

    def _build_options(self, sdk: Any, role: str) -> Any:
        """Construct the SDK's ClaudeAgentOptions / AgentDefinition.

        The SDK's actual class names may evolve. We defer to whatever
        the installed version exposes, passing keyword arguments that
        match the published API circa v0.x. If a future SDK changes
        the shape, update the mapping here.
        """
        allowed_tools = list(_ROLE_TO_TOOLS[role])

        # Try multiple SDK API shapes. The stable one is
        # `ClaudeAgentOptions(allowed_tools=..., cwd=..., permission_mode=...)`.
        options_kwargs: dict[str, Any] = {
            "allowed_tools": allowed_tools,
            "cwd": str(self.project_root),
            "permission_mode": self.permission_mode,
        }
        if self.model:
            options_kwargs["model"] = self.model

        if self.install_hooks:
            options_kwargs["hooks"] = self._build_hook_config()

        # Preferred constructor
        if hasattr(sdk, "ClaudeAgentOptions"):
            return sdk.ClaudeAgentOptions(**options_kwargs)
        # Fallback: some SDK versions use a plain dict
        return options_kwargs

    def _build_hook_config(self) -> dict:
        """Return a dict mapping hook events to command strings.

        Mirrors the shape of `.claude/settings.local.json` so the
        path guard and audit hooks fire for every tool call.
        """
        hook_dir = self.themis_home / "templates" / "hooks"
        path_guard = str(hook_dir / "blind_tdd_path_guard.py")
        audit = str(hook_dir / "blind_tdd_audit.py")
        py = sys.executable
        return {
            "PreToolUse": [
                {
                    "matcher": "Read|Grep|Glob|Edit|Write|NotebookEdit|NotebookRead",
                    "hooks": [
                        {"type": "command", "command": f"{py} {path_guard}"}
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": f"{py} {audit}"}
                    ],
                }
            ],
        }

    async def _run_query(
        self,
        sdk: Any,
        prompt: str,
        options: Any,
        diag: SpawnDiagnostics,
    ) -> None:
        """Run the SDK query and drain the message stream.

        The SDK's `query()` returns an async iterator of messages. We
        don't parse them — the agent's side effects (test files,
        triage report) are what matter. We just need to drive the
        stream to completion.
        """
        query_fn = getattr(sdk, "query", None)
        if query_fn is None:
            raise AttributeError(
                "claude_agent_sdk does not expose `query()` — "
                "incompatible SDK version"
            )

        result = query_fn(prompt=prompt, options=options)

        # Support both async iterators and awaitables-that-return-iterators
        if hasattr(result, "__aiter__"):
            async for _msg in result:
                diag.message_count += 1
        elif hasattr(result, "__await__"):
            awaited = await result
            if hasattr(awaited, "__aiter__"):
                async for _msg in awaited:
                    diag.message_count += 1
            elif hasattr(awaited, "__iter__"):
                for _msg in awaited:
                    diag.message_count += 1
        elif hasattr(result, "__iter__"):
            for _msg in result:
                diag.message_count += 1
        else:
            raise RuntimeError(
                f"unexpected return type from sdk.query(): {type(result).__name__}"
            )

    def _failure(
        self,
        error: str,
        *,
        agent_id: str,
        diagnostics: SpawnDiagnostics | None = None,
        output_files: list[str] | None = None,
    ) -> dict:
        return {
            "success": False,
            "agent_id": agent_id,
            "error": error,
            "output_files": output_files or [],
            "diagnostics": _diag_to_dict(diagnostics) if diagnostics else None,
            "timestamp": _now_iso(),
        }


# ---------------------------------------------------------------------------
# Shared helpers (duplicated from claude_code_spawner so each spawner is
# self-contained — no cross-module coupling)
# ---------------------------------------------------------------------------

def _default_themis_home() -> Path:
    env = os.environ.get("THEMIS_HOME") or os.environ.get("RALPH_HOME")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    return here.parents[2]


def _expected_outputs_for(role: str, inputs: dict) -> list[str]:
    task_id = inputs.get("task_id", "unknown")
    if role == "test_writer":
        return [f".themis/blind_tdd/triage/{task_id}.json"]
    if role == "test_runner":
        return [f".themis/blind_tdd/green_report/{task_id}.json"]
    if role == "arbiter":
        challenge_id = inputs.get("challenge_id", task_id)
        return [f".themis/blind_tdd/rulings/{challenge_id}.json"]
    return []


def _build_prompt(template: str, inputs: dict, expected_outputs: list[str]) -> str:
    context = {
        "task_id": inputs.get("task_id"),
        "session_id": inputs.get("session_id"),
        "task": inputs.get("task"),
        "test_dirs": inputs.get("test_dirs"),
        "red_phase_hashes": inputs.get("red_phase_hashes"),
        "triage_report": inputs.get("triage_report"),
        "challenge_id": inputs.get("challenge_id"),
        "expected_output_files": expected_outputs,
    }
    context = {k: v for k, v in context.items() if v is not None}
    context_json = json.dumps(context, indent=2, ensure_ascii=False)
    return (
        template
        + "\n\n---\n\n"
        + "# Context for this run\n\n"
        + "```json\n"
        + context_json
        + "\n```\n\n"
        + "When done, verify every path in `expected_output_files` exists.\n"
    )


def _diag_to_dict(diag: SpawnDiagnostics | None) -> dict | None:
    if diag is None:
        return None
    return {
        "role": diag.role,
        "tools_allowed": list(diag.tools_allowed),
        "message_count": diag.message_count,
        "sdk_error": diag.sdk_error,
        "duration_seconds": diag.duration_seconds,
    }


# ---------------------------------------------------------------------------
# CLI smoke test: `python -m blind_tdd.spawners.agent_sdk_spawner`
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: agent_sdk_spawner.py <role> <task_id>", file=sys.stderr)
        sys.exit(1)

    role = sys.argv[1]
    task_id = sys.argv[2]

    spawner = AgentSdkSpawner()
    print(f"themis_home:       {spawner.themis_home}")
    print(f"project_root:     {spawner.project_root}")
    print(f"allowed tools:    {_ROLE_TO_TOOLS.get(role, [])}")
    print(f"expected outputs: {_expected_outputs_for(role, {'task_id': task_id})}")
    try:
        spawner._get_sdk()
        print("claude_agent_sdk: installed ✓")
    except ImportError as e:
        print(f"claude_agent_sdk: NOT installed — {e}")
