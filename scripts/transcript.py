#!/usr/bin/env python3
"""Read behavioural signals out of a session transcript.

WHY
---
The prompt text alone supports only a handful of checks. The transcript records what
actually happened: which tools ran, which files were read, how many times. That makes
behavioural anti-patterns measurable rather than guessed at.

Every signal here is a COUNT of something that happened, never an interpretation. A
nudge that fires on a judgement call will sometimes be wrong, and a coach that cries
wolf gets muted.

Transcript shapes differ per tool. Anything unparsable degrades to an empty signal
set, so callers see "nothing to report" rather than a wrong number.
"""

import json
from collections import Counter
from pathlib import Path

# How much history a signal considers. Recent behaviour is what is actionable.
WINDOW_RECORDS = 600


def _records(path: Path, limit: int = WINDOW_RECORDS) -> list[dict]:
    """Parse the last `limit` JSONL records, newest last."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            block = 128 * 1024
            data = b""
            while size > 0 and data.count(b"\n") <= limit:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
    except OSError:
        return []

    out = []
    for line in data.decode("utf-8", errors="replace").splitlines()[-limit:]:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        # Filter here, once, rather than guarding every rec.get() downstream. A JSONL
        # line can legitimately be an array or a scalar, and every consumer below
        # assumes a mapping. Guarding at the boundary is the only version of this that
        # stays correct as new consumers are added.
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _as_dict(value: object) -> dict:
    """Coerce a tool input to a dict.

    Transcript fields are untrusted: a malformed or unexpected shape must not crash a
    hook, because that breaks the user's turn. Callers only ever read keys, so an empty
    dict is a safe stand-in for anything that is not one.
    """
    return value if isinstance(value, dict) else {}


def _tool_calls(records: list[dict]) -> list[tuple[str, dict]]:
    """Extract (tool_name, input) pairs in order. Tolerates several transcript shapes."""
    calls: list[tuple[str, dict]] = []
    for rec in records:
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls.append(
                        (block.get("name") or "", _as_dict(block.get("input")))
                    )
        # Codex and OpenCode may record a flatter shape.
        name = rec.get("tool_name") or rec.get("tool")
        if isinstance(name, str) and name:
            raw = rec.get("tool_input")
            if not isinstance(raw, dict):
                raw = rec.get("args")
            calls.append((name, _as_dict(raw)))
    return calls


def _result_verdict(rec: dict) -> bool | None:
    """True if this record is a failed tool result, False if successful, None if neither.

    Claude Code puts the flag on `tool_result` blocks within `message.content`. Codex
    and OpenCode may use a flatter shape, so both are checked.
    """
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                # A tool_use_error tag is how some failures surface in the body text.
                body = block.get("content")
                tagged = isinstance(body, str) and "tool_use_error" in body
                return bool(block.get("is_error")) or tagged
    # Flatter shapes used by other hosts, and the legacy sibling object.
    for key in ("toolUseResult", "tool_result", "result"):
        value = rec.get(key)
        if isinstance(value, dict):
            return bool(value.get("is_error") or value.get("error"))
    return None


def _path_of(tool_input: object) -> str | None:
    """Pull a file path out of a tool input, whatever shape it arrives in.

    Guards the type here rather than trusting callers. `_tool_calls` normalises its
    output, but this is also reachable from anywhere a future caller passes a raw
    transcript value, and a crash in a hook breaks the user's turn.
    """
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "filePath", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def signals(transcript_path: object) -> dict:
    """Return counted facts about recent session behaviour.

    Keys are always present so callers need no defensive lookups:
      reads_since_edit   consecutive read-only tool calls with no edit
      repeat_reads       (path, count) for the most re-read file, count >= 2
      tool_calls         total tool calls in the window
      edits              edit/write calls in the window
      bash_ratio         share of tool calls that were shell commands, 0.0-1.0
      error_streak       trailing run of failed tool results
      no_test_after_edit True if files changed but nothing test-shaped ran since
      compactions       count of automatic compaction boundaries in the window

    `transcript_path` arrives from an untrusted hook payload and is not annotated as a
    string, because it is not guaranteed to be one. `Path()` raises TypeError on a
    non-string, and a crash here breaks the user's turn.
    """
    empty = {
        "reads_since_edit": 0,
        "repeat_reads": None,
        "tool_calls": 0,
        "edits": 0,
        "bash_ratio": 0.0,
        "error_streak": 0,
        "no_test_after_edit": False,
        "compactions": 0,
    }
    if not isinstance(transcript_path, str) or not transcript_path:
        return empty
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return empty

    records = _records(path)
    if not records:
        return empty

    # Note: do NOT return early when there are no tool calls. A transcript can contain
    # failed results with no parseable tool_use blocks, and the error streak still
    # matters in that case.
    calls = _tool_calls(records)

    edit_tools = {"Edit", "Write", "NotebookEdit", "MultiEdit", "apply_patch", "edit"}
    read_tools = {"Read", "Grep", "Glob", "read", "grep", "glob"}
    shell_tools = {"Bash", "PowerShell", "shell", "bash"}

    reads_since_edit = 0
    for name, _ in reversed(calls):
        if name in edit_tools:
            break
        if name in read_tools:
            reads_since_edit += 1

    read_paths = Counter(
        p for name, inp in calls if name in read_tools and (p := _path_of(inp))
    )
    repeat = None
    if read_paths:
        top_path, count = read_paths.most_common(1)[0]
        if count >= 2:
            repeat = (top_path, count)

    edits = sum(1 for name, _ in calls if name in edit_tools)
    shells = sum(1 for name, _ in calls if name in shell_tools)

    # Trailing run of errors: a repeated failure usually means the approach is wrong,
    # not that one more attempt will land.
    #
    # The error flag lives on tool_result blocks inside message.content, NOT on the
    # sibling `toolUseResult` object. An earlier version of this function looked in the
    # latter and therefore never found a single error: measured across 30 real
    # transcripts, 0 hits there against 132 at the correct location.
    error_streak = 0
    for rec in reversed(records):
        verdict = _result_verdict(rec)
        if verdict is None:
            continue  # not a tool result; keep looking back
        if verdict is False:
            break  # a success ends the streak
        error_streak += 1

    # Did anything test-shaped run after the most recent edit?
    test_words = ("test", "pytest", "jest", "vitest", "lint", "build", "tsc", "cargo")
    seen_edit = False
    ran_check = False
    for name, inp in reversed(calls):
        if name in edit_tools:
            seen_edit = True
            break
        if name in shell_tools:
            cmd = str(inp.get("command") or inp.get("cmd") or "").lower()
            if any(w in cmd for w in test_words):
                ran_check = True

    # Repeated automatic compaction is the guide's clearest scoping signal: the task is
    # too big for the window, and each event costs the user real wall-clock time.
    # `or {}` is not enough here: a non-dict value is truthy and then raises on .get().
    compactions = 0
    for rec in records:
        meta = rec.get("compactMetadata")
        if isinstance(meta, dict) and meta.get("trigger") == "auto":
            compactions += 1

    return {
        "reads_since_edit": reads_since_edit,
        "repeat_reads": repeat,
        "tool_calls": len(calls),
        "edits": edits,
        "bash_ratio": round(shells / len(calls), 2) if calls else 0.0,
        "error_streak": error_streak,
        "no_test_after_edit": seen_edit and not ran_check,
        "compactions": compactions,
    }


if __name__ == "__main__":  # manual check: python3 transcript.py <transcript>
    import sys

    print(json.dumps(signals(sys.argv[1] if len(sys.argv) > 1 else None), indent=2))
