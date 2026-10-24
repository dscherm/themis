"""`blind_tdd.mutate --json` persists per-mutant verdicts, including which tests killed each.

Until now `run_mutation` returned a summary and a survivors list and forgot the kills.
Downstream readers (the schermness evidence ladder) need the mutant x test graph: which
test was seen to fail under which change. A killed mutant with no named killer is a
number, not evidence.
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from blind_tdd import mutate as M


def _fixture(tmp_path: Path) -> tuple[Path, str]:
    src = tmp_path / "calc.py"
    src.write_text(textwrap.dedent('''
        def bigger(a, b):
            return a if a > b else b
        '''), encoding="utf-8")
    test = tmp_path / "test_calc.py"
    test.write_text(textwrap.dedent('''
        from calc import bigger

        def test_bigger_picks_the_larger():
            assert bigger(2, 1) == 2

        def test_bigger_is_symmetric():
            assert bigger(1, 2) == 2
        '''), encoding="utf-8")
    cmd = f'"{sys.executable}" -m pytest test_calc.py -q --no-header -p no:cacheprovider -rfE'
    return src, cmd


def test_json_records_each_mutant_and_its_killers(tmp_path):
    src, cmd = _fixture(tmp_path)
    out = tmp_path / "MUT.json"
    rc = M.main([str(src), "--test-cmd", cmd, "--cwd", str(tmp_path), "--json", str(out)])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["source"].endswith("calc.py")
    assert data["summary"]["total"] == len(data["mutants"]) > 0
    killed = [m for m in data["mutants"] if m["verdict"] == "killed"]
    assert killed, "the comparison operator swap must be caught by the two tests"
    for m in killed:
        assert m["killers"], "a killed mutant must name the test(s) that failed: %r" % m
        assert all("::test_" in k for k in m["killers"])
    for m in data["mutants"]:
        assert set(m) >= {"operator", "lineno", "before", "after", "verdict", "killers"}


def test_run_mutation_still_returns_the_summary_without_json(tmp_path):
    src, cmd = _fixture(tmp_path)
    r = M.run_mutation(src, cmd, cwd=tmp_path)
    assert r.total == r.killed + r.survived + r.timed_out + r.errored
    assert len(r.records) == r.total
