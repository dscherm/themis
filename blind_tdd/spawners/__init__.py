"""Pluggable blind-TDD agent spawners.

Implementations of the `AgentSpawner` protocol from `orchestrator.py`.

- `ClaudeCodeSpawner`: spawns a fresh `claude -p` subprocess with role-specific
  settings and hooks wired up. Runs inside an interactive Claude Code session
  OR standalone from a shell — the subprocess is independent of the parent.
- `AgentSdkSpawner`: uses the Claude Agent SDK's programmatic API for
  non-Claude-Code automation (CI, shell scripts, long-lived processes).
  Requires `pip install claude-agent-sdk`.
"""

from .claude_code_spawner import ClaudeCodeSpawner
from .agent_sdk_spawner import AgentSdkSpawner

__all__ = ["ClaudeCodeSpawner", "AgentSdkSpawner"]
