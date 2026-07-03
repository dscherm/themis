"""Shared artifact/ignore patterns for file-set computation.

Build, cache, and redirect artifacts must never enter a file set that Themis
hashes, hash-checks, or discovers tests from. If they did, a `.pyc`
materializing in a locked test dir between the red and green phases, a
vendored `test_*.py` under `node_modules/`, or a literal `NUL` file left by a
`>NUL` / `2>NUL` redirect run under git-bash (Windows does not special-case the
DOS device there) would change the computed set and fail the gate on noise — a
false verdict, the one failure the verification layer must never produce.

The gate's byte-level strictness on *declared* oracle files is unaffected: this
only filters paths that, by name, are never source. A real test file with a
normal extension outside an artifact directory is still hashed exactly as
before, and a genuinely new one still breaks the seal.

Every place that enumerates a file set (`orchestrator._hash_test_files`,
`coverage.extract_covers`, `gate_integration._run_quality_review_phase`,
`probe_driver._hash_sandbox_tests`) routes through `is_artifact_path` so the
ignore list lives in one place instead of being re-derived per call site.
"""

from __future__ import annotations

from pathlib import PurePath

# Directory names that never contain source or oracle files. Matched against
# individual path *components* (exact, case-insensitive), never as substrings —
# a legitimately-named `tests/node_modules_shim/test_x.py` is NOT excluded.
ARTIFACT_DIRS = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
    "node_modules",
})

# Exact file names (case-insensitive) that are redirect/build artifacts.
# `NUL` is the file a `>NUL` / `2>NUL` redirect creates when a Windows command
# is run under git-bash, which does not treat NUL as the null device.
ARTIFACT_FILE_NAMES = frozenset({"nul"})

# File suffixes for compiled byte-code — never source, even when they sit next
# to (or share a stem with) a real test file.
ARTIFACT_SUFFIXES = frozenset({".pyc", ".pyo", ".pyd"})


def is_artifact_path(path) -> bool:
    """True if `path` is a build/cache/redirect artifact to exclude from any
    hashed or discovered file set.

    A path is an artifact if any component names a known artifact directory,
    the final component is a known artifact file name, or its suffix is a
    compiled-bytecode suffix. All comparisons are case-insensitive so the
    Windows `NUL` device file and mixed-case cache dirs are caught.
    """
    p = PurePath(str(path))
    for part in p.parts:
        if part.lower() in ARTIFACT_DIRS:
            return True
    if p.name.lower() in ARTIFACT_FILE_NAMES:
        return True
    if p.suffix.lower() in ARTIFACT_SUFFIXES:
        return True
    return False
