---
description: Interview the operator to build a project-specific security AC pack (explores the repo first, drafts concrete criteria, installs via pack_wizard).
argument-hint: "[optional: a one-line description of what this project must defend]"
allowed-tools: Bash, Read, Grep, Glob, AskUserQuestion
---

You are running the security-pack tailoring interview. Your job is to replace the
generic default security AC pack with **project-specific** security acceptance
criteria, decided **with the user**, and install them so the blind-TDD gate derives
sealed, independent security tests from them on every matching task.

Background you must convey briefly at the start:
- The gate's trust boundary is the spec: a task whose criteria never mention input
  validation earns a confident pass without any. The pack closes that — but generic
  criteria ("any public entry point") derive weak tests. Concrete criteria that name
  this project's real functions and sinks derive strong ones. That's what this
  interview produces.
- The finished pack is **spec content the user owns**: it gets committed, reviewed
  like a spec change, and the gate fingerprints it (a pack changed mid-task fails
  the green phase). You draft; the user decides.

## Ground rules (follow these exactly)

1. **Explore before asking.** Never ask the user a question the repo can answer.
   Gather facts first, then ask the user to *confirm and prioritize*, citing the
   evidence you found (file paths, function names) — not to recall their codebase
   from memory.
2. **One question at a time**, via AskUserQuestion. Offer concrete options drawn
   from your exploration, plus honest descriptions of the trade-offs.
3. **The exit gate is objective, not vibes.** A draft is done when
   `python -m blind_tdd.pack_wizard --lint <draft>` reports **zero errors and zero
   genericity warnings** (notes warnings are acceptable if the user says so).
   Iterate: draft → lint → fix → re-lint. Do not present a draft to the user for
   final approval until it lints clean of errors.
4. **The user approves; you never install silently.** Show the final pack and the
   exact install command before running it.

## Step 1 — explore the attack surface

Map the repo (Grep/Glob/Read; keep it to a few minutes of work):

- **Entry points**: exported/public functions, CLI arg parsing, HTTP routes,
  file readers — wherever outside data enters. Check `public_api.md` first if it
  exists; it is the curated surface.
- **Sinks**: file-path construction, `subprocess`/shell calls, SQL/query building,
  HTML/markup rendering, `eval`/`exec`/deserialization.
- **Secret handling**: config loading, env-var reads, anything named token/key/
  password/credential.
- **Error surfaces**: what failures reach users or logs (exception handlers,
  API error responses, CLI stderr).

If `$ARGUMENTS` describes what the project must defend, use it to focus the sweep.

## Step 2 — interview, one question per round

Ask the user to confirm what matters, in roughly this order, always citing repo
evidence ("I found `X` at `path:line` — ..."):

1. Which discovered entry points take untrusted input, and are there size/type
   limits the criteria should pin down? (Get actual limits — "rejects strings over
   4096 chars" beats "rejects oversized input".)
2. Which sinks are reachable from those inputs, and which injection shapes apply
   (path traversal, shell metacharacters, SQL quotes, markup)?
3. What counts as a secret in this repo, and which files must the no-hardcoded-
   secrets scan cover?
4. What may an error message shown to a caller contain, and what must it never
   contain?
5. Anything the default pack covers that does NOT apply here (so it can be
   dropped rather than triaged as needs_human on every task)?

Stop asking when every remaining question would only confirm what the user
already said — then move to drafting. If the user says "just use your judgment",
draft from the exploration evidence and mark your assumptions clearly in the
final approval step.

## Step 3 — draft and lint-loop

Write the draft to `.themis/security_pack_draft.json`, shaped like
`templates/blind_tdd/security_ac_pack.json` (fields: `version`, `name`,
`description`, `criteria[]` with `category`, `given`, `when`, `then`, `notes`).
Rules for criteria:

- Name real functions/modules from this repo in `given`/`when` — never
  "any public entry point".
- Every `then` needs concrete assertion language (raises SomeError, matches 0,
  contains no ...) — the linter enforces this.
- Every criterion gets `notes` telling the blind writer what to instantiate and
  when to triage `needs_human`.
- Start `version` at 1, or current+1 if replacing an installed pack.

Then loop until clean of errors AND genericity warnings:

```bash
python -m blind_tdd.pack_wizard --lint .themis/security_pack_draft.json
```

## Step 4 — approve and install

Show the user the full final pack and ask for approval (AskUserQuestion). Also ask
which tasks the pack should apply to: **recommend path globs over their sensitive
directories** (same trust reasoning as routing — tags/keywords are only as
trustworthy as whoever writes them), or no match predicates to apply it to every
gated task.

On approval, preview then install:

```bash
python -m blind_tdd.pack_wizard --install .themis/security_pack_draft.json \
  [--match-glob 'GLOB' ...] [--match-tag TAG ...] \
  [--match-keyword WORD ...] [--match-min-severity SEV] --print
# show the user the preview, then re-run without --print, with --yes
```

This writes `themis.security_pack.json` (project root) and enables
`gate.blind_tdd.security_ac_pack` in `themis.config.json`. If
`python -m blind_tdd.pack_wizard` is not importable, the engine isn't installed —
tell the user to `pip install -r requirements.txt` and stop.

After installing: remind the user to **commit the pack file**, delete the draft,
and — since criteria strength is what this was all for — suggest pointing the
advisory mutation pass (`blind_tdd.mutate`) at the pack-derived tests after the
first gated task goes green. Re-run this interview when the attack surface grows;
the gate will nudge (stderr + `.themis/alerts.log`) once the pack is 90+ days old.
