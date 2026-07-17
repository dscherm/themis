"""Regression tests for Rust (.rs) AC-tag coverage extraction.

`cargo test` was allow-listed for the blind runner, but `extract_covers` had
no `.rs` branch — so a tagged Rust suite read as a coverage gap, the same
suffix-dispatch bug that hid GDScript. These pin the `.rs` extractor:
`// Covers: AC-N` comments above `#[test] fn` or on the fn's first body line
are attributed to the enclosing test fn.
"""
from __future__ import annotations

from blind_tdd.coverage import extract_covers

_RUST = """use crate::xp::XpCurve;

// Covers: AC-1 — a fresh curve returns 0 for level 1
#[test]
fn level_one_is_zero() {
    assert_eq!(XpCurve::new().xp_for_level(1), 0);
}

#[tokio::test]
async fn strictly_increasing() {
    // Covers: AC-2, AC-3
    let c = XpCurve::new();
    assert!(c.xp_for_level(3) > c.xp_for_level(2));
}

fn helper_not_a_test() {
    // Covers: AC-99  (must NOT be attributed — no #[test])
}
"""


def test_rust_covers_extracted_above_and_in_body(tmp_path):
    d = tmp_path / "tests"
    d.mkdir()
    (d / "xp_test.rs").write_text(_RUST, encoding="utf-8")

    anns = extract_covers(d)
    covered = {ac for a in anns for ac in a.covers}
    assert covered == {"AC-1", "AC-2", "AC-3"}

    # Attribution is per test fn; the non-#[test] helper claims nothing.
    tagged = {a.test_name: a.covers for a in anns if a.covers}
    assert tagged == {
        "level_one_is_zero": ["AC-1"],
        "strictly_increasing": ["AC-2", "AC-3"],
    }
    assert "helper_not_a_test" not in tagged


def test_rust_untagged_test_has_no_coverage(tmp_path):
    d = tmp_path / "tests"
    d.mkdir()
    (d / "plain_test.rs").write_text(
        "#[test]\nfn does_a_thing() {\n    assert!(true);\n}\n",
        encoding="utf-8",
    )
    assert {ac for a in extract_covers(d) for ac in a.covers} == set()
