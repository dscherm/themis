"""ClaudeCodeSpawner — spawn blind agents via `claude -p` subprocess.

Implements the `AgentSpawner` protocol from `orchestrator.py`. Each call to
`spawn()` launches an isolated `claude -p` subprocess with:

  1. Role-appropriate `.claude/settings.local.json` so PreToolUse/PostToolUse
     hooks fire for the child session and its tool list is properly scoped.
  2. The blind-TDD path-guard and audit hook scripts copied into
     `.claude/hooks/` if not already present.
  3. A full prompt built by concatenating the role's prompt template with a
     structured "Context" block containing the task spec, test dirs, session
     id, and expected output file paths.
  4. `--permission-mode acceptEdits` so the child agent can write its outputs
     without being interactively blocked.

After the subprocess returns (or times out), the spawner:

  - Restores the previous `.claude/settings.local.json` (save/restore pattern)
  - Verifies the expected output files exist for the given role
  - Returns the standard spawner result dict

The subprocess is NOT the same Claude Code session as the parent orchestrator.
Each call is a fresh agent with no memory of prior phases — which is exactly
what blind TDD requires. The trade-off is that each spawn consumes API budget
like any other `claude -p` invocation.

## Template resolution

The spawner needs to know where the Themis templates live so it can
copy hook scripts and settings files. This is controlled by `ralph_home` which
defaults to the `RALPH_HOME` env var, then the installed package location.

## Non-goals

- This module does NOT orchestrate the red/green phases — that's the
  orchestrator. It only spawns one agent per call.
- This module does NOT enforce blindness — that's the hooks. It only ensures
  the hooks are wired up before the subprocess starts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def subscription_env(base: dict | None = None, *, strip_api_key: bool = True) -> dict:
    """Build the environment for a `claude -p` blind-agent subprocess.

    When `strip_api_key` is True (the default), `ANTHROPIC_API_KEY` is removed so
    the CLI authenticates via the logged-in Claude Code **subscription** instead
    of billing the Anthropic API per token — the cause of "Credit balance too
    low" spawn failures. Set it False for environments that intend API billing
    (e.g. CI with no subscription login), via `gate.blind_tdd.spawn_auth: api`.

    Note: this is a cost/reliability control, NOT a blindness control — blindness
    is enforced by the PreToolUse path-guard hook regardless of which auth the
    subprocess uses.
    """
    src = dict(os.environ if base is None else base)
    if strip_api_key:
        src.pop("ANTHROPIC_API_KEY", None)
    return src


# ---------------------------------------------------------------------------
# Role → template file mapping
# ---------------------------------------------------------------------------

# Role → settings template filename (in templates/blind_tdd/)
_ROLE_TO_SETTINGS = {
    "test_writer": "settings.blind-writer.json",
    "test_runner": "settings.blind-runner.json",
    "arbiter": "settings.blind-arbiter.json",
}

# Hook scripts that must be present in .claude/hooks/ before spawning.
_HOOK_SCRIPTS = [
    "blind_tdd_path_guard.py",
    "blind_tdd_audit.py",
]


@dataclass
class SpawnDiagnostics:
    """Diagnostic info about a single spawn attempt (for debugging)."""
    cmd: list[str] = field(default_factory=list)
    returncode: int | None = None
    duration_seconds: float = 0.0
    stdout_tail: str = ""
    stderr_tail: str = ""
    settings_backed_up: bool = False
    hooks_installed: list[str] = field(default_factory=list)


class ClaudeCodeSpawner:
    """Spawns blind agents as fresh `claude -p` subprocesses.

    Args:
        themis_home: Path to the Themis checkout (for templates/). Defaults
            to the `THEMIS_HOME` env var (or legacy `RALPH_HOME`), falling back
            to detecting it from this module's own location. `ralph_home` is
            accepted as a deprecated alias.
        claude_binary: Name or path of the `claude` executable.
        timeout_seconds: How long to wait for an agent subprocess to finish.
        project_root: The project the agent should run in. Defaults to cwd.
        settings_path: Where to write the temporary role-specific settings.
        hooks_dir: Where to install the hook scripts in the project.
    """

    def __init__(
        self,
        *,
        themis_home: str | Path | None = None,
        claude_binary: str | list[str] = "claude",
        timeout_seconds: int = 1800,
        project_root: str | Path | None = None,
        settings_path: str = ".claude/settings.local.json",
        hooks_dir: str = ".claude/hooks",
        model: str | None = None,
        strip_api_key: bool = True,
        ralph_home: str | Path | None = None,  # deprecated alias for themis_home
    ) -> None:
        _home = themis_home if themis_home is not None else ralph_home
        self.themis_home = Path(_home) if _home else _default_themis_home()
        self.claude_binary = claude_binary
        self.timeout_seconds = timeout_seconds
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.settings_path = Path(settings_path)
        self.hooks_dir = Path(hooks_dir)
        self.model = model
        # When True (default), strip ANTHROPIC_API_KEY from the child env so the
        # blind-agent `claude -p` runs on the subscription, not API billing.
        self.strip_api_key = strip_api_key
        self._last_diagnostics: SpawnDiagnostics | None = None

    # ------------------------------------------------------------------
    # Public interface (matches AgentSpawner Protocol)
    # ------------------------------------------------------------------

    def spawn(self, *, role: str, prompt_template: str, inputs: dict) -> dict:
        """Spawn a blind agent via `claude -p` and wait for completion.

        Returns a dict with keys:
            success: bool
            output_files: list[str] — paths the agent was expected to write
            agent_id: str (the session id passed into inputs, for traceability)
            error: str (present on failure)
            diagnostics: SpawnDiagnostics dict (for debugging)
        """
        task_id = inputs.get("task_id", "unknown")
        session_id = inputs.get("session_id", f"{role}-{task_id}")

        settings_template = self._resolve_settings_template(role)
        if not settings_template.exists():
            return self._failure(
                f"settings template not found for role {role!r}: {settings_template}",
                agent_id=session_id,
            )

        expected_outputs = _expected_outputs_for(role, inputs)

        diag = SpawnDiagnostics()
        self._last_diagnostics = diag

        # 1. Install hook scripts into .claude/hooks/
        try:
            diag.hooks_installed = self._install_hooks()
        except OSError as e:
            return self._failure(
                f"failed to install hook scripts: {e}",
                agent_id=session_id,
                diagnostics=diag,
            )

        # 2. Back up the existing settings.local.json (if any), then write
        #    the role-specific settings in its place.
        backup_path, had_existing = self._backup_settings()
        diag.settings_backed_up = backup_path is not None

        try:
            self._install_settings(settings_template)

            # 3. Build the full prompt with context block
            full_prompt = _build_prompt(prompt_template, inputs, expected_outputs)

            # 4. Build the command and run it
            cmd = self._build_cmd()
            diag.cmd = cmd

            start = datetime.now(timezone.utc)
            try:
                completed = subprocess.run(
                    cmd,
                    input=full_prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    cwd=str(self.project_root),
                    encoding="utf-8",
                    errors="replace",
                    env=subscription_env(strip_api_key=self.strip_api_key),
                )
            except subprocess.TimeoutExpired as e:
                diag.returncode = None
                diag.duration_seconds = self.timeout_seconds
                diag.stderr_tail = _tail(e.stderr or "", 2000)
                diag.stdout_tail = _tail(e.stdout or "", 2000)
                return self._failure(
                    f"claude -p timed out after {self.timeout_seconds}s",
                    agent_id=session_id,
                    diagnostics=diag,
                )
            except (OSError, FileNotFoundError) as e:
                return self._failure(
                    f"failed to invoke {self.claude_binary!r}: {e}",
                    agent_id=session_id,
                    diagnostics=diag,
                )

            diag.returncode = completed.returncode
            diag.duration_seconds = (
                datetime.now(timezone.utc) - start
            ).total_seconds()
            diag.stdout_tail = _tail(completed.stdout or "", 2000)
            diag.stderr_tail = _tail(completed.stderr or "", 2000)

            if completed.returncode != 0:
                return self._failure(
                    f"claude -p exited with code {completed.returncode}",
                    agent_id=session_id,
                    diagnostics=diag,
                )

            # 5. Verify expected output files exist (resolved under project_root)
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

        finally:
            # 6. Always restore previous settings
            self._restore_settings(backup_path, had_existing)

    # ------------------------------------------------------------------
    # Template / settings resolution
    # ------------------------------------------------------------------

    def _resolve_settings_template(self, role: str) -> Path:
        filename = _ROLE_TO_SETTINGS.get(role)
        if not filename:
            # Return a nonexistent path; caller checks .exists()
            return self.themis_home / "templates" / "blind_tdd" / f"<unknown-role-{role}>"
        return self.themis_home / "templates" / "blind_tdd" / filename

    def _install_hooks(self) -> list[str]:
        """Copy blind-TDD hook scripts into the project's .claude/hooks/.

        Returns the list of hook script names that were installed (or already
        present). Raises OSError on copy failure.
        """
        src_dir = self.themis_home / "templates" / "hooks"
        dst_dir = self.project_root / self.hooks_dir
        dst_dir.mkdir(parents=True, exist_ok=True)
        installed: list[str] = []
        for script in _HOOK_SCRIPTS:
            src = src_dir / script
            dst = dst_dir / script
            if not src.exists():
                raise OSError(f"hook template missing: {src}")
            # Always overwrite so we stay in sync with template updates.
            shutil.copy2(src, dst)
            installed.append(script)
        return installed

    def _backup_settings(self) -> tuple[Path | None, bool]:
        """Back up the current settings.local.json, if any.

        Returns (backup_path, had_existing_file).
        """
        settings_full = self.project_root / self.settings_path
        if not settings_full.exists():
            return None, False
        backup = settings_full.with_suffix(
            settings_full.suffix + ".blind-tdd-backup"
        )
        shutil.copy2(settings_full, backup)
        return backup, True

    def _install_settings(self, template_path: Path) -> None:
        settings_full = self.project_root / self.settings_path
        settings_full.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template_path, settings_full)

    def _restore_settings(self, backup_path: Path | None, had_existing: bool) -> None:
        settings_full = self.project_root / self.settings_path
        try:
            if had_existing and backup_path is not None and backup_path.exists():
                shutil.move(str(backup_path), str(settings_full))
            else:
                # No prior file — delete the one we installed.
                if settings_full.exists():
                    settings_full.unlink()
        except OSError:
            # Best-effort: if restoration fails we do not want to mask the
            # primary failure reason. A lingering backup file is acceptable.
            pass

    def _build_cmd(self) -> list[str]:
        if isinstance(self.claude_binary, (list, tuple)):
            head = list(self.claude_binary)
        else:
            head = [self.claude_binary]
        cmd = head + [
            "-p",
            "--permission-mode",
            "acceptEdits",
            "--output-format",
            "text",
        ]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
# Module-level helpers
# ---------------------------------------------------------------------------

def _default_themis_home() -> Path:
    """Resolve THEMIS_HOME (or legacy RALPH_HOME) from env, then this module's location."""
    env = os.environ.get("THEMIS_HOME") or os.environ.get("RALPH_HOME")
    if env:
        return Path(env)
    # blind_tdd/spawners/claude_code_spawner.py → repo root
    here = Path(__file__).resolve()
    return here.parents[2]


def _expected_outputs_for(role: str, inputs: dict) -> list[str]:
    """What files should exist after a successful agent run for this role?"""
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
    """Concatenate the role prompt template with a structured context block."""
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
    # Strip keys the role doesn't need (None values) so the prompt stays focused.
    context = {k: v for k, v in context.items() if v is not None}

    context_json = json.dumps(context, indent=2, ensure_ascii=False)

    return (
        template
        + "\n\n---\n\n"
        + "# Context for this run\n\n"
        + "You are being invoked for a specific task. The following JSON block\n"
        + "contains everything you need: the task spec with acceptance criteria,\n"
        + "the test directories to write into, the session id, and the exact\n"
        + "output file paths you must produce before exiting.\n\n"
        + "```json\n"
        + context_json
        + "\n```\n\n"
        + "When you are done, verify that each path in `expected_output_files`\n"
        + "exists on disk. Do NOT emit any other output to stdout — the parent\n"
        + "orchestrator reads your result from those files.\n"
    )


def _tail(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return "...[truncated]...\n" + text[-max_chars:]


def _diag_to_dict(diag: SpawnDiagnostics | None) -> dict | None:
    if diag is None:
        return None
    return {
        "cmd": list(diag.cmd),
        "returncode": diag.returncode,
        "duration_seconds": diag.duration_seconds,
        "stdout_tail": diag.stdout_tail,
        "stderr_tail": diag.stderr_tail,
        "settings_backed_up": diag.settings_backed_up,
        "hooks_installed": list(diag.hooks_installed),
    }


# ---------------------------------------------------------------------------
# CLI smoke test: `python -m blind_tdd.spawners.claude_code_spawner`
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """Smoke test — prints what the spawner would do without running claude.

    Usage:
        python -m blind_tdd.spawners.claude_code_spawner <role> <task_id>
    """
    if len(sys.argv) < 3:
        print("Usage: claude_code_spawner.py <role> <task_id>", file=sys.stderr)
        sys.exit(1)

    role = sys.argv[1]
    task_id = sys.argv[2]

    spawner = ClaudeCodeSpawner()
    print(f"themis_home:      {spawner.themis_home}")
    print(f"project_root:    {spawner.project_root}")
    print(f"settings target: {spawner.project_root / spawner.settings_path}")
    print(f"hooks dir:       {spawner.project_root / spawner.hooks_dir}")
    print(
        f"settings template: {spawner._resolve_settings_template(role)}  "
        f"(exists={spawner._resolve_settings_template(role).exists()})"
    )
    expected = _expected_outputs_for(role, {"task_id": task_id})
    print(f"expected outputs: {expected}")
    print(f"cmd: {spawner._build_cmd()}")
