"""Smoke test for the assembled blind-tdd plugin (BT4 AC-2).

Asserts the plugin bundles the Themis engine *via pip* rather than vendoring it:

  - the engine package (`blind_tdd` from the `themis-blind-tdd` distribution) is
    importable and resolves to an installed site/dist location, NOT to a copy
    inside this plugin;
  - the plugin tree contains no vendored engine modules and no reference to the
    deleted ralph-local `tools/blind_tdd/` path;
  - `requirements.txt` records the Themis engine pin.

Run: python -m pytest plugin/blind-tdd/test_plugin_smoke.py -q
"""

from __future__ import annotations

import importlib.metadata as ilm
import importlib.util
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_PLUGIN_DIR = _HERE.parent

# Engine modules that would indicate the orchestration was vendored in.
_ENGINE_MODULE_NAMES = {
    "orchestrator.py",
    "session.py",
    "gate_integration.py",
    "coverage.py",
    "challenge.py",
    "preflight.py",
    "schema_validator.py",
}


def test_engine_distribution_installed():
    """The Themis engine is available as the themis-blind-tdd pip distribution."""
    version = ilm.version("themis-blind-tdd")  # raises PackageNotFoundError if absent
    assert version, "themis-blind-tdd reports an empty version"


def test_engine_imports_resolve_to_installed_package_not_plugin():
    """`import blind_tdd` resolves to the installed engine, not a plugin-local copy."""
    spec = importlib.util.find_spec("blind_tdd")
    assert spec is not None, "blind_tdd engine is not importable; run pip install -r requirements.txt"
    assert spec.origin, "blind_tdd has no module origin (unexpected namespace package)"
    origin = Path(spec.origin).resolve()  # .../blind_tdd/__init__.py
    # Must not live underneath this plugin directory (i.e. not vendored).
    assert _PLUGIN_DIR not in origin.parents, (
        f"blind_tdd resolved to a plugin-local copy ({origin}); the engine must be "
        "consumed via pip, not vendored into the plugin"
    )


def test_no_vendored_engine_modules_in_plugin():
    """No engine source files were copied into the plugin tree."""
    found = [p for p in _PLUGIN_DIR.rglob("*.py") if p.name in _ENGINE_MODULE_NAMES]
    assert not found, f"vendored engine modules found in plugin: {[str(p) for p in found]}"


def test_no_reference_to_deleted_ralph_local_engine_path():
    """Nothing in the plugin points at the removed ralph-local tools/blind_tdd/ path."""
    offenders: list[str] = []
    for path in _PLUGIN_DIR.rglob("*"):
        if not path.is_file() or path.suffix == ".pyc":
            continue
        if "__pycache__" in path.parts:
            continue
        if path.resolve() == _HERE:
            continue  # this test names the sentinel path on purpose
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "tools/blind_tdd" in text or "tools\\blind_tdd" in text:
            offenders.append(str(path.relative_to(_PLUGIN_DIR)))
    assert not offenders, f"plugin files still reference tools/blind_tdd: {offenders}"


def test_requirements_records_engine_pin():
    """requirements.txt pins the Themis engine distribution."""
    reqs = _PLUGIN_DIR / "requirements.txt"
    assert reqs.exists(), "requirements.txt is missing; run assemble.py"
    text = reqs.read_text(encoding="utf-8")
    assert "themis-blind-tdd" in text
    assert "github.com/dscherm/themis" in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
