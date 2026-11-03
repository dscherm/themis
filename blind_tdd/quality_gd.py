"""Test-quality candidates for GDScript (GUT) test files.

The GDScript counterpart of the AST checks in `quality_review`. GDScript has
no parser in the standard library, so this reads lines: a test is a
`func test_*` line, and its body is every following line indented deeper.
That is enough for the shapes these checks look for, and like the Python
checks it aims for zero false positives over catching everything.

Candidates are questions for the human, never verdicts:

  * `empty_test`: no assertion call at all (no `assert_*`, no helper whose
    name contains "assert", no `fail_test`);
  * `pending_test`: the test calls `pending()`. GUT counts a pending test as
    neither passed nor failed, so it can sit in a suite and never fail;
  * `trivial_assertion`: every assertion is structurally trivial
    (`assert_true(true)`, `assert_not_null(x)`, `assert_eq(a, a)`,
    `assert_true(x)` on a bare name, `assert_gte(x.size(), 0)`, or only
    `pass_test()`);
  * `mock_shadows_uut`: `double()` / `partial_double()` / `stub()` aimed at
    the script under test, which can replace the code the test is meant to
    check.
"""

from __future__ import annotations

import re
from pathlib import Path

from .quality_review import QualityCandidate


_ANY_FUNC_RE = re.compile(r"^(\s*)(?:static\s+)?func\s+(\w+)\s*\(")
_ONE_LINE_RE = re.compile(
    r"^\s*(?:static\s+)?func\s+\w+\s*\([^)]*\)\s*(?:->\s*[\w\[\], ]+?)?\s*:(.*)$"
)
_CALL_RE = re.compile(r"\b(\w+)\s*\(")
_DOUBLE_RE = re.compile(r"\b(double|partial_double|stub)\s*\(")
_PRELOAD_CONST_RE = re.compile(
    r"^\s*(?:const|var)\s+(\w+)\s*(?::[^=]*)?=\s*(?:preload|load)\s*\(\s*[\"']([^\"']+)[\"']"
)
_CLASS_NAME_RE = re.compile(r"^\s*class_name\s+(\w+)", re.MULTILINE)


def _strip_comment(line: str) -> str:
    """Drop a trailing `#` comment, leaving `#` inside strings alone."""
    out = []
    quote = ""
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        out.append(ch)
    return "".join(out)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _test_functions(lines: list[str]) -> list[tuple[str, int, list[str]]]:
    """(name, 1-based line, body lines with comments stripped) per test."""
    return [f for f in _functions(lines) if f[0].startswith("test_")]


def _continuation_lines(lines: list[str]) -> list[bool]:
    """Per line: True if it starts inside a string, bracket or `\\` line
    continuation opened on an earlier line. Such a line is part of the
    statement above it, whatever its indentation."""
    out = []
    quote = ""
    depth = 0
    backslash = False
    for line in lines:
        out.append(bool(quote) or depth > 0 or backslash)
        backslash = False
        i = 0
        while i < len(line):
            if quote:
                if line[i] == "\\":
                    i += 2
                    continue
                if line.startswith(quote, i):
                    i += len(quote)
                    quote = ""
                    continue
                i += 1
                continue
            ch = line[i]
            if ch == "#":
                break
            if line.startswith('"""', i) or line.startswith("'''", i):
                quote = line[i:i + 3]
                i += 3
                continue
            if ch in "\"'":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            i += 1
        if not quote and line.rstrip().endswith("\\"):
            backslash = True
    return out


def _functions(lines: list[str]) -> list[tuple[str, int, list[str]]]:
    """(name, 1-based line, body lines with comments stripped) for every
    `func` in the file, tests and helpers alike."""
    cont = _continuation_lines(lines)
    out = []
    i = 0
    while i < len(lines):
        m = _ANY_FUNC_RE.match(lines[i]) if not cont[i] else None
        if not m:
            i += 1
            continue
        base = _indent(lines[i])
        name, start = m.group(2), i + 1
        # A one-line body: `func test_x() -> void: assert_true(f())`
        one = _ONE_LINE_RE.match(_strip_comment(lines[i]))
        body = [one.group(1)] if one and one.group(1).strip() else []
        i += 1
        while i < len(lines):
            raw = lines[i]
            if (not cont[i] and raw.strip() and not raw.lstrip().startswith("#")
                    and _indent(raw) <= base):
                break
            body.append(raw if cont[i] else _strip_comment(raw))
            i += 1
        out.append((name, start, body))
    return out


def _asserting_helpers(lines: list[str]) -> set[str]:
    """Names of non-test functions in the file that assert, directly or by
    calling another such helper. A test that calls one is not empty."""
    helpers = {n: "\n".join(b) for n, _, b in _functions(lines) if not n.startswith("test_")}
    asserting: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, body in helpers.items():
            if name in asserting:
                continue
            if _assertion_calls(body, asserting):
                asserting.add(name)
                changed = True
    return asserting


def _call_args(text: str, open_paren: int) -> tuple[list[str], int]:
    """Top-level comma-split arguments of the call whose `(` is at
    `open_paren`, and the index just past its `)`."""
    depth = 0
    quote = ""
    args, cur = [], []
    i = open_paren
    while i < len(text):
        ch = text[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in "([{":
            depth += 1
            if depth > 1:
                cur.append(ch)
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                args.append("".join(cur).strip())
                return [a for a in args if a], i + 1
            cur.append(ch)
        elif ch == "," and depth == 1:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    args.append("".join(cur).strip())
    return [a for a in args if a], len(text)


def _blank_strings(text: str) -> str:
    """`text` with every string literal's contents replaced by spaces, same
    length, so a call written inside a string is not found as a call."""
    out = []
    quote = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and i + 1 < len(text):
                out.append("  ")
                i += 2
                continue
            if ch == quote:
                quote = ""
                out.append(ch)
            else:
                out.append("\n" if ch == "\n" else " ")
        else:
            if ch in "\"'":
                quote = ch
            out.append(ch)
        i += 1
    return "".join(out)


def _assertion_calls(body: str, helpers: set[str] = frozenset()) -> list[tuple[str, list[str]]]:
    """(callee, args) for every call that asserts something: a GUT assert,
    a name containing "assert", or a file-local helper in `helpers`."""
    calls = []
    for m in _CALL_RE.finditer(_blank_strings(body)):
        name = m.group(1)
        if ("assert" in name.lower() or name in helpers
                or name in ("fail_test", "pass_test", "pending")):
            args, _ = _call_args(body, m.end() - 1)
            calls.append((name, args))
    return calls


def _norm(expr: str) -> str:
    return re.sub(r"\s+", "", expr)


def _is_trivial(name: str, args: list[str]) -> bool:
    if name == "pass_test":
        return True
    if not name.startswith("assert_"):
        return False  # a project helper: assume it checks something real
    first = args[0] if args else ""
    if name in ("assert_true", "assert_false") and first in ("true", "false"):
        return True
    # `assert_null(x)` claims something is absent, which is a real check
    # (`assert_null(re.search(src))`); only the existence check is trivial.
    if name == "assert_not_null":
        return True
    # Unlike Python's `assert x`, `assert_true(x)` on a bare name is not
    # flagged: assigning a computed bool and then asserting it is idiomatic
    # GDScript (Soapbox-2D test_project_settings.gd:80), and flagging it was
    # most of the noise on a real suite.
    # `assert_eq(x, x)` is a tautology only when nothing is called: the same
    # call twice is how a determinism test reads (test_race_rng.gd:14).
    if (name in ("assert_eq", "assert_same") and len(args) >= 2
            and _norm(args[0]) == _norm(args[1]) and "(" not in args[0]):
        return True
    if name == "assert_gte" and len(args) >= 2 and args[1] == "0" and re.search(r"\.(size|length)\(\)$", _norm(first)):
        return True
    if name == "assert_true" and re.fullmatch(r".+\.(size|length)\(\)>=0", _norm(first)):
        return True
    if name == "assert_true" and re.fullmatch(r".+!=null", _norm(first)):
        return True
    return False


def _uut_names(test_text: str, public_module: str) -> set[str]:
    """Names by which a test file can refer to the script under test: its
    `res://` path, a const/var preloaded from that path, and its
    `class_name` when the source file is readable."""
    path = public_module.replace("\\", "/").lstrip("/")
    if path.startswith("res://"):
        path = path[len("res://"):]
    names = {f"res://{path}"}
    for line in test_text.splitlines():
        m = _PRELOAD_CONST_RE.match(line)
        if m and m.group(2).replace("res://", "") == path:
            names.add(m.group(1))
    try:
        cm = _CLASS_NAME_RE.search(Path(path).read_text(encoding="utf-8"))
        if cm:
            names.add(cm.group(1))
    except OSError:
        pass
    return names


def _mocks_shadowing_uut(path: Path, text: str, public_module: str) -> list[QualityCandidate]:
    names = _uut_names(text, public_module)
    out = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw)
        for m in _DOUBLE_RE.finditer(line):
            args, _ = _call_args(line, m.end() - 1)
            target = args[0].strip("\"'") if args else ""
            if target in names:
                out.append(QualityCandidate(
                    kind="mock_shadows_uut",
                    location=f"{path}:{i}",
                    test_name="(double)",
                    detail=f"Test calls `{m.group(1)}()` on the script under test (`{target}`).",
                    question=(
                        f"`{path.name}:{i}` doubles or stubs the script being tested. Is this: "
                        f"(a) a partial double that only stubs a collaborator method, "
                        f"(b) replacing the code under test so the test passes trivially, or "
                        f"(c) something else worth clarifying?"
                    ),
                    evidence=raw.strip()[:200],
                ))
    return out


def scan_test_file(path: Path, public_module: str | None = None) -> list[QualityCandidate]:
    """Quality candidates for one GUT test file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    lines = text.splitlines()
    helpers = _asserting_helpers(lines)
    out: list[QualityCandidate] = []
    for name, lineno, body_lines in _test_functions(lines):
        body = "\n".join(body_lines)
        snippet = "\n".join(lines[lineno - 1: lineno + len(body_lines)])[:300]
        calls = _assertion_calls(body, helpers)
        real = [c for c in calls if c[0] not in ("pending",)]
        if any(c[0] == "pending" for c in calls):
            out.append(QualityCandidate(
                kind="pending_test",
                location=f"{path}:{lineno}",
                test_name=name,
                detail=f"`{name}` calls `pending()`; GUT counts it as neither passed nor failed.",
                question=(
                    f"`{name}` is marked pending. Is this: "
                    f"(a) a known gap that should be filed and tracked, "
                    f"(b) a test that should be finished before this task closes, or "
                    f"(c) intentionally parked?"
                ),
                evidence=snippet,
            ))
            continue
        if not real:
            out.append(QualityCandidate(
                kind="empty_test",
                location=f"{path}:{lineno}",
                test_name=name,
                detail=f"`{name}` makes no assertion call.",
                question=(
                    f"`{name}` has no assertions. Is this: "
                    f"(a) a smoke test that should fail only on an engine error, "
                    f"(b) relying on checks elsewhere, or "
                    f"(c) missing assertions that should be added?"
                ),
                evidence=snippet,
            ))
            continue
        if all(_is_trivial(n, a) for n, a in real):
            first = real[0]
            expr = f"{first[0]}({', '.join(first[1])})"[:80]
            out.append(QualityCandidate(
                kind="trivial_assertion",
                location=f"{path}:{lineno}",
                test_name=name,
                detail=(
                    f"`{name}` has {len(real)} assertion(s), all structurally trivial "
                    f"(existence / truthiness / tautology)."
                ),
                question=(
                    f"`{name}`'s only assertions are existence / truthiness checks "
                    f"(first one: `{expr}`). Is this: "
                    f"(a) the actual requirement, "
                    f"(b) a weak test that should assert specific values, or "
                    f"(c) fine because stricter tests elsewhere cover the values?"
                ),
                evidence=snippet,
            ))
    if public_module and public_module.lower().endswith(".gd"):
        out.extend(_mocks_shadowing_uut(path, text, public_module))
    return out
