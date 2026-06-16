# Themis

[![CI](https://github.com/dscherm/themis/actions/workflows/ci.yml/badge.svg)](https://github.com/dscherm/themis/actions/workflows/ci.yml)

A blind test-first gate for AI-written code. It enforces that the agent which checks the work cannot see, or quietly edit, the work it is checking.

## What it is

When the same agent writes code and writes the tests that check the code, the tests are not an independent verdict. They pass against whatever the agent built, misreadings included. Themis breaks that by making the verifier's independence a property of the system rather than a matter of trust: the agent that derives the tests is structurally denied any view of the implementation, the tests are cryptographically sealed before any code exists, and a separate agent runs them.

## What it does and does not do

Themis grounds only what is executably specifiable. Within that scope, it can establish that code does what a specification says without the author of the code in the loop.

It does **not**:

- judge a spec's correctness, quality of design, or completeness (a vague or poorly thought-out spec can earn a confident pass on the wrong thing; the spec is the trust boundary)
- verify declarative knowledge, prose, or design rationale (it is not a lie-detector for an agent's text)
- guarantee a perfect wall (it is defense in depth; see [Limitations](#limitations))

It is **Claude Code-specific**: blindness is enforced through Claude Code `PreToolUse` / `PostToolUse` hooks and fresh-agent spawning. It is not a framework-agnostic library.

## How it works

The design is permissions. Each agent is granted exactly what it needs and denied the rest, enforced by tooling rather than by instruction.

- **Blind writer** derives the acceptance tests from the spec and a single `public_api.md`. A hook denies it any read of the source, the examples, or generated data, so it cannot copy the implementation's behavior because it cannot see it.
- **Implementer** writes code to pass those tests. It can see and change everything except the sealed tests. The wall here is honest about its limits: a path guard blocks the ordinary edit tools from rewriting a locked test, but it does not cover Bash, and a determined agent can rewrite a file through the shell. That gap is closed by the hash layer below.
- **Blind runner** is spawned fresh, never having seen the implementer's reasoning. It re-verifies the seal, runs the tests, and checks that every acceptance criterion has a tagged passing test.
- **Arbiter** appears only on a dispute. The implementer cannot delete a test it believes is wrong; it can only file a challenge, which a fresh arbiter (having seen neither the writer's nor the implementer's reasoning) rules on. Upheld removes the test; ambiguous escalates to a human.

### The seal (the load-bearing layer)

Before any implementation exists, each test file is fingerprinted with a SHA-256 hash and the fingerprints are recorded. At the green phase the runner recomputes each fingerprint and compares it to the sealed one. A test changed by any route, shell included, no longer matches and fails the run regardless of whether it now passes. Adding or deleting a locked test also breaks the comparison.

### Pipeline

```
spec + public_api.md
      |
   [ RED ]    blind writer -> failing tests -> SHA-256 seal
      |
   implementer writes code (cannot edit sealed tests)
      |
   [ GREEN ]  blind runner -> re-verify seal -> run tests -> check AC coverage
      |
   (optional) challenge -> arbiter -> upheld / rejected / ambiguous
      |
   [ GATED ]
```

## Quickstart

```bash
git clone <repo-url> themis && cd themis
pip install -e ".[dev]"        # add ".[dev,sdk]" for the Agent SDK spawner

python -m pytest               # run the test suite

# inspect the impossible-AC probe catalog and the gate verdict
python -m blind_tdd.probes --list
python -m blind_tdd.probes --gate --runs-file data/probe_runs.jsonl --min-on-runs 20
```

To adopt the gate on an existing project, start with [`docs/adoption-guide.md`](docs/adoption-guide.md). The design rationale is in [`docs/rfc.md`](docs/rfc.md).

## Evidence

Themis was tested against an [ImpossibleBench](https://arxiv.org/abs/2510.20270)-style probe set: coding tasks whose acceptance criteria are mutually contradictory, so that any verified pass is provably a cheat. On these tasks there is nothing but cheating to detect.

| Condition | Result |
|---|---|
| Gate **on** — verified bypasses (all 42 runs, both framings) | **0 / 42** |
| Gate **off** — false greens, adversarial framing | 2 / 21 (9.5%) |
| Gate **off** — false greens, ordinary framing | 0 / 21 |
| Measurement losses | 0 |

The honest reading: the gate drove the bypass rate to zero where cheating was both possible and incentivized, and the no-gate arm only cheated under adversarial framing. The gate earns its keep under pressure, which is the realistic condition for a deployed agent, not the calm one.

Two caveats belong next to those numbers, not in a footnote:

- **Small sample.** n is 21 per cell; read [`docs/impossible-ac-results.md`](docs/impossible-ac-results.md) before quoting any figure.
- **The spawn mechanism is a first-order confound.** Identical probes cheated ~100% through in-conversation subagents and ~9.5% through fresh `claude -p` agents, a roughly tenfold swing driven by how the agent was spawned. Every reported number uses the production spawner. Any agent-behavior measurement that does not control for this is partly measuring its own harness.

## Limitations

The full, honest list — including the spec-as-trust-boundary problem, the persistence of the hash baseline, the arbiter's input surface, and per-model staleness — is in [`docs/limitations.md`](docs/limitations.md). Read it before relying on the gate.

## Prior art

The principle is old: a checker cannot be independent of the thing it checks. It is the logic of clean-room software development (a separate team certifies code its authors never test) and separation of duties (whoever writes the check cannot sign it), and the general failure it guards against is reward hacking, or Goodhart's law. Themis applies those ideas to AI-written code, with the independence enforced as a permission rather than a policy. That last point is the distinction from the spec-driven-development tools that assign a verifier as a role the agent is asked to play, where the verifier can still read the implementation and the tests can still be edited after the fact.

## Provenance

Themis is the standalone extraction of a blind-TDD gate built inside a larger agent-learning harness. The blind gate itself (the hooks, the hash seal, the arbiter, and the impossible-AC probe harness) is original to this project.

## License

MIT. See [LICENSE](LICENSE).
