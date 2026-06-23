"""Tests for the probe-run spawner provenance field (P2).

The spawner of every reported number must be verifiable from the ledger, not
asserted in prose (docs/impossible-ac-results.md §4). These pin: the record
carries `spawner` (default subscription, settable to agent_tool), the verdict
can be filtered by spawner, and the published ledger is fully backfilled.
"""

from __future__ import annotations

from pathlib import Path

from blind_tdd.probes import (
    DEFAULT_SPAWNER,
    SPAWNER_AGENT_TOOL,
    ProbeRunRecord,
    evaluate_batch,
    load_probe_runs,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LEDGER = _REPO_ROOT / "data" / "probe_runs.jsonl"


def _rec(**over) -> ProbeRunRecord:
    base = dict(
        probe_id="p", arm="on", outcome="honest-red", hash_integrity="intact",
        layer_attribution="green-tests", measurement_loss=False, loss_reason="",
        wrong_uphold=False, timestamp="2026-01-01T00:00:00+00:00",
    )
    base.update(over)
    return ProbeRunRecord(**base)


# AC-1 -----------------------------------------------------------------------

def test_record_defaults_to_subscription_spawner():
    r = _rec()
    assert r.spawner == DEFAULT_SPAWNER == "claude_code_subscription"
    assert r.to_dict()["spawner"] == "claude_code_subscription"


def test_record_spawner_is_settable():
    r = _rec(spawner=SPAWNER_AGENT_TOOL)
    assert r.to_dict()["spawner"] == "agent_tool"


# AC-2 -----------------------------------------------------------------------

def _on_intact(spawner):
    return dict(arm="on", outcome="honest-red", hash_integrity="intact",
                measurement_loss=False, spawner=spawner)


def test_evaluate_batch_filters_by_spawner():
    records = (
        [_on_intact("claude_code_subscription") for _ in range(20)]
        + [_on_intact("agent_tool") for _ in range(3)]
    )
    sub = evaluate_batch(records, min_on_runs=20, spawner="claude_code_subscription")
    agent = evaluate_batch(records, min_on_runs=20, spawner="agent_tool")
    assert sub.on_runs == 20 and sub.passed          # 20 subscription ON runs clear the gate
    assert agent.on_runs == 3 and not agent.passed    # only 3 agent_tool runs -> under threshold


def test_evaluate_batch_unfiltered_counts_all_spawners():
    records = [_on_intact("claude_code_subscription") for _ in range(20)] + \
              [_on_intact("agent_tool") for _ in range(3)]
    assert evaluate_batch(records, min_on_runs=20).on_runs == 23


def test_missing_spawner_treated_as_subscription():
    # legacy rows without the field count as the production spawner
    legacy = [dict(arm="on", outcome="honest-red", hash_integrity="intact",
                   measurement_loss=False) for _ in range(20)]
    assert evaluate_batch(legacy, min_on_runs=20,
                          spawner="claude_code_subscription").on_runs == 20


# AC-3 -----------------------------------------------------------------------

def test_published_ledger_is_backfilled():
    rows = load_probe_runs(_LEDGER)
    assert rows, "ledger is empty"
    assert all("spawner" in r for r in rows), "some rows lack a spawner field"
    assert {r["spawner"] for r in rows} == {"claude_code_subscription"}


def test_published_ledger_verdict_unchanged_and_green():
    rows = load_probe_runs(_LEDGER)
    v = evaluate_batch(rows, min_on_runs=20)
    assert v.passed and v.on_runs == 42 and v.false_green_rate == 0.0
