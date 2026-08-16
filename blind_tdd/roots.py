"""Derive the blindness seal's denied roots from the project's own config.

## The problem this replaces

`session._DEFAULT_BLOCKED` used to be a fixed list — `src/**`, `examples/**`,
`.git/**`, `data/generated/**` — applied to every project regardless of its
actual layout. That is a JavaScript project's shape. A Python project laid
out as `server/`, `core/`, `mastery_core/`, `bridge/` (no `src/` at all) got
a deny-list that matched nothing real: the blind writer's `blocked_paths`
denied a directory that doesn't exist, while the directories that actually
hold the implementation went unmentioned. Whether that mattered in practice
depended on `allowed_paths` also being correctly scoped for every spawn path
— and at least one spawn path (a manually-brief agent run as an in-session
subagent, per the adoption guide) has no `PreToolUse` hook installed at all,
making the brief's own denied-roots list — and its prose — the only defense.
A seal that matches nothing must never be indistinguishable from a seal that
holds; this module makes the difference visible by failing loudly instead of
silently producing an empty/wrong deny-list.

## The fix

`derive_blocked_paths(config, project_root)` reads `stack.languages` (or,
absent that, the keys of `stack.test_runner`) from the project's own
`schermness.config.json` and returns real denied-path globs:

  - For JS-family languages (`node`, `javascript`, `typescript`), the
    conventional `src/**` + `examples/**` deny-list is kept *as a convention
    keyed off the declared language*, not applied universally. This is
    deliberate, not laziness: some JS projects (e.g. co-located tests) point
    `stack.test_runner.node.test_dir` AT `src/`, so a scan that excludes
    "wherever the test runner points" would wrongly un-seal the very
    directory that also holds the implementation.

  - For every other declared language, the roots are found by scanning the
    project's top-level directories for files with that language's source
    extension, excluding the directories the project's own config names as
    test locations (`stack.test_runner.<lang>.test_dir`,
    `gate.blind_tdd.test_dirs`) and a small set of universal tooling/VCS
    directories no ecosystem treats as source (`.git`, `node_modules`,
    `__pycache__`, `dist`, `build`, and anything dot- or dunder-prefixed).

If no language can be determined, or a declared non-JS language has no known
extension mapping, or scanning finds zero source-bearing directories, this
raises `RootInferenceError` rather than returning an empty or partial list.
Callers MUST NOT catch that and fall back silently — surface it as a gate
failure so a human fixes the config instead of a blind session running with
nothing actually blocked.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Languages that get the conventional src/**+examples/** deny-list instead of
# a filesystem scan (see module docstring for why: their own test_dir can
# legitimately point at the same directory as the implementation).
_JS_LANGS = {"node", "javascript", "typescript", "js", "ts"}
_JS_CONVENTION_ROOTS = ("src", "examples")

# Source-file extensions per language, used only for the scanning path
# (non-JS languages). Extend this when onboarding a new scanned language.
_LANG_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "python": (".py",),
    "rust": (".rs",),
    "go": (".go",),
    "csharp": (".cs",),
    "c#": (".cs",),
    "java": (".java",),
    "ruby": (".rb",),
}

# Top-level directory names no ecosystem treats as source, regardless of
# language. This is scaffolding/VCS/build output, not an implementation
# guess — kept intentionally small.
_NEVER_SOURCE_DIRS = {"node_modules", "dist", "build", ".git"}

# Denied unconditionally, independent of language — implementation-adjacent
# regardless of project shape (unchanged from the previous universal default).
_UNCONDITIONAL_BLOCKED = [".git/**", "data/generated/**"]


class RootInferenceError(RuntimeError):
    """Raised when the blindness seal's denied roots cannot be determined.

    A seal that matches nothing must never be indistinguishable from a seal
    that holds. Callers must surface this as a visible failure (a gate
    result with a clear reason), not swallow it and proceed with an empty
    or partial deny-list.
    """


@dataclass
class SealedRoots:
    """The result of root inference: what got blocked, and how."""

    blocked_paths: list[str]
    roots: list[str]
    languages: list[str]
    method: str  # e.g. "js-convention", "language-scan", "js-convention+language-scan"

    def to_record(self) -> dict:
        """A JSON-serializable record for auditing (see red_state["sealed_roots"])."""
        return {
            "blocked_paths": list(self.blocked_paths),
            "roots": list(self.roots),
            "languages": list(self.languages),
            "method": self.method,
        }


def _stack_languages(stack_cfg: dict) -> list[str]:
    langs = stack_cfg.get("languages")
    if isinstance(langs, list) and langs:
        return [str(lang).lower() for lang in langs]
    # Some configs (older projects, synthetic sandboxes) declare
    # stack.test_runner.<lang> without an explicit stack.languages array —
    # the test_runner keys ARE the declared languages in that case.
    test_runner = stack_cfg.get("test_runner")
    if isinstance(test_runner, dict) and test_runner:
        return [str(lang).lower() for lang in test_runner.keys()]
    return []


def _configured_test_dir_tops(config: dict, stack_cfg: dict, languages: list[str]) -> set[str]:
    """Top-level directory names the project's own config marks as test
    locations — never treated as a source root even if it contains code."""
    tops: set[str] = set()

    blind_tdd_dirs = ((config.get("gate") or {}).get("blind_tdd") or {}).get("test_dirs") or []
    for entry in blind_tdd_dirs:
        top = str(entry).strip("/").split("/")[0]
        if top:
            tops.add(top)

    test_runner = stack_cfg.get("test_runner") or {}
    for lang in languages:
        lang_cfg = test_runner.get(lang) or {}
        test_dir = lang_cfg.get("test_dir")
        if test_dir:
            top = str(test_dir).strip("/").split("/")[0]
            if top:
                tops.add(top)

    return tops


def _scan_source_roots(
    project_root: Path, extensions: tuple[str, ...], exclude_tops: set[str],
) -> list[str]:
    """Top-level directories under project_root that contain at least one
    file matching `extensions`, excluding known-non-source directories."""
    try:
        entries = sorted(p.name for p in project_root.iterdir() if p.is_dir())
    except OSError:
        return []

    roots: list[str] = []
    for name in entries:
        if name.startswith(".") or name.startswith("__"):
            continue
        if name in _NEVER_SOURCE_DIRS or name in exclude_tops:
            continue
        candidate = project_root / name
        has_source = any(
            path.is_file() and path.suffix in extensions
            for path in candidate.rglob("*")
        )
        if has_source:
            roots.append(name)
    return roots


def derive_blocked_paths(config: dict, project_root: str | Path = ".") -> SealedRoots:
    """Derive the blindness seal's `blocked_paths` from the project's own config.

    Args:
        config: the full project config dict (schermness.config.json contents).
        project_root: directory to scan for non-JS languages. Defaults to CWD,
            matching every other blind_tdd module's assumption that the
            gate runs from the project root.

    Raises:
        RootInferenceError: if no language is declared, a declared non-JS
            language has no known extension mapping, or scanning finds no
            source-bearing top-level directory.
    """
    root = Path(project_root)
    stack_cfg = config.get("stack") or {}
    languages = _stack_languages(stack_cfg)

    if not languages:
        raise RootInferenceError(
            "cannot derive blindness-seal denied roots: config declares no "
            "stack.languages and no stack.test_runner entries. A project "
            "this ambiguous must not get an empty/no-op seal — declare "
            "stack.languages in schermness.config.json (or ralph.config.json), "
            "or construct the orchestrator with blocked_paths set explicitly."
        )

    js_langs = [lang for lang in languages if lang in _JS_LANGS]
    other_langs = [lang for lang in languages if lang not in _JS_LANGS]

    blocked: list[str] = []
    roots: list[str] = []
    methods: list[str] = []

    if js_langs:
        for name in _JS_CONVENTION_ROOTS:
            blocked.append(f"{name}/**")
            roots.append(name)
        methods.append("js-convention")

    if other_langs:
        exclude_tops = _configured_test_dir_tops(config, stack_cfg, other_langs)
        scanned: list[str] = []
        for lang in other_langs:
            extensions = _LANG_EXTENSIONS.get(lang)
            if not extensions:
                raise RootInferenceError(
                    f"cannot derive blindness-seal denied roots: language "
                    f"{lang!r} (declared in stack.languages) has no known "
                    f"source-file extension mapping in blind_tdd/roots.py. "
                    f"Add it there, or construct the orchestrator with "
                    f"blocked_paths set explicitly."
                )
            for found in _scan_source_roots(root, extensions, exclude_tops):
                if found not in scanned:
                    scanned.append(found)

        if not scanned:
            raise RootInferenceError(
                f"cannot derive blindness-seal denied roots for language(s) "
                f"{other_langs} under {root}: no top-level directory contains "
                f"source files for the declared language(s), after excluding "
                f"the configured test dir(s) {sorted(exclude_tops)}. A seal "
                f"that denies nothing is worse than no seal at all — refusing "
                f"to proceed rather than emitting an empty deny-list. If this "
                f"project's layout is unconventional, construct the "
                f"orchestrator with blocked_paths set explicitly."
            )

        blocked.extend(f"{name}/**" for name in scanned)
        roots.extend(scanned)
        methods.append("language-scan")

    blocked.extend(_UNCONDITIONAL_BLOCKED)

    return SealedRoots(
        blocked_paths=blocked,
        roots=roots,
        languages=languages,
        method="+".join(methods),
    )
