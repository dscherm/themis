---
description: Configure which tasks the blind-TDD gate engages on (writes themis.config.json routing).
argument-hint: "[optional: a one-line description of what kinds of tasks are high-stakes here]"
allowed-tools: Bash, Read
---

You are running the blind-TDD gate's setup interview. Your job is to decide, **with the
user**, which tasks the (expensive, multi-agent) blind gate should engage on, and then
persist that as a routing policy in `themis.config.json`.

Background you must convey briefly:
- The blind gate costs several agent passes where an ordinary in-loop verifier makes one.
  Most tasks don't need it. Reserve it for work where a false green is expensive.
- This choice is made **once, by the human, at setup** — on purpose. The trigger must be a
  rule the implementing agent does not control; if the agent could choose whether the gate
  fires, it would skip it exactly when the pressure to cheat is highest.

The gate selects tasks by any of four predicates (a task is gated if it matches ANY):
1. **path globs** — e.g. `billing/**`, `auth/**`, `*.sql`. **Most evasion-resistant**:
   matched against the task's declared `files` / `public_surface`, which the worker doesn't
   author. Prefer these.
2. **tags** — e.g. `security`, `high-stakes` (only as trustworthy as whoever writes the tags).
3. **min severity** — `low` < `medium` < `high` < `critical`.
4. **keywords** in the spec text — e.g. `payment`, `password`, `token` (loosest).

Steps:
1. If the user passed `$ARGUMENTS`, use it to seed your suggestions, but still confirm.
2. Ask whether they want to gate **all** tasks or **selectively** by type. If they're
   unsure, recommend selective with path globs over their sensitive directories.
3. For selective, gather any of the four predicates they want. Push them toward path globs;
   explain the weaker trust of tags/keywords. It's fine to combine several.
4. Ask whether to enable the gate now (`enabled: true`).
5. Show them the resulting policy and the exact command you'll run, then run it.

Persist by calling the engine's setup CLI with the chosen flags (this merges into any
existing `themis.config.json` without clobbering other keys):

```bash
python -m blind_tdd.init --yes \
  [--enable | --disable] \
  [--mode all|selective] \
  [--path-glob 'GLOB' ...] \
  [--tag TAG ...] \
  [--min-severity low|medium|high|critical] \
  [--keyword WORD ...]
```

Run with `--print` first to preview the merged config, show it to the user, then run without
`--print` (keep `--yes`) to write it. If `python -m blind_tdd.init` is not importable, the
engine isn't installed — tell the user to `pip install -r requirements.txt` (the plugin ships
the engine pin) and stop.

After writing, remind them of the next step: mark a current task (`THEMIS_TASK` env var or
`.themis/current_task.json`) and run the gate. Point to `docs/adoption-guide.md`.
