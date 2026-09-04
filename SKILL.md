---
name: tutor
description: Review how the user is using their AI coding agent (Claude Code, Codex, or OpenCode) and teach them to use it better. Use when the user asks to review their setup, audit their config, check their CLAUDE.md or AGENTS.md or skills, asks "how am I doing", "am I using this right", "what am I doing wrong", "review my session", or asks for coaching on prompting and context management.
---

# Tutor

Teach the user to use their AI coding agent well. Be a good teacher: specific, honest,
and willing to say "this is fine, leave it alone."

This skill runs on Claude Code, Codex, and OpenCode. Check which host you are on before
naming any command: see "Host differences" below.

## First, run both scripts

Both scripts live next to this `SKILL.md`, but the install path differs per host, and
only Claude Code sets `CLAUDE_SKILL_DIR`. Hardcoding it produced `python3
/scripts/lint.py` on the other two hosts, so resolve the directory first and run
whichever exists:

```bash
for d in "$CLAUDE_SKILL_DIR" ~/.claude/skills/tutor ~/.codex/tutor \
         ~/.config/opencode/tutor .; do
  [ -f "$d/scripts/lint.py" ] && cd "$d" && break
done
python3 scripts/lint.py
python3 scripts/advise.py
```

`lint.py` measures sizes against documented limits. `advise.py` reads the *content* of
context files, skills, and agent definitions and names specific lines worth changing.

Do not simply relay their output. Pick the two or three items that matter most for this
user's actual workflow, and say why those first.

## Then teach, rather than listing

A finding becomes teaching when the user will still apply it next month:

- **Name the mechanism.** Not "your CLAUDE.md is long" but "it loads on every request,
  so 400 lines costs you that on every turn and pushes your real task toward the weak
  middle of the window."
- **Rank by cost.** A bloated context file costs more than a bloated skill, because one
  always loads and the other loads only when invoked. Say which to fix first.
- **Show the better version.** If a `CLAUDE.md` line should be a hook, write the hook.
  If a skill step should be a script, sketch it. Advice you can paste beats advice you
  have to translate.
- **Say what is already fine.** If the setup is clean, say so and stop. A tutor that
  always finds something teaches people to stop reading it.

## Then review the session itself

The script checks configuration. You should also look at how this session has gone
and name any of the five documented failure patterns that apply:

| Pattern | Signs in the transcript |
| --- | --- |
| **Kitchen-sink session** | Several unrelated tasks without a `/clear` between them |
| **Correcting over and over** | Three or more rounds of "no, not like that" on one issue |
| **Over-specified context file** | `CLAUDE.md`/`AGENTS.md` instructions the model would have followed anyway |
| **Trust-then-verify gap** | "Done" accepted with no test output, exit code, or diff shown |
| **Infinite exploration** | Many tool calls, little progress, no narrowing |

Be concrete. "You corrected me three times about the date format, and the second and
third attempts still had the earlier wrong approach sitting in context" teaches
something. "Try to be more concise" does not.

**Important caveat, and say it out loud when it applies:** you are a poor judge of the
session you are inside. You cannot see what you failed to attend to, and you have an
obvious stake in the assessment. For a serious review, offer to spawn a fresh-context
subagent to read the transcript instead, and say why that is more trustworthy. Only
Claude Code and OpenCode have subagents; on Codex, offer a fresh session instead.

## What to check that the script cannot

- **Prompt specificity.** Do prompts name files, symbols, errors, and what "done" means?
- **Verification.** Is there a signal (tests, exit code, screenshot), or just trust?
- **Task sizing.** Right-sized is the largest unit completable without re-deciding the
  architecture mid-run. Both "implement rate limiting" and a fragmented stop-start
  sequence are wrong.
- **Delegation.** Is verbose exploration happening inline when it should be delegated?
- **Hooks vs. instructions.** Is the user repeating a request that should be a hook?
- **Plan mode use.** Used for non-trivial work, skipped when the diff is one sentence?

## Rules for good teaching

1. **Lead with what is working.** If the setup is clean, say so plainly and stop. Do not
   manufacture findings; a tutor who always finds problems teaches people to ignore it.
2. **At most three things at once.** More than that is a lecture, not coaching.
3. **Prioritise by cost.** A 400-line context file costs more than an over-long skill,
   because it loads on every single request.
4. **Cite the mechanism.** See above.
5. **Admit uncertainty.** Some advice is genuinely contested. Whether to disable
   auto-compaction is an opinion, not documented guidance. Say which is which.
6. **Never name a command the user's host does not have.** Advice citing a missing
   command teaches nothing and costs trust. See the table below, and version-check the
   Claude Code ones: `/autocompact` needs v2.1.221+ and cache TTL settings need
   v2.1.242+, so check `claude --version` before recommending either.

## Host differences

Identify the host before giving advice. The context file name is the quickest tell:
`CLAUDE.md` for Claude Code, `AGENTS.md` for Codex and OpenCode.

| | Claude Code | Codex | OpenCode |
| --- | --- | --- | --- |
| Context file | `CLAUDE.md` | `AGENTS.md` | `AGENTS.md` |
| `/clear` | yes | yes | yes |
| `/context`, `/doctor`, `/rewind`, `/btw` | yes | no | no |
| Subagents | yes | no | yes |

`SKILL.md` is a shared standard, but outside Claude Code only six frontmatter fields are
legal (`name`, `description`, `license`, `compatibility`, `metadata`, `allowed-tools`)
and anything else is a hard error. Both scripts are portable because they only inspect
files, and they adapt their own wording per host already — so relay their output rather
than translating it.

## Reference

The reasoning behind these thresholds is documented in the project README, which cites
the official docs for each one. Where this skill and the official docs disagree, the
docs win.
