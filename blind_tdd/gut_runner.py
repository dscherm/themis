"""Measured green reports for GDScript (GUT) test suites.

`orchestrator.measure_test_run` builds a green report from pytest's own
JUnit XML, so the numbers come from the runner, not from the agent. Before
this module a GDScript task had no such path: its green report was written
by hand, and the gate could only take its word.

This is the GUT counterpart. It runs the project's GUT command
(`stack.test_runner.gdscript.command`) on the target test files with
`-gjunit_xml_file`, which GUT passes to its own JUnit exporter, and builds
the same report shape from that file.

One check is added that pytest does not need. GUT leaves a test script that
fails to load (parse error, missing preload) out of its results entirely,
and exits as if nothing was wrong (ralph-universal lesson
gut-silently-skips-a-test-script-that-fails-to-load). So every `func test_*`
defined in a target file that is absent from the JUnit file is recorded as
failed, with the reason, whatever the project's runner script counts.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree


CONFIG_FILES = ("schermness.config.json", "ralph.config.json")
_TEST_FUNC = re.compile(r"^\s*(?:static\s+)?func\s+(test_\w*)\s*\(", re.MULTILINE)


def configured_command(cwd: Path | str | None = None) -> str | None:
    """`stack.test_runner.gdscript.command` from the project config, or None."""
    base = Path(cwd) if cwd is not None else Path.cwd()
    for name in CONFIG_FILES:
        p = base / name
        if not p.is_file():
            continue
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        runner = ((cfg.get("stack") or {}).get("test_runner") or {}).get("gdscript") or {}
        cmd = runner.get("command") if isinstance(runner, dict) else None
        if isinstance(cmd, str) and cmd.strip():
            return cmd
    return None


def gd_test_files(targets: list[str], cwd: Path | str | None = None) -> list[str]:
    """Expand directories to the `test_*.gd` files under them, sorted;
    keep `.gd` files as given. Paths stay relative to `cwd`."""
    base = Path(cwd) if cwd is not None else Path.cwd()
    out: list[str] = []
    for t in targets:
        p = base / t
        if p.is_dir():
            out.extend(sorted(f.relative_to(base).as_posix() for f in p.rglob("test_*.gd")))
        elif str(t).lower().endswith(".gd"):
            out.append(Path(t).as_posix())
    seen: set[str] = set()
    return [f for f in out if not (f in seen or seen.add(f))]


def read_gut_junit(path: Path) -> list[dict]:
    """Per-case results from GUT's JUnit XML. GUT writes the script path,
    without `res://`, as the `classname`; a pending test is `<skipped>`."""
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return []
    cases = []
    for tc in root.iter("testcase"):
        if tc.find("failure") is not None or tc.find("error") is not None:
            result = "fail"
        elif tc.find("skipped") is not None:
            result = "skip"
        else:
            result = "pass"
        file = tc.get("file") or (tc.get("classname") or "").replace("res://", "")
        cases.append({"name": tc.get("name") or "", "file": file, "result": result})
    return cases


def _defined_tests(files: list[str], cwd: Path) -> dict[str, list[str]]:
    out = {}
    for f in files:
        try:
            out[f] = _TEST_FUNC.findall((cwd / f).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            out[f] = []
    return out


def measure_gut_run(
    targets: list,
    *,
    command: str | None = None,
    cwd: Path | str | None = None,
    timeout: int = 3600,
) -> dict:
    """Run GUT over `targets` and build a green report from what it did.

    Same shape as `measure_test_run`, with `measured.runner = "gut"`.
    """
    # Deferred: orchestrator imports this module's caller chain.
    from .mutate_gd import resolve_shell
    from .orchestrator import _aggregate_junit_cases, _now_iso

    base = Path(cwd) if cwd is not None else Path.cwd()
    command = command or configured_command(base)
    if not command:
        raise ValueError(
            "no GUT command: pass `command`, or set stack.test_runner.gdscript.command "
            "in schermness.config.json"
        )
    files = gd_test_files([str(t) for t in targets], base)
    if not files:
        raise ValueError(f"no test_*.gd files in {targets!r}")

    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "gut_junit.xml"
        cmd = resolve_shell(shlex.split(command)) + files + [
            f"-gjunit_xml_file={junit.resolve().as_posix()}",
        ]
        proc = subprocess.run(
            cmd, cwd=str(base), capture_output=True, text=True,
            errors="replace", timeout=timeout,
        )
        cases = read_gut_junit(junit)
        junit_written = junit.is_file()

    # A defined test GUT never reported did not run: its script most likely
    # failed to load. Count it as failed, never as absent.
    reported = {(c["file"], c["name"].split("[", 1)[0]) for c in cases}
    not_run = []
    for f, names in _defined_tests(files, base).items():
        for n in names:
            if (f, n) not in reported:
                not_run.append(f"{f}::{n}")
                cases.append({"name": n, "file": f, "result": "fail",
                              "reason": "defined but not run (script failed to load?)"})

    per_test = _aggregate_junit_cases(cases)
    passed = sum(1 for c in per_test if c["result"] == "pass")
    failed = sum(1 for c in per_test if c["result"] == "fail")
    skipped = sum(1 for c in per_test if c["result"] == "skip")
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return {
        "phase": "green",
        "overall": "pass" if proc.returncode == 0 and failed == 0 and passed > 0 else "fail",
        "tests_passed": passed,
        "tests_failed": failed,
        "tests_skipped": skipped,
        "per_test": per_test,
        "measured": {
            "runner": "gut",
            "command": " ".join(cmd[:-1]) + " -gjunit_xml_file=<tmp>",
            "exit_code": proc.returncode,
            "targets": files,
            "cwd": str(base.resolve()),
            "generated_at": _now_iso(),
            "junit_written": junit_written,
            "not_run": not_run,
            "output_tail": output[-2000:],
        },
    }

