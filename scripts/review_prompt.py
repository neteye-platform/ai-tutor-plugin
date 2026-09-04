#!/usr/bin/env python3
"""Ask the assistant to tutor the user on their prompt, in-band.

WHY THIS IS DIFFERENT FROM coach.py
-----------------------------------
`coach.py` is a regex: it counts words and matches patterns, and speaks only through
`systemMessage`, which reaches the user without touching the model's context. Cheap,
deterministic, and incapable of judgement.

Real tutoring needs judgement. "Is this prompt well-formed for the task?" is not a
countable property, and the only thing in the loop capable of answering it is the model
that is about to act on the prompt.

So this hook uses `additionalContext`, which the docs state is "added as context that
Claude can see and act on" for `UserPromptSubmit`. It asks the assistant to open its
reply with a short coaching note when, and only when, the prompt could have been
materially better.

THE HONEST TRADE-OFF
--------------------
This costs context, unlike `coach.py`. That is unavoidable: to have judgement applied
you must put the request where the judge can see it. The budget is kept small (a few
hundred tokens), it is opt-in, and it goes quiet once the user has been taught.

Enable with:  TUTOR_REVIEW=1        (off by default)
Verbosity:    TUTOR_REVIEW_MODE=teach|brief|off
"""

import json
import os
import sys
import time
from pathlib import Path


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

# Stop after this many coaching notes in one session. Being taught the same lesson
# repeatedly is nagging, not teaching.
MAX_NOTES_PER_SESSION = 3
STATE_TTL_S = 7 * 24 * 3600  # delete per-session counters after a week

# Prompts shorter than this are usually acknowledgements ("yes", "go on", "thanks")
# and there is nothing to coach.
MIN_WORDS = 4

BRIEF = """\
<tutor-request>
Before answering, judge whether the user's prompt above could have been materially \
better, using these criteria:

- Does it name the file, symbol, or error, or must you go looking?
- Does it say what "done" means, or how the result gets verified?
- Is the scope bounded, or open-ended?
- Does it bundle several unrelated asks?

If it was already good, say NOTHING about it and just answer. Do not praise a good \
prompt; silence is the signal.

If one thing would have helped materially, open your reply with a single line:
"tutor: <what to add next time, and the concrete benefit>"
Then answer the question normally. Never withhold the answer over prompt style.
</tutor-request>"""

TEACH = """\
<tutor-request>
You are also acting as a tutor for how to use this tool well. Before answering, judge \
the user's prompt above against these criteria:

- **Specificity.** Does it name the file, symbol, line, or error message, or must you \
  scan to find the subject? Broad prompts cause broad reading, which fills the context \
  window and degrades later answers.
- **Verification.** Does it say how the result gets checked (tests, build, exit code, \
  screenshot)? Without a signal the user is the only error detector, reviewing work \
  that merely looks right.
- **Scope.** Is the unit of work bounded, and small enough that its diff can honestly \
  be reviewed?
- **Single concern.** Does it bundle unrelated asks? Later ones get handled worse, and \
  failure becomes hard to attribute.
- **Context fit.** Given the conversation so far, would starting fresh have served \
  better than continuing here?

Rules:
1. If the prompt was already well-formed, say NOTHING about it. Do not praise it, and \
   do not invent a suggestion to seem useful. Silence is how the user learns their \
   prompt was fine.
2. If exactly one improvement would have mattered, open with:
   "tutor: <the improvement>, because <the mechanism it avoids>."
   Give the mechanism, not just the rule: a reason transfers to the next situation, an \
   instruction does not.
3. At most one note. Never a list.
4. Then answer the actual question, fully. Coaching never replaces the work, and never \
   delays it.
5. If the user says they do not want coaching, stop for the rest of the session.
</tutor-request>"""


def load_count(session: str) -> int:
    try:
        return int((STATE_DIR / f"{session}.notes").read_text().strip())
    except (OSError, ValueError):
        return 0


def bump_count(session: str, current: int) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / f"{session}.notes").write_text(str(current + 1))
    except OSError:
        pass
    # Old counters serve no purpose; coach.py prunes its own files the same way.
    cutoff = time.time() - STATE_TTL_S
    try:
        for entry in STATE_DIR.glob("*.notes"):
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
    except OSError:
        pass


def main() -> None:
    mode = os.environ.get("TUTOR_REVIEW_MODE", "").lower()
    enabled = os.environ.get("TUTOR_REVIEW") == "1" or mode in ("teach", "brief")
    if not enabled or mode == "off":
        sys.exit(0)

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if not isinstance(data, dict):
        sys.exit(0)

    raw_prompt = data.get("prompt")
    prompt = raw_prompt.strip() if isinstance(raw_prompt, str) else ""
    session = data.get("session_id") or data.get("sessionID") or "default"
    if not isinstance(session, str):
        session = "default"

    # Nothing to coach: slash commands, acknowledgements, or an empty turn.
    if not prompt or prompt.startswith("/") or len(prompt.split()) < MIN_WORDS:
        sys.exit(0)

    # Opting out mid-session should actually work.
    if any(
        phrase in prompt.lower()
        for phrase in ("stop tutoring", "no tutor", "disable tutor", "stop coaching")
    ):
        bump_count(session, MAX_NOTES_PER_SESSION)
        print(json.dumps({"systemMessage": "tutor: coaching off for this session."}))
        sys.exit(0)

    count = load_count(session)
    if count >= MAX_NOTES_PER_SESSION:
        sys.exit(0)

    body = TEACH if mode != "brief" else BRIEF
    bump_count(session, count)
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": body,
                }
            }
        )
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
