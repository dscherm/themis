# Post-ruling re-seal: what an upheld challenge does to the red seal

**Status:** shipped 2026-10-24. Code: `gate_integration.record_pending_ruling`,
`gate_integration.reseal_after_rulings`, the "POST-RULING RE-SEAL" block in
`_run_blind_tdd_gate`, and the call from `challenge.apply_ruling`. Tests:
`blind_tdd/test_challenge.py` (apply_ruling → red state) and
`blind_tdd/test_hash_integrity.py` (the scenario through the gate).

## The failure this closes

Twice on 2026-10-24, in ralph-universal's beads BL-2 and BM-1, the same thing
happened:

1. The red phase sealed the blind tests (sha256 per file in
   `.themis/blind_tdd/red_state/<task>.json`).
2. The implementer challenged one test. A fresh arbiter **upheld** it
   (`rulings/chal-BL-2-ce29d626.json`, `rulings/chal-BM-1-a7fa0e2a.json`) and
   prescribed a narrow rewrite — one assertion in BL-2, one fixture value in
   BM-1.
3. `apply_ruling` deleted the file. A writer recreated it with only the
   prescribed change.
4. The next gate run compared the tree against the red state, which still held
   the **old** hash, and reported `test file integrity compromised: 1 file(s)
   modified between red and green phases`.

Under `enforcement: warn` the commit went through anyway, with a
`test_hash_break` record in the tamper ledger that was false. Under `strict`
the gate would have blocked a rewrite the engine itself ordered. Either way the
runner agent was left explaining the mismatch away in prose
(`"Mismatch is sanctioned, not tampering"`), which is exactly the kind of
unverifiable assertion the seal exists to replace.

## The design question, and the decision

> When an upheld ruling forces a test rewrite and an implementation already
> exists, does the red phase re-run in full, or does a "post-ruling re-seal"
> mode hash the rewritten file without requiring anything of the tests?

**Decision: post-ruling re-seal. The red phase does not re-run.**

Three reasons, in order of weight:

1. **A full re-run would re-hash the whole tree and launder unrelated edits.**
   The seal's job during the implementation window is to notice that *any*
   locked file changed. Re-sealing everything at the moment of the ruling
   would stamp whatever state every other file is in as the new baseline —
   an implementer who had quietly weakened a second test would have it
   accepted for free. The re-seal touches the one file the ruling names and
   leaves every other hash exactly as the original red phase recorded it, so
   the green check still fails on that second file. (`test_unrelated_edit_
   still_breaks_integrity_after_a_reseal` and the claude_code resume test
   both pin this.)

2. **The red phase here never required the tests to fail.** Read
   `run_red_phase`: it validates the schema, spawns a sealed blind writer,
   checks that every criterion has a tagged test or a triage entry, and
   hashes. It explicitly does *not* run the tests ("We don't require tests to
   pass yet"). The ordering guarantee comes from blind authorship — the writer
   could not see the implementation — not from an observed red. So "the tests
   must fail" is not a property a re-run would restore; the only thing a
   re-run adds over a re-seal is the full-tree hash, which is the harm in (1).

3. **The writer that does the rewrite is the same kind of writer.** Under the
   `claude_code` spawner the gate calls `resume_red_phase_after_ruling`, which
   opens a sealed `blind_session` for the `test_writer` role with the arbiter's
   reasoning appended to its brief — the blindness claim for the rewritten file
   is the same claim the original red phase made. Under the `manual` spawner
   the operator dispatches the writer by hand, exactly as they did for the
   original red phase; the gate's evidence about blindness is the same in both
   phases, no better and no worse.

## Mechanics

**At ruling time** (`challenge.apply_ruling`, verdict `upheld`): after deleting
the disputed file, `record_pending_ruling` appends to the task's red state

```json
"pending_rulings": [{
  "challenge_id": "chal-BM-1-a7fa0e2a",
  "test_file": "tests/contracts/test_bm1_bd_memory.py",
  "criterion_affected": "AC-1",
  "resolution": "fixture_precondition_fix: change ONLY the value of ...",
  "sealed_hash": "ed694f29...",
  "applied_at": "2026-10-24T05:40:00+00:00"
}]
```

`sealed_hash` is the hash the red phase recorded for that file, so the gate can
later tell a rewrite from a `git checkout` of the deleted file. The outcome is
reported in `ChallengeResult.reseal_recorded` / `reseal_note`. If the task has
no red state (a challenge outside a gated red phase), nothing is recorded and
the note says so.

**At the next gate run** (green side of `_run_blind_tdd_gate`, after the seal
HMAC and security-pack checks, before `run_green_phase`):
`reseal_after_rulings` looks at each pending entry's file.

| File state | What happens |
|---|---|
| exists, hash ≠ `sealed_hash` | hash replaced in `test_file_hashes`; entry moves to `rulings_applied` with `resealed_hash`; green runs and accepts it |
| missing | stays pending, `status: missing` |
| exists, hash = `sealed_hash` | stays pending, `status: unchanged` (restored, not rewritten) |

If anything is still pending and the spawner is `claude_code`, the gate calls
`resume_red_phase_after_ruling` (the ruling document from `rulings/` as
context), then re-seals again — still only the ordered files; the resumed
writer's merged triage report replaces the stored one. The manual spawner is
deliberately **not** routed through the resume: its completion check would see
the original triage report on disk and call a writer that never ran
"complete".

If anything is still pending after that, the gate returns
`reason: ruling_rewrite_pending` (phase `green`, `passed` = not strict), with a
message naming each ruling id, its criterion, the file, and why it is pending.
Green is not run and nothing goes to the tamper ledger — a missing file is not
tampering, it is an unfinished rewrite.

**Citation.** Once applied, every gate result for the task carries
`details.rulings_applied` (the full entries, both hashes) and the message
ends with `N sealed file(s) re-hashed under upheld ruling(s) <ids>.` On a
hash break elsewhere the message adds that the named files are covered by no
ruling. `RedPhaseResult.rulings_applied` is passed to the blind runner's
inputs and recorded in the `blind_green_phase` observation, so the citation
survives the green pass clearing the red state.

## Trust boundary — what this does and does not check

- **Scope: one file, same path.** The ruling licenses a change to the file the
  challenge named. A rewrite that lands at a different path is a new file to
  the hash set and is flagged like any other addition. Both real rulings
  rewrote in place; the arbiter's `resolution` should keep saying so.
- **Content is not checked against the resolution.** The gate accepts
  whatever is at the path. It records `sealed_hash` and `resealed_hash` so a
  human can `git diff` the two and read them against the ruling's
  `resolution`, but nothing mechanical does. This is the same boundary the
  original red phase has (it never reads test content either); an operator
  who wants it enforced reviews the applied entry at commit time.
- **HMAC.** `pending_rulings` and `rulings_applied` are in `_SEAL_EXTRA_KEYS`,
  so with `THEMIS_SEAL_KEY` set they are signed with the baseline.
  `_rewrite_red_state` refuses to touch a signed record when the key is
  unavailable or the HMAC fails — writing it anyway would leave an
  *unsigned* record, which verifies trivially at green. So an implementer
  running `apply_ruling` with a forged ruling in a key-less shell gets the
  test deleted (as before) but no licence: the seal is untouched, the refusal
  is in `reseal_note`, and green flags the file exactly as it did on
  2026-10-24. Without a key configured, this is forgeable in the same way
  everything else in the red state is — see `docs/limitations.md`.
- **Arbiter judgment is still the weak link.** A wrong `upheld` now licenses
  a rewrite the gate will accept, where before it produced a loud (if
  misattributed) failure. The caps in `max_challenges_per_task` /
  `max_challenges_per_criterion` bound how often that can happen per task;
  the `rulings_applied` record makes each instance auditable.

## Operator notes

- Nothing to configure. Existing red states without `pending_rulings` behave
  exactly as before; the HMAC of records written before this change still
  verifies (extras are folded in only when present).
- Under `manual`, the message to act on is `ruling_rewrite_pending`: dispatch
  the writer, have it rewrite the file at the same path per the ruling's
  `resolution`, re-run the gate.
- BL-2 and BM-1 themselves were committed under `warn` before this landed.
  Their red states carry no pending entry and their ledger rows stay as
  recorded; this change is forward-looking.
