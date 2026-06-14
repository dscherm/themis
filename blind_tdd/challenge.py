"""Challenge protocol — Agent #3 (arbiter) orchestration.

When the implementing agent believes a blind-written test incorrectly
encodes its acceptance criterion, it can file a **challenge**. A fresh
arbiter agent (Agent #3) reads only the disputed test file, the task
spec, and the challenge document, then issues one of three rulings:

- `upheld`: the test is incorrect. It is deleted and a fresh Agent #1
  is asked to rewrite tests for the affected criterion.
- `rejected`: the test is correct. The challenge fails and the
  implementing agent must make the test pass.
- `ambiguous`: the spec itself is unclear; escalate to a human via the
  `human_input` drop box.

Per-task and per-criterion challenge caps (from
`gate.blind_tdd.max_challenges_per_task` and
`max_challenges_per_criterion`) prevent abuse of the protocol. Once a
cap is exceeded, further challenges are rejected automatically and the
implementing agent must escalate directly.

## File layout

- `.themis/blind_tdd/challenges/<challenge_id>.json` — the challenge doc
  (written by `file_challenge`, read by the arbiter)
- `.themis/blind_tdd/rulings/<challenge_id>.json` — the ruling (written
  by the arbiter, consumed by `apply_ruling`)
- `.themis/blind_tdd/challenge_log.jsonl` — append-only log of all
  challenges filed across all tasks, used for cap enforcement and
  cross-project analytics

## Observations recorded

The orchestrator observations stream receives one entry per major
event: `challenge_filed`, `challenge_cap_exceeded`, `arbiter_ruling`,
`challenge_resolved`. These feed the dashboard tiles planned in
Phase C2.

## This module does NOT

- spawn Agent #1 for test rewrites (that's the orchestrator's red
  phase — apply_ruling only marks the criterion as needing rewrite)
- contact the human directly (it writes a human_input request and
  returns; the caller is responsible for blocking on the response)
- modify implementation source files

See `docs/reference/blind-tdd-gate-rfc.md` sections R7 and R8 for the full
design rationale.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


# Default caps — overridden by config
DEFAULT_MAX_PER_TASK = 3
DEFAULT_MAX_PER_CRITERION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _challenges_dir() -> Path:
    return Path(".themis") / "blind_tdd" / "challenges"


def _rulings_dir() -> Path:
    return Path(".themis") / "blind_tdd" / "rulings"


def _challenge_log() -> Path:
    return Path(".themis") / "blind_tdd" / "challenge_log.jsonl"


def _human_requests_dir() -> Path:
    return Path(".themis") / "human_requests"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Challenge:
    challenge_id: str
    task_id: str
    test_file: str
    test_name: str
    criterion: str
    argument: str
    proposed_fix: str = ""
    challenger: str = "unknown"
    filed_at: str = ""

    def to_dict(self) -> dict:
        return {
            "challenge_id": self.challenge_id,
            "task": self.task_id,
            "test_file": self.test_file,
            "test_name": self.test_name,
            "criterion": self.criterion,
            "argument": self.argument,
            "proposed_fix": self.proposed_fix,
            "challenger": self.challenger,
            "filed_at": self.filed_at,
        }


@dataclass
class Ruling:
    challenge_id: str
    task_id: str
    ruling: str  # "upheld" | "rejected" | "ambiguous"
    reasoning: str
    criterion_affected: str
    resolution: str = ""
    arbiter_id: str = ""
    ruled_at: str = ""

    def to_dict(self) -> dict:
        return {
            "challenge_id": self.challenge_id,
            "task": self.task_id,
            "ruling": self.ruling,
            "reasoning": self.reasoning,
            "criterion_affected": self.criterion_affected,
            "resolution": self.resolution,
            "arbiter_id": self.arbiter_id,
            "ruled_at": self.ruled_at,
        }


@dataclass
class ChallengeResult:
    """End-to-end result returned by `process_challenge`."""
    success: bool
    action: str  # "test_deleted" | "rejected" | "escalated_to_human" | "cap_exceeded" | "arbiter_failed"
    challenge_id: str
    message: str = ""
    ruling: Ruling | None = None
    human_request_id: str | None = None
    affected_criterion: str = ""
    affected_test_file: str = ""
    cap_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "action": self.action,
            "challenge_id": self.challenge_id,
            "message": self.message,
            "ruling": self.ruling.to_dict() if self.ruling else None,
            "human_request_id": self.human_request_id,
            "affected_criterion": self.affected_criterion,
            "affected_test_file": self.affected_test_file,
            "cap_reason": self.cap_reason,
        }


# ---------------------------------------------------------------------------
# Spawner protocol (same signature as the orchestrator's)
# ---------------------------------------------------------------------------

class ArbiterSpawner(Protocol):
    def spawn(self, *, role: str, prompt_template: str, inputs: dict) -> dict:
        ...


# ---------------------------------------------------------------------------
# Filing challenges
# ---------------------------------------------------------------------------

def _new_challenge_id(task_id: str) -> str:
    return f"chal-{task_id}-{uuid.uuid4().hex[:8]}"


def file_challenge(
    *,
    task_id: str,
    test_file: str,
    test_name: str,
    criterion: str,
    argument: str,
    proposed_fix: str = "",
    challenger: str = "unknown",
    challenge_id: str | None = None,
) -> Challenge:
    """Create a challenge document and append it to the challenge log.

    Returns the Challenge object. The caller is responsible for calling
    `spawn_arbiter` and `apply_ruling` afterwards — or just use
    `process_challenge` which wraps all three steps.
    """
    if not argument or not argument.strip():
        raise ValueError("challenge argument must not be empty")
    if not criterion:
        raise ValueError("challenge must identify a criterion")

    chal = Challenge(
        challenge_id=challenge_id or _new_challenge_id(task_id),
        task_id=task_id,
        test_file=test_file,
        test_name=test_name,
        criterion=criterion.upper(),
        argument=argument.strip(),
        proposed_fix=proposed_fix.strip(),
        challenger=challenger,
        filed_at=_now_iso(),
    )

    d = _challenges_dir()
    d.mkdir(parents=True, exist_ok=True)
    challenge_path = d / f"{chal.challenge_id}.json"
    challenge_path.write_text(
        json.dumps(chal.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Append to the cross-task log for cap enforcement and analytics
    log = _challenge_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(chal.to_dict(), ensure_ascii=False) + "\n")

    return chal


# ---------------------------------------------------------------------------
# Cap enforcement
# ---------------------------------------------------------------------------

def _iter_log() -> list[dict]:
    log = _challenge_log()
    if not log.exists():
        return []
    out: list[dict] = []
    try:
        with log.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def count_challenges(*, task_id: str, criterion: str | None = None) -> int:
    """Count filed challenges for a task (and optionally a specific criterion)."""
    crit = criterion.upper() if criterion else None
    n = 0
    for entry in _iter_log():
        if entry.get("task") != task_id:
            continue
        if crit is not None and entry.get("criterion", "").upper() != crit:
            continue
        n += 1
    return n


def check_challenge_caps(
    *,
    task_id: str,
    criterion: str,
    max_per_task: int = DEFAULT_MAX_PER_TASK,
    max_per_criterion: int = DEFAULT_MAX_PER_CRITERION,
) -> tuple[bool, str]:
    """Return (allowed, reason).

    Called BEFORE filing a new challenge. If `allowed` is False, the
    caller must NOT file the challenge and should escalate to human.
    The check counts challenges already in the log, so calling this
    AFTER filing would include the new one in the count.
    """
    per_task = count_challenges(task_id=task_id)
    if per_task >= max_per_task:
        return (
            False,
            f"per-task cap reached: {per_task}/{max_per_task} challenges "
            f"already filed for task {task_id!r}",
        )
    per_crit = count_challenges(task_id=task_id, criterion=criterion)
    if per_crit >= max_per_criterion:
        return (
            False,
            f"per-criterion cap reached: {per_crit}/{max_per_criterion} "
            f"challenges already filed for {criterion.upper()} on task {task_id!r}",
        )
    return True, ""


# ---------------------------------------------------------------------------
# Spawning the arbiter
# ---------------------------------------------------------------------------

def spawn_arbiter(
    *,
    challenge: Challenge,
    spawner: ArbiterSpawner,
    arbiter_prompt: str,
    task_spec: dict,
) -> Ruling | None:
    """Spawn Agent #3 and wait for the ruling file.

    Returns the parsed Ruling, or None if the spawn failed or the
    arbiter did not produce a valid ruling document.
    """
    spawn_inputs = {
        "task_id": challenge.task_id,
        "challenge_id": challenge.challenge_id,
        "task": task_spec,
        "challenge": challenge.to_dict(),
        "test_file": challenge.test_file,
    }
    try:
        result = spawner.spawn(
            role="arbiter",
            prompt_template=arbiter_prompt,
            inputs=spawn_inputs,
        )
    except Exception as e:  # defensive: spawner errors must not propagate
        return None

    if not result.get("success"):
        return None

    ruling_path = _rulings_dir() / f"{challenge.challenge_id}.json"
    if not ruling_path.exists():
        return None

    try:
        raw = json.loads(ruling_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    verdict = str(raw.get("ruling", "")).lower().strip()
    if verdict not in ("upheld", "rejected", "ambiguous"):
        return None

    return Ruling(
        challenge_id=str(raw.get("challenge_id", challenge.challenge_id)),
        task_id=str(raw.get("task", challenge.task_id)),
        ruling=verdict,
        reasoning=str(raw.get("reasoning", "")),
        criterion_affected=str(raw.get("criterion_affected", challenge.criterion)).upper(),
        resolution=str(raw.get("resolution", "")),
        arbiter_id=str(raw.get("arbiter_id", "")),
        ruled_at=str(raw.get("ruled_at", _now_iso())),
    )


# ---------------------------------------------------------------------------
# Ruling resolution
# ---------------------------------------------------------------------------

def apply_ruling(
    *,
    challenge: Challenge,
    ruling: Ruling,
    human_input_timeout: int = 3600,
) -> ChallengeResult:
    """Execute the ruling's side effects.

    - `upheld`: delete the disputed test file (it was wrong). Caller
      must re-spawn Agent #1 with the ruling's reasoning as additional
      context to rewrite tests for the affected criterion.
    - `rejected`: no side effects. The implementing agent must make the
      test pass.
    - `ambiguous`: file a human input request and return; the caller is
      expected to block on the response.
    """
    verdict = ruling.ruling.lower()

    if verdict == "upheld":
        test_path = Path(challenge.test_file)
        deleted = False
        try:
            if test_path.exists():
                test_path.unlink()
                deleted = True
        except OSError as e:
            return ChallengeResult(
                success=False,
                action="arbiter_failed",
                challenge_id=challenge.challenge_id,
                message=f"ruling upheld but failed to delete {test_path}: {e}",
                ruling=ruling,
                affected_criterion=ruling.criterion_affected,
                affected_test_file=str(test_path),
            )
        return ChallengeResult(
            success=True,
            action="test_deleted" if deleted else "test_already_missing",
            challenge_id=challenge.challenge_id,
            message=(
                f"challenge upheld: {test_path} removed. Rewrite tests for "
                f"{ruling.criterion_affected} with the arbiter's reasoning as context."
            ),
            ruling=ruling,
            affected_criterion=ruling.criterion_affected,
            affected_test_file=str(test_path),
        )

    if verdict == "rejected":
        return ChallengeResult(
            success=True,
            action="rejected",
            challenge_id=challenge.challenge_id,
            message=(
                f"challenge rejected: the test is correct as written. "
                f"Make it pass. Arbiter reasoning: {ruling.reasoning[:300]}"
            ),
            ruling=ruling,
            affected_criterion=ruling.criterion_affected,
            affected_test_file=challenge.test_file,
        )

    if verdict == "ambiguous":
        # Write a human input request via the drop box
        req_id = _write_human_request(challenge, ruling, timeout=human_input_timeout)
        return ChallengeResult(
            success=True,
            action="escalated_to_human",
            challenge_id=challenge.challenge_id,
            message=(
                f"challenge ambiguous: escalated to human at "
                f"{_human_requests_dir() / (req_id + '.json')}"
            ),
            ruling=ruling,
            human_request_id=req_id,
            affected_criterion=ruling.criterion_affected,
            affected_test_file=challenge.test_file,
        )

    # Unknown verdict — should have been filtered by spawn_arbiter
    return ChallengeResult(
        success=False,
        action="arbiter_failed",
        challenge_id=challenge.challenge_id,
        message=f"arbiter returned unknown verdict {ruling.ruling!r}",
        ruling=ruling,
    )


def _write_human_request(challenge: Challenge, ruling: Ruling, *, timeout: int) -> str:
    """Drop an ambiguous-ruling human request and return its id."""
    d = _human_requests_dir()
    d.mkdir(parents=True, exist_ok=True)
    req_id = f"amb-{challenge.challenge_id}"
    req = {
        "request_id": req_id,
        "agent": "blind_tdd_challenge",
        "category": "blind_tdd_ambiguous_ruling",
        "phase": "arbiter",
        "task": challenge.task_id,
        "criterion": ruling.criterion_affected,
        "question": (
            f"The blind-TDD arbiter could not conclusively rule on challenge "
            f"{challenge.challenge_id!r} against criterion {ruling.criterion_affected}. "
            f"Please review and decide:\n\n"
            f"**Test file:** {challenge.test_file}\n"
            f"**Test name:** {challenge.test_name}\n\n"
            f"**Implementer's argument:**\n{challenge.argument}\n\n"
            f"**Arbiter's reasoning:**\n{ruling.reasoning}\n\n"
            f"Choose an option."
        ),
        "options": [
            {"value": "uphold",
             "label": "Uphold — delete the test and have a fresh writer rewrite it"},
            {"value": "reject",
             "label": "Reject — the test is correct, implementer must make it pass"},
            {"value": "rewrite_spec",
             "label": "The spec itself is unclear — rewrite the criterion first"},
        ],
        "free_text_allowed": True,
        "blocking": True,
        "timeout_seconds": timeout,
        "created_at": _now_iso(),
    }
    (d / f"{req_id}.json").write_text(
        json.dumps(req, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    return req_id


# ---------------------------------------------------------------------------
# Observation recording (shared helper)
# ---------------------------------------------------------------------------

def _record_observation(data: dict, path: str = ".themis/observations.jsonl") -> None:
    """Append an observation to the orchestrator's stream."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
    except OSError:
        pass  # observation failures never block gate flow


# ---------------------------------------------------------------------------
# Top-level: full challenge flow
# ---------------------------------------------------------------------------

def process_challenge(
    *,
    task_id: str,
    test_file: str,
    test_name: str,
    criterion: str,
    argument: str,
    proposed_fix: str = "",
    challenger: str = "unknown",
    task_spec: dict,
    spawner: ArbiterSpawner,
    arbiter_prompt: str,
    max_per_task: int = DEFAULT_MAX_PER_TASK,
    max_per_criterion: int = DEFAULT_MAX_PER_CRITERION,
    human_input_timeout: int = 3600,
    observations_path: str = ".themis/observations.jsonl",
) -> ChallengeResult:
    """End-to-end challenge processing: cap check → file → spawn → rule.

    Returns a ChallengeResult with `action` indicating what happened:
    - `cap_exceeded`: the challenge was not filed; caller must escalate
    - `test_deleted`: challenge was upheld; caller must re-run red phase
    - `rejected`: challenge was rejected; implementer must make test pass
    - `escalated_to_human`: ambiguous; caller must wait for human response
    - `arbiter_failed`: arbiter could not rule (spawn failed, bad output)
    """
    # 1. Cap check — count existing challenges BEFORE filing
    allowed, reason = check_challenge_caps(
        task_id=task_id,
        criterion=criterion,
        max_per_task=max_per_task,
        max_per_criterion=max_per_criterion,
    )
    if not allowed:
        _record_observation({
            "type": "challenge_cap_exceeded",
            "task": task_id,
            "criterion": criterion.upper(),
            "reason": reason,
            "timestamp": _now_iso(),
        }, observations_path)
        return ChallengeResult(
            success=False,
            action="cap_exceeded",
            challenge_id="",
            message=reason,
            affected_criterion=criterion.upper(),
            affected_test_file=test_file,
            cap_reason=reason,
        )

    # 2. File the challenge
    try:
        challenge = file_challenge(
            task_id=task_id,
            test_file=test_file,
            test_name=test_name,
            criterion=criterion,
            argument=argument,
            proposed_fix=proposed_fix,
            challenger=challenger,
        )
    except ValueError as e:
        return ChallengeResult(
            success=False,
            action="arbiter_failed",
            challenge_id="",
            message=f"failed to file challenge: {e}",
            affected_criterion=criterion.upper(),
            affected_test_file=test_file,
        )

    _record_observation({
        "type": "challenge_filed",
        "task": task_id,
        "challenge_id": challenge.challenge_id,
        "criterion": challenge.criterion,
        "test_file": challenge.test_file,
        "test_name": challenge.test_name,
        "challenger": challenge.challenger,
        "timestamp": challenge.filed_at,
    }, observations_path)

    # 3. Spawn arbiter
    ruling = spawn_arbiter(
        challenge=challenge,
        spawner=spawner,
        arbiter_prompt=arbiter_prompt,
        task_spec=task_spec,
    )
    if ruling is None:
        return ChallengeResult(
            success=False,
            action="arbiter_failed",
            challenge_id=challenge.challenge_id,
            message=(
                f"arbiter did not produce a valid ruling for "
                f"{challenge.challenge_id}"
            ),
            affected_criterion=challenge.criterion,
            affected_test_file=challenge.test_file,
        )

    _record_observation({
        "type": "arbiter_ruling",
        "task": task_id,
        "challenge_id": challenge.challenge_id,
        "ruling": ruling.ruling,
        "criterion_affected": ruling.criterion_affected,
        "arbiter_id": ruling.arbiter_id,
        "resolution": ruling.resolution,
        "timestamp": ruling.ruled_at,
    }, observations_path)

    # 4. Apply the ruling
    result = apply_ruling(
        challenge=challenge,
        ruling=ruling,
        human_input_timeout=human_input_timeout,
    )

    _record_observation({
        "type": "challenge_resolved",
        "task": task_id,
        "challenge_id": challenge.challenge_id,
        "action": result.action,
        "criterion": result.affected_criterion,
        "timestamp": _now_iso(),
    }, observations_path)

    return result


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """Summarize challenges filed for a task:
        python -m blind_tdd.challenge <task_id>
    """
    if len(sys.argv) < 2:
        print("Usage: challenge.py <task_id>")
        sys.exit(1)
    task_id = sys.argv[1]
    entries = [e for e in _iter_log() if e.get("task") == task_id]
    print(f"Challenges for task {task_id!r}: {len(entries)}")
    for e in entries:
        print(f"  {e.get('challenge_id')}: {e.get('criterion')} — {e.get('test_name')}")
