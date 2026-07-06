"""Smart-gate integration for the blind-TDD orchestrator.

This module is the thin adapter between `smart_gate.py` and
`BlindTddOrchestrator`. It answers three questions:

  1. Should the blind gate engage for this commit?
     → Yes iff `gate.blind_tdd.enabled` is true AND a current task is
       identifiable AND that task has `acceptance_criteria` + `public_surface`.

  2. Which phase are we in (red or green)?
     → Red iff no red-state file exists for the current task yet.
     → Green iff a red-state file exists (implementation just finished).

  3. What does the caller do with the result?
     → Red pass: gate passes, saves red state, returns "ready for implementation".
     → Red fail: gate fails with the schema/spawn/coverage error.
     → Green pass: gate passes, clears red state, the task is now fully gated.
     → Green fail: gate fails with hash-mismatch / test-failure / coverage detail.

## Current task identification

Three sources, checked in order:

  a. Env var `RALPH_BLIND_TDD_TASK` — explicit override, used by CLI flags
     and the host loop scripts.
  b. `.themis/current_task.json` — written by the host's task-selection logic.
  c. Fall back to parsing `plan.md` for the first `"passes": false` task.

If none resolve, the blind gate returns `phase="skipped"` with a clear
reason and does not block the commit. This is the correct behavior for
commits that aren't tied to a planned task (e.g. pure docs changes).

## Red state persistence

Between red and green phases, the implementing agent writes code. Smart
gate may be re-invoked several times during that window. We persist the
red-phase output at `.themis/blind_tdd/red_state/<task_id>.json`:

    {
      "task_id": "task-42",
      "timestamp": "2026-04-10T21:00:00Z",
      "test_file_hashes": { "tests/contracts/foo.py": "abc..." },
      "triage_report": { ... },
      "red_session_id": "blind-test_writer-ab12cd34"
    }

On the next gate run, if this file exists, we skip red and go straight
to green. The file is deleted on green-pass so the next task can start
a fresh red phase.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import is_artifact_path
from .escalation import (
    KIND_SEAL_RECORD_TAMPERED,
    KIND_SUPPRESSION_INTRODUCED,
    KIND_TEST_HASH_BREAK,
    evaluate_escalation,
    record_tamper,
)
from .security_pack import (
    apply_pack,
    load_pack,
    normalize_pack_config,
    pack_fingerprint,
    resolve_pack_path,
)
from .suppression import diff_suppressions, scan_repo, summarize_findings
from .orchestrator import (
    BlindTddOrchestrator,
    GreenPhaseResult,
    ManualSpawner,
    RedPhaseResult,
)
from .preflight import preflight_task, MODE_OFF
from .routing import RoutingDecision, evaluate_routing, normalize_routing
from .schema_validator import validate_task


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_blind_tdd_config(config: dict) -> dict:
    """Return the `gate.blind_tdd` sub-config with defaults applied."""
    raw = (config.get("gate") or {}).get("blind_tdd") or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "enforcement": str(raw.get("enforcement", "strict")),  # "strict" | "warn"
        "test_dirs": list(raw.get("test_dirs") or ["tests/contracts/", "tests/integration/"]),
        "public_api_file": str(raw.get("public_api_file", "public_api.md")),
        "max_challenges_per_task": int(raw.get("max_challenges_per_task", 3)),
        "max_challenges_per_criterion": int(raw.get("max_challenges_per_criterion", 1)),
        "human_input_timeout": int(raw.get("human_input_timeout", 3600)),
        "spawner": str(raw.get("spawner", "claude_code")),  # "claude_code" | "manual"
        "spawn_auth": str(raw.get("spawn_auth", "subscription")),  # "subscription" | "api"
        "quality_review": raw.get("quality_review") or {},  # {"enabled": bool}
        "claude_binary": str(raw.get("claude_binary", "claude")),
        "themis_home": raw.get("themis_home", raw.get("ralph_home")),  # None → autodetect; ralph_home = legacy alias
        "spawn_timeout_seconds": int(raw.get("spawn_timeout_seconds", 1800)),
        "preflight": str(raw.get("preflight", "strict")),
        "routing": normalize_routing(raw.get("routing")),
        # Advisory suppression-marker audit (see suppression.py). Default ON:
        # it never fails a run, it only warns and feeds the tamper ledger.
        "suppression_audit": bool(raw.get("suppression_audit", True)),
        # Operator-authored security acceptance criteria appended to matching
        # tasks before the writer spawn (see security_pack.py). Default OFF.
        "security_ac_pack": normalize_pack_config(raw.get("security_ac_pack")),
    }


# ---------------------------------------------------------------------------
# Current task resolution
# ---------------------------------------------------------------------------

def load_current_task(config: dict) -> tuple[dict | None, str]:
    """Resolve the current task via env var, state file, or plan.md.

    Returns (task_dict, source_description).
    task_dict is None if no task could be resolved.
    """
    # (a) Env var override (THEMIS_TASK; RALPH_BLIND_TDD_TASK is a legacy alias)
    themis_env = os.environ.get("THEMIS_TASK", "").strip()
    legacy_env = os.environ.get("RALPH_BLIND_TDD_TASK", "").strip()
    env_id = themis_env or legacy_env
    env_name = "THEMIS_TASK" if themis_env else "RALPH_BLIND_TDD_TASK"
    if env_id:
        task = _find_task_in_plan(env_id)
        if task is not None:
            return task, f"env {env_name}={env_id}"
        # Env var set but no match — that's an error, not a skip
        return None, f"env {env_name}={env_id} but task not found in plan.md"

    # (b) .themis/current_task.json
    state_file = Path(".themis") / "current_task.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("id"):
                # If it's a full task spec, use it directly; otherwise look it
                # up in plan.md by id.
                if "acceptance_criteria" in data or "public_surface" in data:
                    return data, f"{state_file}"
                task = _find_task_in_plan(str(data["id"]))
                if task is not None:
                    return task, f"{state_file} → plan.md"
        except (OSError, json.JSONDecodeError):
            pass

    # (c) plan.md fallback — first task with passes:false
    task = _find_first_incomplete_task()
    if task is not None:
        return task, "plan.md first incomplete task"

    return None, "no current task resolvable"


def _find_task_in_plan(task_id: str) -> dict | None:
    """Locate a task by id inside plan.md JSON code blocks."""
    for plan_path in ("plan.md", "fix_plan.md"):
        p = Path(plan_path)
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for block in _extract_json_blocks(text):
            try:
                obj = json.loads(block)
            except json.JSONDecodeError:
                continue
            for task in _iter_tasks(obj):
                if str(task.get("id", "")) == task_id:
                    return task
    return None


def _find_first_incomplete_task() -> dict | None:
    """Return the first task in plan.md with `passes: false`."""
    for plan_path in ("plan.md", "fix_plan.md"):
        p = Path(plan_path)
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for block in _extract_json_blocks(text):
            try:
                obj = json.loads(block)
            except json.JSONDecodeError:
                continue
            for task in _iter_tasks(obj):
                # Skip tasks marked `deferred_at`; auto-pick would stage a
                # blind-TDD brief that the user explicitly paused. Explicit
                # selection (RALPH_BLIND_TDD_TASK or .themis/current_task.json)
                # still wins — those override the deferral by design.
                if task.get("passes") is False and not task.get("deferred_at"):
                    return task
    return None


def _extract_json_blocks(text: str) -> list[str]:
    """Yield the inside of every ```json ... ``` fence in the text."""
    blocks: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("```json"):
            i += 1
            buf: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            if buf:
                blocks.append("\n".join(buf))
        i += 1
    return blocks


def _iter_tasks(obj) -> list[dict]:
    """Flatten a plan JSON object into a list of task dicts."""
    out: list[dict] = []
    if isinstance(obj, dict):
        if "id" in obj and ("title" in obj or "description" in obj or "acceptance_criteria" in obj):
            out.append(obj)
        for key in ("tasks", "items", "backlog"):
            v = obj.get(key)
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        out.append(item)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                out.append(item)
    return out


# ---------------------------------------------------------------------------
# Red-state persistence
# ---------------------------------------------------------------------------

def _red_state_path(task_id: str) -> Path:
    return Path(".themis") / "blind_tdd" / "red_state" / f"{task_id}.json"


# ---------------------------------------------------------------------------
# Seal-record integrity (optional HMAC over the red-state baseline)
#
# The hash lock defends the *test files*. But the green phase trusts the
# baseline hashes stored in red_state/<task_id>.json — a plain file in the
# project tree. A Bash-capable implementer could edit a locked test AND rewrite
# its stored hash here, re-sealing the cheat. Signing the baseline closes that:
# the HMAC is computed with a key the agent's environment never carries (the
# spawner strips THEMIS_SEAL_KEY, like ANTHROPIC_API_KEY), so a tampered record
# fails verification at green.
#
# Opt-in by design: set THEMIS_SEAL_KEY in the host/gate environment. When it is
# unset, records are written unsigned and verification is a no-op — same behavior
# as before, no false breakage. This raises the bar from "edit a JSON number" to
# "exfiltrate a key the spawner deliberately withholds"; it does NOT defend
# against an adversary who already controls the gate process itself.
# ---------------------------------------------------------------------------

SEAL_KEY_ENV = "THEMIS_SEAL_KEY"


def _seal_key() -> bytes | None:
    raw = os.environ.get(SEAL_KEY_ENV)
    return raw.encode("utf-8") if raw else None


# Red-state keys beyond the test hashes that the HMAC also covers when
# present. Signing them closes the same re-seal hole for the suppression
# baseline (an implementer pre-dating its own markers) and the security-pack
# fingerprint (an implementer pointing the record at a weakened pack).
_SEAL_EXTRA_KEYS = ("suppression_baseline", "security_pack")


def _seal_extras(state: dict) -> dict:
    return {k: state[k] for k in _SEAL_EXTRA_KEYS if k in state}


def _seal_signature(
    task_id: str, test_file_hashes: dict, key: bytes, extras: dict | None = None,
) -> str:
    """Deterministic HMAC-SHA256 over the task id + its sealed test hashes.

    `extras` (suppression baseline, security-pack record) are folded in only
    when present, so records written before those fields existed still verify
    against the original two-field payload.
    """
    payload_obj: dict = {"task_id": task_id, "test_file_hashes": test_file_hashes}
    if extras:
        payload_obj["extras"] = extras
    payload = json.dumps(
        payload_obj,
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify_red_state_seal(state: dict) -> tuple[bool, str]:
    """Check a loaded red-state record's HMAC before its baseline is trusted.

    Returns (ok, reason). Fail-closed: if a record carries a signature but no key
    is available to check it, the seal is NOT trusted. Deleting a signed extras
    field (e.g. the suppression baseline) also fails: the recomputed payload no
    longer matches the stored signature.
    """
    stored = state.get("seal_hmac")
    key = _seal_key()
    if stored is None:
        # Unsigned record (no key set at red time). Back-compat: nothing to verify.
        return True, "unsigned (THEMIS_SEAL_KEY not set at seal time)"
    if key is None:
        return False, ("red-state is signed but THEMIS_SEAL_KEY is unavailable to "
                       "verify it — refusing to trust the baseline")
    expected = _seal_signature(
        state.get("task_id", ""), state.get("test_file_hashes") or {}, key,
        extras=_seal_extras(state),
    )
    if hmac.compare_digest(expected, str(stored)):
        return True, "seal verified"
    return False, "red-state HMAC mismatch — the seal record was modified"


def save_red_state(
    task_id: str,
    result: RedPhaseResult,
    *,
    suppression_baseline: dict | None = None,
    security_pack: dict | None = None,
) -> None:
    p = _red_state_path(task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "task_id": task_id,
        "timestamp": _now_iso(),
        "test_file_hashes": result.test_file_hashes,
        "triage_report": result.triage_report,
        "spawn_agent_id": (result.spawn_result or {}).get("agent_id"),
    }
    if suppression_baseline is not None:
        state["suppression_baseline"] = suppression_baseline
    if security_pack is not None:
        state["security_pack"] = security_pack
    key = _seal_key()
    if key is not None:
        state["seal_hmac"] = _seal_signature(
            task_id, result.test_file_hashes, key, extras=_seal_extras(state),
        )
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def load_red_state(task_id: str) -> dict | None:
    p = _red_state_path(task_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_red_state(task_id: str) -> None:
    p = _red_state_path(task_id)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def _red_state_to_result(state: dict) -> RedPhaseResult:
    return RedPhaseResult(
        passed=True,
        reason="loaded from red state",
        test_file_hashes=dict(state.get("test_file_hashes") or {}),
        triage_report=dict(state.get("triage_report") or {}),
    )


# ---------------------------------------------------------------------------
# Orchestrator construction
# ---------------------------------------------------------------------------

def _make_orchestrator(btd_cfg: dict) -> BlindTddOrchestrator:
    spawner_kind = btd_cfg["spawner"]
    if spawner_kind == "claude_code":
        try:
            from .spawners.claude_code_spawner import ClaudeCodeSpawner
        except ImportError:
            spawner = ManualSpawner()
        else:
            spawner = ClaudeCodeSpawner(
                themis_home=btd_cfg.get("themis_home"),
                claude_binary=btd_cfg.get("claude_binary", "claude"),
                timeout_seconds=btd_cfg.get("spawn_timeout_seconds", 1800),
                strip_api_key=(btd_cfg.get("spawn_auth", "subscription") != "api"),
            )
    else:
        spawner = ManualSpawner()

    # Resolve prompt template paths relative to a configured home, if any
    # (THEMIS_HOME / config themis_home; RALPH_HOME is a legacy alias).
    home = btd_cfg.get("themis_home") or os.environ.get("THEMIS_HOME") or os.environ.get("RALPH_HOME")
    if home:
        base = Path(home) / "templates" / "blind_tdd" / "prompts"
    else:
        # Derive from this module's location: blind_tdd/gate_integration.py
        base = Path(__file__).resolve().parents[1] / "templates" / "blind_tdd" / "prompts"

    return BlindTddOrchestrator(
        spawner=spawner,
        test_dirs=btd_cfg["test_dirs"],
        writer_prompt_path=str(base / "test_writer.md"),
        runner_prompt_path=str(base / "test_runner.md"),
        arbiter_prompt_path=str(base / "arbiter.md"),
    )


# ---------------------------------------------------------------------------
# Public result object
# ---------------------------------------------------------------------------

@dataclass
class BlindGateResult:
    passed: bool
    phase: str  # "skipped" | "red" | "green" | "error"
    message: str
    reason: str = ""
    task_id: str | None = None
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Main entry point — called from smart_gate.main()
# ---------------------------------------------------------------------------

def _check_warn_mode_staleness(btd_cfg: dict) -> None:
    """Emit a prominent warning if blind_tdd enforcement has been 'warn'
    for more than 30 days. Writes to stderr + .themis/alerts.log.

    The adoption guide recommends starting in warn mode to validate the
    flow, then flipping to strict once the first task passes. This check
    catches projects that forget to promote.
    """
    import time as _time

    if btd_cfg["enforcement"] != "warn":
        return

    marker = Path(".themis") / "blind_tdd_warn_since.json"
    state_dir = Path(".themis")
    state_dir.mkdir(parents=True, exist_ok=True)
    now = _time.time()

    if not marker.exists():
        try:
            marker.write_text(
                json.dumps({"warn_since": now, "note": "auto-created on first warn-mode gate run"}),
                encoding="utf-8",
            )
        except OSError:
            pass
        return

    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        warn_since = float(data.get("warn_since", now))
    except (OSError, json.JSONDecodeError, ValueError):
        return

    days = (now - warn_since) / 86400
    if days < 30:
        return

    msg = (
        f"⚠ blind_tdd enforcement has been 'warn' for {int(days)} days.\n"
        f"  The adoption guide recommends flipping to 'strict' after the\n"
        f"  first successful gated task. Edit gate.blind_tdd.enforcement\n"
        f"  (gate.blind_tdd.enforcement) to 'strict', or set it back to 'strict'\n"
        f"  to dismiss this warning."
    )
    print(msg, file=sys.stderr)
    try:
        ts = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        with open(state_dir / "alerts.log", "a", encoding="utf-8") as f:
            f.write(f"[{ts}] blind_tdd warn-mode stale ({int(days)}d)\n")
    except OSError:
        pass


PACK_STALE_DAYS = 90
# A stale pack is checked on every gate invocation; without a throttle it
# would re-print and re-log each run. One nudge per interval is enough.
PACK_NUDGE_INTERVAL_DAYS = 7


def _check_pack_staleness(pack_path: Path) -> None:
    """Nudge (stderr + alerts.log, never fatal) when the security pack file
    hasn't been touched in PACK_STALE_DAYS. A tailored pack rots as the
    codebase grows new entry points and sinks — the fix is re-running the
    pack interview (`/blind-tdd:security-pack-setup`), same spirit as the
    warn-mode staleness check above. Throttled via a marker file so a stale
    pack nudges once per PACK_NUDGE_INTERVAL_DAYS, not once per run."""
    import time as _time

    try:
        age_days = (_time.time() - pack_path.stat().st_mtime) / 86400
    except OSError:
        return
    if age_days < PACK_STALE_DAYS:
        return

    marker = Path(".themis") / "blind_tdd" / "pack_stale_nudged.json"
    now = _time.time()
    try:
        last = float(json.loads(marker.read_text(encoding="utf-8"))["last_nudge"])
        if (now - last) / 86400 < PACK_NUDGE_INTERVAL_DAYS:
            return
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        pass  # no/corrupt marker → nudge and (re)write it

    msg = (
        f"⚠ security AC pack {pack_path} is {int(age_days)} days old.\n"
        f"  New entry points and sinks added since then aren't covered by "
        f"its criteria.\n"
        f"  Re-run the pack interview (/blind-tdd:security-pack-setup or "
        f"python -m blind_tdd.pack_wizard) to refresh it."
    )
    print(msg, file=sys.stderr)
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state_dir = Path(".themis")
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / "alerts.log", "a", encoding="utf-8") as f:
            f.write(f"[{ts}] security AC pack stale ({int(age_days)}d): {pack_path}\n")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({"last_nudge": now, "pack_path": str(pack_path)}),
            encoding="utf-8",
        )
    except OSError:
        pass


def run_blind_tdd_gate(config: dict) -> BlindGateResult:
    """Run the appropriate blind-TDD phase for the current task.

    Returns a BlindGateResult. Never raises — any internal failure is
    represented as `passed=False, phase="error"` with a reason.
    """
    btd_cfg = get_blind_tdd_config(config)

    if not btd_cfg["enabled"]:
        return BlindGateResult(
            passed=True, phase="skipped",
            message="blind-tdd disabled in config",
        )

    _check_warn_mode_staleness(btd_cfg)

    task, source = load_current_task(config)
    if task is None:
        # If the user has the gate enabled but no task is resolvable, we
        # still allow the commit but log a clear message. An alternative
        # would be to fail — but then doc-only commits get blocked.
        return BlindGateResult(
            passed=True, phase="skipped",
            message=f"blind-tdd enabled but no current task ({source})",
        )

    task_id = str(task.get("id", "unknown"))

    # Routing: does the policy select this task for the (expensive) blind gate?
    # The decision is made from operator-authored task metadata, never from the
    # implementing agent — see routing.py's trust note. A non-selected task is
    # skipped here so an ordinary in-loop verifier can cover it.
    decision = evaluate_routing(task, btd_cfg["routing"])

    # Tamper history feeds the next prediction: a hash break on an earlier run
    # was recorded as evidence (see escalation.py), and a task similar to a
    # tampered one is escalated here — gated even where the selective policy
    # would skip it. Additive only: history can force the gate on, never off.
    # The match is also surfaced in `details.tamper_escalation`, the pre-spawn
    # signal a containment tier (an OS-level sandbox) would key off.
    escalation = evaluate_escalation(task)
    escalation_details = {"tamper_escalation": escalation.matched} if escalation.escalate else {}
    if escalation.escalate and not decision.gate:
        decision = RoutingDecision(
            gate=True,
            reason=f"escalated by tamper history: {escalation.reason}",
            matched=escalation.matched,
        )

    if not decision.gate:
        return BlindGateResult(
            passed=True,
            phase="skipped",
            message=f"task {task_id!r} not selected by blind-tdd routing: {decision.reason}",
            reason="routing_excluded",
            task_id=task_id,
            details={"routing_reason": decision.reason, "source": source},
        )

    # Security AC pack: append the operator-authored security criteria to a
    # matching task BEFORE anything downstream sees it — the writer derives
    # sealed tests for them, and the green coverage check holds the
    # implementation to them. Applied identically on the red and green
    # invocations (same task + same pack → same augmented task), with the
    # pack fingerprint recorded in red state so a pack that changes mid-task
    # fails green with a specific reason instead of a confusing coverage gap.
    pack_record: dict | None = None
    sp_cfg = btd_cfg["security_ac_pack"]
    if sp_cfg["enabled"]:
        pack_match = evaluate_routing(task, sp_cfg["match"])
        if pack_match.gate:
            pack_path = resolve_pack_path(sp_cfg["pack_path"], btd_cfg.get("themis_home"))
            pack, pack_err = load_pack(pack_path)
            if pack is None:
                # Fail loudly: silently skipping would quietly drop the
                # security criteria the operator asked for (fail-open).
                fail = btd_cfg["enforcement"] == "strict"
                return BlindGateResult(
                    passed=not fail,
                    phase="error",
                    message=f"security AC pack enabled but unusable: {pack_err}",
                    reason="security_pack_invalid",
                    task_id=task_id,
                    details={"pack_path": str(pack_path), "source": source},
                )
            _check_pack_staleness(pack_path)
            task, injected_ids = apply_pack(task, pack)
            pack_record = {
                "fingerprint": pack_fingerprint(pack),
                "version": pack.get("version"),
                "injected_ids": injected_ids,
            }

    # Validate the task spec before spending API budget on an agent spawn.
    vr = validate_task(task)
    if not vr.valid:
        fail = btd_cfg["enforcement"] == "strict"
        return BlindGateResult(
            passed=not fail,
            phase="error",
            message=(
                f"task {task_id!r} failed blind-tdd schema validation:\n"
                + "\n".join(f"  - {e}" for e in vr.errors)
            ),
            reason="schema_invalid",
            task_id=task_id,
            details={"errors": vr.errors, "warnings": vr.warnings, "source": source},
        )

    # Preflight: structural quality check before we burn API budget.
    # Controlled by gate.blind_tdd.preflight = strict | warn | off (default strict).
    preflight = preflight_task(task, config)
    if not preflight.ready:
        # strict mode, at least one check failed
        fail = btd_cfg["enforcement"] == "strict"
        return BlindGateResult(
            passed=not fail,
            phase="error",
            message=(
                f"task {task_id!r} failed blind-tdd preflight checks:\n"
                + "\n".join(f"  - {e}" for e in preflight.errors)
            ),
            reason="preflight_failed",
            task_id=task_id,
            details={
                "preflight_mode": preflight.mode,
                "errors": preflight.errors,
                "warnings": preflight.warnings,
                "source": source,
            },
        )

    orch = _make_orchestrator(btd_cfg)

    # Have we already completed the red phase for this task?
    red_state = load_red_state(task_id)

    if red_state is None:
        # ---- RED PHASE ----
        try:
            red = orch.run_red_phase(task)
        except Exception as e:  # defensive — must never raise to smart_gate
            return BlindGateResult(
                passed=False, phase="error",
                message=f"red phase crashed: {type(e).__name__}: {e}",
                reason="red_crash",
                task_id=task_id,
            )

        if not red.passed:
            # Manual spawner is a special non-failure: it's an advisory
            # "brief staged, run the agent, re-invoke the gate".
            is_manual_pending = bool((red.spawn_result or {}).get("manual_mode"))
            if is_manual_pending:
                return BlindGateResult(
                    passed=True,
                    phase="red_pending",
                    message=(
                        f"manual spawner staged brief for task {task_id!r} at "
                        f"{(red.spawn_result or {}).get('brief_path', '<unknown>')}. "
                        f"Run the writer agent and re-invoke the gate to continue."
                    ),
                    task_id=task_id,
                    details={"spawn_result": red.spawn_result},
                )
            fail = btd_cfg["enforcement"] == "strict"
            return BlindGateResult(
                passed=not fail,
                phase="red",
                message=f"red phase did not pass: {red.reason}",
                reason="red_failed",
                task_id=task_id,
                details={
                    "spawn_result": red.spawn_result,
                    "violations": red.violations,
                    "missing_criteria": red.coverage.missing if red.coverage else [],
                },
            )

        # Baseline the repo's suppression markers at seal time — the green
        # phase diffs against this to spot markers introduced during the
        # implementation window (advisory; see suppression.py).
        suppression_baseline = scan_repo() if btd_cfg["suppression_audit"] else None
        save_red_state(
            task_id, red,
            suppression_baseline=suppression_baseline,
            security_pack=pack_record,
        )

        return BlindGateResult(
            passed=True, phase="red",
            message=(
                f"red phase passed for task {task_id!r}. "
                f"{len(red.test_file_hashes)} test file(s) hashed. "
                f"Implementation phase may begin."
            ),
            task_id=task_id,
            details={
                "tested_criteria": sorted((red.coverage.covered_by_tests or {}).keys())
                if red.coverage else [],
                "needs_human_criteria": red.coverage.covered_by_triage if red.coverage else [],
                "test_file_count": len(red.test_file_hashes),
                **escalation_details,
            },
        )

    # ---- GREEN PHASE ----
    # Before trusting the stored baseline, verify the seal record wasn't itself
    # tampered with (a Bash-capable implementer rewriting both a locked test and
    # its recorded hash). No-op when records are unsigned (THEMIS_SEAL_KEY unset).
    seal_ok, seal_reason = verify_red_state_seal(red_state)
    if not seal_ok:
        # Too late to contain this run — but excellent evidence for the next
        # one: tasks like this get escalated by routing from here on.
        record_tamper(task, KIND_SEAL_RECORD_TAMPERED, seal_reason)
        fail = btd_cfg["enforcement"] == "strict"
        return BlindGateResult(
            passed=not fail,
            phase="green",
            message=f"seal record integrity check failed: {seal_reason}",
            reason="seal_tampered",
            task_id=task_id,
            details={"seal_reason": seal_reason, **escalation_details},
        )

    # The pack in effect now must be the pack in effect at red — the sealed
    # tests were derived from it. A mismatch in either direction (changed,
    # newly enabled, or disabled mid-task) invalidates the red baseline.
    recorded_fp = (red_state.get("security_pack") or {}).get("fingerprint")
    current_fp = (pack_record or {}).get("fingerprint")
    if recorded_fp != current_fp:
        fail = btd_cfg["enforcement"] == "strict"
        return BlindGateResult(
            passed=not fail,
            phase="green",
            message=(
                "security AC pack changed between red and green phases "
                f"(sealed fingerprint {recorded_fp!r}, current {current_fp!r}). "
                "Restore the pack/config in effect at red, or clear "
                f"{_red_state_path(task_id)} to restart the task from red."
            ),
            reason="security_pack_changed",
            task_id=task_id,
            details={
                "sealed_pack_fingerprint": recorded_fp,
                "current_pack_fingerprint": current_fp,
                **escalation_details,
            },
        )

    # Same pack, but the task's own ACs changed mid-task → the pack criteria
    # renumber (a sealed test says `Covers: AC-4`, the augmented task now calls
    # it AC-5). That would surface as a baffling coverage gap; catch it here
    # with a specific reason instead. injected_ids is HMAC-covered with the
    # rest of the security_pack record.
    recorded_ids = (red_state.get("security_pack") or {}).get("injected_ids")
    current_ids = (pack_record or {}).get("injected_ids")
    if recorded_ids != current_ids:
        fail = btd_cfg["enforcement"] == "strict"
        return BlindGateResult(
            passed=not fail,
            phase="green",
            message=(
                "security-pack criteria renumbered between red and green "
                f"(sealed ids {recorded_ids!r}, current {current_ids!r}) — the "
                "task's own acceptance_criteria changed after the tests were "
                "sealed. Restore the task spec, or clear "
                f"{_red_state_path(task_id)} to restart the task from red."
            ),
            reason="security_pack_desynced",
            task_id=task_id,
            details={
                "sealed_injected_ids": recorded_ids,
                "current_injected_ids": current_ids,
                **escalation_details,
            },
        )

    red_result = _red_state_to_result(red_state)
    try:
        green = orch.run_green_phase(task, red_result)
    except Exception as e:
        return BlindGateResult(
            passed=False, phase="error",
            message=f"green phase crashed: {type(e).__name__}: {e}",
            reason="green_crash",
            task_id=task_id,
        )

    if not green.passed:
        if green.hash_break:
            # A sealed test's fingerprint changed between red and green. The
            # run is already failed above; the record's job is the future —
            # it feeds routing so similar tasks are escalated before spawn.
            record_tamper(task, KIND_TEST_HASH_BREAK, green.reason)
        fail = btd_cfg["enforcement"] == "strict"
        return BlindGateResult(
            passed=not fail,
            phase="green",
            message=f"green phase did not pass: {green.reason}",
            reason="green_failed",
            task_id=task_id,
            details={
                "hash_match": green.hash_match,
                "hash_break": green.hash_break,
                "violations": green.violations,
                "green_report_summary": _summarize_green(green),
                **escalation_details,
            },
        )

    # Advisory suppression audit: markers introduced during the implementation
    # window warn on this result and land in the tamper ledger (feeding the
    # next run's escalation) — they never fail this run. See suppression.py.
    suppression_details: dict = {}
    suppression_warning = ""
    baseline = red_state.get("suppression_baseline")
    if btd_cfg["suppression_audit"] and isinstance(baseline, dict):
        findings = diff_suppressions(baseline, scan_repo())
        if findings:
            summary = summarize_findings(findings)
            record_tamper(task, KIND_SUPPRESSION_INTRODUCED, summary)
            suppression_details = {"suppression_findings": findings}
            suppression_warning = (
                f" WARNING (advisory): {len(findings)} lint/security suppression "
                f"marker(s) introduced during implementation — recorded to the "
                f"tamper ledger: {summary}"
            )

    # Green pass → clear red state so the next task starts fresh
    clear_red_state(task_id)

    # Optional post-green quality review (advisory deep-interview-style).
    # Skipped when the config flag is off or the reviewer artifact is
    # already fresh. Returns its own phase on first invocation so the
    # LLM knows to conduct the interview.
    qr_cfg = btd_cfg.get("quality_review") or {}
    if isinstance(qr_cfg, dict) and qr_cfg.get("enabled"):
        qr_result = _run_quality_review_phase(task, task_id, btd_cfg["test_dirs"])
        if qr_result is not None:
            return qr_result

    return BlindGateResult(
        passed=True, phase="green",
        message=(
            f"green phase passed for task {task_id!r}: "
            f"{green.green_report.get('tests_passed', 0)} test(s) passing, "
            f"coverage verified." + suppression_warning
        ),
        task_id=task_id,
        details={
            "tests_passed": green.green_report.get("tests_passed"),
            "tests_failed": green.green_report.get("tests_failed"),
            "hash_match": True,
            **escalation_details,
            **suppression_details,
        },
    )


def _run_quality_review_phase(
    task: dict,
    task_id: str,
    test_dirs: list[str],
) -> "BlindGateResult | None":
    """Post-green deep-interview-style quality review. Returns a
    BlindGateResult with phase="quality_review_pending" on first run (staging
    a brief for the LLM to pick up), or None if the review is already
    complete (caller falls through to the normal green return).

    Advisory only — failure to adjudicate does not block the commit in warn
    mode. The interview output at
    `.themis/blind_tdd/quality_review/<task_id>.md` is a human-curated
    artifact; this function never writes it automatically.
    """
    from . import quality_review as qr
    home = os.environ.get("THEMIS_HOME") or os.environ.get("RALPH_HOME")
    if home:
        prompt_path = Path(home) / "prompts" / "quality-reviewer.md"
    else:
        prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "quality-reviewer.md"

    # Enumerate test files from the configured test_dirs. Cheaper than
    # threading test_file_hashes through from the green phase, and equivalent
    # since the green runner just verified the full set.
    test_files: list[Path] = []
    for td in test_dirs:
        d = Path(td)
        if d.is_dir():
            test_files.extend(
                p for p in d.rglob("test_*.py")
                if p.is_file() and not is_artifact_path(p)
            )
    public_module = (task.get("public_surface") or {}).get("module")
    src_files: list[Path] = []
    if public_module:
        p = Path(public_module)
        if p.exists():
            src_files.append(p)

    brief_path = qr.PENDING_DIR / f"{task_id}.md"
    if qr.is_review_complete(task_id, brief_path):
        # Clean up the stale brief after the review was finalized.
        try:
            brief_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None  # caller returns normal green pass

    scan_result = qr.scan(task_id, test_files, src_files, public_module=public_module)

    # Scanner found nothing — auto-complete with a clean review.
    if scan_result.is_clean():
        qr.FINAL_DIR.mkdir(parents=True, exist_ok=True)
        qr.final_review_path(task_id).write_text(
            f"# Quality Review — `{task_id}`\n\n"
            "**Verdict:** PASS\n\n"
            "Scanner surfaced zero candidates; no interview needed.\n",
            encoding="utf-8",
        )
        return None

    qr.write_pending_brief(scan_result, prompt_path)
    return BlindGateResult(
        passed=True,
        phase="quality_review_pending",
        message=(
            f"quality review staged for task {task_id!r}: "
            f"{len(scan_result.candidates)} candidate(s) at {brief_path}. "
            f"Run the reviewer (conduct interview via AskUserQuestion) and "
            f"write the final verdict to {qr.final_review_path(task_id)}."
        ),
        task_id=task_id,
        details={
            "candidate_count": len(scan_result.candidates),
            "candidate_kinds": sorted({c.kind for c in scan_result.candidates}),
            "brief_path": str(brief_path),
            "final_path": str(qr.final_review_path(task_id)),
        },
    )


def _summarize_green(green: GreenPhaseResult) -> dict:
    rep = green.green_report or {}
    return {
        "tests_collected": rep.get("tests_collected"),
        "tests_passed": rep.get("tests_passed"),
        "tests_failed": rep.get("tests_failed"),
        "overall": rep.get("overall"),
    }
