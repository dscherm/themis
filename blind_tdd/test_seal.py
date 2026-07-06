"""Tests for the red-state seal HMAC (hardening the baseline against re-sealing).

The hash lock defends the test files; this defends the *record of the lock*. With
a key set, a tampered red-state record fails verification at green — so an agent
that rewrites both a locked test and its stored hash cannot re-seal the cheat
without the key (which the spawner withholds). Unsigned mode (no key) is
unchanged behavior.
"""

from __future__ import annotations

import types

from blind_tdd import gate_integration as gi
from blind_tdd.spawners.claude_code_spawner import subscription_env

_KEY = "test-secret-key"


def _fake_red(hashes: dict):
    return types.SimpleNamespace(
        test_file_hashes=hashes, triage_report={}, spawn_result=None,
    )


def test_unsigned_when_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv(gi.SEAL_KEY_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    gi.save_red_state("t1", _fake_red({"tests/a.py": "h1"}))
    state = gi.load_red_state("t1")
    assert "seal_hmac" not in state
    ok, reason = gi.verify_red_state_seal(state)
    assert ok and "unsigned" in reason


def test_sign_and_verify_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv(gi.SEAL_KEY_ENV, _KEY)
    monkeypatch.chdir(tmp_path)
    gi.save_red_state("t1", _fake_red({"tests/a.py": "h1"}))
    state = gi.load_red_state("t1")
    assert state.get("seal_hmac")
    ok, reason = gi.verify_red_state_seal(state)
    assert ok, reason


def test_tampered_hashes_fail_verification(tmp_path, monkeypatch):
    monkeypatch.setenv(gi.SEAL_KEY_ENV, _KEY)
    monkeypatch.chdir(tmp_path)
    gi.save_red_state("t1", _fake_red({"tests/a.py": "h1"}))
    state = gi.load_red_state("t1")
    # the attacker rewrites the recorded hash, but can't recompute the HMAC
    state["test_file_hashes"]["tests/a.py"] = "tampered-to-match-a-weak-test"
    ok, reason = gi.verify_red_state_seal(state)
    assert not ok and "mismatch" in reason


def test_signed_record_fails_closed_without_key(monkeypatch):
    monkeypatch.setenv(gi.SEAL_KEY_ENV, _KEY)
    sig = gi._seal_signature("t1", {"tests/a.py": "h1"}, _KEY.encode())
    state = {"task_id": "t1", "test_file_hashes": {"tests/a.py": "h1"}, "seal_hmac": sig}
    monkeypatch.delenv(gi.SEAL_KEY_ENV, raising=False)  # key gone at verify time
    ok, reason = gi.verify_red_state_seal(state)
    assert not ok and "unavailable" in reason


def test_wrong_key_fails_verification(monkeypatch):
    sig = gi._seal_signature("t1", {"tests/a.py": "h1"}, b"keyA")
    state = {"task_id": "t1", "test_file_hashes": {"tests/a.py": "h1"}, "seal_hmac": sig}
    monkeypatch.setenv(gi.SEAL_KEY_ENV, "keyB")
    ok, _ = gi.verify_red_state_seal(state)
    assert not ok


def test_extras_signed_roundtrip(tmp_path, monkeypatch):
    """Suppression baseline + pack record ride under the same HMAC."""
    monkeypatch.setenv(gi.SEAL_KEY_ENV, _KEY)
    monkeypatch.chdir(tmp_path)
    gi.save_red_state(
        "t1", _fake_red({"tests/a.py": "h1"}),
        suppression_baseline={"src/app.py": {"noqa": 1}},
        security_pack={"fingerprint": "fp1", "version": 1, "injected_ids": ["AC-2"]},
    )
    state = gi.load_red_state("t1")
    assert state["suppression_baseline"] == {"src/app.py": {"noqa": 1}}
    ok, reason = gi.verify_red_state_seal(state)
    assert ok, reason


def test_tampered_suppression_baseline_fails(tmp_path, monkeypatch):
    """An implementer pre-dating its own markers into the baseline is caught."""
    monkeypatch.setenv(gi.SEAL_KEY_ENV, _KEY)
    monkeypatch.chdir(tmp_path)
    gi.save_red_state(
        "t1", _fake_red({"tests/a.py": "h1"}),
        suppression_baseline={},
    )
    state = gi.load_red_state("t1")
    state["suppression_baseline"] = {"src/app.py": {"nosec": 5}}
    ok, reason = gi.verify_red_state_seal(state)
    assert not ok and "mismatch" in reason


def test_deleting_signed_extras_fails(tmp_path, monkeypatch):
    """Dropping a signed extras field changes the payload — fail-closed."""
    monkeypatch.setenv(gi.SEAL_KEY_ENV, _KEY)
    monkeypatch.chdir(tmp_path)
    gi.save_red_state(
        "t1", _fake_red({"tests/a.py": "h1"}),
        security_pack={"fingerprint": "fp1", "version": 1, "injected_ids": []},
    )
    state = gi.load_red_state("t1")
    del state["security_pack"]
    ok, _ = gi.verify_red_state_seal(state)
    assert not ok


def test_old_two_field_records_still_verify(monkeypatch):
    """Back-compat: a record signed before extras existed uses the original
    two-field payload and must keep verifying."""
    sig = gi._seal_signature("t1", {"tests/a.py": "h1"}, _KEY.encode())
    state = {"task_id": "t1", "test_file_hashes": {"tests/a.py": "h1"}, "seal_hmac": sig}
    monkeypatch.setenv(gi.SEAL_KEY_ENV, _KEY)
    ok, reason = gi.verify_red_state_seal(state)
    assert ok, reason


def test_spawner_strips_seal_key():
    # The key must never reach a spawned agent, regardless of auth mode.
    env = subscription_env({"THEMIS_SEAL_KEY": "secret", "FOO": "bar"}, strip_api_key=False)
    assert "THEMIS_SEAL_KEY" not in env
    assert env.get("FOO") == "bar"   # unrelated vars preserved


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
