"""Coverage verification for blind TDD.

Checks that every acceptance criterion ID declared in a task has either:
  - At least one passing test tagged with `Covers: AC-N` in its docstring
  - Or a `needs_human` entry in the triage report from Agent #1

Used in both the red phase (verify tests were written for every criterion)
and the green phase (verify every criterion has a PASSING test).

## Test annotation format

Tests must include `Covers: AC-N` in their docstring. Multiple criteria can
be covered by a single test with a comma-separated list:

    def test_foo():
        '''Covers: AC-1, AC-3'''
        ...

The parser also accepts:
    - `Covers: AC-1` (single)
    - `Covers: AC-1, AC-2, AC-3` (comma-separated)
    - `Covers: AC-1 AC-2` (space-separated)
    - Multi-line docstrings with the annotation on any line

## Usage

    from blind_tdd.coverage import extract_covers, verify_coverage

    # Extract which tests cover which criteria
    covered = extract_covers(Path("tests/contracts/"))
    # {"test_foo": ["AC-1"], "test_bar": ["AC-2", "AC-3"]}

    # Verify coverage against a task spec
    result = verify_coverage(task, test_dir, triage_report, passing_tests)
    if not result.all_covered:
        print(f"Missing criteria: {result.missing}")
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .artifacts import is_artifact_path


# Matches `Covers: AC-1` or `Covers: AC-1, AC-2` or `Covers: AC-1 AC-2`
_COVERS_PATTERN = re.compile(
    r"Covers\s*:\s*((?:AC-\d+[\s,]*)+)",
    re.IGNORECASE,
)
_AC_ID_PATTERN = re.compile(r"AC-\d+", re.IGNORECASE)

# A task id: an upper-case letter, then letters/digits, optional '-'/'.' parts.
_TASK_TOKEN = r"[A-Z][A-Za-z0-9]*(?:[-.][A-Za-z0-9]+)*"
_TASK_RE = re.compile(_TASK_TOKEN)
# One tag: `AC-N`, `AC-N (TASK)` or `TASK AC-N`. A TASK that is itself an AC id
# is not a prefix qualifier.
_COVERS_ITEM = (
    r"(?:(?P<pre>(?!(?i:AC)-\d+\b)" + _TASK_TOKEN + r")[ \t]+)?"
    r"(?P<ac>(?i:AC)-\d+)"
    r"(?:[ \t]*\((?P<par>[^()\n]*)\))?"
)
_COVERS_QUALIFIED_PATTERN = re.compile(
    r"(?i:Covers)\s*:\s*(?P<body>(?:" + _COVERS_ITEM + r"[\s,]*)+)"
)
_COVERS_ITEM_RE = re.compile(_COVERS_ITEM)


def _as_task_id(text: str | None) -> str | None:
    """`text` if it is a well-formed task id (contains a digit), else None."""
    if text is None:
        return None
    text = text.strip()
    if _TASK_RE.fullmatch(text) and any(ch.isdigit() for ch in text):
        return text
    return None


@dataclass
class TestAnnotation:
    """One test function and its criterion annotations."""
    test_name: str
    test_file: str
    covers: list[str] = field(default_factory=list)
    docstring: str = ""
    covers_tasks: dict[str, list] = field(default_factory=dict)
    """AC id -> qualifiers seen for it (a task id, or None for an unqualified tag)."""


@dataclass
class CoverageResult:
    """Result of verifying coverage against a task spec."""
    all_covered: bool
    covered_by_tests: dict[str, list[str]] = field(default_factory=dict)
    """Map of AC-N → list of test names covering it (via `Covers:` docstring)."""

    covered_by_triage: list[str] = field(default_factory=list)
    """AC-N values that were escalated to human via Agent #1's triage report."""

    missing: list[str] = field(default_factory=list)
    """AC-N values with no test AND no triage entry — a coverage gap."""

    not_passing: list[str] = field(default_factory=list)
    """AC-N values that have tests but none are in the passing set (green phase only)."""

    extra_tests: list[str] = field(default_factory=list)
    """Tests with `Covers: AC-X` annotations where AC-X is not in the task spec."""

    qualified: list[str] = field(default_factory=list)
    """AC-N values with at least one counted tag qualified with this task's id."""

    ambiguous: list[str] = field(default_factory=list)
    """AC-N values covered only by unqualified tags (could belong to any task)."""


def _parse_covers_from_docstring(docstring: str) -> list[str]:
    """Extract all AC-N IDs from a docstring's `Covers:` annotation."""
    if not docstring:
        return []
    ids: list[str] = []
    for match in _COVERS_PATTERN.finditer(docstring):
        chunk = match.group(1)
        for m in _AC_ID_PATTERN.finditer(chunk):
            # Normalize to upper-case AC-N
            ids.append(m.group(0).upper())
    # Deduplicate preserving order
    seen = set()
    unique = []
    for ac in ids:
        if ac not in seen:
            seen.add(ac)
            unique.append(ac)
    return unique


def parse_covers_qualified(docstring: str) -> list[tuple[str, str | None]]:
    """Return (AC id upper-cased, task id or None) for each `Covers:` tag.

    `AC-N (TASK)` and `TASK AC-N` are qualified; anything else, including a
    lower-case word in parentheses, leaves the tag unqualified.
    """
    if not isinstance(docstring, str):
        raise TypeError("docstring must be a str")
    pairs: list[tuple[str, str | None]] = []
    for match in _COVERS_QUALIFIED_PATTERN.finditer(docstring):
        for item in _COVERS_ITEM_RE.finditer(match.group("body")):
            task = _as_task_id(item.group("pre")) or _as_task_id(item.group("par"))
            pair = (item.group("ac").upper(), task)
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def _add_pairs(ann: TestAnnotation, pairs: list[tuple[str, str | None]]) -> None:
    for ac, task in pairs:
        if ac not in ann.covers:
            ann.covers.append(ac)
        quals = ann.covers_tasks.setdefault(ac, [])
        if task not in quals:
            quals.append(task)


def _annotation(name: str, path: Path, pairs, doc: str) -> TestAnnotation:
    ann = TestAnnotation(test_name=name, test_file=str(path), docstring=doc[:500])
    _add_pairs(ann, pairs)
    return ann


def _extract_from_python_file(path: Path) -> list[TestAnnotation]:
    """Parse a Python test file and extract test function annotations."""
    results: list[TestAnnotation] = []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return results

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return results

    def _walk(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                docstring = ast.get_docstring(node) or ""
                results.append(_annotation(
                    node.name, path, parse_covers_qualified(docstring), docstring,
                ))
        for child in ast.iter_child_nodes(node):
            _walk(child)

    _walk(tree)
    return results


def _extract_from_js_file(path: Path) -> list[TestAnnotation]:
    """Parse a JS/TS test file with regex (no AST dep).

    Looks for `it('...')` or `test('...')` calls with a preceding block
    comment containing `Covers: AC-N`.
    """
    results: list[TestAnnotation] = []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return results

    # Match: /* ... Covers: AC-N ... */  followed by (it|test)('name', ...)
    # Also handle // Covers: AC-N on a line just before a test declaration.
    pattern = re.compile(
        r"(?:(?P<block>/\*[\s\S]*?\*/)|(?P<line>(?://.*\n\s*)+))"
        r"\s*(?:it|test)\s*\(\s*['\"`](?P<name>[^'\"`]+)['\"`]",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        comment = match.group("block") or match.group("line") or ""
        pairs = parse_covers_qualified(comment)
        if pairs:
            results.append(_annotation(match.group("name"), path, pairs, comment))

    # Also handle standalone `it('test name — Covers: AC-1', () => ...)` style
    inline_pattern = re.compile(
        r"(?:it|test)\s*\(\s*['\"`](?P<name>[^'\"`]*Covers:[^'\"`]*)['\"`]",
    )
    for match in inline_pattern.finditer(source):
        name = match.group("name")
        pairs = parse_covers_qualified(name)
        if pairs:
            results.append(_annotation(name, path, pairs, name))

    return results


def _extract_from_csharp_file(path: Path) -> list[TestAnnotation]:
    """Parse a C# xUnit test file with regex.

    Looks for `[Fact]` or `[Theory]` attributes with a preceding XML
    comment containing `Covers: AC-N`.
    """
    results: list[TestAnnotation] = []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return results

    pattern = re.compile(
        r"(?P<comment>(?:///[^\n]*\n\s*)+)"
        r"\s*\[(?:Fact|Theory)[^\]]*\]"
        r"\s*(?:public\s+)?(?:async\s+)?(?:void|Task)\s+(?P<name>\w+)",
        re.MULTILINE,
    )
    for match in pattern.finditer(source):
        comment = match.group("comment")
        pairs = parse_covers_qualified(comment)
        if pairs:
            results.append(_annotation(match.group("name"), path, pairs, comment))
    return results


def _extract_from_gdscript_file(path: Path) -> list[TestAnnotation]:
    """Parse a GDScript test file (GUT or a TestBase-style harness).

    Test methods are `func test_*()` / `func _test_*()`. GDScript has no
    docstrings, so the `Covers: AC-N` tag lives in a `#`/`##` comment either
    just above the method or on the method's first body line. Attribute each
    Covers comment to the enclosing test method; a Covers seen before any test
    method is held and attached to the next one (comment-above style).
    """
    results: list[TestAnnotation] = []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return results

    func_re = re.compile(r"^\s*func\s+(?P<name>_?test\w*)\s*\(", re.IGNORECASE)
    anns: dict[str, TestAnnotation] = {}
    current: str | None = None
    pending: list[tuple[str, str | None]] = []

    def _ensure(name: str) -> TestAnnotation:
        if name not in anns:
            ann = TestAnnotation(test_name=name, test_file=str(path), covers=[])
            anns[name] = ann
            results.append(ann)
        return anns[name]

    for line in source.splitlines():
        m = func_re.match(line)
        if m:
            current = m.group("name") or ""
            ann = _ensure(current)
            _add_pairs(ann, pending)
            pending = []
            continue
        pairs = parse_covers_qualified(line)
        if not pairs:
            continue
        if current is not None:
            _add_pairs(anns[current], pairs)
        else:
            pending.extend(pairs)
    return results


def _extract_from_rust_file(path: Path) -> list[TestAnnotation]:
    """Parse a Rust test file. Test fns carry a `#[test]` attribute (also
    `#[tokio::test]`, `#[rstest]`, ...); the `Covers: AC-N` tag lives in a
    `//`/`///` comment above the attribute or on the fn's first body line.
    Attribute each Covers to the nearest `#[test] fn`.
    """
    results: list[TestAnnotation] = []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return results

    attr_re = re.compile(r"^\s*#\[\s*[\w:]*test\b")
    fn_re = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(?P<name>\w+)\s*\(")
    anns: dict[str, TestAnnotation] = {}
    current: str | None = None
    pending: list[tuple[str, str | None]] = []
    saw_test_attr = False

    def _ensure(name: str) -> TestAnnotation:
        if name not in anns:
            ann = TestAnnotation(test_name=name, test_file=str(path), covers=[])
            anns[name] = ann
            results.append(ann)
        return anns[name]

    for line in source.splitlines():
        if attr_re.match(line):
            saw_test_attr = True
            current = None  # the covers seen next belong to the upcoming test fn
            continue
        fm = fn_re.match(line)
        if fm:
            if saw_test_attr:
                name = fm.group("name") or ""
                current = name
                ann = _ensure(name)
                _add_pairs(ann, pending)
                pending = []
            else:
                current = None  # a non-test fn; body covers no longer attach
            saw_test_attr = False
            continue
        pairs = parse_covers_qualified(line)
        if not pairs:
            continue
        if current is not None:
            _add_pairs(anns[current], pairs)
        else:
            pending.extend(pairs)
    return results


def extract_covers(test_dir: Path | str) -> list[TestAnnotation]:
    """Walk a test directory and extract all test annotations.

    Supports Python (.py), JavaScript/TypeScript (.js/.ts/.tsx/.jsx),
    C# (.cs), GDScript (.gd), and Rust (.rs).
    """
    test_dir = Path(test_dir)
    if not test_dir.exists():
        return []

    results: list[TestAnnotation] = []
    for path in sorted(test_dir.rglob("*")):
        if not path.is_file():
            continue
        if is_artifact_path(path):
            # Never mine coverage annotations from bytecode caches or vendored
            # deps (e.g. a `Covers: AC-1` string inside node_modules).
            continue
        results.extend(extract_covers_from_file(path))
    return results


def extract_covers_from_file(path: Path | str) -> list[TestAnnotation]:
    """Test annotations of one file, dispatched on its extension."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _extract_from_python_file(path)
    if suffix in (".js", ".ts", ".tsx", ".jsx"):
        return _extract_from_js_file(path)
    if suffix == ".cs":
        return _extract_from_csharp_file(path)
    if suffix == ".gd":
        return _extract_from_gdscript_file(path)
    if suffix == ".rs":
        return _extract_from_rust_file(path)
    return []


def _norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _plain_test_name(name: str) -> str:
    """A passing-test name reduced to its function: no `[param]`, no `path::Class::`."""
    name = name.split("[", 1)[0]
    return name.rsplit("::", 1)[-1]


def verify_coverage(
    task: dict,
    test_dirs: list[Path | str],
    triage_report: dict | None = None,
    passing_test_names: set[str] | None = None,
    exclude_files: set[str] | None = None,
) -> CoverageResult:
    """Verify that every criterion in the task is covered.

    Args:
        task: The task spec (must have `acceptance_criteria` with AC-N IDs)
        test_dirs: List of directories to scan for test files
        triage_report: Optional Agent #1 triage report. Criteria marked
            `needs_human` here count as "covered" (via escalation).
        passing_test_names: Optional set of test names that PASSED in the
            green phase. If provided, coverage requires a PASSING test,
            not just any test with the annotation.
        exclude_files: Optional test-file paths whose annotations must not
            count. The green phase passes the files that appeared after the
            red seal and were judged out of this task's scope, so a file the
            seal does not protect can never satisfy one of its criteria.

    Returns:
        CoverageResult with all_covered flag and detailed breakdown.
    """
    # Extract all criterion IDs from the task
    criteria = task.get("acceptance_criteria") or []
    all_criteria: list[str] = []
    for c in criteria:
        if isinstance(c, dict) and "id" in c:
            all_criteria.append(str(c["id"]).upper())

    # Collect test annotations from all test dirs
    annotations: list[TestAnnotation] = []
    for td in test_dirs:
        annotations.extend(extract_covers(td))
    if exclude_files:
        excluded = {_norm_path(f) for f in exclude_files}
        annotations = [a for a in annotations if _norm_path(a.test_file) not in excluded]

    # Build: AC-N → [list of test names]
    task_id = str(task.get("id", "")).lower()
    covered_by_tests: dict[str, list[str]] = {}
    qualified_acs: set[str] = set()
    extra_tests: list[str] = []
    for ann in annotations:
        for ac in ann.covers:
            if ac in all_criteria:
                quals = ann.covers_tasks.get(ac) or [None]
                counted = [q for q in quals
                           if q is None or q.lower() == task_id]
                if not counted:
                    continue
                if any(q is not None for q in counted):
                    qualified_acs.add(ac)
                covered_by_tests.setdefault(ac, []).append(ann.test_name)
            else:
                extra_tests.append(f"{ann.test_name} (covers {ac})")

    # Build: AC-N triage escalations
    covered_by_triage: list[str] = []
    if triage_report and isinstance(triage_report, dict):
        triage_entries = triage_report.get("triage", [])
        if isinstance(triage_entries, list):
            for entry in triage_entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("status") == "needs_human":
                    crit_id = entry.get("criterion", "").upper()
                    if crit_id:
                        covered_by_triage.append(crit_id)

    # Determine missing and not_passing
    missing: list[str] = []
    not_passing: list[str] = []
    passing_plain = (
        {_plain_test_name(n) for n in passing_test_names}
        if passing_test_names is not None else None
    )
    for ac in all_criteria:
        if ac in covered_by_triage:
            continue
        tests_for_ac = covered_by_tests.get(ac, [])
        if not tests_for_ac:
            missing.append(ac)
            continue
        if passing_test_names is not None:
            # Green phase: at least one test covering this AC must be passing
            if not any(t in passing_test_names or t in passing_plain
                       for t in tests_for_ac):
                not_passing.append(ac)

    all_covered = not missing and not not_passing

    return CoverageResult(
        all_covered=all_covered,
        covered_by_tests=covered_by_tests,
        covered_by_triage=covered_by_triage,
        missing=missing,
        not_passing=not_passing,
        extra_tests=extra_tests,
        qualified=[ac for ac in all_criteria
                   if ac in qualified_acs and ac not in missing],
        ambiguous=[ac for ac in all_criteria
                   if ac in covered_by_tests and ac not in qualified_acs],
    )


if __name__ == "__main__":
    # CLI: scan a directory and report extracted annotations
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python coverage.py <test_dir>")
        sys.exit(1)

    test_dir = Path(sys.argv[1])
    annotations = extract_covers(test_dir)

    print(f"Found {len(annotations)} test function(s) with annotations:")
    for ann in annotations:
        covers_str = ", ".join(ann.covers) if ann.covers else "(none)"
        print(f"  {ann.test_file}::{ann.test_name}  Covers: {covers_str}")

    # Summary per criterion
    by_criterion: dict[str, list[str]] = {}
    for ann in annotations:
        for ac in ann.covers:
            by_criterion.setdefault(ac, []).append(ann.test_name)

    print(f"\nCriteria coverage summary:")
    for ac in sorted(by_criterion):
        tests = by_criterion[ac]
        print(f"  {ac}: {len(tests)} test(s)  ({', '.join(tests[:3])}{'...' if len(tests) > 3 else ''})")
