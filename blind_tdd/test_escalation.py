"""Tests for tamper-history escalation (detection feeding the next prediction)."""

from __future__ import annotations

import json

import pytest

from blind_tdd.escalation import (
    KIND_SEAL_RECORD_TAMPERED,
    KIND_TEST_HASH_BREAK,
    LEDGER_PATH,
    evaluate_escalation,
    load_ledger,
    record_tamper,
)


BILLING_TASK = {
    "id": "task-billing-1",
    "title": "Charge flow",
    "files": ["billing/charge.py"],
    "tags": ["payments"],
    "severity": "high",
}


# --------------------------------------------------------------------------- #
# Ledger IO
# --------------------------------------------------------------------------- #

def test_record_and_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record_tamper(BILLING_TASK, KIND_TEST_HASH_BREAK, "2 file(s) modified")
    records = load_ledger()
    assert len(records) == 1
    rec = records[0]
    assert rec["kind"] == KIND_TEST_HASH_BREAK
    assert rec["task_id"] == "task-billing-1"
    assert rec["paths"] == ["billing/charge.py"]
    assert rec["tags"] == ["payments"]
    assert rec["severity"] == "high"
    assert rec["detail"] == "2 file(s) modified"
    assert rec["timestamp"]


def test_load_missing_ledger_is_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_ledger() == []


def test_load_skips_corrupt_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    LEDGER_PATH.parent.mkdir(parents=True)
    LEDGER_PATH.write_text(
        "not json\n"
        + json.dumps({"kind": KIND_SEAL_RECORD_TAMPERED, "task_id": "t-1"}) + "\n"
        + "[1, 2]\n",
        encoding="utf-8",
    )
    records = load_ledger()
    assert len(records) == 1
    assert records[0]["task_id"] == "t-1"


def test_record_non_dict_task_is_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record_tamper("not a task", KIND_TEST_HASH_BREAK)  # type: ignore[arg-type]
    assert load_ledger() == []


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

def _ledger_for(task, kind=KIND_TEST_HASH_BREAK):
    """An in-memory ledger record as record_tamper would write it."""
    from blind_tdd.routing import task_paths
    return [{
        "kind": kind,
        "task_id": str(task.get("id", "")),
        "paths": task_paths(task),
        "tags": [t.lower() for t in task.get("tags", [])],
    }]


def test_same_task_id_escalates():
    records = _ledger_for(BILLING_TASK)
    d = evaluate_escalation({"id": "task-billing-1", "title": "retry"}, records)
    assert d.escalate
    assert "task:task-billing-1" in d.matched
    assert KIND_TEST_HASH_BREAK in d.reason


def test_shared_tag_escalates_case_insensitively():
    records = _ledger_for(BILLING_TASK)
    d = evaluate_escalation({"id": "other", "tags": ["PAYMENTS", "ui"]}, records)
    assert d.escalate
    assert "tag:payments" in d.matched


def test_same_directory_escalates():
    records = _ledger_for(BILLING_TASK)
    d = evaluate_escalation({"id": "other", "files": ["billing/refund.py"]}, records)
    assert d.escalate
    assert d.matched == ["path:billing/refund.py ~ billing/charge.py"]


def test_unrelated_directory_does_not_escalate():
    records = _ledger_for(BILLING_TASK)
    d = evaluate_escalation({"id": "other", "files": ["docs/notes.py"]}, records)
    assert not d.escalate
    assert d.matched == []


def test_top_level_files_only_match_exactly():
    records = [{"kind": "k", "task_id": "a", "paths": ["setup.py"], "tags": []}]
    assert not evaluate_escalation({"id": "b", "files": ["conftest.py"]}, records).escalate
    assert evaluate_escalation({"id": "b", "files": ["setup.py"]}, records).escalate


def test_module_path_neighborhood():
    """public_surface.module is matched via its path form."""
    records = [{
        "kind": "k", "task_id": "a",
        "paths": ["src/engine/core"], "tags": [],
    }]
    d = evaluate_escalation(
        {"id": "b", "public_surface": {"module": "src.engine.loader"}}, records,
    )
    assert d.escalate


def test_matches_are_deduped_across_records():
    records = _ledger_for(BILLING_TASK) + _ledger_for(BILLING_TASK)
    d = evaluate_escalation({"id": "task-billing-1"}, records)
    assert d.matched.count("task:task-billing-1") == 1


def test_empty_ledger_never_escalates():
    d = evaluate_escalation(BILLING_TASK, [])
    assert not d.escalate


def test_non_dict_task_never_escalates():
    d = evaluate_escalation(None, _ledger_for(BILLING_TASK))  # type: ignore[arg-type]
    assert not d.escalate


def test_disk_ledger_feeds_evaluation(tmp_path, monkeypatch):
    """End to end: record on run N, escalate the similar task on run N+1."""
    monkeypatch.chdir(tmp_path)
    record_tamper(BILLING_TASK, KIND_TEST_HASH_BREAK, "seal broken")
    d = evaluate_escalation({"id": "task-billing-2", "files": ["billing/refund.py"]})
    assert d.escalate


def test_deleting_ledger_reverts_to_base_policy(tmp_path, monkeypatch):
    """Escalation is additive-only: removing the ledger lowers nothing below
    the human-set base policy — evaluation just stops escalating."""
    monkeypatch.chdir(tmp_path)
    record_tamper(BILLING_TASK, KIND_TEST_HASH_BREAK)
    assert evaluate_escalation(BILLING_TASK).escalate
    LEDGER_PATH.unlink()
    assert not evaluate_escalation(BILLING_TASK).escalate


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
