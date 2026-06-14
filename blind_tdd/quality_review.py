"""Deep-interview-style quality review for post-green blind-TDD tasks.

After the blind-TDD green phase passes (tests pass, hashes match, coverage
verified), this module scans the committed test files + src changes for
*candidate* quality concerns — trivial assertions, empty test bodies, tests
that mock the unit under test, src-side test-mode branches.

Candidates are NOT verdicts. They're questions to ask the human. The bridge-
mode LLM reads the staged candidates and conducts a judgment-elicitation
dialog (via AskUserQuestion), then writes a final review artifact with the
human's adjudications. This mirrors the `prompts/deep-interview.md` pattern
but for post-implementation test-quality judgment instead of pre-task spec
clarification.

Scanning is pure AST/regex — no LLM needed. The LLM is involved only in the
dialog, which runs in the current session (no separate API bill).
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


PENDING_DIR = Path(".themis") / "blind_tdd" / "quality_review_pending"
FINAL_DIR = Path(".themis") / "blind_tdd" / "quality_review"


@dataclass
class QualityCandidate:
    kind: str          # "trivial_assertion" | "empty_test" | "mock_shadows_uut" | "test_mode_branch"
    location: str      # "file:line"
    test_name: str     # test function name, or "(src)" for src-side findings
    detail: str        # short human-readable description
    question: str      # suggested AskUserQuestion phrasing
    evidence: str      # short code snippet


@dataclass
class QualityReviewScan:
    task_id: str
    candidates: list[QualityCandidate] = field(default_factory=list)
    test_files_scanned: list[str] = field(default_factory=list)
    src_files_scanned: list[str] = field(default_factory=list)

    def is_clean(self) -> bool:
        return not self.candidates


# ---------------------------------------------------------------------------
# AST-based test-quality detection
# ---------------------------------------------------------------------------

def _is_trivial_assertion(test: ast.expr) -> bool:
    """True iff the assert's test expression is structurally trivial.

    Covers: `assert True`, `assert False`, `assert <Name>`, `assert x is not
    None`, `assert x is None`, `assert len(x) >= 0`, `assert x == x`.
    Deliberately narrow — aiming for zero false positives on legitimate tests.
    """
    if isinstance(test, ast.Constant):
        return True
    if isinstance(test, ast.Name):
        return True
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and len(test.comparators) == 1:
        op = test.ops[0]
        right = test.comparators[0]
        # `x is None` / `x is not None`
        if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(right, ast.Constant) and right.value is None:
            return True
        # `len(x) >= 0`
        if (isinstance(op, ast.GtE)
            and isinstance(right, ast.Constant) and right.value == 0
            and isinstance(test.left, ast.Call)
            and isinstance(test.left.func, ast.Name)
            and test.left.func.id == "len"):
            return True
        # `x == x` (identical subexpressions)
        if isinstance(op, ast.Eq) and ast.dump(test.left) == ast.dump(right):
            return True
    return False


def _scan_test_function(path: Path, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[QualityCandidate]:
    out: list[QualityCandidate] = []
    asserts = [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]
    snippet = ast.get_source_segment(path.read_text(encoding="utf-8"), fn) or fn.name
    snippet = snippet[:300]

    if not asserts:
        out.append(QualityCandidate(
            kind="empty_test",
            location=f"{path}:{fn.lineno}",
            test_name=fn.name,
            detail=f"`{fn.name}` contains no assert statements.",
            question=(
                f"`{fn.name}` has no assertions. Is this: "
                f"(a) a smoke test that should fail only on exception, "
                f"(b) relying on side-effect verification elsewhere, or "
                f"(c) missing assertions that should be added?"
            ),
            evidence=snippet,
        ))
        return out

    trivial = [a for a in asserts if _is_trivial_assertion(a.test)]
    if trivial and len(trivial) == len(asserts):
        first = trivial[0]
        expr = ast.unparse(first.test)[:80]
        out.append(QualityCandidate(
            kind="trivial_assertion",
            location=f"{path}:{first.lineno}",
            test_name=fn.name,
            detail=(
                f"`{fn.name}` has {len(trivial)} assertion(s), all structurally trivial "
                f"(existence / truthiness / tautology)."
            ),
            question=(
                f"`{fn.name}`'s only assertions are existence / truthiness checks "
                f"(first one: `{expr}`). Is this: "
                f"(a) the actual requirement (function must succeed and return SOMETHING), "
                f"(b) a weak test that should assert specific values, or "
                f"(c) fine because stricter tests elsewhere cover the values?"
            ),
            evidence=snippet,
        ))
    return out


def _mocks_shadowing_uut(path: Path, public_module: str) -> list[QualityCandidate]:
    """Flag `@patch("<public_module>.<symbol>")` / `mock.patch.object(<symbol_from_public_module>, ...)`
    inside a test file — suggests the test mocks the thing it's supposed to be testing.
    """
    candidates: list[QualityCandidate] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return candidates
    # Normalize module path: "tools/tag_matcher.py" -> "tag_matcher" AND "tools.tag_matcher"
    module_stem = Path(public_module).stem
    module_dotted = public_module.replace("/", ".").removesuffix(".py")
    patterns = [
        (rf'@patch\(\s*["\']({re.escape(module_stem)}|{re.escape(module_dotted)})\.', "decorator patch"),
        (rf'mock\.patch\(\s*["\']({re.escape(module_stem)}|{re.escape(module_dotted)})\.', "mock.patch call"),
    ]
    for i, line in enumerate(text.splitlines(), start=1):
        for pat, label in patterns:
            if re.search(pat, line):
                candidates.append(QualityCandidate(
                    kind="mock_shadows_uut",
                    location=f"{path}:{i}",
                    test_name="(decorator)",
                    detail=(
                        f"Test patches a symbol from `{module_stem}`, the module under test "
                        f"({label})."
                    ),
                    question=(
                        f"`{path.name}:{i}` mocks a symbol from the module being tested. Is this: "
                        f"(a) legitimately isolating a collaborator inside the module, "
                        f"(b) shadowing the function under test with a mock that makes the "
                        f"test trivially pass, or (c) something else worth clarifying?"
                    ),
                    evidence=line.strip()[:200],
                ))
    return candidates


# ---------------------------------------------------------------------------
# Regex-based src-side detection
# ---------------------------------------------------------------------------

_SRC_ESCAPE_HATCH_PATTERNS = [
    (r'\bif\s+TEST_MODE\b', "TEST_MODE"),
    (r'os\.environ\.get\s*\(\s*["\']TESTING?["\']', "os.environ TESTING"),
    (r'\bsettings\.TESTING\b', "settings.TESTING (Django)"),
    (r'\bapp\.config\[[\'"]TESTING[\'"]\]', "app.config TESTING (Flask)"),
    (r'\bprocess\.env\.NODE_ENV\s*===?\s*["\']test["\']', "NODE_ENV === 'test'"),
]


def _scan_src_for_escape_hatches(path: Path) -> list[QualityCandidate]:
    candidates: list[QualityCandidate] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return candidates
    for i, line in enumerate(text.splitlines(), start=1):
        for pat, label in _SRC_ESCAPE_HATCH_PATTERNS:
            if re.search(pat, line):
                candidates.append(QualityCandidate(
                    kind="test_mode_branch",
                    location=f"{path}:{i}",
                    test_name="(src)",
                    detail=f"Source has a test-mode branch ({label}).",
                    question=(
                        f"`{path.name}:{i}` branches on `{label}`. Is this: "
                        f"(a) legitimate framework-conventional behavior, "
                        f"(b) a shim the implementer added so tests pass without real logic, or "
                        f"(c) intentional environment-aware behavior worth documenting?"
                    ),
                    evidence=line.strip()[:200],
                ))
    return candidates


# ---------------------------------------------------------------------------
# Top-level scan entry point
# ---------------------------------------------------------------------------

def scan(
    task_id: str,
    test_files: list[Path],
    src_files: list[Path],
    public_module: str | None = None,
) -> QualityReviewScan:
    """Produce quality candidates for the interview loop.

    Args:
        task_id: the task whose green phase just passed
        test_files: absolute paths to hash-locked test files from the green report
        src_files: absolute paths to src files changed in this commit
        public_module: `public_surface.module` from the task spec (used for
            mock-shadows-uut detection); None to skip that check
    """
    scan_result = QualityReviewScan(task_id=task_id)
    for tf in test_files:
        scan_result.test_files_scanned.append(str(tf))
        try:
            tree = ast.parse(tf.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                scan_result.candidates.extend(_scan_test_function(tf, node))
        if public_module:
            scan_result.candidates.extend(_mocks_shadowing_uut(tf, public_module))
    for sf in src_files:
        scan_result.src_files_scanned.append(str(sf))
        scan_result.candidates.extend(_scan_src_for_escape_hatches(sf))
    return scan_result


# ---------------------------------------------------------------------------
# Brief staging (ManualSpawner pattern)
# ---------------------------------------------------------------------------

def write_pending_brief(scan_result: QualityReviewScan, prompt_path: Path) -> Path:
    """Stage a brief at `.themis/blind_tdd/quality_review_pending/<task_id>.md`
    that the LLM reads to conduct the interview.
    """
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    brief = PENDING_DIR / f"{scan_result.task_id}.md"
    prompt_text = ""
    if prompt_path.exists():
        try:
            prompt_text = prompt_path.read_text(encoding="utf-8")
        except OSError:
            prompt_text = ""

    lines = [
        f"# Blind TDD Quality Review — `{scan_result.task_id}`",
        "",
        f"Staged {datetime.now(timezone.utc).isoformat()}. "
        f"{len(scan_result.candidates)} candidate(s) for human judgment.",
        "",
        "## Instructions",
        "",
        prompt_text or "(quality-reviewer prompt not found; follow the pattern in prompts/deep-interview.md)",
        "",
        "## Candidates",
        "",
    ]
    if not scan_result.candidates:
        lines.append("_No candidates found. Write an empty verdict file to signal completion._")
    for i, c in enumerate(scan_result.candidates, start=1):
        lines.extend([
            f"### Candidate {i}: `{c.kind}` at `{c.location}`",
            "",
            f"**Detail.** {c.detail}",
            "",
            f"**Suggested question.** {c.question}",
            "",
            "**Evidence:**",
            "",
            "```",
            c.evidence,
            "```",
            "",
        ])
    lines.extend([
        "## Output",
        "",
        f"After the interview, write the final verdict to "
        f"`.themis/blind_tdd/quality_review/{scan_result.task_id}.md` with the "
        "human's adjudications (kept / fixed / dismissed-as-false-positive) and "
        "an overall verdict: PASS / NEEDS_WORK.",
        "",
        "Candidates as JSON (for tooling):",
        "",
        "```json",
        json.dumps(
            {"task_id": scan_result.task_id,
             "candidates": [asdict(c) for c in scan_result.candidates]},
            indent=2,
        ),
        "```",
    ])
    brief.write_text("\n".join(lines), encoding="utf-8")
    return brief


def final_review_path(task_id: str) -> Path:
    return FINAL_DIR / f"{task_id}.md"


def is_review_complete(task_id: str, brief_path: Path | None = None) -> bool:
    """The review is complete when the final artifact exists AND is newer
    than the pending brief (mirror of ManualSpawner freshness check).
    """
    final = final_review_path(task_id)
    if not final.exists():
        return False
    if brief_path is None or not brief_path.exists():
        return True
    return final.stat().st_mtime >= brief_path.stat().st_mtime
