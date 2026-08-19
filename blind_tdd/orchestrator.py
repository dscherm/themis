"""Blind TDD orchestrator.

Coordinates the red/implementation/green phases for a single task. Spawns
agents via the Claude Code Agent tool (or a pluggable spawner), manages
the session lifecycle, and records observations.

## Phase flow

1. **Validate**: check the task's acceptance_criteria and public_surface
   fields via schema_validator.
2. **Red phase**: activate a blind writer session, spawn Agent #1 with the
   writer prompt, collect test files and triage report, deactivate session.
   Run the tests against the current codebase and verify they fail as
   expected (ImportError/AttributeError only). Hash the test files for
   later integrity check.
3. **Implementation phase**: return control to the caller. The implementing
   agent writes the code. This phase is NOT managed by the orchestrator.
4. **Green phase**: activate a blind runner session, spawn Agent #2 with
   the runner prompt, collect the green report, deactivate session. Verify
   test file hashes match the red phase (no tampering) and all criteria
   are covered by passing tests.
5. (Optional) **Challenge phase**: if tests fail and the implementing agent
   files a challenge, spawn Agent #3 (arbiter) to rule.

## This module is the glue — most of the heavy lifting is in:
  - session.py        (activate/deactivate blind sessions)
  - schema_validator.py (validate task spec)
  - coverage.py       (verify every AC has a passing test)
  - The hooks         (enforce blindness at tool call level)

## Agent spawning

The actual spawning of Agents #1/#2/#3 is a **pluggable interface** because
it depends on the runtime (Claude Code Agent tool, Claude Agent SDK, or a
CI environment). This module defines the interface; the concrete spawner
is injected or falls back to a "manual" spawner that writes task files
for a human to run agents against.

See `AgentSpawner` below.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from .artifacts import is_artifact_path
from .coverage import verify_coverage, CoverageResult
from .schema_validator import validate_task, ValidationResult
from .session import (
    blind_session,
    get_audit_log,
    audit_violations,
    settings_template_for,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _hash_test_files(test_dirs: list[Path]) -> dict[str, str]:
    """Compute sha256 of every test file under the given dirs."""
    hashes: dict[str, str] = {}
    for td in test_dirs:
        td = Path(td)
        if not td.exists():
            continue
        for path in sorted(td.rglob("*")):
            if not path.is_file():
                continue
            if is_artifact_path(path):
                # A .pyc/__pycache__/node_modules/NUL artifact appearing in a
                # locked test dir must not change the sealed hash set.
                continue
            if path.suffix.lower() not in (".py", ".js", ".ts", ".tsx", ".jsx", ".cs"):
                continue
            hashes[str(path)] = _hash_file(path)
    return hashes


def _build_resume_context_block(ruling: dict) -> str:
    """Construct a context block appended to the writer prompt on resume."""
    criterion = str(ruling.get("criterion_affected", "?")).upper()
    reasoning = str(ruling.get("reasoning", "")).strip() or "(no reasoning provided)"
    resolution = str(ruling.get("resolution", "")).strip() or "(none)"
    return (
        "\n\n---\n\n"
        "# Resume context — prior test rejected by arbiter\n\n"
        f"The blind test you wrote previously for **{criterion}** was\n"
        "challenged by the implementing agent. A separate arbiter agent\n"
        "reviewed the challenge and ruled it **UPHELD** — the prior test\n"
        "was incorrect or flawed. That test has been deleted.\n\n"
        "## Arbiter reasoning (read this carefully)\n\n"
        f"{reasoning}\n\n"
        "## Suggested resolution\n\n"
        f"{resolution}\n\n"
        "## Your job now\n\n"
        f"- Rewrite tests for {criterion} that avoid the specific flaw\n"
        "  the arbiter identified. Do NOT repeat the same mistake.\n"
        f"- Leave other tests in place — they cover other criteria and\n"
        f"  are still valid.\n"
        f"- The triage report you write must still be a complete report\n"
        f"  for ALL criteria, not just the affected one. Copy unchanged\n"
        f"  entries from the previous report; the orchestrator will merge.\n"
        f"- Blindness rules still apply: do not read implementation code.\n"
    )


def _sanitize_task_for_brief(task: dict) -> dict:
    """Strip fields from a task spec that would hand a blind agent a reading
    list into implementation code.

    `steps` is host/bridge-authored guidance for the IMPLEMENTING agent
    (e.g. "read server/unit_store.py", "read core/compliance.py") — useful
    once the code exists, actively harmful handed to a blind writer/runner/
    arbiter, whose brief otherwise names only the task spec, acceptance
    criteria, and public_surface. The brief-building spawners (ClaudeCodeSpawner,
    AgentSdkSpawner, ManualSpawner) all receive `inputs["task"]` verbatim and
    dump it into the agent's context, so this must be applied here — the one
    place every spawn path shares — not per-spawner.
    """
    if not isinstance(task, dict) or "steps" not in task:
        return task
    sanitized = dict(task)
    del sanitized["steps"]
    return sanitized


def _merge_triage(previous: dict, new: dict) -> dict:
    """Merge two triage reports, with entries from `new` taking precedence.

    Both reports have shape:
        {"task": ..., "triage": [{criterion, status, reason, ...}, ...]}

    Entries are keyed by `criterion`. If a criterion appears in `new`,
    its entry wins; otherwise the entry from `previous` is kept.
    """
    if not isinstance(previous, dict) and not isinstance(new, dict):
        return {}
    out = dict(new) if isinstance(new, dict) else dict(previous)
    prev_entries = (previous or {}).get("triage") or []
    new_entries = (new or {}).get("triage") or []

    by_crit: dict[str, dict] = {}
    for entry in prev_entries:
        if isinstance(entry, dict) and entry.get("criterion"):
            by_crit[str(entry["criterion"]).upper()] = entry
    for entry in new_entries:
        if isinstance(entry, dict) and entry.get("criterion"):
            by_crit[str(entry["criterion"]).upper()] = entry

    out["triage"] = list(by_crit.values())
    return out


def _expected_manual_output(role: str, task_id: str, inputs: dict) -> Path | None:
    """What file does the manual agent need to produce to signal completion?"""
    if role == "test_writer":
        return Path(".themis") / "blind_tdd" / "triage" / f"{task_id}.json"
    if role == "test_runner":
        return Path(".themis") / "blind_tdd" / "green_report" / f"{task_id}.json"
    if role == "arbiter":
        challenge_id = inputs.get("challenge_id", task_id)
        return Path(".themis") / "blind_tdd" / "rulings" / f"{challenge_id}.json"
    return None


# ---------------------------------------------------------------------------
# Pluggable agent spawner interface
# ---------------------------------------------------------------------------

class AgentSpawner(Protocol):
    """Interface for spawning blind agents.

    Implementations:
      - ClaudeCodeSpawner: uses the Agent tool in an interactive Claude Code session
      - AgentSdkSpawner: uses the Claude Agent SDK for standalone automation
      - ManualSpawner: writes task files and waits for human to run agents manually
    """

    def spawn(self, *, role: str, prompt_template: str, inputs: dict) -> dict:
        """Spawn a blind agent and wait for it to complete.

        Args:
            role: "test_writer" | "test_runner" | "arbiter"
            prompt_template: the prompt text (content of prompts/<role>.md)
            inputs: dict of context the agent needs (task spec, file paths, etc.)

        Returns:
            dict with at least:
              - `success`: bool
              - `output_files`: list of paths the agent wrote
              - `agent_id`: string identifier
              - `error`: optional error message
        """
        ...


class ManualSpawner:
    """Fallback spawner that stages task files and pauses for human execution.

    Used when blind-TDD runs in a context without direct agent-spawn support
    (e.g. running smart_gate.py from a shell script). The orchestrator writes
    a task brief to `.themis/blind_tdd/pending/<role>-<task_id>.md` and waits
    for the human to run the agent and drop the output files in place.
    """

    def spawn(self, *, role: str, prompt_template: str, inputs: dict) -> dict:
        pending_dir = Path(".themis") / "blind_tdd" / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)

        task_id = inputs.get("task_id", "unknown")
        brief_path = pending_dir / f"{role}-{task_id}.md"

        # Completion detection: the expected output file must exist AND be
        # newer than the brief that asked for it. Otherwise re-running the
        # gate after a brief is re-staged would treat a stale prior output
        # as fresh completion (false green). When no brief is on disk yet,
        # mere existence is sufficient (typical first-time-after-completion
        # case where the orchestrator is invoked again from a clean state).
        expected_output = _expected_manual_output(role, task_id, inputs)
        output_is_fresh = (
            expected_output is not None
            and expected_output.exists()
            and (
                not brief_path.exists()
                or expected_output.stat().st_mtime >= brief_path.stat().st_mtime
            )
        )
        if output_is_fresh:
            # Clean up the stale brief, if any.
            if brief_path.exists():
                try:
                    brief_path.unlink()
                except OSError:
                    pass
            return {
                "success": True,
                "manual_mode": False,
                "manual_completed": True,
                "agent_id": f"manual-{role}-{task_id}",
                "output_files": [str(expected_output)],
                "message": (
                    f"Manual spawn completed: {expected_output} exists; "
                    f"proceeding to verification."
                ),
            }

        # Step 2 is what installs the path guard and denies Bash. Never guess
        # the filename from the role — say plainly when none is mapped, so a
        # reader knows the run would be unguarded rather than assuming it was
        # configured by a file they could not find.
        settings_file = settings_template_for(role)
        if settings_file:
            step_two = [
                f"2. Apply the blind-TDD settings template for this role:",
                f"   `.claude/settings.json` ← `templates/blind_tdd/{settings_file}`",
            ]
        else:
            step_two = [
                f"2. **STOP — no settings template is mapped for role `{role}`.**",
                f"   Blindness would NOT be enforced: Bash stays available and the",
                f"   PreToolUse path guard is never installed. Add a template for this",
                f"   role to `blind_tdd.session.ROLE_SETTINGS_TEMPLATES` before running.",
            ]

        brief_lines = [
            f"# Blind TDD — {role} task",
            f"",
            f"Task: {task_id}",
            f"Role: {role}",
            f"",
            f"## Instructions",
            f"",
            prompt_template,
            f"",
            f"## Inputs",
            f"",
            f"```json",
            json.dumps(inputs, indent=2, ensure_ascii=False),
            f"```",
            f"",
            f"## How to run this agent",
            f"",
            f"1. Copy the above inputs into a fresh Claude Code session.",
            *step_two,
            f"3. Run the agent.",
            f"4. Verify the output files were created.",
            f"5. Delete this brief file to signal completion.",
        ]
        brief_path.write_text("\n".join(brief_lines), encoding="utf-8")

        return {
            "success": False,  # human has to complete it
            "manual_mode": True,
            "brief_path": str(brief_path),
            "message": (
                f"Manual spawn: wrote brief to {brief_path}. "
                f"Run the agent, then re-invoke the orchestrator."
            ),
        }


# ---------------------------------------------------------------------------
# Phase results
# ---------------------------------------------------------------------------

@dataclass
class RedPhaseResult:
    passed: bool
    reason: str = ""
    test_file_hashes: dict[str, str] = field(default_factory=dict)
    triage_report: dict = field(default_factory=dict)
    coverage: CoverageResult | None = None
    violations: list[dict] = field(default_factory=list)
    spawn_result: dict = field(default_factory=dict)
    # Which roots the blind session actually sealed for this run, and how
    # they were derived (see blind_tdd.roots) — an auditable record of what
    # blindness claim this red phase can support. Empty when the orchestrator
    # was constructed without an explicit/derived blocked_paths (falls back
    # to session.py's generic defaults, unrecorded — legacy/direct-API path).
    sealed_roots: dict = field(default_factory=dict)


@dataclass
class GreenPhaseResult:
    passed: bool
    reason: str = ""
    green_report: dict = field(default_factory=dict)
    hash_match: bool = False
    # True only when the seal comparison actually ran and found a modified
    # test file — hash_match alone can't distinguish tampering from a spawn
    # failure that never got as far as hashing.
    hash_break: bool = False
    coverage: CoverageResult | None = None
    violations: list[dict] = field(default_factory=list)
    spawn_result: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class BlindTddOrchestrator:
    def __init__(
        self,
        *,
        spawner: AgentSpawner | None = None,
        test_dirs: list[str] | None = None,
        writer_prompt_path: str = "templates/blind_tdd/prompts/test_writer.md",
        runner_prompt_path: str = "templates/blind_tdd/prompts/test_runner.md",
        arbiter_prompt_path: str = "templates/blind_tdd/prompts/arbiter.md",
        observations_path: str = ".themis/observations.jsonl",
        blocked_paths: list[str] | None = None,
        sealed_roots_meta: dict | None = None,
    ) -> None:
        self.spawner = spawner or ManualSpawner()
        self.test_dirs = test_dirs or ["tests/contracts/", "tests/integration/"]
        self.writer_prompt_path = writer_prompt_path
        self.runner_prompt_path = runner_prompt_path
        self.arbiter_prompt_path = arbiter_prompt_path
        self.observations_path = observations_path
        # None (the default) preserves the old behavior: every blind_session()
        # call below falls back to session.py's generic default_blocked_paths().
        # A caller that knows the project's real layout (gate_integration, via
        # blind_tdd.roots.derive_blocked_paths) passes the derived list here so
        # every spawned session actually seals the project's implementation
        # directories instead of a JS-shaped guess.
        self.blocked_paths = blocked_paths
        self.sealed_roots_meta = sealed_roots_meta or {}

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, task: dict) -> ValidationResult:
        return validate_task(task)

    # ------------------------------------------------------------------
    # Red phase
    # ------------------------------------------------------------------

    def run_red_phase(self, task: dict) -> RedPhaseResult:
        """Activate a blind writer session, spawn Agent #1, verify tests exist.

        This does NOT run the tests — that is done by the caller after Agent #1
        returns, so the orchestrator has full control over the test execution
        environment (the agent is blind to src/ and can't run pytest itself
        since runner commands aren't in its whitelist).
        """
        task_id = task.get("id", "unknown")

        # 1. Validate schema
        vr = self.validate(task)
        if not vr.valid:
            return RedPhaseResult(
                passed=False,
                reason=f"schema validation failed: {'; '.join(vr.errors)}",
            )

        # 2. Load writer prompt
        prompt = self._load_prompt(self.writer_prompt_path)

        # 3. Activate blind session and spawn Agent #1
        spawn_result: dict = {}
        violations: list[dict] = []
        try:
            with blind_session(
                agent_role="test_writer",
                task_id=task_id,
                blocked_paths=self.blocked_paths,
            ) as session:
                spawn_result = self.spawner.spawn(
                    role="test_writer",
                    prompt_template=prompt,
                    inputs={
                        "task_id": task_id,
                        "task": _sanitize_task_for_brief(task),
                        "test_dirs": self.test_dirs,
                        "session_id": session["session_id"],
                    },
                )
                # Collect audit violations during the session
                violations = audit_violations(session["session_id"])
        except RuntimeError as e:
            return RedPhaseResult(
                passed=False,
                reason=f"session activation failed: {e}",
            )

        # 4. If manual spawn, return early — the human needs to do work
        if spawn_result.get("manual_mode"):
            return RedPhaseResult(
                passed=False,
                reason="manual spawn requested — complete the brief and re-run",
                spawn_result=spawn_result,
            )

        if not spawn_result.get("success", False):
            return RedPhaseResult(
                passed=False,
                reason=f"agent spawn failed: {spawn_result.get('error', 'unknown')}",
                spawn_result=spawn_result,
                violations=violations,
            )

        # 5. Load triage report
        triage_path = Path(".themis") / "blind_tdd" / "triage" / f"{task_id}.json"
        triage: dict = {}
        if triage_path.exists():
            try:
                triage = json.loads(triage_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return RedPhaseResult(
                    passed=False,
                    reason=f"triage report at {triage_path} is not valid JSON",
                    spawn_result=spawn_result,
                    violations=violations,
                )
        else:
            return RedPhaseResult(
                passed=False,
                reason=f"triage report missing at {triage_path}",
                spawn_result=spawn_result,
                violations=violations,
            )

        # 6. Verify coverage — red phase only checks that every AC has either
        #    a test annotation OR a triage entry. We don't require tests to
        #    pass yet (they're expected to fail with ImportError).
        coverage = verify_coverage(
            task,
            self.test_dirs,
            triage_report=triage,
            passing_test_names=None,  # red phase: any tagged test counts
        )

        if not coverage.all_covered:
            return RedPhaseResult(
                passed=False,
                reason=f"coverage gap after red phase: missing={coverage.missing}",
                test_file_hashes=_hash_test_files([Path(d) for d in self.test_dirs]),
                triage_report=triage,
                coverage=coverage,
                spawn_result=spawn_result,
                violations=violations,
            )

        # 7. Check for blindness violations in the audit log
        if violations:
            # Emit one observation per violation attempt so the dashboard
            # can surface a "blindness integrity red flag" tile.
            for v in violations:
                self._record_observation({
                    "type": "blindness_violation_attempt",
                    "task": task_id,
                    "session_id": session["session_id"],
                    "agent_role": "test_writer",
                    "tool": v.get("tool", ""),
                    "path": v.get("path", ""),
                    "timestamp": _now_iso(),
                })
            return RedPhaseResult(
                passed=False,
                reason=(
                    f"blind writer agent attempted {len(violations)} forbidden "
                    f"tool call(s); blindness integrity compromised"
                ),
                test_file_hashes=_hash_test_files([Path(d) for d in self.test_dirs]),
                triage_report=triage,
                coverage=coverage,
                spawn_result=spawn_result,
                violations=violations,
            )

        # 8. Emit triage escalation observations — one per criterion that
        #    was escalated to human. These feed the dashboard's "pattern
        #    of untestable criteria" detection.
        for entry in (triage.get("triage") or []):
            if isinstance(entry, dict) and entry.get("status") == "needs_human":
                self._record_observation({
                    "type": "triage_escalation",
                    "task": task_id,
                    "criterion": str(entry.get("criterion", "")).upper(),
                    "category": entry.get("category", "unknown"),
                    "reason": entry.get("reason", ""),
                    "timestamp": _now_iso(),
                })

        # 9. Hash the test files for later integrity check
        hashes = _hash_test_files([Path(d) for d in self.test_dirs])

        self._record_observation({
            "type": "blind_red_phase",
            "task": task_id,
            "timestamp": _now_iso(),
            "red_pass": True,
            "test_file_count": len(hashes),
            "tested_criteria": sorted(coverage.covered_by_tests.keys()),
            "needs_human_criteria": coverage.covered_by_triage,
            "spawn_result_summary": {
                "agent_id": spawn_result.get("agent_id"),
                "success": spawn_result.get("success"),
            },
        })

        return RedPhaseResult(
            passed=True,
            reason="red phase passed",
            test_file_hashes=hashes,
            triage_report=triage,
            coverage=coverage,
            violations=[],
            spawn_result=spawn_result,
            sealed_roots=dict(self.sealed_roots_meta),
        )

    # ------------------------------------------------------------------
    # Green phase
    # ------------------------------------------------------------------

    def run_green_phase(
        self,
        task: dict,
        red_result: RedPhaseResult,
    ) -> GreenPhaseResult:
        """After implementation, spawn Agent #2 to run the tests."""
        task_id = task.get("id", "unknown")

        if not red_result.passed:
            return GreenPhaseResult(
                passed=False,
                reason="red phase did not pass; green phase skipped",
            )

        prompt = self._load_prompt(self.runner_prompt_path)

        spawn_result: dict = {}
        violations: list[dict] = []
        try:
            with blind_session(
                agent_role="test_runner",
                task_id=task_id,
                blocked_paths=self.blocked_paths,
            ) as session:
                spawn_result = self.spawner.spawn(
                    role="test_runner",
                    prompt_template=prompt,
                    inputs={
                        "task_id": task_id,
                        "task": _sanitize_task_for_brief(task),
                        "test_dirs": self.test_dirs,
                        "red_phase_hashes": red_result.test_file_hashes,
                        "triage_report": red_result.triage_report,
                        "session_id": session["session_id"],
                    },
                )
                violations = audit_violations(session["session_id"])
        except RuntimeError as e:
            return GreenPhaseResult(
                passed=False,
                reason=f"session activation failed: {e}",
            )

        if spawn_result.get("manual_mode"):
            return GreenPhaseResult(
                passed=False,
                reason="manual spawn requested — complete the brief and re-run",
                spawn_result=spawn_result,
            )

        if not spawn_result.get("success", False):
            return GreenPhaseResult(
                passed=False,
                reason=f"agent spawn failed: {spawn_result.get('error', 'unknown')}",
                spawn_result=spawn_result,
                violations=violations,
            )

        # Load the green report
        report_path = Path(".themis") / "blind_tdd" / "green_report" / f"{task_id}.json"
        if not report_path.exists():
            return GreenPhaseResult(
                passed=False,
                reason=f"green report missing at {report_path}",
                spawn_result=spawn_result,
                violations=violations,
            )
        try:
            green_report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return GreenPhaseResult(
                passed=False,
                reason=f"green report at {report_path} is not valid JSON",
                spawn_result=spawn_result,
                violations=violations,
            )

        # Verify test file hashes match the red phase
        current_hashes = _hash_test_files([Path(d) for d in self.test_dirs])
        hash_match = current_hashes == red_result.test_file_hashes
        if not hash_match:
            modified = sorted(
                set(red_result.test_file_hashes.keys())
                | set(current_hashes.keys())
            )
            mismatched = [
                f for f in modified
                if current_hashes.get(f) != red_result.test_file_hashes.get(f)
            ]
            return GreenPhaseResult(
                passed=False,
                reason=(
                    f"test file integrity compromised: {len(mismatched)} file(s) "
                    f"modified between red and green phases: {mismatched[:5]}"
                ),
                green_report=green_report,
                hash_match=False,
                hash_break=True,
                spawn_result=spawn_result,
                violations=violations,
            )

        # Verify coverage with passing test names from the green report
        passing_names: set[str] = set()
        for entry in green_report.get("per_test", []):
            if isinstance(entry, dict) and entry.get("result") == "pass":
                name = entry.get("name")
                if name:
                    passing_names.add(name)

        coverage = verify_coverage(
            task,
            self.test_dirs,
            triage_report=red_result.triage_report,
            passing_test_names=passing_names,
        )

        if not coverage.all_covered:
            return GreenPhaseResult(
                passed=False,
                reason=(
                    f"coverage gap in green phase: "
                    f"missing={coverage.missing}, "
                    f"not_passing={coverage.not_passing}"
                ),
                green_report=green_report,
                hash_match=True,
                coverage=coverage,
                spawn_result=spawn_result,
                violations=violations,
            )

        if violations:
            for v in violations:
                self._record_observation({
                    "type": "blindness_violation_attempt",
                    "task": task_id,
                    "agent_role": "test_runner",
                    "phase": "green",
                    "tool": v.get("tool", ""),
                    "path": v.get("path", ""),
                    "timestamp": _now_iso(),
                })
            return GreenPhaseResult(
                passed=False,
                reason=(
                    f"blind runner agent attempted {len(violations)} forbidden "
                    f"tool call(s); blindness integrity compromised"
                ),
                green_report=green_report,
                hash_match=True,
                coverage=coverage,
                spawn_result=spawn_result,
                violations=violations,
            )

        # Verify the runner reported overall pass
        if green_report.get("overall") != "pass":
            return GreenPhaseResult(
                passed=False,
                reason=f"runner reported overall={green_report.get('overall')}",
                green_report=green_report,
                hash_match=True,
                coverage=coverage,
                spawn_result=spawn_result,
            )

        self._record_observation({
            "type": "blind_green_phase",
            "task": task_id,
            "timestamp": _now_iso(),
            "green_pass": True,
            "tests_passed": green_report.get("tests_passed"),
            "tests_failed": green_report.get("tests_failed"),
            "coverage_verified": True,
            "hash_match": True,
        })

        return GreenPhaseResult(
            passed=True,
            reason="green phase passed",
            green_report=green_report,
            hash_match=True,
            coverage=coverage,
            spawn_result=spawn_result,
        )

    # ------------------------------------------------------------------
    # Resume after an upheld challenge ruling
    # ------------------------------------------------------------------

    def resume_red_phase_after_ruling(
        self,
        task: dict,
        previous_red: "RedPhaseResult",
        ruling: dict,
    ) -> "RedPhaseResult":
        """Re-spawn Agent #1 after an upheld challenge.

        Context: the implementing agent filed a challenge against a
        specific test, the arbiter upheld it, and `apply_ruling` deleted
        the disputed test file. Now a fresh writer must rewrite the
        tests for the affected criterion, with the arbiter's reasoning
        injected as additional context so the same mistake isn't
        repeated.

        Args:
            task: the task spec (unchanged from the original red phase)
            previous_red: the RedPhaseResult from the original red phase
                — used to preserve the triage report for criteria that
                were already escalated
            ruling: dict with keys `reasoning`, `criterion_affected`,
                `resolution` (optional). Usually the `Ruling.to_dict()`
                output from `blind_tdd.challenge`.

        Returns a fresh RedPhaseResult with:
            - passed: True if the rewrite covered every criterion and
              the session had no blindness violations
            - test_file_hashes: RE-computed across the full test tree
              so the green phase's hash check reflects the rewrite
            - triage_report: the NEW triage report from the writer
              (merged with the previous one for criteria the new writer
              did not re-triage)
        """
        task_id = task.get("id", "unknown")

        # Schema validation is still required — spec could have changed
        vr = self.validate(task)
        if not vr.valid:
            return RedPhaseResult(
                passed=False,
                reason=f"schema validation failed on resume: {'; '.join(vr.errors)}",
            )

        prompt = self._load_prompt(self.writer_prompt_path)
        prompt += _build_resume_context_block(ruling)

        affected_criterion = str(ruling.get("criterion_affected", "")).upper()

        spawn_result: dict = {}
        violations: list[dict] = []
        try:
            with blind_session(
                agent_role="test_writer",
                task_id=task_id,
                blocked_paths=self.blocked_paths,
            ) as session:
                spawn_result = self.spawner.spawn(
                    role="test_writer",
                    prompt_template=prompt,
                    inputs={
                        "task_id": task_id,
                        "task": _sanitize_task_for_brief(task),
                        "test_dirs": self.test_dirs,
                        "session_id": session["session_id"],
                        "resume_context": {
                            "affected_criterion": affected_criterion,
                            "arbiter_reasoning": ruling.get("reasoning", ""),
                            "arbiter_resolution": ruling.get("resolution", ""),
                            "previous_triage": previous_red.triage_report,
                        },
                    },
                )
                violations = audit_violations(session["session_id"])
        except RuntimeError as e:
            return RedPhaseResult(
                passed=False,
                reason=f"session activation failed on resume: {e}",
            )

        if spawn_result.get("manual_mode"):
            return RedPhaseResult(
                passed=False,
                reason="manual spawn requested — complete the brief and re-run",
                spawn_result=spawn_result,
            )

        if not spawn_result.get("success", False):
            return RedPhaseResult(
                passed=False,
                reason=f"writer respawn failed: {spawn_result.get('error', 'unknown')}",
                spawn_result=spawn_result,
                violations=violations,
            )

        # Load the (potentially new) triage report and merge with the
        # previous one — the new writer may only have re-triaged the
        # affected criterion, so we keep the old entries for the rest.
        triage_path = Path(".themis") / "blind_tdd" / "triage" / f"{task_id}.json"
        new_triage: dict = {}
        if triage_path.exists():
            try:
                new_triage = json.loads(triage_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                new_triage = {}

        merged_triage = _merge_triage(previous_red.triage_report, new_triage)

        # Coverage check against the merged triage — any criterion must
        # either have a tagged test in the filesystem or a triage entry.
        coverage = verify_coverage(
            task,
            self.test_dirs,
            triage_report=merged_triage,
            passing_test_names=None,
        )
        if not coverage.all_covered:
            return RedPhaseResult(
                passed=False,
                reason=(
                    f"coverage gap after resume: missing={coverage.missing}"
                ),
                test_file_hashes=_hash_test_files([Path(d) for d in self.test_dirs]),
                triage_report=merged_triage,
                coverage=coverage,
                spawn_result=spawn_result,
                violations=violations,
            )

        if violations:
            for v in violations:
                self._record_observation({
                    "type": "blindness_violation_attempt",
                    "task": task_id,
                    "agent_role": "test_writer",
                    "phase": "red_resume",
                    "tool": v.get("tool", ""),
                    "path": v.get("path", ""),
                    "timestamp": _now_iso(),
                })
            return RedPhaseResult(
                passed=False,
                reason=(
                    f"blind writer agent attempted {len(violations)} forbidden "
                    f"tool call(s) during resume; blindness integrity compromised"
                ),
                test_file_hashes=_hash_test_files([Path(d) for d in self.test_dirs]),
                triage_report=merged_triage,
                coverage=coverage,
                spawn_result=spawn_result,
                violations=violations,
            )

        # Fresh hashes across the entire test tree — this is what the
        # green phase will compare against.
        hashes = _hash_test_files([Path(d) for d in self.test_dirs])

        self._record_observation({
            "type": "blind_red_phase_resumed",
            "task": task_id,
            "timestamp": _now_iso(),
            "red_pass": True,
            "test_file_count": len(hashes),
            "tested_criteria": sorted(coverage.covered_by_tests.keys()),
            "needs_human_criteria": coverage.covered_by_triage,
            "affected_criterion": affected_criterion,
            "spawn_result_summary": {
                "agent_id": spawn_result.get("agent_id"),
                "success": spawn_result.get("success"),
            },
        })

        return RedPhaseResult(
            passed=True,
            reason="red phase resumed after upheld ruling",
            test_file_hashes=hashes,
            triage_report=merged_triage,
            coverage=coverage,
            violations=[],
            spawn_result=spawn_result,
            sealed_roots=dict(self.sealed_roots_meta),
        )

    def resume_green_phase(
        self,
        task: dict,
        red_result: "RedPhaseResult",
    ) -> "GreenPhaseResult":
        """Thin alias for `run_green_phase` — makes challenge-resolution
        flow more readable at the call site.

        After `resume_red_phase_after_ruling` returns a fresh RedPhaseResult,
        call this (or `run_green_phase`) to re-execute the runner against
        the updated tests.
        """
        return self.run_green_phase(task, red_result)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_prompt(self, path: str) -> str:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"Blind TDD prompt template not found at {p}. "
                f"Install Themis templates/blind_tdd/prompts/."
            )
        return p.read_text(encoding="utf-8")

    def _record_observation(self, data: dict) -> None:
        """Append an observation to .themis/observations.jsonl."""
        obs_path = Path(self.observations_path)
        obs_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with obs_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except OSError:
            pass  # observation failures never block the gate
