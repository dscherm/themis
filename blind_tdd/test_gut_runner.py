"""Measured green reports for GUT suites (blind_tdd/gut_runner.py).

A stand-in for GUT writes JUnit XML the way GUT 9 does (script path in
`classname`, pending as `<skipped>`), so these run without Godot. The real
GUT run is recorded in docs/gdscript-pipeline.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from blind_tdd.gut_runner import configured_command, gd_test_files, measure_gut_run
from blind_tdd.orchestrator import record_runner_handshake

# Reports every `func test_*` in each file passed to it, except: a file
# containing NOLOAD is left out (GUT's silent skip of a script that fails to
# load), a name containing "fail" fails, one containing "pending" is skipped.
FAKE_GUT = '''\
import re, sys, pathlib
files = [a for a in sys.argv[1:] if not a.startswith("-g")]
junit = [a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("-gjunit_xml_file=")][0]
out = ["<testsuites>"]
code = 0
for f in files:
    text = pathlib.Path(f).read_text()
    if "NOLOAD" in text:
        continue
    out.append('<testsuite name="res://%s">' % f)
    for n in re.findall(r"func (test_\\w*)", text):
        body = ""
        if "fail" in n:
            body = '<failure message="failed">x</failure>'; code = 1
        elif "pending" in n:
            body = '<skipped message="pending">x</skipped>'
        out.append('<testcase name="%s" classname="%s">%s</testcase>' % (n, f, body))
    out.append("</testsuite>")
out.append("</testsuites>")
pathlib.Path(junit).write_text("\\n".join(out))
sys.exit(code)
'''


def _project(tmp_path: Path, tests: dict[str, str]) -> str:
    (tmp_path / "fake_gut.py").write_text(FAKE_GUT, encoding="utf-8")
    for rel, body in tests.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("extends GutTest\n" + body, encoding="utf-8")
    return f"{Path(sys.executable).as_posix()} fake_gut.py"


def test_a_green_suite_is_measured_from_gut_junit(tmp_path):
    cmd = _project(tmp_path, {"tests/test_a.gd": "func test_x():\n\tpass\nfunc test_y():\n\tpass\n"})
    r = measure_gut_run(["tests"], command=cmd, cwd=tmp_path)
    assert (r["overall"], r["tests_passed"], r["tests_failed"]) == ("pass", 2, 0)
    assert r["measured"]["runner"] == "gut"
    assert {t["file"] for t in r["per_test"]} == {"tests/test_a.gd"}


def test_a_failing_test_makes_the_report_fail(tmp_path):
    cmd = _project(tmp_path, {"tests/test_a.gd": "func test_ok():\n\tpass\nfunc test_fail_x():\n\tpass\n"})
    r = measure_gut_run(["tests"], command=cmd, cwd=tmp_path)
    assert r["overall"] == "fail" and r["tests_failed"] == 1


def test_a_script_gut_skipped_counts_as_failed_not_absent(tmp_path):
    """GUT leaves a script that fails to load out of its results and can exit
    0. Its tests must read as failed."""
    cmd = _project(tmp_path, {
        "tests/test_a.gd": "func test_ok():\n\tpass\n",
        "tests/test_b.gd": "# NOLOAD\nfunc test_never():\n\tpass\n",
    })
    r = measure_gut_run(["tests"], command=cmd, cwd=tmp_path)
    assert r["measured"]["exit_code"] == 0
    assert r["overall"] == "fail"
    assert r["measured"]["not_run"] == ["tests/test_b.gd::test_never"]


def test_pending_is_skipped_not_passed(tmp_path):
    cmd = _project(tmp_path, {"tests/test_a.gd": "func test_ok():\n\tpass\nfunc test_pending_x():\n\tpass\n"})
    r = measure_gut_run(["tests"], command=cmd, cwd=tmp_path)
    assert (r["tests_passed"], r["tests_skipped"]) == (1, 1)


def test_the_command_comes_from_the_project_config(tmp_path):
    (tmp_path / "schermness.config.json").write_text(json.dumps(
        {"stack": {"test_runner": {"gdscript": {"command": "bash run.sh"}}}}), encoding="utf-8")
    assert configured_command(tmp_path) == "bash run.sh"
    assert configured_command(tmp_path / "nowhere") is None


def test_directories_expand_to_gd_test_files_only(tmp_path):
    _project(tmp_path, {"tests/test_a.gd": "", "tests/sub/test_b.gd": "", "tests/helper.gd": ""})
    (tmp_path / "tests" / "test_c.py").write_text("", encoding="utf-8")
    assert gd_test_files(["tests"], tmp_path) == ["tests/sub/test_b.gd", "tests/test_a.gd"]


def test_no_command_and_no_config_is_an_error(tmp_path):
    _project(tmp_path, {"tests/test_a.gd": "func test_x():\n\tpass\n"})
    with pytest.raises(ValueError, match="no GUT command"):
        measure_gut_run(["tests"], cwd=tmp_path)


def test_record_runner_handshake_with_gut_writes_a_measured_report(tmp_path):
    cmd = _project(tmp_path, {"tests/test_a.gd": "func test_x():\n\tpass\n"})
    out = record_runner_handshake(
        "PB-9", targets=["tests"], cwd=tmp_path, runner="gut", command=cmd,
        green_report_dir=tmp_path / "gr",
    )
    data = json.loads(Path(out).read_text(encoding="utf-8"))
    assert data["task"] == "PB-9" and data["overall"] == "pass"
    assert data["measured"]["runner"] == "gut"


def test_an_unknown_runner_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown runner"):
        record_runner_handshake("T", targets=["x"], cwd=tmp_path, runner="nose")
