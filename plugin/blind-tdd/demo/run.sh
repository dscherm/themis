#!/usr/bin/env bash
# Deterministic, self-contained demo of the blind-tdd plugin.
#
# Drives the REAL shipped hook scripts (no model calls, no network) through the
# four beats of the gate, so it is reproducible and safe to record:
#
#   1. a blind writer's read of the implementation is BLOCKED by the path-guard
#   2. the same writer may still read its spec surface (public_api.md)
#   3. red -> green: a sealed failing test goes green once code is written
#   4. the seal + lock catch tampering: editing a hash-locked test is blocked,
#      and a changed test no longer matches its sealed SHA-256
#
# Usage:  bash plugin/blind-tdd/demo/run.sh
# Record: see plugin/blind-tdd/demo/README.md

set -u

# --- locate the plugin's real hooks (this script lives in plugin/blind-tdd/demo)
DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$DEMO_DIR/.." && pwd)"
HOOKS="$PLUGIN_ROOT/hooks"
PYTHON="${PYTHON:-python}"

# --- presentation helpers ----------------------------------------------------
if [ -t 1 ] || [ -n "${DEMO_COLOR:-}" ]; then B="\033[1m"; C="\033[36m"; G="\033[32m"; R="\033[31m"; D="\033[2m"; X="\033[0m"
else B=""; C=""; G=""; R=""; D=""; X=""; fi
step() { printf "\n${B}${C}== %s ==${X}\n" "$1"; }
say()  { printf "${D}# %s${X}\n" "$1"; }
run()  { printf "${B}\$ %s${X}\n" "$*"; eval "$@"; }

# --- scratch project ---------------------------------------------------------
PROJ="$(mktemp -d 2>/dev/null || echo "${TMPDIR:-/tmp}/blind-tdd-demo.$$")"
mkdir -p "$PROJ"
cleanup() { rm -rf "$PROJ"; }
trap cleanup EXIT
cd "$PROJ"
mkdir -p src tests .themis/blind_tdd/red_state

printf "${B}blind-tdd plugin — demo${X}  ${D}(scratch: %s)${X}\n" "$PROJ"

# The only thing the blind writer may read beyond the task spec:
cat > public_api.md <<'EOF'
# public_api.md
module: calculator
adds:
  - add(a: int, b: int) -> int   # returns the sum; add(2, 3) == 5
EOF

# Open a blind WRITER session: src is blocked, spec + tests are allowed.
cat > .themis/blind_tdd/active_session.json <<'EOF'
{
  "session_id": "demo-writer",
  "agent_role": "test_writer",
  "task_id": "calc-add",
  "allowed_paths": ["tests/**", "public_api.md", "plan.md"],
  "blocked_paths": ["src/**", "examples/**", ".git/**"]
}
EOF

# A pre-existing implementation the writer must NOT be able to peek at.
cat > src/calculator.py <<'EOF'
def add(a, b):
    return a + b
EOF

# ----------------------------------------------------------------------------
step "1. The blind writer tries to read the implementation"
say "Claude Code sends this PreToolUse event to the path-guard hook:"
echo '{"tool_name":"Read","tool_input":{"file_path":"src/calculator.py"}}' \
  | "$PYTHON" "$HOOKS/blind_tdd_path_guard.py"; rc=$?
printf -- "${R}${B}-> hook exit %s (2 = BLOCKED): the writer cannot see the code.${X}\n" "$rc"

step "2. The same writer reads its allowed spec surface"
echo '{"tool_name":"Read","tool_input":{"file_path":"public_api.md"}}' \
  | "$PYTHON" "$HOOKS/blind_tdd_path_guard.py"; rc=$?
printf -- "${G}${B}-> hook exit %s (0 = allowed): public_api.md is on the whitelist.${X}\n" "$rc"

# ----------------------------------------------------------------------------
step "3a. RED — writer authors a failing test from the spec alone"
cat > tests/test_calculator.py <<'EOF'
from calculator import add

def test_add_two_and_three():   # Covers: AC-1
    assert add(2, 3) == 5
EOF
say "Run the test before any implementation is importable:"
rm -f src/calculator.py        # red phase: implementation does not exist yet
if PYTHONPATH=src "$PYTHON" -m pytest -q tests/ >/dev/null 2>&1; then
  printf "${R}unexpected: test passed with no implementation${X}\n"
else
  printf -- "${R}${B}-> tests FAIL (RED): no implementation yet, as required.${X}\n"
fi

step "3b. SEAL — fingerprint the test with SHA-256 before code exists"
SEAL=$("$PYTHON" - <<'PY'
import hashlib, json, pathlib
p = pathlib.Path("tests/test_calculator.py")
h = hashlib.sha256(p.read_bytes()).hexdigest()
state = {"task_id": "calc-add", "test_file_hashes": {"tests/test_calculator.py": h}}
pathlib.Path(".themis/blind_tdd/red_state/calc-add.json").write_text(json.dumps(state, indent=2))
print(h)
PY
)
printf "sealed tests/test_calculator.py -> ${D}sha256:%s${X}\n" "${SEAL:0:16}..."

step "3c. GREEN — implementer writes code; the sealed test goes green"
cat > src/calculator.py <<'EOF'
def add(a, b):
    return a + b
EOF
if PYTHONPATH=src "$PYTHON" -m pytest -q tests/ >/dev/null 2>&1; then
  printf -- "${G}${B}-> tests PASS (GREEN): the sealed AC-1 test is satisfied.${X}\n"
else
  printf "${R}unexpected: green phase failed${X}\n"
fi

# ----------------------------------------------------------------------------
step "4a. The implementer tries to edit the hash-locked test — BLOCKED"
say "A runner/implementer role editing a locked test hits the path-guard:"
cat > .themis/blind_tdd/active_session.json <<'EOF'
{
  "session_id": "demo-runner",
  "agent_role": "test_runner",
  "task_id": "calc-add",
  "allowed_paths": ["tests/**", "src/**"],
  "blocked_paths": []
}
EOF
echo '{"tool_name":"Edit","tool_input":{"file_path":"tests/test_calculator.py"}}' \
  | "$PYTHON" "$HOOKS/blind_tdd_path_guard.py"; rc=$?
printf -- "${R}${B}-> hook exit %s (2 = BLOCKED): a sealed test cannot be edited in-band.${X}\n" "$rc"

step "4b. Even a tamper through the shell breaks the seal"
say "Suppose a test is changed anyway (e.g. weakened to assert nothing):"
cat > tests/test_calculator.py <<'EOF'
from calculator import add

def test_add_two_and_three():   # Covers: AC-1
    assert True   # tampered: no longer checks the result
EOF
NOW=$("$PYTHON" - <<'PY'
import hashlib, pathlib
print(hashlib.sha256(pathlib.Path("tests/test_calculator.py").read_bytes()).hexdigest())
PY
)
if [ "$NOW" = "$SEAL" ]; then
  printf "${R}unexpected: hash unchanged${X}\n"
else
  printf "sealed:  ${D}%s${X}\n" "${SEAL:0:16}..."
  printf "current: ${D}%s${X}\n" "${NOW:0:16}..."
  printf -- "${R}${B}-> SHA-256 mismatch: the runner fails the gate regardless of the result.${X}\n"
fi

printf "\n${B}${G}Demo complete.${X} ${D}Blindness, the seal, and the lock are enforced by the shipped hooks.${X}\n"
