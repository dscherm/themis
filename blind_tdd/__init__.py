"""Blind TDD gate infrastructure for ralph-universal.

See `docs/reference/blind-tdd-gate-rfc.md` for the full design.

Modules:
  - session: activate/deactivate blind agent sessions (drives the hooks)
  - schema_validator: validate acceptance_criteria + public_surface in plan.md tasks
  - (future) orchestrator: spawn and coordinate Agents #1, #2, #3
  - (future) coverage: verify every AC-N has a matching test
  - (future) challenge: arbiter protocol
"""
