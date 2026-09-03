#!/usr/bin/env python3
"""Estimate context-window usage from a session transcript.

WHY THIS EXISTS
---------------
Hook payloads do not carry context-window usage on any of the three tools. On Claude
Code `context_window` is a statusline-only field; Codex and OpenCode expose no
equivalent to hooks at all. But every hook on all three tools *does* receive
`transcript_path`, and transcripts record per-response token usage.

So instead of a per-tool bridge, read the transcript. One mechanism, three tools.

The number is an estimate of the same quantity the Claude Code statusline reports as
`used_percentage`: total input tokens on the most recent response, which is
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`. Output tokens
are deliberately excluded, matching the statusline's definition.
"""

import json
from pathlib import Path

# Model context windows.
#
# IMPORTANT: 1M context is opt-in, not implied by a model name. An earlier version
# mapped bare `sonnet-5` and `opus-5` to 1M, which made a session at the real brink of
# compaction report as 17% full instead of 84%. Ground truth from 31 real compaction
# boundaries in this project: median preTokens 167,630, i.e. these sessions compact at
# the 200K boundary. Only the explicit extended-context marker earns the larger figure.
DEFAULT_WINDOW = 200_000
LARGE_WINDOW = 1_000_000
LARGE_WINDOW_HINTS = ("[1m]", "-1m", "fable")


def window_for(model: str | None) -> int:
    if not model:
        return DEFAULT_WINDOW
    slug = model.lower()
    if any(h in slug for h in LARGE_WINDOW_HINTS):
        return LARGE_WINDOW
    return DEFAULT_WINDOW


def observed_window(records: list[str]) -> int | None:
    """Infer the real window from the session's own compaction history.

    Far more reliable than guessing from a model slug: if this session has compacted,
    the token count at that moment IS the practical ceiling. Returns None when the
    session has never compacted.
    """
    import json as _json

    best = None
    for line in records:
        if "compactMetadata" not in line:
            continue
        try:
            rec = _json.loads(line)
        except (ValueError, TypeError):
            continue
        pre = (rec.get("compactMetadata") or {}).get("preTokens")
        if isinstance(pre, int) and pre > 0:
            best = max(best or 0, pre)
    if not best:
        return None
    # Compaction triggers at or just below the limit, so round up to the nearest
    # plausible window rather than treating the observed figure as the ceiling itself.
    return LARGE_WINDOW if best > 400_000 else DEFAULT_WINDOW


def _tail_lines(path: Path, limit: int = 400) -> list[str]:
    """Read the last `limit` lines without loading a large transcript into memory."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            block = 64 * 1024
            data = b""
            while size > 0 and data.count(b"\n") <= limit:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
        return data.decode("utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []


def _usage_from_record(rec: dict) -> dict | None:
    """Find a usage object, tolerating the different shapes across tools."""
    # `message` and `info` are untrusted: a string or list here must not raise, or the
    # hook dies and the user's turn breaks. Only dicts can carry a nested usage object.
    msg = rec.get("message")
    info = rec.get("info")
    for candidate in (
        msg.get("usage") if isinstance(msg, dict) else None,
        rec.get("usage"),
        info.get("tokens") if isinstance(info, dict) else None,  # OpenCode-style
    ):
        if isinstance(candidate, dict) and candidate:
            return candidate
    return None


def _total_input(usage: dict) -> int | None:
    """Sum the input-side token counts. Mirrors the statusline's definition."""
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    if any(k in usage for k in keys):
        return sum(int(usage.get(k) or 0) for k in keys)
    # OpenCode nests differently: {input, output, cache: {read, write}}
    if "input" in usage:
        cache = usage.get("cache") or {}
        return (
            int(usage.get("input") or 0)
            + int(cache.get("read") or 0)
            + int(cache.get("write") or 0)
        )
    return None


def used_percentage(
    transcript_path: str | None, model: str | None = None
) -> int | None:
    """Return 0-100, or None when it cannot be determined.

    None is a first-class answer: better a quiet coach than one warning on invented
    numbers.
    """
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return None

    lines = _tail_lines(path)
    total = None
    seen_model = model
    # Walk backwards: the most recent usage record is the current context size.
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not seen_model:
            msg = rec.get("message")
            if isinstance(msg, dict):
                seen_model = msg.get("model")
        usage = _usage_from_record(rec)
        if usage:
            total = _total_input(usage)
            if total:
                break

    if not total:
        return None
    window = observed_window(lines) or window_for(seen_model)
    pct = round(total / window * 100)
    return max(0, min(100, pct))


if __name__ == "__main__":  # manual check: python3 context_usage.py <transcript>
    import sys

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    print(used_percentage(arg))
