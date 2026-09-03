#!/usr/bin/env python3
"""Hostile-input check: the scripts must never crash, never write to stderr, and
always exit 0. A coach that breaks a turn is worse than no coach."""

import json
import os
import subprocess
import sys
import tempfile
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
    # Wrongly-typed TOP-LEVEL payload fields. Distinct from the nested cases below:
    # these never reach transcript parsing, they raise in the argument handling itself.
    # `Path()` raises TypeError on a non-string, and TypeError was caught nowhere.
    ("coach.py", '{"prompt":"fix it","transcript_path":123}', "transcript_path is int"),
    ("coach.py", '{"prompt":"fix it","transcript_path":["a"]}', "transcript_path list"),
    (
        "coach.py",
        '{"prompt":"fix it","transcript_path":{"a":1}}',
        "transcript_path dict",
    ),
    ("coach.py", '{"prompt":"fix it","transcript_path":true}', "transcript_path bool"),
    # `context_window` truthy but not a dict: `or {}` does not save a .get() here.
    ("coach.py", '{"prompt":"fix it","context_window":"nope"}', "context_window str"),
    ("coach.py", '{"prompt":"fix it","context_window":[1]}', "context_window list"),
    ("coach.py", '{"prompt":"fix it","context_window":7}', "context_window int"),
    # A non-finite or out-of-range gauge reading straight from the host.
    (
        "coach.py",
        '{"prompt":"fix it","context_window":{"used_percentage":NaN}}',
        "inline pct is NaN",
    ),
    (
        "coach.py",
        '{"prompt":"fix it","context_window":{"used_percentage":Infinity}}',
        "inline pct is Infinity",
    ),
    (
        "coach.py",
        '{"prompt":"fix it","context_window":{"used_percentage":9999}}',
        "inline pct out of range",
    ),
    # `model` cases need a transcript that actually yields a usage total, or the function
    # returns before window_for() is ever called and the case proves nothing. They are
    # built against a real temp transcript further down instead.
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
    ("lint.py", "[]", "json array on stdin"),
    ("lint.py", "42", "bare scalar on stdin"),
    ("lint.py", "not json", "non-json on stdin"),
]

# Malformed *nested* transcript records. An earlier version assumed `message`, `info`,
# and the tool-input fields were always dicts, and raised AttributeError on anything
# else, which killed the hook mid-turn. An empty file such as /dev/null never reaches
# that parsing at all, so these need real records with wrongly-typed nested fields.
NESTED_RECORD_CASES = [
    ({"message": "a string, not a dict"}, "message is a string"),
    ({"message": ["a", "list"]}, "message is a list"),
    ({"message": 42}, "message is a number"),
    ({"info": "not a dict"}, "info is a string"),
    ({"info": ["x"]}, "info is a list"),
    ({"usage": "not a dict"}, "usage is a string"),
    ({"message": {"usage": "not a dict"}}, "nested usage is a string"),
    ({"message": {"content": "not a list"}}, "content is a string"),
    ({"message": {"content": [None, 7, "x"]}}, "content holds non-dicts"),
    (
        {
            "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": "str"}]
            }
        },
        "tool_use input is a string",
    ),
    (
        {
            "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": ["a"]}]
            }
        },
        "tool_use input is a list",
    ),
    ({"tool_name": "Read", "tool_input": ["a"]}, "flat tool_input is a list"),
    ({"tool_name": "Read", "tool_input": "s", "args": 1}, "flat input and args wrong"),
    ({"toolUseResult": "not a dict"}, "toolUseResult is a string"),
    ({"compactMetadata": "not a dict"}, "compactMetadata is a string"),
]

fails = 0


def check(script: str, payload: str, label: str, env: dict | None = None) -> None:
    """Run one script against one payload and report any contract violation.

    The contract a hook must honour: exit 0, write nothing to stderr, and emit either
    valid JSON or nothing at all. Human-readable prose reaching a hook is a bug,
    because the host expects JSON.

    `env` overlays the parent environment, so a case can redirect TUTOR_STATE_DIR into a
    temp dir instead of reading and writing the user's real state.
    """
    global fails
    # Fixed interpreter, repo-controlled script path, shell=False: no injection surface.
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / script)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,  # asserting on returncode is the point
        env={**os.environ, **env} if env else None,
    )
    problems = []
    if proc.returncode != 0:
        problems.append(f"exit={proc.returncode}")
    if proc.stderr.strip():
        problems.append("stderr=" + proc.stderr.strip().splitlines()[-1][:60])
    if proc.stdout.strip():
        try:
            json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            problems.append("stdout not valid JSON")
        if proc.stdout.startswith("tutor:"):
            problems.append("leaked CLI prose into hook output")
    if problems:
        fails += 1
    print(
        f"  {script:18} {label:26} {'FAIL ' + '; '.join(problems) if problems else 'ok'}"
    )


for script, payload, label in CASES:
    check(script, payload, label)

# Write each malformed record to a real transcript so the nested-field parsing in
# transcript.py and context_usage.py actually runs against it.
with tempfile.TemporaryDirectory() as tmp:
    for i, (record, label) in enumerate(NESTED_RECORD_CASES):
        path = Path(tmp) / f"t{i}.jsonl"
        # Repeat each record several times, and pad with well-formed read calls, so the
        # deeper code paths actually execute. Some of them only run once a threshold is
        # crossed: the repeat-read counter, for instance, is built with a filter that
        # short-circuits, so a single malformed record slips past it untouched. A test
        # that does not cross the threshold proves nothing.
        padding = json.dumps(
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "/tmp/a.ts"},
                        }
                    ]
                }
            }
        )
        usage = json.dumps({"message": {"usage": {"input_tokens": 10}}})
        path.write_text(
            "\n".join(
                [json.dumps(record)] * 4
                + [padding] * 3
                + [usage]
                + [json.dumps(record)] * 2
            )
            + "\n"
        )
        check(
            "coach.py",
            json.dumps(
                {
                    "prompt": "fix the parser",
                    "session_id": f"nested{i}",
                    "transcript_path": str(path),
                }
            ),
            label,
        )

# Whole records that are not mappings at all. A JSONL line can legitimately be an
# array or a scalar, and the marker strings the parsers grep for can appear inside one,
# so `rec.get(...)` must never run before the record's own type is checked. These are
# raw lines rather than dicts, so they bypass json.dumps entirely.
RAW_LINE_CASES = [
    ('["compactMetadata"]', "array containing the marker"),
    ('{"a": ["compactMetadata"]}', "marker nested in an array"),
    ('["message", "usage"]', "array of field names"),
    ("[1, 2, 3]", "array of numbers"),
    ('{"compactMetadata": ["not", "a", "dict"]}', "compactMetadata is an array"),
    ('{"compactMetadata": {"preTokens": "lots"}}', "preTokens is a string"),
    ('{"message": {"usage": {"input_tokens": "many"}}}', "token count is a string"),
    # `json.loads` accepts these non-standard literals, and int() then raises ValueError
    # on NaN and OverflowError on the infinities. Nothing upstream caught either, so a
    # transcript carrying one killed the hook and broke the user's turn.
    ('{"message": {"usage": {"input_tokens": NaN}}}', "token count is NaN"),
    ('{"message": {"usage": {"input_tokens": Infinity}}}', "token count is Infinity"),
    ('{"message": {"usage": {"input_tokens": -Infinity}}}', "token count is -Infinity"),
    (
        '{"message": {"usage": {"cache_read_input_tokens": Infinity}}}',
        "cache token is Infinity",
    ),
    # OpenCode's nested shape reaches _int() by a different branch.
    ('{"message": {"usage": {"input": NaN, "cache": {"read": 1}}}}', "OpenCode NaN"),
    (
        '{"message": {"usage": {"input": 10, "cache": {"read": Infinity}}}}',
        "OpenCode cache Infinity",
    ),
    # A negative count would subtract from the total and hide real context pressure.
    ('{"message": {"usage": {"input_tokens": -500000}}}', "token count is negative"),
    ('{"compactMetadata": {"preTokens": Infinity}}', "preTokens is Infinity"),
]

with tempfile.TemporaryDirectory() as tmp:
    for i, (raw, label) in enumerate(RAW_LINE_CASES):
        path = Path(tmp) / f"r{i}.jsonl"
        # Repeat so any threshold-gated code path is reached, and include one sane
        # usage record so the walk does not stop before touching the bad lines.
        path.write_text(
            "\n".join(
                [raw] * 3 + ['{"message": {"usage": {"input_tokens": 10}}}'] + [raw] * 2
            )
            + "\n"
        )
        check(
            "coach.py",
            json.dumps(
                {
                    "prompt": "fix the parser",
                    "session_id": f"raw{i}",
                    "transcript_path": str(path),
                }
            ),
            label,
        )

# A wrongly-typed `model`. This is NOT merely hostile input: the Claude Code statusline
# payload carries `model` as a dict, and `.lower()` on one raised AttributeError. These
# need a transcript that yields a real usage total, because used_percentage() returns
# before window_for() is reached when there is nothing to measure.
MODEL_CASES = [
    ('{"id":"sonnet"}', "model is a dict"),
    ("123", "model is an int"),
    ('["a"]', "model is a list"),
    ("true", "model is a bool"),
]

with tempfile.TemporaryDirectory() as tmp:
    usable = Path(tmp) / "usage.jsonl"
    usable.write_text('{"message": {"usage": {"input_tokens": 180000}}}\n')
    for i, (model, label) in enumerate(MODEL_CASES):
        check(
            "coach.py",
            '{"prompt":"fix the parser","session_id":"model%d",'
            '"model":%s,"transcript_path":"%s"}' % (i, model, usable),
            label,
        )

# The `used-pct` gauge file written by the statusline. `float()` accepts "inf" and "nan"
# and `int()` then raises OverflowError, which the surrounding `except (OSError,
# ValueError)` does NOT catch. TUTOR_STATE_DIR is redirected so these never touch the
# user's real gauge file, and the transcript path is deliberately unusable so the
# fallback branch is the one that runs.
USED_PCT_CASES = [
    ("inf", "used-pct is inf"),
    ("-inf", "used-pct is -inf"),
    ("nan", "used-pct is nan"),
    ("9999", "used-pct out of range"),
    ("-40", "used-pct negative"),
    ("not a number", "used-pct is prose"),
    ("", "used-pct is empty"),
]

with tempfile.TemporaryDirectory() as tmp:
    for i, (content, label) in enumerate(USED_PCT_CASES):
        state = Path(tmp) / f"s{i}"
        state.mkdir()
        (state / "used-pct").write_text(content)
        check(
            "coach.py",
            json.dumps({"prompt": "fix the parser", "session_id": f"pct{i}"}),
            label,
            env={"TUTOR_STATE_DIR": str(state)},
        )

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
import coach  # noqa: E402  -- import must follow the sys.path.insert above

for content, label in STATE_CASES:
    session = "robustness_probe"
    state_file = coach.STATE_DIR / f"{session}.json"
    try:
        coach.STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_file.write_text(content)
    except OSError:
        print(f"  {'coach.py':18} {label:26} skipped (state dir unwritable)")
        continue
    check(
        "coach.py",
        json.dumps(
            {"prompt": "a clear prompt naming src/x.ts line 3", "session_id": session}
        ),
        label,
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
    f"  {'advise.py':18} {'nonexistent dir':26} "
    f"{'FAIL ' + proc.stderr.strip()[:60] if bad else 'ok'}"
)

print(f"\n{'PASS' if fails == 0 else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
