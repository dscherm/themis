#!/usr/bin/env python3
"""Generate demo.cast (asciinema v2) from a real run of run.sh.

asciinema's own `rec` needs a Unix pty (no termios on Windows), so instead of
hand-recording we run the demo for real and synthesize a cast with deterministic
timing. The *content* is the actual output of the shipped hooks; only the
inter-line delays are synthetic. Re-run any time to refresh the artifact:

    python plugin/blind-tdd/demo/make_cast.py

Play it with:  asciinema play plugin/blind-tdd/demo/demo.cast
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_RUN_SH = _HERE / "run.sh"
_OUT = _HERE / "demo.cast"

WIDTH, HEIGHT = 100, 32
# Synthetic pacing: a beat between lines, a longer pause on the headline verdicts.
LINE_DELAY = 0.35
PAUSE_AFTER = ("BLOCKED", "RED", "GREEN", "mismatch", "allowed")


def main() -> int:
    # Two input routes:
    #   (a) piped transcript on stdin (Windows-friendly — git-bash spawned from
    #       Python gets a stripped env and can't find `python`, so run the demo
    #       from a real shell and pipe it in):
    #         DEMO_COLOR=1 bash run.sh 2>&1 | python make_cast.py
    #   (b) no pipe -> run run.sh directly (works on Linux/macOS).
    if not sys.stdin.isatty():
        captured = sys.stdin.read()
    else:
        import os
        proc = subprocess.run(
            ["bash", _RUN_SH.name],      # relative name + cwd avoids Windows backslash-path issues
            cwd=str(_HERE),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,    # interleave hook BLOCKED messages in real order
            text=True,
            env={**os.environ, "DEMO_COLOR": "1"},
        )
        captured = proc.stdout
    lines = captured.splitlines()
    if not lines:
        print("run.sh produced no output", file=sys.stderr)
        return 1

    header = {
        "version": 2,
        "width": WIDTH,
        "height": HEIGHT,
        "timestamp": 0,
        "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"},
        "title": "blind-tdd plugin demo",
    }

    events = []
    t = 0.0
    for line in lines:
        t += LINE_DELAY
        events.append([round(t, 3), "o", line + "\r\n"])
        if any(k in line for k in PAUSE_AFTER):
            t += 0.9  # let the verdict land

    with _OUT.open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(header) + "\n")
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    print(f"wrote {_OUT} ({len(events)} frames, {round(t, 1)}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
