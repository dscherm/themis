"""Tamper-history escalation — detection on run N feeds the prediction for run N+1.

A hash break is caught after the fact: by the time the green phase sees a
fingerprint mismatch, the tampering has already run. Nothing here tries to
contain the run that broke the seal — for that run it is too late. What the
break *is* good for is learning. It is strong evidence that this task, this
area of the codebase, this kind of work, produced a tampering attempt. So the
gate records the event in a ledger, and routing consults the ledger before the
next run spawns anything: a task similar to a previously tampered one is
escalated — gated even where the selective policy would have skipped it — and
the match is exposed (`evaluate_escalation`) as the pre-spawn signal an outer
containment tier (an OS-level sandbox around the whole run) can key off before
any agent exists. Detection informs the next prediction; it never triggers a
wall around a run already in progress.

Similarity is judged from the same operator-authored metadata routing already
trusts (see routing.py's trust note) — the task id, its declared paths, its
tags — never from anything the implementing agent writes. A match means: same
task re-run, a task in the same directory, or a task sharing a tag with the
tampered one.

The ledger is additive-only by design: a match can force the gate ON for a
task the base policy would have skipped; nothing here can turn the gate OFF.
A worker that deletes `.themis/blind_tdd/tamper_ledger.jsonl` therefore only
reverts to the human-set base policy — it cannot lower the wall below where
the operator put it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .routing import task_paths


LEDGER_PATH = Path(".themis") / "blind_tdd" / "tamper_ledger.jsonl"

# Recorded event kinds.
KIND_TEST_HASH_BREAK = "test_hash_break"        # a sealed test's fingerprint changed
KIND_SEAL_RECORD_TAMPERED = "seal_record_tampered"  # the baseline record's HMAC failed


@dataclass
class EscalationDecision:
    """The outcome of matching a task against the tamper ledger."""

    escalate: bool
    reason: str
    matched: list[str] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_tags(task: dict) -> list[str]:
    tags = task.get("tags")
    if not isinstance(tags, list):
        return []
    return [t.strip().lower() for t in tags if isinstance(t, str) and t.strip()]


def record_tamper(task: dict, kind: str, detail: str = "") -> None:
    """Append a tamper-evidence record for `task` to the ledger.

    Called by the gate when the green phase catches a broken seal. Stores only
    operator-authored metadata (id, declared paths, tags, severity) — the
    fields future routing decisions are allowed to trust. Never raises: a
    ledger write failure must not mask the gate failure that triggered it.
    """
    if not isinstance(task, dict):
        return
    severity = task.get("severity")
    record = {
        "timestamp": _now_iso(),
        "kind": kind,
        "detail": detail,
        "task_id": str(task.get("id", "")).strip(),
        "paths": task_paths(task),
        "tags": _task_tags(task),
        "severity": severity if isinstance(severity, str) else None,
    }
    try:
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_ledger() -> list[dict]:
    """All parseable ledger records. A corrupt line is skipped, never fatal."""
    if not LEDGER_PATH.exists():
        return []
    try:
        text = LEDGER_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _parent_dir(path: str) -> str:
    p = path.replace("\\", "/").strip("/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def _same_neighborhood(a: str, b: str) -> bool:
    """Exact path match, or two entries in the same directory.

    Top-level entries (no directory component) only match exactly — "a.py"
    and "b.py" are not neighbors, or every root-level task would escalate
    every other.
    """
    a = a.replace("\\", "/").strip("/")
    b = b.replace("\\", "/").strip("/")
    if not a or not b:
        return False
    if a == b:
        return True
    pa, pb = _parent_dir(a), _parent_dir(b)
    return bool(pa) and pa == pb


def _match_record(task: dict, record: dict) -> list[str]:
    matched: list[str] = []

    tid = str(task.get("id", "")).strip()
    rid = str(record.get("task_id", "")).strip()
    if tid and tid == rid:
        matched.append(f"task:{tid}")

    shared = set(_task_tags(task)) & {
        t for t in (record.get("tags") or []) if isinstance(t, str)
    }
    matched.extend(f"tag:{t}" for t in sorted(shared))

    rec_paths = [p for p in (record.get("paths") or []) if isinstance(p, str)]
    for p in task_paths(task):
        for q in rec_paths:
            if _same_neighborhood(p, q):
                matched.append(f"path:{p} ~ {q}")
                break
    return matched


def evaluate_escalation(task: dict, records: list[dict] | None = None) -> EscalationDecision:
    """Should this task be escalated because of prior tamper evidence?

    This is the prediction the ledger feeds. The gate uses it to override a
    selective-routing skip; a containment tier would call it *before spawning
    anything* to decide whether the whole run belongs in a sandbox. When
    `records` is None the ledger is loaded from disk.
    """
    if not isinstance(task, dict):
        return EscalationDecision(escalate=False, reason="non-dict task — nothing to match")
    if records is None:
        records = load_ledger()

    matched: list[str] = []
    kinds: set[str] = set()
    for record in records:
        hits = _match_record(task, record)
        if hits:
            matched.extend(hits)
            kinds.add(str(record.get("kind", "unknown")))

    matched = list(dict.fromkeys(matched))  # dedupe, keep order
    if matched:
        return EscalationDecision(
            escalate=True,
            reason=(
                "prior tamper evidence (" + ", ".join(sorted(kinds)) + ") "
                "matches this task: " + "; ".join(matched)
            ),
            matched=matched,
        )
    return EscalationDecision(escalate=False, reason="no tamper record matches this task")
