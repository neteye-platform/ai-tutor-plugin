#!/usr/bin/env python3
"""Live prompting coach.

Runs as a `UserPromptSubmit` hook on Claude Code and Codex, which share the event
name and the JSON contract. The OpenCode plugin shells out to it from the
`chat.message` hook, passing the same shape.

Design rules, in priority order:

1. Speak via `systemMessage`, never `additionalContext`. The message reaches the
   user and NOT the model's context, so coaching about context hygiene does not
   itself consume context.
2. Stay silent unless a measurable threshold trips. A coach that comments every
   turn gets muted; one that speaks twice a session gets read.
3. At most one nudge per turn, and never the same nudge twice in a session.
   Repetition is what turns advice into noise.
4. Never block. Exit 0 always. This is teaching, not policy.
5. Never name a command the user's tool does not have. `/clear` exists on all
   three; `/context` and `/doctor` are Claude Code only.
"""

import json
import math
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from context_usage import used_percentage
from transcript import signals


def _state_dir() -> Path:
    """Where to keep per-session bookkeeping.

    Must not default to ~/.claude: a Codex-only or OpenCode-only user should not get a
    directory created for a tool they do not have installed. Prefer an explicit
    override, then the host tool's own config dir, and only then Claude Code's.
    """
    for var in ("CLAUDE_PLUGIN_DATA", "TUTOR_STATE_DIR"):
        value = os.environ.get(var)
        if value:
            return Path(value)
    home = Path.home()
    for candidate in (
        home / ".claude",
        home / ".codex",
        home / ".config" / "opencode",
    ):
        if candidate.is_dir():
            return candidate / "tutor-state"
    # Nothing detected: keep state out of the way rather than inventing a config dir.
    return home / ".cache" / "ai-tutor"


STATE_DIR = _state_dir()

# --- thresholds -------------------------------------------------------------
# Deliberately conservative. A nudge that fires on reasonable behaviour trains you to
# ignore all of them, so every number here errs toward silence.
VAGUE_MAX_WORDS = 6  # short AND no concrete referent
CORRECTION_STREAK = 3  # docs: after two failed corrections, /clear and re-prompt
CONTEXT_WARN_PCT = 75  # leave room to act before compaction
SESSION_TURN_HINT = 40  # long session; worth asking if the task changed
USED_PCT_MAX_AGE_S = 120  # ignore a stale statusline reading from a previous session
STATE_TTL_S = 7 * 24 * 3600  # delete per-session state after a week

# Prompt-shape thresholds
MULTI_TASK_HITS = 2  # two or more joiners before calling it bundled
NO_DETAIL_MAX_WORDS = 15  # a long bug report probably does contain detail

# Transcript-derived thresholds
ERROR_STREAK = 3  # trailing failed tool calls
EXPLORING_CALLS = 25  # total calls in the window
EXPLORING_READS = 12  # consecutive reads with no edit
REREAD_COUNT = 3  # same file read this many times
BASH_HEAVY_CALLS = 30  # enough calls for the ratio to mean something
BASH_HEAVY_RATIO = 0.8  # share that were shell commands
COMPACTION_HINT = 3  # automatic compactions before it is a scoping problem

CORRECTION_MARKERS = re.compile(
    r"\b(no,|not (like )?that|nope|wrong|i said|again|still (broken|failing|wrong)|"
    r"that('s| is) not|undo|revert)\b",
    re.IGNORECASE,
)

VERIFY_MARKERS = re.compile(
    r"\b(test|tests|verify|check|lint|build|run|typecheck|assert|screenshot|"
    r"exit code|output|prove)\b",
    re.IGNORECASE,
)

BUILD_MARKERS = re.compile(
    r"\b(implement|add|build|create|write|fix|refactor|migrate|change|update)\b",
    re.IGNORECASE,
)

CONCRETE = re.compile(r"[/\\.]|@|:\d|\b[a-z_]+\(\)|\.[a-z]{2,4}\b", re.IGNORECASE)

# Several unrelated asks in one message. The model handles the first well and the rest
# progressively worse, and you cannot tell which part failed.
MULTI_TASK = re.compile(
    r"\b(also|and then|after that|additionally|plus|as well as|then do|"
    r"once (that|you)|finally)\b",
    re.IGNORECASE,
)

# Politeness costs nothing, but hedging costs precision: "maybe try sort of fixing it"
# gives the model no target to hit.
HEDGES = re.compile(
    r"\b(maybe|perhaps|possibly|might|could you (maybe|possibly)|sort of|kind of|"
    r"i think maybe|if you want|whatever you think|somehow)\b",
    re.IGNORECASE,
)

# Asking for a rewrite of something large, with no constraint on scope.
BIG_REWRITE = re.compile(
    r"\b(rewrite|refactor|redo|rearchitect|restructure|overhaul|clean up|moderni[sz]e)\b",
    re.IGNORECASE,
)
SCOPE_LIMIT = re.compile(
    r"\b(only|just|single|one (file|function|module)|in \S+\.\w+|keep|preserve|"
    r"without changing|leave)\b",
    re.IGNORECASE,
)

# "It doesn't work" with no error text. The model then has to guess the symptom.
BROKEN_NO_DETAIL = re.compile(
    r"\b(does ?n[o']?t work|not working|broken|fails?|failing|error|crash(es|ed)?|bug)\b",
    re.IGNORECASE,
)
HAS_EVIDENCE = re.compile(
    r"(```|traceback|exception|stderr|stdout|line \d+|:\d+|error:|warn|\$ )",
    re.IGNORECASE,
)

# Emoji are plain UTF-8, so they survive in systemMessage where ANSI colour cannot.
# One symbol repeated 1-3 times encodes severity: no vocabulary to learn, and the
# urgency is legible at a glance.
SIREN = "🚨"

# 3 = acting on this now saves you real time or money
# 2 = worth fixing before you continue
# 1 = a nudge, ignore it freely
SEVERITY = {
    # 3 = acting now saves real time or money
    "context": 3,  # everything degrades from here
    "streak": 3,  # you are burning turns going in circles
    "errors": 3,  # repeated failures mean the approach is wrong
    "thrashing": 3,  # the task does not fit the window
    # 2 = worth fixing before you continue
    "verify": 2,  # shipping unverified work
    "unverified_edit": 2,  # files changed, nothing checked them
    "exploring": 2,  # many calls, no progress
    "no_detail": 2,  # a bug report with no evidence in it
    "big_rewrite": 2,  # unbounded change, unreviewable diff
    # 1 = a nudge, ignore it freely
    "vague": 1,
    "length": 1,
    "multi_task": 1,
    "hedging": 1,
    "reread": 1,
    "bash_heavy": 1,
}

# Terse versions for the statusline's second row, which has limited width.
SHORT = {
    "context": "context filling up, consider /clear",
    "streak": "repeated corrections, try /clear + one better prompt",
    "errors": "repeated tool failures, rethink the approach",
    "thrashing": "compacting repeatedly; the task is too big for one session",
    "verify": "no verification named",
    "unverified_edit": "files changed but nothing ran to check them",
    "exploring": "lots of calls, little progress; consider a subagent",
    "no_detail": "paste the actual error text",
    "big_rewrite": "unbounded rewrite; scope it to one file or function",
    "vague": "name the file, symbol, or error",
    "length": "long session, /clear if the task changed",
    "multi_task": "several asks in one prompt; split them",
    "hedging": "hedged wording gives no target",
    "reread": "same file read repeatedly",
    "bash_heavy": "mostly shell calls; a code-intelligence plugin may help",
}


def sirens(key):
    return SIREN * SEVERITY.get(key, 1)


def detect_tool(data):
    """Which agent are we running under? Advice must not name absent commands.

    Claude Code sends `permission_mode` plus a `context_window` object; Codex sends
    `permission_mode` and `model` but no `context_window`. The OpenCode shim sets
    `tutor_host` explicitly, because its payload is synthesised by the plugin.
    """
    host = data.get("tutor_host")
    if host in ("claude", "codex", "opencode"):
        return host
    if "context_window" in data:
        return "claude"
    if "turn_id" in data or "model" in data:
        return "codex"
    return "claude"


# `/clear` is verified present on Claude Code and OpenCode, and listed in the Codex
# built-in command set. `/context` and `/doctor` are Claude Code only, so nudges must
# not name them elsewhere: advice that cites a missing command teaches nothing and
# costs trust.
CLEAR_CMD = {"claude": "/clear", "codex": "/clear", "opencode": "/clear"}
HAS_CONTEXT_CMD = {"claude": True, "codex": False, "opencode": False}


def read_used_pct(data):
    """How full is the context window? Returns 0-100, or None if unknowable.

    Three sources, in order of preference. None is a first-class answer: a quiet coach
    beats one warning on invented numbers.

    1. The payload, if a tool ever includes it. Today none do.
    2. The transcript at `transcript_path`, which every hook on all three tools
       receives, and which records per-response token usage. This is the portable
       path and the reason the context nudge works beyond Claude Code.
    3. A file published by the Claude Code statusline. Only a fallback now, for the
       case where a transcript is unreadable but a statusline is running.
    """
    # `or {}` is not enough: a string or list here is truthy and then raises on .get().
    window = data.get("context_window")
    inline = window.get("used_percentage") if isinstance(window, dict) else None
    # bool is an int subclass, and NaN/Infinity survive json.loads and then raise in
    # int(), so both are screened before the conversion rather than after.
    if (
        isinstance(inline, (int, float))
        and not isinstance(inline, bool)
        and math.isfinite(inline)
    ):
        # The host reports this; clamp rather than trust, so a bad reading cannot
        # produce a "context is 9999% full" nudge.
        return max(0, min(100, int(inline)))

    pct = used_percentage(data.get("transcript_path"), data.get("model"))
    if pct is not None:
        return pct

    try:
        note = STATE_DIR / "used-pct"
        # Stale readings are worse than none: a value from a previous session would
        # fire a spurious nudge on turn one.
        if time.time() - note.stat().st_mtime > USED_PCT_MAX_AGE_S:
            return None
        raw = float(note.read_text().strip())
    except (OSError, ValueError):
        return None
    # float() accepts "inf" and "nan", and int() then raises OverflowError, which the
    # clause above does not catch. Screen instead of widening it: a non-finite gauge
    # reading is meaningless anyway.
    if not math.isfinite(raw):
        return None
    return max(0, min(100, int(raw)))


DEFAULT_STATE = {"turns": 0, "streak": 0, "shown": []}


def _int_or_zero(value) -> int:
    """bool is an int subclass, so exclude it explicitly."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def load_state(session):
    """Read per-session bookkeeping, tolerating a corrupt or stale file.

    Valid JSON of the wrong shape is the dangerous case: an array, a scalar, or a dict
    with a missing or wrongly-typed key all parse cleanly and then raise on first use,
    which kills the hook and breaks the user's turn. A partial write during pruning, or
    two sessions writing at once, produces exactly that, so every field is validated
    rather than trusted.
    """
    path = STATE_DIR / f"{session}.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):  # ValueError covers JSONDecodeError
        return dict(DEFAULT_STATE)
    if not isinstance(raw, dict):
        return dict(DEFAULT_STATE)
    shown = raw.get("shown")
    return {
        "turns": _int_or_zero(raw.get("turns")),
        "streak": _int_or_zero(raw.get("streak")),
        "shown": [k for k in shown if isinstance(k, str)]
        if isinstance(shown, list)
        else [],
    }


def save_state(session, state):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / f"{session}.json").write_text(json.dumps(state))
    except OSError:
        pass  # never let bookkeeping break the turn
    _prune_state()


def _prune_state():
    """Delete state from sessions that ended long ago.

    Without this, one small file per session accumulates forever. Nothing here is worth
    keeping once a session is over. Runs on every save and stays silent on any error,
    because bookkeeping must never break a turn.
    """
    cutoff = time.time() - STATE_TTL_S
    try:
        entries = list(STATE_DIR.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name == "used-pct":  # written by the statusline, always current
            continue
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def pick_nudge(prompt, state, used_pct, tool="claude", sig=None):
    """Return (key, message) for the single most useful nudge, or None.

    Ordered by urgency, not by category: the first match wins, so at most one nudge
    fires per turn no matter how many conditions are true.
    """
    words = prompt.split()
    shown = set(state["shown"])
    clear = CLEAR_CMD.get(tool, "/clear")
    sig = sig or {}
    sig = {
        "error_streak": sig.get("error_streak", 0),
        "no_test_after_edit": sig.get("no_test_after_edit", False),
        "compactions": sig.get("compactions", 0),
        "edits": sig.get("edits", 0),
        "tool_calls": sig.get("tool_calls", 0),
        "reads_since_edit": sig.get("reads_since_edit", 0),
        "repeat_reads": sig.get("repeat_reads"),
        "bash_ratio": sig.get("bash_ratio", 0.0),
    }

    def fresh(key):
        return key not in shown

    # 1. Context pressure. Most urgent, because it degrades everything else.
    if used_pct and used_pct >= CONTEXT_WARN_PCT and fresh("context"):
        return "context", (
            f"context is {used_pct}% full. Performance degrades as it fills, and your "
            f"earlier instructions have drifted toward the weak middle of the window. "
            f"If this is a new task, {clear} is free. If not, restate the key constraints "
            f"so they land back at the end where attention is strongest."
        )

    # 2. Repeated automatic compaction. Each one costs wall-clock time and loses
    #    detail, so hitting it repeatedly means the task does not fit the window.
    if sig["compactions"] >= COMPACTION_HINT and fresh("thrashing"):
        return "thrashing", (
            f"this session has auto-compacted {sig['compactions']} times. Each one takes "
            f"real time and silently drops detail from early in the conversation. That is "
            f"a scoping signal rather than a window problem: split the work, and write "
            f"anything that must survive to a file, since files are re-injected after "
            f"compaction while conversation is summarised away."
        )

    # 3. Correction spiral. The docs name this one explicitly.
    if state["streak"] >= CORRECTION_STREAK and fresh("streak"):
        return "streak", (
            f"that's {state['streak']} corrections in a row. Each failed attempt stays in "
            f"context and keeps pulling attention toward the wrong approach. The documented "
            f"fix is to {clear} and write one better prompt rather than correcting a "
            f"fourth time."
        )

    # 4. Repeated tool failures. Another attempt rarely helps; the premise is wrong.
    if sig["error_streak"] >= ERROR_STREAK and fresh("errors"):
        return "errors", (
            f"the last {sig['error_streak']} tool calls failed. A repeated failure usually "
            f"means the approach is wrong rather than that one more attempt will land, and "
            f"every failed attempt stays in context pulling attention toward the dead end. "
            f"Stop and say what you actually expected to happen."
        )

    # 5. Files changed and nothing checked them. The documented trust-then-verify gap.
    if sig["no_test_after_edit"] and sig["edits"] >= 1 and fresh("unverified_edit"):
        return "unverified_edit", (
            "files have been edited but nothing test-shaped has run since. Right now the "
            "only error detector in the loop is you, reading code that looks right. Ask for "
            "the test suite, a build, or a linter before you move on."
        )

    # 6. Exploration with no output. The 'infinite exploration' anti-pattern.
    if (
        sig["tool_calls"] >= EXPLORING_CALLS
        and sig["reads_since_edit"] >= EXPLORING_READS
        and fresh("exploring")
    ):
        return "exploring", (
            f"{sig['reads_since_edit']} reads in a row with no edit, across "
            f"{sig['tool_calls']} tool calls. Unmanaged exploration is the classic context "
            f"killer: every grepped line and dead hypothesis competes with the work still "
            f"to come. Delegate the search to a subagent and get back a conclusion, or "
            f"narrow it to one file."
        )

    # 7. A bug report with no evidence in it.
    if (
        BROKEN_NO_DETAIL.search(prompt)
        and not HAS_EVIDENCE.search(prompt)
        and len(words) <= NO_DETAIL_MAX_WORDS
        and fresh("no_detail")
    ):
        return "no_detail", (
            "you reported something broken without the actual error. Paste the message, "
            "stack trace, or failing output: without it the model has to guess the symptom "
            "first, which usually means reading half the codebase to find candidates."
        )

    # 8. Unbounded rewrite. The diff becomes too large to review honestly.
    if (
        BIG_REWRITE.search(prompt)
        and not SCOPE_LIMIT.search(prompt)
        and fresh("big_rewrite")
    ):
        return "big_rewrite", (
            "that asks for a rewrite without bounding it. An unscoped refactor produces a "
            "diff too large to review honestly, and a checkpoint you rubber-stamp is worse "
            "than none. Name the file or function, and say what must not change."
        )

    # Transcript-derived signals come before prompt-shape ones from here on: what
    # actually happened is stronger evidence than how a sentence is worded, and a short
    # prompt during a real problem should surface the problem, not the prompt style.

    # 9. Same file read repeatedly. Usually means it should have been kept in view.
    if (
        sig["repeat_reads"]
        and sig["repeat_reads"][1] >= REREAD_COUNT
        and fresh("reread")
    ):
        name = Path(sig["repeat_reads"][0]).name
        return "reread", (
            f"'{name}' has been read {sig['repeat_reads'][1]} times this session. Each read "
            f"costs the file's full length again. If it matters throughout the task, say so "
            f"once and ask for the relevant part to be quoted rather than re-read."
        )

    # 10. Mostly shell calls. A code-intelligence plugin is usually cheaper.
    if (
        sig["tool_calls"] >= BASH_HEAVY_CALLS
        and sig["bash_ratio"] >= BASH_HEAVY_RATIO
        and fresh("bash_heavy")
    ):
        return "bash_heavy", (
            f"{int(sig['bash_ratio'] * 100)}% of recent tool calls were shell commands. If "
            f"you are grepping to find definitions, a code-intelligence plugin replaces a "
            f"grep plus several candidate file reads with one lookup, and shrinks context "
            f"at the same time."
        )

    # 11. Vague prompt: short with nothing concrete to anchor on.
    if len(words) <= VAGUE_MAX_WORDS and not CONCRETE.search(prompt) and fresh("vague"):
        return "vague", (
            "that prompt is short and names no file, symbol, or error. Vague prompts trigger "
            "broad scanning: 'fix the bug' reads twenty files, while 'the null check in "
            "parseConfig at line 40 fails on empty input' reads one. Naming the location "
            "and what 'done' means will cost you less and get you closer."
        )

    # 12. Build request with no verification signal named.
    if (
        BUILD_MARKERS.search(prompt)
        and not VERIFY_MARKERS.search(prompt)
        and len(words) > VAGUE_MAX_WORDS
        and fresh("verify")
    ):
        return "verify", (
            "you asked for a change without naming how it gets verified. This is the "
            "'trust-then-verify gap': without a signal (test output, exit code, screenshot) "
            "you are the only error detector, reviewing code that merely looks right. "
            "Add 'and run the tests' or say what proof you want back."
        )

    # 13. Several unrelated asks bundled into one message.
    if (
        len(MULTI_TASK.findall(prompt)) >= MULTI_TASK_HITS
        and len(words) >= 12
        and fresh("multi_task")
    ):
        return "multi_task", (
            "that prompt bundles several asks together. The first tends to be handled well "
            "and later ones progressively worse, and when something goes wrong you cannot "
            "tell which part failed. Send them one at a time, verifying as you go."
        )

    # 14. Hedged wording. Politeness is fine; vagueness about the target is not.
    if len(HEDGES.findall(prompt)) >= 2 and fresh("hedging"):
        return "hedging", (
            "the wording here is hedged ('maybe', 'sort of', 'if you want'), which leaves "
            "no clear target to hit. Being blunt about what you want is not rudeness, it is "
            "specification: say the outcome, and say what would count as done."
        )

    # 15. Long session. Gentle, once only.
    if state["turns"] >= SESSION_TURN_HINT and fresh("length"):
        return "length", (
            f"{state['turns']} turns in this session. If you've moved on to unrelated work "
            f"since the start, {clear} costs nothing and gives the next task a clean, "
            f"focused context. Mixing several tasks in one session is the single most "
            f"common cause of degraded output."
        )

    return None


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    # A hook must tolerate anything on stdin. A crash here breaks the user's turn,
    # which is far worse than missing a coaching opportunity.
    if not isinstance(data, dict):
        sys.exit(0)

    raw_prompt = data.get("prompt")
    prompt = raw_prompt.strip() if isinstance(raw_prompt, str) else ""
    session = data.get("session_id") or data.get("sessionID") or "default"
    if not isinstance(session, str):
        session = "default"
    if not prompt or prompt.startswith("/"):
        sys.exit(0)  # slash commands aren't prompts to coach

    tool = detect_tool(data)
    state = load_state(session)
    state["turns"] += 1
    if CORRECTION_MARKERS.search(prompt):
        state["streak"] += 1
    else:
        state["streak"] = 0

    used_pct = read_used_pct(data)

    sig = signals(data.get("transcript_path"))
    nudge = pick_nudge(prompt, state, used_pct, tool, sig)
    if nudge:
        key, message = nudge
        state["shown"].append(key)

        out = {"systemMessage": f"{sirens(key)} tutor: {message}"}

        # systemMessage cannot be coloured: hooks have no controlling terminal, and
        # terminalSequence explicitly rejects colour sequences. So write a short note
        # for the statusline (which CAN colour it) to pick up and render in blue.
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            (STATE_DIR / "latest-note").write_text(
                f"{sirens(key)} {SHORT.get(key, key)}"
            )
        except OSError:
            pass

        # A bare BEL is on the terminalSequence allowlist. Use it only for the two
        # nudges worth interrupting for, or it becomes an annoyance.
        if key in ("context", "streak"):
            out["terminalSequence"] = "\a"

        print(json.dumps(out))

    save_state(session, state)
    sys.exit(0)


if __name__ == "__main__":
    main()
