"""Suppression-marker audit — advisory detection of check-evasion comments.

A well-documented failure mode of AI-written code is masking a problem
instead of fixing it: leaving a hardcoded secret in place and silencing the
linter that would flag it with a `# noqa` / `# nosec` comment. That is the
same move as editing a test to make it pass — gaming the check rather than
the code — one layer over, in the lint/scanner dimension the seal does not
watch.

This module watches it. The gate captures a baseline of suppression markers
across the repo when the tests are sealed (red phase), rescans at green, and
diffs. A marker that appears during the implementation window is reported as
an advisory warning on the green result and recorded in the tamper ledger
(`escalation.KIND_SUPPRESSION_INTRODUCED`), where it feeds the same
additive-only routing escalation as a hash break: similar tasks get gated on
later runs.

Advisory by design — a new marker NEVER fails the run. There are legitimate
reasons to suppress a lint rule, and a false-positive gate teaches operators
to turn the gate off. The ledger record is the teeth: evasion evidence
widens future gate coverage instead of blocking today's commit.

The baseline lives in the red-state record and is covered by the optional
seal HMAC (see gate_integration), so a Bash-capable implementer cannot
pre-date its own markers into the baseline without the key.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .artifacts import is_artifact_path


# Marker name → pattern. Case-insensitive. Names are stable identifiers that
# appear in ledger records and result details — don't rename casually.
SUPPRESSION_PATTERNS: dict[str, str] = {
    "noqa": r"#\s*noqa\b",                              # flake8 / ruff
    "nosec": r"#\s*nosec\b",                            # bandit
    "type-ignore": r"#\s*type:\s*ignore\b",             # mypy / pyright
    "pylint-disable": r"#\s*pylint:\s*disable",         # pylint
    "no-cover": r"#\s*pragma:\s*no\s*cover\b",          # coverage.py
    "eslint-disable": r"\beslint-disable",              # eslint (all variants)
    "ts-ignore": r"@ts-(?:ignore|expect-error)\b",      # typescript
    "istanbul-ignore": r"istanbul\s+ignore\b",          # JS coverage
    "suppress-warnings": r"@SuppressWarnings\b",        # java
    "pragma-warning-disable": r"#pragma\s+warning\s*[( ]\s*disable",  # C# / C++
    "nolint": r"//\s*nolint\b",                         # golangci-lint / clang-tidy
    "nosemgrep": r"\bnosemgrep\b",                      # semgrep
    "rubocop-disable": r"rubocop\s*:\s*disable\b",      # rubocop
}

_COMPILED = {
    name: re.compile(pattern, re.IGNORECASE)
    for name, pattern in SUPPRESSION_PATTERNS.items()
}

# Only files that plausibly hold source code are scanned.
SOURCE_SUFFIXES = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".cs", ".java", ".go", ".rb",
    ".kt", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp", ".php", ".swift",
    ".scala", ".m", ".mm",
})

# Directories pruned from the walk, on top of artifacts.is_artifact_path and
# the blanket dot-prefix rule (.git, .themis, .venv, ...).
_SKIP_DIRS = frozenset({
    "node_modules", "__pycache__", "venv", "env",
    "dist", "build", "target", "vendor",
})

# Safety caps so a pathological tree can't stall the gate. The walk is sorted,
# so a truncated scan is at least deterministic between red and green.
MAX_FILES = 20_000
MAX_FILE_BYTES = 1_000_000


def scan_text(text: str) -> dict[str, int]:
    """Marker name → occurrence count for one file's text."""
    counts: dict[str, int] = {}
    for name, rx in _COMPILED.items():
        n = len(rx.findall(text))
        if n:
            counts[name] = n
    return counts


def _should_skip_dir(name: str) -> bool:
    return name.startswith(".") or name.lower() in _SKIP_DIRS


def scan_repo(root: Path | str = ".") -> dict[str, dict[str, int]]:
    """Suppression-marker counts for every source file under `root`.

    Returns {relative_path (forward slashes): {marker_name: count}} — only
    files with at least one marker appear, so a clean repo yields {}.
    Unreadable or oversized files are skipped; this is an advisory scan and
    must never raise.
    """
    root = Path(root)
    results: dict[str, dict[str, int]] = {}
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _should_skip_dir(d))
        for fname in sorted(filenames):
            path = Path(dirpath) / fname
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            if is_artifact_path(path):
                continue
            seen += 1
            if seen > MAX_FILES:
                return results
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            counts = scan_text(text)
            if counts:
                rel = path.relative_to(root)
                results[str(rel).replace("\\", "/")] = counts
    return results


def diff_suppressions(
    baseline: dict[str, dict[str, int]],
    current: dict[str, dict[str, int]],
) -> list[dict]:
    """Markers that appeared (or multiplied) since the baseline.

    Returns a sorted list of {"path", "marker", "baseline", "current"} for
    every (file, marker) whose count increased. Removals are ignored —
    deleting a suppression is never evidence of evasion.
    """
    baseline = baseline if isinstance(baseline, dict) else {}
    findings: list[dict] = []
    for path in sorted(current):
        before_file = baseline.get(path) or {}
        for marker in sorted(current[path]):
            before = int(before_file.get(marker, 0))
            after = int(current[path][marker])
            if after > before:
                findings.append({
                    "path": path,
                    "marker": marker,
                    "baseline": before,
                    "current": after,
                })
    return findings


def summarize_findings(findings: list[dict]) -> str:
    """One-line human summary, e.g. 'src/foo.py: +1 nosec; src/b.js: +2 eslint-disable'."""
    parts = [
        f"{f['path']}: +{f['current'] - f['baseline']} {f['marker']}"
        for f in findings
    ]
    return "; ".join(parts)
