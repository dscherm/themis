"""Tests for the blind-TDD setup wizard (blind_tdd.init)."""

from __future__ import annotations

import json

from blind_tdd.init import (
    build_routing,
    load_config,
    main,
    merge_config,
    run_interview,
)


# --------------------------------------------------------------------------- #
# merge_config — preserves unrelated keys
# --------------------------------------------------------------------------- #

def test_merge_preserves_other_gate_keys():
    existing = {
        "gate": {"blind_tdd": {"enabled": True, "enforcement": "warn", "spawner": "manual"}},
        "other": {"keep": 1},
    }
    routing = build_routing(mode="selective", path_globs=["billing/**"], tags=[],
                            min_severity=None, keywords=[])
    merged = merge_config(existing, enabled=None, routing=routing)
    btd = merged["gate"]["blind_tdd"]
    assert btd["enforcement"] == "warn"          # untouched
    assert btd["spawner"] == "manual"            # untouched
    assert btd["enabled"] is True                # untouched (enabled=None)
    assert btd["routing"]["path_globs"] == ["billing/**"]
    assert merged["other"] == {"keep": 1}        # unrelated top-level key kept


def test_merge_sets_enabled_when_provided():
    merged = merge_config({}, enabled=True,
                          routing=build_routing(mode="all", path_globs=[], tags=[],
                                                min_severity=None, keywords=[]))
    assert merged["gate"]["blind_tdd"]["enabled"] is True


def test_merge_on_empty_config_creates_structure():
    merged = merge_config({}, enabled=None,
                          routing=build_routing(mode="all", path_globs=[], tags=[],
                                                min_severity=None, keywords=[]))
    assert merged["gate"]["blind_tdd"]["routing"]["mode"] == "all"


# --------------------------------------------------------------------------- #
# load_config
# --------------------------------------------------------------------------- #

def test_load_config_missing_returns_empty(tmp_path):
    assert load_config(tmp_path / "nope.json") == {}


def test_load_config_bad_json_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_config(p) == {}


# --------------------------------------------------------------------------- #
# non-interactive CLI
# --------------------------------------------------------------------------- #

def test_cli_flags_write_routing(tmp_path):
    cfg = tmp_path / "themis.config.json"
    rc = main([
        "--config", str(cfg),
        "--enable",
        "--path-glob", "billing/**",
        "--tag", "security",
        "--min-severity", "high",
        "--keyword", "password",
        "--yes",
    ])
    assert rc == 0
    data = json.loads(cfg.read_text(encoding="utf-8"))
    btd = data["gate"]["blind_tdd"]
    assert btd["enabled"] is True
    assert btd["routing"]["mode"] == "selective"
    assert btd["routing"]["path_globs"] == ["billing/**"]
    assert btd["routing"]["tags"] == ["security"]
    assert btd["routing"]["min_severity"] == "high"
    assert btd["routing"]["keywords"] == ["password"]


def test_cli_print_only_does_not_write(tmp_path, capsys):
    cfg = tmp_path / "themis.config.json"
    rc = main(["--config", str(cfg), "--tag", "security", "--print"])
    assert rc == 0
    assert not cfg.exists()
    assert '"routing"' in capsys.readouterr().out


def test_cli_merges_into_existing(tmp_path):
    cfg = tmp_path / "themis.config.json"
    cfg.write_text(json.dumps(
        {"gate": {"blind_tdd": {"enabled": True, "enforcement": "strict"}}}
    ), encoding="utf-8")
    main(["--config", str(cfg), "--tag", "security", "--yes"])
    btd = json.loads(cfg.read_text(encoding="utf-8"))["gate"]["blind_tdd"]
    assert btd["enforcement"] == "strict"     # preserved
    assert btd["enabled"] is True             # preserved (no --enable/--disable)
    assert btd["routing"]["tags"] == ["security"]


def test_cli_disable_flag(tmp_path):
    cfg = tmp_path / "themis.config.json"
    main(["--config", str(cfg), "--disable", "--mode", "all", "--yes"])
    btd = json.loads(cfg.read_text(encoding="utf-8"))["gate"]["blind_tdd"]
    assert btd["enabled"] is False
    assert btd["routing"]["mode"] == "all"


# --------------------------------------------------------------------------- #
# interactive interview (injected ask/out)
# --------------------------------------------------------------------------- #

def test_interview_selective_path():
    answers = iter([
        "s",                  # selective
        "billing/**, auth/**",  # path globs
        "security",           # tags
        "high",               # severity
        "password",           # keywords
        "y",                  # enable
    ])
    lines: list[str] = []
    enabled, routing = run_interview(ask=lambda _p: next(answers), out=lines.append)
    assert enabled is True
    assert routing["mode"] == "selective"
    assert routing["path_globs"] == ["billing/**", "auth/**"]
    assert routing["tags"] == ["security"]
    assert routing["min_severity"] == "high"
    assert routing["keywords"] == ["password"]


def test_interview_all_mode_skips_predicate_prompts():
    answers = iter(["a", "n"])  # all tasks, don't enable
    enabled, routing = run_interview(ask=lambda _p: next(answers), out=lambda _s: None)
    assert enabled is False
    assert routing["mode"] == "all"


def test_interview_selective_but_blank_stays_selective_and_warns():
    # An explicit "selective" choice is respected even with no predicates;
    # routing_warnings (surfaced by the CLI summary) flags that it gates nothing,
    # rather than silently gating everything.
    from blind_tdd.routing import routing_warnings

    answers = iter(["s", "", "", "", "", "y"])  # selective, all predicates blank
    enabled, routing = run_interview(ask=lambda _p: next(answers), out=lambda _s: None)
    assert routing["mode"] == "selective"
    assert routing_warnings(routing)
    assert enabled is True


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
