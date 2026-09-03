#!/usr/bin/env python3
"""Hostile-input check: the scripts must never crash, never write to stderr, and
always exit 0. A coach that breaks a turn is worse than no coach."""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CASES = [
    ("coach.py", "", "empty stdin"),
    ("coach.py", "not json at all", "garbage input"),
    ("coach.py", '{"prompt":null}', "null prompt"),
    ("coach.py", '{"prompt":123}', "non-string prompt"),
    ("coach.py", '{"prompt":"hi","transcript_path":"/dev/null"}', "empty transcript"),
    ("coach.py", '{"prompt":"hi","transcript_path":"/etc/passwd"}', "non-JSONL file"),
    ("coach.py", '{"prompt":"x","transcript_path":"/root/nope"}', "unreadable path"),
    ("coach.py", '{"prompt":"' + "a " * 5000 + '"}', "huge prompt"),
    ("coach.py", '{"prompt":"h\\u00e9llo w\\u00f6rld \\ud83d\\udea8"}', "unicode"),
    ("coach.py", '{"prompt":"a\\u0000b control chars"}', "nul byte in prompt"),
    (
        "coach.py",
        '{"session_id":{"nested":"object"},"prompt":"test this"}',
        "odd session id",
    ),
    ("coach.py", "[]", "json array not object"),
    ("lint.py", '{"cwd":"/nonexistent"}', "bad cwd"),
    ("lint.py", "{}", "no cwd"),
    ("lint.py", '{"cwd":null}', "null cwd"),
    ("review_prompt.py", '{"prompt":"test a thing"}', "disabled by default"),
    # Regression cases: nested transcript fields are untrusted, and a wrong type here
    # used to raise AttributeError and break the turn.
    ("coach.py", '{"prompt":"x","transcript_path":"/dev/null"}', "empty transcript"),
    ("lint.py", "[]", "json array on stdin"),
    ("lint.py", "42", "bare scalar on stdin"),
    ("lint.py", "not json", "non-json on stdin"),
]

fails = 0
for script, payload, label in CASES:
    # Fixed interpreter, repo-controlled script path, shell=False: no injection surface.
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / script)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,  # asserting on returncode is the point
    )
    problems = []
    if proc.returncode != 0:
        problems.append(f"exit={proc.returncode}")
    if proc.stderr.strip():
        problems.append("stderr=" + proc.stderr.strip().splitlines()[-1][:60])
    # Any stdout must be valid JSON, or the host cannot parse it. Human-readable
    # prose reaching a hook is a bug: the tool expects JSON or nothing.
    if proc.stdout.strip():
        try:
            json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            problems.append("stdout not valid JSON")
        if proc.stdout.startswith("tutor:"):
            problems.append("leaked CLI prose into hook output")
    status = "FAIL " + "; ".join(problems) if problems else "ok"
    if problems:
        fails += 1
    print(f"  {script:18} {label:24} {status}")

# Corrupt state files: valid JSON of the wrong shape parses fine and then raises on
# first use. A partial write during pruning, or two sessions writing at once, produces
# exactly this, so each field must be validated rather than trusted.
STATE_CASES = [
    ("[]", "state is an array"),
    ("42", "state is a scalar"),
    ('"hi"', "state is a string"),
    ("{}", "state is an empty dict"),
    ('{"foo": 1}', "state missing every key"),
    ('{"turns": "x", "streak": 0, "shown": []}', "turns is a string"),
    ('{"turns": 0, "streak": null, "shown": []}', "streak is null"),
    ('{"turns": 0, "streak": 0, "shown": "abc"}', "shown is a string"),
    ('{"turns": 0, "streak": 0, "shown": [1, 2]}', "shown holds non-strings"),
]

sys.path.insert(0, str(REPO / "scripts"))
import coach  # imported after sys.path is extended above

for content, label in STATE_CASES:
    session = "robustness_probe"
    state_file = coach.STATE_DIR / f"{session}.json"
    try:
        coach.STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_file.write_text(content)
    except OSError:
        print(f"  {'coach.py':18} {label:24} skipped (state dir unwritable)")
        continue
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "coach.py")],
        input=json.dumps(
            {"prompt": "a clear prompt naming src/x.ts line 3", "session_id": session}
        ),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    probs = []
    if proc.returncode != 0:
        probs.append(f"exit={proc.returncode}")
    if proc.stderr.strip():
        probs.append(proc.stderr.strip().splitlines()[-1][:50])
    if probs:
        fails += 1
    print(
        f"  {'coach.py':18} {label:24} {'FAIL ' + '; '.join(probs) if probs else 'ok'}"
    )
    state_file.unlink(missing_ok=True)

# advise.py is a CLI, so it may print prose; it must still not crash. Checked once,
# outside the loop above.
# Fixed interpreter, repo-controlled script path, shell=False: no injection surface.
proc = subprocess.run(
    [sys.executable, str(REPO / "scripts" / "advise.py"), "/nonexistent"],
    capture_output=True,
    text=True,
    timeout=30,
    check=False,
)
bad = proc.returncode != 0 or proc.stderr.strip()
fails += bool(bad)
print(
    f"  {'advise.py':18} {'nonexistent dir':24} "
    f"{'FAIL ' + proc.stderr.strip()[:60] if bad else 'ok'}"
)

print(f"\n{'PASS' if fails == 0 else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
