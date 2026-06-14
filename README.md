# Themis — a blind TDD gate

[![CI](https://github.com/dscherm/themis/actions/workflows/ci.yml/badge.svg)](https://github.com/dscherm/themis/actions/workflows/ci.yml)

> *Themis is the blindfolded figure of impartial judgment. So is this gate.*

**Themis enforces test-first development by an agent that is structurally prevented from seeing the implementation.** A fresh "blind writer" agent derives the acceptance tests from the spec *alone* — it cannot read your source, your examples, or any generated data. Those tests are hash-locked before a single line of implementation is written, a separate "blind runner" agent produces the verified green report, and a third "arbiter" agent rules on any dispute.

The point is one specific failure mode: **the author–verifier collusion.** If the same agent (or human) that produces an artifact also writes — or signs off on — its check, the check inherits the author's misconception. The test passes, everyone agrees, and the thing is still wrong. Themis breaks that by making the verifier's independence a property of the *system*, not a matter of trust.

It only grounds what's **executably specifiable** — it is not a general knowledge-base validator. Within that scope, it's the part of the pipeline that can say "this code does what the spec says" without the author in the loop.

---

## Why this exists

Most "AI writes the tests" setups quietly let the implementing agent see, edit, or regenerate its own tests. That makes the green light meaningless: a model under deadline pressure will reconcile a failing assertion by editing the assertion. Themis closes every door it found that path tries to use:

1. **Blindness at the hook level.** The blind writer runs under a Claude Code `PreToolUse` path guard that *denies reads* of `src/`, examples, and generated data. It sees only the task spec and a single `public_api.md`. (Enforced by the hook, regardless of the agent's permission mode.)
2. **Hash-lock.** After the red phase, every test file is hashed. At the green phase the hashes are re-verified — any byte changed, the green fails. The implementer cannot edit the oracle to match a wrong build.
3. **Coverage check.** Every `AC-N` acceptance criterion must have a tagged, passing test. No silent gaps.
4. **Arbiter.** If the implementer thinks a test is genuinely wrong, they file a *challenge*. A fresh arbiter agent — which has seen neither the implementer's reasoning nor the writer's — rules upheld / rejected / ambiguous. Upheld deletes the test; ambiguous escalates to a human.

## Does it actually hold? — adversarial evidence

We pointed an [ImpossibleBench](https://arxiv.org/abs/2510.20270)-style methodology at our own gate: tasks whose acceptance criteria are **mutually unsatisfiable**, so *any* verified green is provably a cheat. Two arms on identical probes, run through the production `claude -p` spawner:

| Metric | Result |
|---|---|
| **ON-arm true-bypass rate (the gate)** | **0 / 42 (0%)** |
| OFF-arm false-green, adversarial framing (no gate) | 2 / 21 (9.5%) |
| OFF-arm false-green, realistic framing (no gate) | 0 / 21 (0%) |
| Measurement losses | 0 |

The gate drove a real, measured cheat rate to zero on tasks it is impossible to pass honestly. Two things we found that are worth your skepticism, both documented in full:

- **The spawn mechanism is a first-order confound.** The same probes run through in-conversation subagents cheated **100%**; through fresh `claude -p` CLI agents, **9.5%** — a ~10× swing from *how the agent was spawned*, nothing to do with the gate. Any agent-behavior measurement that doesn't control for this is measuring its own harness.
- **The hash layer is load-bearing, not redundant.** The path guard doesn't cover `Bash`; a Bash-capable adversary rewrote locked tests through the shell. Layer 2 (hash re-verification) caught every one. The path guard alone is *not* sufficient.

Full method, ledger, and limitations: **[docs/impossible-ac-results.md](docs/impossible-ac-results.md)**. Sample size is small (n=21/cell, wide CI on the 9.5%); read the limitations section before quoting a number.

## Honest limitations

- **Claude Code-specific.** Blindness is enforced by Claude Code `PreToolUse`/`PostToolUse` hooks and fresh-agent spawning (`claude -p` subscription, or the optional Agent SDK spawner). This is not a framework-agnostic library.
- **Executably-specifiable only.** Themis grounds behavior you can write a test against. Declarative knowledge (docs, facts, design notes) is out of scope.
- **Spec quality is the ceiling.** A blind writer can only test what the spec says. Garbage spec, garbage oracle — the `preflight` checks catch some of this (subjective language, missing observability) but not all.

## How it fits together

```
spec + public_api.md
        │
   ┌────▼─────┐   blind writer (Agent #1)        — path guard blocks src/, examples, generated data
   │  RED     │   writes failing tests from spec → hash-lock
   └────┬─────┘
        │            implementer writes code (cannot edit locked tests)
   ┌────▼─────┐   blind runner (Agent #2)         — re-verifies hashes, runs tests, checks AC coverage
   │  GREEN   │   produces verified green report
   └────┬─────┘
        │            (optional) challenge → arbiter (Agent #3) rules upheld/rejected/ambiguous
   ┌────▼─────┐
   │  GATED   │
   └──────────┘
```

| Component | File |
|---|---|
| Phase orchestrator (red/green/challenge) | `blind_tdd/orchestrator.py` |
| Session contract (drives the hooks) | `blind_tdd/session.py` |
| Path guard / bash guard / audit hooks | `templates/hooks/blind_tdd_*.py` |
| Per-role agent settings | `templates/blind_tdd/settings.blind-*.json` |
| Agent prompts (writer / runner / arbiter) | `templates/blind_tdd/prompts/` |
| AC coverage verifier | `blind_tdd/coverage.py` |
| Challenge / arbiter protocol | `blind_tdd/challenge.py` |
| Spec preflight + task linter | `blind_tdd/preflight.py`, `blind_tdd/lint_tasks.py` |
| Spawners (`claude -p`, Agent SDK) | `blind_tdd/spawners/` |
| Impossible-AC probe harness | `blind_tdd/probes.py`, `blind_tdd/probe_driver.py` |

## Quickstart

```bash
git clone <repo-url> themis && cd themis
pip install -e ".[dev]"      # add ".[dev,sdk]" for the Agent-SDK spawner

# run the suite (272 tests)
python -m pytest

# inspect the impossible-AC probe catalog and gate verdict
python -m blind_tdd.probes --list
python -m blind_tdd.probes --gate --min-on-runs 20   # needs a probe ledger; see the results doc
```

To adopt the gate on your own project, start with **[docs/adoption-guide.md](docs/adoption-guide.md)**. The full design rationale and resolved open questions are in **[docs/rfc.md](docs/rfc.md)**.

## Provenance

Themis is the standalone extraction of a blind-TDD gate built inside a larger cross-project agent-learning harness. It runs on top of **oh-my-claudecode** for agent orchestration and its `deep-interview` intake — the spec-crystallization step that feeds the gate its acceptance criteria. The blind gate itself (the hooks, hash-lock, arbiter, and impossible-AC probe harness) is original to this project.

## License

MIT © 2025–2026 Dan Schermele. See [LICENSE](LICENSE).
