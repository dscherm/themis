"""Per-language facts the gate needs, in one place.

Most of the gate is language-neutral: it hashes files, runs the configured
test command, and reads `Covers:` tags. A few stages have to read source
code, and each of those used to hard-code `.py`. They now ask this module:

  * which file extensions are source (`public_api.md` headings, the
    suppression scan);
  * how a test file names its task (`Bead: <id>`);
  * which file names are test files (`test_*.py`, `test_*.gd`);
  * which comments silence a check (suppression markers);
  * which quality scanner reads a test file.

An extension this module does not know is never treated as "nothing found".
Callers use `language_for` and report a file with no scanner as not scanned
(see lessons/silent-suffix-dispatch-fails-open.md).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


_BEAD_RE = re.compile(r"^[ \t]*Bead:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)", re.MULTILINE)


def _python_bead(text: str) -> str | None:
    """The id on a `Bead: <id>` line of a Python module docstring."""
    try:
        doc = ast.get_docstring(ast.parse(text))
    except (SyntaxError, ValueError):
        return None
    m = _BEAD_RE.search(doc or "")
    return m.group(1) if m else None


# Lines allowed in a GDScript file's header before the first real statement.
_GD_HEADER_LINE = re.compile(r"^\s*(?:$|#|extends\b|class_name\b|@tool\b|@icon\b|@static_unload\b)")
_GD_BEAD_RE = re.compile(r"^\s*#+[ \t]*Bead:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _gdscript_bead(text: str) -> str | None:
    """The id on a `## Bead: <id>` (or `# Bead: <id>`) comment in the file's
    header: the comments, `extends`, `class_name` and annotations that come
    before the first statement. GDScript has no module docstring, so the
    header block plays that role. A `Bead:` comment further down is a
    comment about something else and does not count."""
    for line in text.splitlines():
        if not _GD_HEADER_LINE.match(line):
            return None
        m = _GD_BEAD_RE.match(line)
        if m:
            return m.group(1)
    return None


@dataclass(frozen=True)
class Language:
    name: str
    extensions: tuple[str, ...]
    # Glob for this language's test files; "" when the gate has no collector.
    test_glob: str = ""
    # Reads `Bead: <id>` from file text; None when the language has no reader.
    bead_reader: Callable[[str], str | None] | None = None
    # Marker name -> regex for comments that silence a check.
    suppression_patterns: dict[str, str] = field(default_factory=dict)
    # A path spelling and a dotted spelling name the same module (Python).
    dotted_modules: bool = False


PYTHON = Language(
    name="python",
    extensions=(".py",),
    test_glob="test_*.py",
    bead_reader=_python_bead,
    dotted_modules=True,
)

GDSCRIPT = Language(
    name="gdscript",
    extensions=(".gd",),
    test_glob="test_*.gd",
    bead_reader=_gdscript_bead,
    suppression_patterns={
        # @warning_ignore(...), @warning_ignore_start(...), @warning_ignore_restore(...)
        "gd-warning-ignore": r"@warning_ignore(?:_start)?\s*\(",
    },
)

# Languages the gate only needs to recognise as source (headings, the
# suppression walk). They have no bead reader or test collector yet.
_RECOGNISED_ONLY = (
    Language("javascript", (".js", ".jsx", ".mjs", ".cjs")),
    Language("typescript", (".ts", ".tsx")),
    Language("csharp", (".cs",)),
    Language("java", (".java",)),
    Language("kotlin", (".kt",)),
    Language("go", (".go",)),
    Language("ruby", (".rb",)),
    Language("rust", (".rs",)),
    Language("c-family", (".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".mm")),
    Language("php", (".php",)),
    Language("swift", (".swift",)),
    Language("scala", (".scala",)),
)

LANGUAGES: tuple[Language, ...] = (PYTHON, GDSCRIPT, *_RECOGNISED_ONLY)

_BY_EXT = {ext: lang for lang in LANGUAGES for ext in lang.extensions}


def language_for(path: str | Path) -> Language | None:
    """The language of `path` by extension, or None if unknown."""
    return _BY_EXT.get(Path(str(path)).suffix.lower())


def source_extensions() -> frozenset[str]:
    """Every extension any known language uses."""
    return frozenset(_BY_EXT)


def collector_globs() -> tuple[str, ...]:
    """Test-file globs for every language the gate can collect."""
    return tuple(lang.test_glob for lang in LANGUAGES if lang.test_glob)


def is_collectable_test(path: str | Path) -> bool:
    """True if `path` is a test file of a language with a collector."""
    lang = language_for(path)
    return bool(lang and lang.test_glob and Path(str(path)).match(lang.test_glob))


def bead_of_text(path: str | Path, text: str) -> str | None:
    lang = language_for(path)
    if lang is None or lang.bead_reader is None:
        return None
    return lang.bead_reader(text)


def bead_of(path: str | Path) -> str | None:
    """The task id a test file names on its `Bead:` line, or None."""
    lang = language_for(path)
    if lang is None or lang.bead_reader is None:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return lang.bead_reader(text)


def extra_suppression_patterns() -> dict[str, str]:
    """Suppression markers contributed by language entries."""
    out: dict[str, str] = {}
    for lang in LANGUAGES:
        out.update(lang.suppression_patterns)
    return out
