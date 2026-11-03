"""Mutation testing for GDScript: the `mutate.py` pass, for Godot projects.

`mutate.py` mutates Python through `ast`, which cannot read GDScript, so
this module mutates tokens instead. It walks the source skipping strings
and comments and changes one operator or literal per mutant, using the same
operator families as `mutate.py`:

    comparison     <  >=   >  <=   ==  !=        (inversion)
    boundary       <  <=   >  >=                 (off-by-one)
    arithmetic     +  -    (binary only)
    aug-assign     +=  -=
    boolean        and  or   &&  ||
    bool-constant  true  false
    int-constant   n -> n + 1

Each mutant runs the project's GUT command (`stack.test_runner.gdscript`)
with a timeout. Godot differs from pytest in three ways, each handled:

  * A mutant that stops the script parsing is `invalid`, not killed: GUT
    would count the test file that preloads it as a failure, which says
    nothing about test strength. It is detected by the mutated file's own
    `res://` path in a parse or load error. GUT's own addon prints parse
    errors on every run under some Godot versions, so any parse error is
    not enough. Invalid mutants are left out of the score.
  * Each run gets its own APPDATA / XDG_DATA_HOME, so `user://` state from
    one mutant cannot leak into the next (ralph-universal lesson
    parallel-godot-runs-share-one-user-folder).
  * The suite runs once unmutated first. If it is not green, the run stops:
    every mutant would read as killed.

A Godot run costs seconds, so `--lines` / `--changed-since` limit mutants to
the lines a task changed.

Usage:
    python -m blind_tdd.mutate_gd addons/x/panel.gd \\
        --test-cmd "bash scripts/tools/run_tests.sh tests/contracts/test_x.gd" \\
        --changed-since HEAD~1 --json .themis/mutation/x.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path


_CMP_SWAP = {"<": ">=", ">=": "<", ">": "<=", "<=": ">", "==": "!=", "!=": "=="}
_BOUNDARY_SWAP = {"<": "<=", "<=": "<", ">": ">=", ">=": ">"}
_BOOL_WORD = {"and": "or", "or": "and"}
_BOOL_SYM = {"&&": "||", "||": "&&"}
_CONST = {"true": "false", "false": "true"}

# Lines that declare rather than compute; mutating them is noise.
_SKIP_LINE = re.compile(r"^\s*(?:@|class_name\b|extends\b|signal\b|enum\b|#)")
_TOKEN = re.compile(
    r"""
    (?P<str>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
  | (?P<comment>\#.*)
  | (?P<num>\b\d+\.\d*|\b\d*\.\d+|\b0[xXbB][0-9a-fA-F_]+|\b\d[\d_]*(?:[eE][+-]?\d+)?)
  | (?P<word>[A-Za-z_]\w*)
  | (?P<op>->|<=|>=|==|!=|&&|\|\||\+=|-=|\*\*|<<|>>|[-+*/%<>=!&|^~(),\[\]{}.:])
    """,
    re.VERBOSE,
)
# Tokens after which `+` / `-` is binary (there is a left operand).
_OPERAND_END = {"num", "word", "str"}
_CLOSERS = {")", "]", "}"}
_NOT_OPERAND_WORDS = {"return", "and", "or", "not", "in", "is", "if", "elif", "while",
                      "for", "match", "when", "var", "const", "await", "else", "as"}


@dataclass
class Mutant:
    index: int
    operator: str
    lineno: int
    before: str
    after: str
    source: str


def _sites_in_line(line: str):
    """(start, end, operator, after) for each mutable token in one line."""
    prev_kind, prev_text = "", ""
    for m in _TOKEN.finditer(line):
        kind = m.lastgroup
        text = m.group(kind)
        if kind == "comment":
            return
        if kind == "op":
            if text in _CMP_SWAP:
                yield m.start(), m.end(), "comparison", _CMP_SWAP[text]
                if text in _BOUNDARY_SWAP:
                    yield m.start(), m.end(), "boundary", _BOUNDARY_SWAP[text]
            elif text in _BOOL_SYM:
                yield m.start(), m.end(), "boolean", _BOOL_SYM[text]
            elif text in ("+=", "-="):
                yield m.start(), m.end(), "aug-assign", "-=" if text == "+=" else "+="
            elif text in ("+", "-"):
                binary = (
                    (prev_kind in _OPERAND_END and prev_text not in _NOT_OPERAND_WORDS)
                    or prev_text in _CLOSERS
                )
                if binary:
                    yield m.start(), m.end(), "arithmetic", "-" if text == "+" else "+"
        elif kind == "word":
            if text in _BOOL_WORD:
                yield m.start(), m.end(), "boolean", _BOOL_WORD[text]
            elif text in _CONST:
                yield m.start(), m.end(), "bool-constant", _CONST[text]
        elif kind == "num" and re.fullmatch(r"\d[\d_]*", text):
            # A negative literal is `-` then the number; mutating the number
            # alone is still a one-step change.
            yield m.start(), m.end(), "int-constant", str(int(text.replace("_", "")) + 1)
        prev_kind, prev_text = kind, text


def _string_continuation(lines: list[str]) -> list[bool]:
    """True for lines that start inside a multi-line string (never mutated)."""
    out = []
    inside = ""
    for line in lines:
        out.append(bool(inside))
        i = 0
        while i < len(line):
            if inside:
                if line[i] == "\\":
                    i += 2
                    continue
                if line.startswith(inside, i):
                    i += len(inside)
                    inside = ""
                    continue
                i += 1
                continue
            if line[i] == "#":
                break
            if line.startswith('"""', i) or line.startswith("'''", i):
                inside = line[i:i + 3]
                i += 3
                continue
            if line[i] in "\"'":
                q = line[i]
                j = i + 1
                while j < len(line) and line[j] != q:
                    j += 2 if line[j] == "\\" else 1
                if j >= len(line):  # string runs past the line end
                    inside = q
                    break
                i = j + 1
                continue
            i += 1
    return out


def discover_mutants(source: str, lines: set[int] | None = None) -> list[Mutant]:
    """Every single-change mutant of `source`, in line then column order.
    `lines` (1-based) limits mutants to those lines."""
    src_lines = source.split("\n")
    in_string = _string_continuation(src_lines)
    mutants: list[Mutant] = []
    for idx, line in enumerate(src_lines):
        lineno = idx + 1
        if lines is not None and lineno not in lines:
            continue
        if in_string[idx] or _SKIP_LINE.match(line):
            continue
        if re.search(r"\b(?:preload|load)\s*\(", line):
            continue
        for start, end, op, after in _sites_in_line(line):
            new_line = line[:start] + after + line[end:]
            new_src = "\n".join(src_lines[:idx] + [new_line] + src_lines[idx + 1:])
            mutants.append(Mutant(len(mutants), op, lineno, line[start:end], after, new_src))
    return mutants


# --- running -----------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _res_path(src_path: Path, project_dir: Path) -> str:
    rel = src_path.resolve().relative_to(project_dir.resolve()).as_posix()
    return f"res://{rel}"


def _broke_parse(output: str, res_path: str) -> bool:
    """True if Godot could not load the mutated script itself."""
    esc = re.escape(res_path)
    return bool(re.search(
        rf'(?:Failed to load script|Could not preload resource script|Could not resolve script)\s+"{esc}"'
        rf"|GDScript::reload \({esc}:\d+\)"
        rf"|Parse Error.*{esc}",
        output,
    ))


def _gut_killers(output: str) -> list[str]:
    """`res://tests/x.gd::test_name` for every test GUT reported as failed."""
    killers: list[str] = []
    script = test = ""
    for raw in _ANSI.sub("", output).splitlines():
        line = raw.strip()
        if line.startswith("res://") and line.endswith(".gd"):
            script, test = line, ""
        elif line.startswith("* "):
            test = line[2:].strip()
        elif line.startswith("[Failed]") and script and test:
            key = f"{script}::{test}"
            if key not in killers:
                killers.append(key)
    return killers


def _isolated_env(scratch: Path, run_id: str) -> dict:
    """Environment with its own `user://` folder (Windows APPDATA, Linux/macOS
    XDG_DATA_HOME)."""
    home = scratch / f"userdata_{run_id}"
    home.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "APPDATA": str(home), "XDG_DATA_HOME": str(home)}


def resolve_shell(cmd: list[str]) -> list[str]:
    """On Windows, point a bare `bash` / `sh` at a real POSIX shell.

    CreateProcess searches System32 before PATH, and System32\\bash.exe is
    the WSL launcher: it runs the script inside Linux, where `C:/...` paths
    (such as a project's godot_bin) do not exist. Prefer the first match on
    PATH that is not the WSL launcher or its WindowsApps alias."""
    if os.name != "nt" or not cmd or cmd[0] not in ("bash", "sh"):
        return cmd
    for d in os.environ.get("PATH", "").split(os.pathsep):
        low = d.lower().replace("/", "\\")
        if low.endswith("\\system32") or "windowsapps" in low:
            continue
        for name in (cmd[0] + ".exe", cmd[0]):
            p = Path(d) / name
            if p.is_file():
                return [str(p), *cmd[1:]]
    for guess in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(guess).is_file():
            return [guess, *cmd[1:]]
    return cmd


def _run(cmd: list[str], cwd: Path, env: dict, timeout: float):
    return subprocess.run(
        cmd, cwd=str(cwd), env=env, capture_output=True, timeout=timeout,
        text=True, errors="replace",
    )


@dataclass
class GdMutationResult:
    total: int = 0
    killed: int = 0
    survived: int = 0
    timed_out: int = 0
    errored: int = 0
    invalid: int = 0
    survivors: list[dict] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Share of valid mutants not surviving. Invalid mutants (the script
        no longer parses) say nothing about the tests and are left out."""
        valid = self.total - self.invalid
        return 1.0 if valid == 0 else (valid - self.survived) / valid

    def summary(self) -> dict:
        return {
            "total": self.total, "killed": self.killed, "survived": self.survived,
            "timed_out": self.timed_out, "errored": self.errored, "invalid": self.invalid,
            "score": round(self.score, 4), "survivors": self.survivors,
        }


class BaselineNotGreen(RuntimeError):
    """The unmutated suite fails, so no mutant result would mean anything."""


def _find_project_dir(src_path: Path) -> Path:
    for d in [src_path.resolve().parent, *src_path.resolve().parents]:
        if (d / "project.godot").is_file():
            return d
    raise FileNotFoundError(f"no project.godot above {src_path}")


def run_mutation(
    src_path: str | Path,
    test_command: str | list[str],
    *,
    lines: set[int] | None = None,
    timeout: float | None = None,
    project_dir: str | Path | None = None,
    progress=None,
) -> GdMutationResult:
    """Mutate `src_path` one change at a time and run `test_command` against
    each mutant, from the Godot project directory.

    The file is restored from its original text in a `finally`. Run this on
    a checkout no one else is editing: the mutated file is live on disk
    while each mutant runs.

    `timeout` defaults to three times the baseline run, at least 30 s.
    """
    src_path = Path(src_path)
    project = Path(project_dir) if project_dir else _find_project_dir(src_path)
    res_path = _res_path(src_path, project)
    cmd = shlex.split(test_command) if isinstance(test_command, str) else list(test_command)
    cmd = resolve_shell(cmd)
    original = src_path.read_text(encoding="utf-8")
    mutants = discover_mutants(original, lines)
    result = GdMutationResult(total=len(mutants))

    scratch = Path(tempfile.mkdtemp(prefix="themis_mutate_gd_"))
    try:
        t0 = time.monotonic()
        base = _run(cmd, project, _isolated_env(scratch, "baseline"), timeout or 600)
        elapsed = time.monotonic() - t0
        if base.returncode != 0:
            raise BaselineNotGreen(
                f"the unmutated suite exits {base.returncode}; fix it before mutating.\n"
                + "\n".join((base.stdout + base.stderr).splitlines()[-15:])
            )
        per_mutant = timeout or max(30.0, 3 * elapsed)
        try:
            for m in mutants:
                rec = {"operator": m.operator, "lineno": m.lineno, "before": m.before,
                       "after": m.after, "verdict": "", "killers": []}
                result.records.append(rec)
                src_path.write_text(m.source, encoding="utf-8")
                try:
                    proc = _run(cmd, project, _isolated_env(scratch, str(m.index)), per_mutant)
                except subprocess.TimeoutExpired:
                    result.timed_out += 1
                    rec["verdict"] = "timed_out"
                except OSError:
                    result.errored += 1
                    rec["verdict"] = "errored"
                else:
                    out = _ANSI.sub("", proc.stdout + proc.stderr)
                    if _broke_parse(out, res_path):
                        result.invalid += 1
                        rec["verdict"] = "invalid"
                    elif proc.returncode == 0:
                        result.survived += 1
                        rec["verdict"] = "survived"
                        result.survivors.append({k: rec[k] for k in ("operator", "lineno", "before", "after")})
                    else:
                        result.killed += 1
                        rec["verdict"] = "killed"
                        rec["killers"] = _gut_killers(out)
                if progress:
                    progress(m, rec)
        finally:
            src_path.write_text(original, encoding="utf-8")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return result


# --- changed lines -----------------------------------------------------------

def changed_lines(src_path: str | Path, since: str, cwd: str | Path | None = None) -> set[int]:
    """1-based lines of `src_path` added or changed since git ref `since`."""
    out = subprocess.run(
        ["git", "diff", "-U0", since, "--", str(src_path)],
        cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=True,
    ).stdout
    lines: set[int] = set()
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", out, re.MULTILINE):
        start, count = int(m.group(1)), int(m.group(2) or 1)
        lines.update(range(start, start + count))
    return lines


def parse_line_spec(spec: str) -> set[int]:
    """`10-40,55` -> {10..40, 55}."""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="blind_tdd.mutate_gd",
        description="Advisory GDScript mutation pass: surface weak (mutant-surviving) GUT tests.",
    )
    ap.add_argument("src", help="the .gd file to mutate")
    ap.add_argument("--test-cmd", required=True,
                    help="command that runs the GUT tests (non-zero exit = a test failed)")
    ap.add_argument("--project", default=None, help="Godot project dir (default: nearest project.godot)")
    ap.add_argument("--timeout", type=float, default=None,
                    help="per-mutant seconds (default: 3x the baseline run, at least 30)")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--lines", default=None, help="only these lines, e.g. 10-40,55")
    scope.add_argument("--changed-since", default=None, metavar="REF",
                       help="only lines changed since this git ref")
    ap.add_argument("--list", action="store_true", help="list the mutants and exit (no Godot run)")
    ap.add_argument("--json", default=None, metavar="PATH", help="write every mutant's verdict here")
    ns = ap.parse_args(argv)

    lines = None
    if ns.lines:
        lines = parse_line_spec(ns.lines)
    elif ns.changed_since:
        lines = changed_lines(ns.src, ns.changed_since)

    if ns.list:
        for m in discover_mutants(Path(ns.src).read_text(encoding="utf-8"), lines):
            print(f"line {m.lineno}: {m.operator} {m.before!r} -> {m.after!r}")
        return 0

    def progress(m, rec):
        print(f"[{m.index + 1}] line {m.lineno} {m.before!r}->{m.after!r}: {rec['verdict']}", flush=True)

    try:
        result = run_mutation(ns.src, ns.test_cmd, lines=lines, timeout=ns.timeout,
                              project_dir=ns.project, progress=progress)
    except BaselineNotGreen as e:
        print(f"baseline not green: {e}")
        return 2
    s = result.summary()
    if ns.json:
        Path(ns.json).parent.mkdir(parents=True, exist_ok=True)
        Path(ns.json).write_text(json.dumps({
            "source": str(Path(ns.src)).replace("\\", "/"), "test_command": ns.test_cmd,
            "summary": s, "mutants": result.records,
        }, indent=1), encoding="utf-8")
        print(f"wrote {ns.json} ({len(result.records)} mutants)")
    print(f"mutants: {s['total']}  killed: {s['killed']}  survived: {s['survived']}  "
          f"timed_out: {s['timed_out']}  errored: {s['errored']}  invalid: {s['invalid']}  "
          f"score: {s['score']}")
    for sv in s["survivors"]:
        print(f"  SURVIVOR line {sv['lineno']}: {sv['operator']} {sv['before']!r} -> {sv['after']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
