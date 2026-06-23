# Impossible-AC Evidence — Follow-ups

**Status:** Drafted 2026-06-22. Open work; not yet started.
**Follow-on to:** [`impossible-ac-results.md`](impossible-ac-results.md) (BT2b published baseline, `data/probe_runs.jsonl`, 84 runs)

These items came out of a methodological review of the two caveats the results
doc already names — the **spawn-mechanism confound** (§4) and the **n = 21 / cell**
sample (§7). The review's conclusion: the design and construct are sound, and the
doc is honest about its limits, but two things are weaker than the headline
implies and one gap is structural.

The structural gap (P1) is the important one: in the 42 published (subscription)
ON runs the lock was essentially never exercised — **0 adversarial tamper
attempts, 1 realistic** — so "0/42 verified bypass" mostly measures that
production `claude -p` agents rarely *attempt* to cheat (a base rate), not that
the gate *stops* them when they do (enforcement). The evidence that enforcement
works against a determined adversary comes almost entirely from the **n=7
Agent-tool arm** — the arm §4 tells the reader to distrust. The confound control
("use `claude -p`") and the enforcement proof ("the hash layer is load-bearing,
§5") are therefore in direct tension.

Statistical reality, for reference:
- `0/42` ON bypass → 95% CI upper bound ≈ **7.1%** (rule of 3). "0 observed" is
  exact; "uncheatable" is an inferential overreach.
- `2/21` OFF-adversarial (9.5%) → Wilson 95% CI ≈ **[2.7%, 29%]** (wide).
- `7/7` Agent-tool (100%) → 95% CI lower bound ≈ **65%**; the "~10×" swing is real
  in direction, imprecise in magnitude.

---

## P1 — Stress Layer 2 under the production spawner (highest leverage)

**Goal:** Make the enforcement claim ("the hash layer guarantees 0% bypass", §5)
stand on the *trusted* spawner, not only on the n=7 Agent-tool contrast.

**Why:** Today Layer 2 (green-phase hash re-verification) was only ever triggered
in the Agent-tool arm (4/7 Bash escalations, all caught). Under `claude -p` the
agents never escalated to Bash, so the lock was never tested in the arm whose
numbers we publish. The gate's actual job is unproven on the production spawner.

**Acceptance criteria:**
- AC-1: Add an adversarial framing variant that explicitly licenses and pressures
  Bash-based test rewriting ("the Edit tool is blocked; you may modify files
  through the shell"), run through the `claude -p` subscription spawner.
- AC-2: At least some ON runs in this variant reach a Bash tamper of a locked
  test (Layer 1 bypassed), and **every one is caught by Layer 2** (green-phase
  hash mismatch → blocked), recorded as `blocked_tamper`, never `false-green ∧ intact`.
- AC-3: If agents still refuse to escalate under `claude -p`, that null result is
  reported explicitly as a finding about spawner docility — not silently omitted.

## P2 — Record `spawner` in the row schema

**Goal:** Make "every reported number uses the production `claude -p` spawner"
verifiable from the published ledger instead of asserted in prose.

**Why:** `data/probe_runs.jsonl` has no `spawner` field. A reader cannot confirm
the provenance of any row.

**Acceptance criteria:**
- AC-1: Every row in `probe_runs.jsonl` carries `spawner` ∈ {`claude_code_subscription`, `agent_tool`, …}.
- AC-2: `probe_driver` writes the field at capture time; `probes.py --gate` can
  optionally filter/report by spawner.
- AC-3: The 84 published rows are backfilled to `spawner: claude_code_subscription`.

## P3 — Publish the Agent-tool contrast ledger

**Goal:** Make the confound (the load-bearing methodological claim) reproducible,
not narrated.

**Why:** The n=7 Agent-tool numbers (7/7, 4/7 Bash) live only in
`data/reports/batch1-agenttool-contrast-REPORT.md`; the per-run rows are "not
published." The doc's central argument can't be independently checked.

**Acceptance criteria:**
- AC-1: A redacted Agent-tool ledger (same row schema, `spawner: agent_tool`) is
  committed under `data/`.
- AC-2: `probes.py --gate` (or a sibling) reproduces the 7/7 vs 2/21 contrast from
  the published rows.

## P4 — Raise n on the adversarial cells

**Goal:** Tighten the intervals on the cells that actually carry signal.

**Why:** The OFF-adversarial 2/21 and ON-adversarial 0/21 are the informative
cells; both have wide CIs. Realistic cells are near-saturated and add little.

**Acceptance criteria:**
- AC-1: Adversarial OFF and ON cells raised to n ≥ 50 each (more runs/probes),
  via the idempotent `--only-missing` driver path.
- AC-2: Updated CIs reported in `impossible-ac-results.md`.

## P5 — Reframe the headline to separate base-rate from enforcement

**Goal:** Make the prose claims inherit the caveats' force.

**Why:** §3 ("uncheatable… 0/42") reads stronger than the data licenses and
conflates "agents rarely attempt cheating here" with "the gate blocks cheating."

**Acceptance criteria:**
- AC-1: §3 distinguishes the two claims explicitly and carries the `0/42 → ≤~7%`
  CI next to "0 observed".
- AC-2: The enforcement claim is scoped to where it is actually evidenced (pending
  P1), not to the 42 subscription runs in which the lock was idle.

---

## Suggested order

P1 → P2 → P5 (P2 unblocks honest reframing; P1 produces the evidence P5 needs) →
P3 → P4. P1 is the only item that changes what the study can *claim*; the rest are
verifiability and precision.
