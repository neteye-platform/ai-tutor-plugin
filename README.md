# ai-tutor-plugin

Teaches you to use AI coding agents well, instead of just using them.

It watches for the habits that quietly degrade output — vague prompts, unverified
changes, a context window filling with dead ends — and explains what to do differently
and *why*. Works with [Claude Code](https://claude.com/claude-code),
[Codex](https://developers.openai.com/codex), and [OpenCode](https://opencode.ai).

```text
🚨🚨 tutor: you asked for a change without naming how it gets verified. Without a
     signal (test output, exit code, screenshot) you are the only error detector,
     reviewing code that merely looks right.
```

Most of it costs **zero tokens**: messages go to your terminal, never into the model's
context. A tool that spent context lecturing you about context would be self-defeating.

## Contents

- [What it does](#what-it-does)
- [Install](#install)
- [What it checks](#what-it-checks)
- [Design rules](#design-rules)
- [Tool support](#tool-support)
- [Configuration](#configuration)
- [Extending it](#extending-it)
- [Status and limitations](#status-and-limitations)

## What it does

Three layers, in ascending order of cost and capability.

### 1. Config audit — automatic, free

At session start it measures your setup against documented limits and says what is
costing you context. Not just *that* a file is too long, but why that matters:

```text
🚨🚨🚨 tutor: 184 agent definitions total roughly 17,250 tokens of frontmatter
       (threshold 15,000). Descriptions load on every request, because the model
       needs them to choose what to delegate. Move detail into each agent's system
       prompt, which loads only when that agent runs.
```

### 2. Live coach — automatic, free

Every prompt is checked before the model sees it, and the session transcript is checked
alongside it. At most one nudge per turn, and usually none.

Severity is one emoji repeated: 🚨🚨🚨 means acting now saves real time or money, 🚨
means ignore it freely if you disagree.

### 3. On-demand review — when you ask

`/tutor` runs a deeper pass: the audit, plus content-aware advice that cites specific
lines, plus a critique of how the session has gone.

```text
skill 'graphify': 4 lines are written as sequential one-off steps (L132, L452, L573,
L580). A skill body stays in context for the rest of the session, so "now do step 3"
still sits there long after step 3 is done. Write standing instructions instead.
```

There is also an opt-in mode where the assistant judges each prompt as it answers.
That one **does** cost context, so it is off by default. See
[Configuration](#configuration).

## Install

### Claude Code

```bash
claude plugin marketplace add neteye-platform/ai-tutor-plugin
claude plugin install tutor@wuerth-tutor
```

Then `/reload-plugins`, or start a new session.

You will be asked to trust the workspace, because the plugin registers hooks. That is
expected and correct: hooks are code that runs on your machine.

### Codex

```bash
mkdir -p ~/.codex/tutor
cp -r scripts ~/.codex/tutor/
cp codex/hooks.json ~/.codex/hooks.json   # merge by hand if you already have one
```

Then, **inside Codex**, run:

```text
/hooks
```

This step is not optional and it is the one people miss. Codex will not execute any
non-managed hook until you have explicitly approved it, so before you do, a fresh
install looks broken: nothing happens and no error appears.

Three things worth knowing about Codex's trust model:

- **Trust is per hook, not per workspace.** Claude Code asks once for a folder; Codex
  tracks each hook separately.
- **Editing a hook re-triggers the prompt.** Pull an update that changes `coach.py` and
  everyone is asked again. That is the model working, not a bug.
- **`--dangerously-bypass-hook-trust` should stay unused.** Its own help says it is
  "intended only for automation that already vets hook sources".

If your organisation sets `allow_managed_hooks_only = true` in `requirements.toml`,
Codex ignores all user and project hooks, and the tutor will not run unless deployed
through the managed config layer. That setting is only honoured in `requirements.toml`.

<details>
<summary>TOML equivalent, if you prefer <code>config.toml</code> to <code>hooks.json</code></summary>

```toml
[[hooks.SessionStart]]
matcher = "startup"

[[hooks.SessionStart.hooks]]
type = "command"
command = "python3 ~/.codex/tutor/scripts/lint.py"
timeout = 10
statusMessage = "tutor: checking setup"

[[hooks.UserPromptSubmit]]

[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "python3 ~/.codex/tutor/scripts/coach.py"
timeout = 5
```

</details>

### OpenCode

```bash
mkdir -p ~/.config/opencode/plugins ~/.config/opencode/tutor
cp -r scripts ~/.config/opencode/tutor/
cp opencode/tutor.js opencode/package.json ~/.config/opencode/plugins/
```

The plugin resolves the scripts relative to its own location, so keep that layout or
edit the `SCRIPTS` constant at the top of `tutor.js`.

### No tool at all

Both analysis scripts run standalone, with no dependencies beyond `python3`:

```bash
python3 scripts/lint.py      # size and budget checks
python3 scripts/advise.py    # content advice, with line numbers
```

Run them from a project directory. They auto-detect which agents you use by looking for
`~/.claude`, `~/.codex`, `~/.config/opencode`, `CLAUDE.md` and `AGENTS.md`, and report
only on what they find.

## What it checks

### Configuration

Paths and command names adapt per tool, so a Codex user is never told to run
`/doctor`.

| Check | Threshold | Why it matters |
| --- | --- | --- |
| Context file length (`CLAUDE.md`, `AGENTS.md`) | 200 lines | Loads on **every** request, so every extra line is a recurring cost |
| Nested context files | 3 or more | They merge silently, so the total is invisible unless you audit each level |
| `SKILL.md` length | 500 lines | Once invoked, the body stays in context for the rest of the session |
| `SKILL.md` size | ~5,000 tokens | Above this it is truncated after compaction, keeping only the start |
| Agent frontmatter total | ~15,000 tokens | Descriptions always load, since they drive delegation choices |
| MCP server count | 5 or more | Tool-selection accuracy degrades past roughly 30–50 loaded tools |
| MCP `alwaysLoad` | any | Defeats deferred loading: right for small toolsets, wrong for broad ones |

`advise.py` adds content-level checks: lines that restate what the model already does,
rules that would be better as a hook than as prose, material readable from the repo
itself, emphasis markers spread so widely they no longer emphasise, skill steps written
as one-off instructions, and agent definitions that do not restrict `tools:`.

### Behaviour

Fifteen nudges. Seven read the **session transcript**, so they react to what actually
happened rather than to how a sentence was worded:

| Nudge | Trigger | Severity |
| --- | --- | --- |
| Context pressure | 75% or more of the window used | 🚨🚨🚨 |
| Compaction thrashing | Three or more automatic compactions in one session | 🚨🚨🚨 |
| Repeated failures | Three consecutive failed tool calls | 🚨🚨🚨 |
| Unverified edits | Files changed with nothing test-shaped run since | 🚨🚨 |
| Infinite exploration | 12+ consecutive reads with no edit, across 25+ calls | 🚨🚨 |
| Repeated reads | Same file read three or more times | 🚨 |
| Shell-heavy | 80%+ of 30+ calls were shell commands | 🚨 |

Eight read the **prompt text**:

| Nudge | Trigger | Severity |
| --- | --- | --- |
| Correction spiral | Three corrections in a row | 🚨🚨🚨 |
| No verification | A change requested with no test, build, or proof named | 🚨🚨 |
| No error detail | Something reported broken with no message or trace pasted | 🚨🚨 |
| Unbounded rewrite | A refactor request with no scope limit | 🚨🚨 |
| Vague prompt | Under seven words with no file, symbol, or error named | 🚨 |
| Bundled asks | Several unrelated requests in one message | 🚨 |
| Hedged wording | Two or more hedges, leaving no clear target | 🚨 |
| Long session | Forty or more turns | 🚨 |

**Only one nudge fires per turn**, whichever is most urgent, and never the same one
twice in a session. Transcript signals are checked before prompt-shape ones, because
observed behaviour is stronger evidence than phrasing: during a real problem you want
to hear about the problem, not about your writing style.

## Design rules

Worth reading before modifying anything:

- **Silent by default.** A coach that comments every turn gets muted. Nudges fire only
  when a measurable threshold trips.
- **Once per session per nudge.** Repetition turns advice into noise.
- **Never blocks.** Always exits 0. This is teaching, not policy enforcement — use
  permissions or a sandbox for real boundaries.
- **Teach the mechanism, not the rule.** "Keep your context file short" does not
  transfer to new situations; "it loads on every request, so every line is a recurring
  cost" does.
- **Countable checks only, in the scripts.** Line counts and token budgets are
  measurable. Judgement calls are left to `/tutor`, where a model can read real context,
  because a regex guessing at tone will be wrong and irritating.
- **Never name a command the host tool lacks.** Advice citing a missing command teaches
  nothing and costs trust.

## Tool support

All three hosts run the same Python, so thresholds and wording live in one place. What
differs is how each invokes it and how it shows you the result.

| Capability | Claude Code | Codex | OpenCode |
| --- | --- | --- | --- |
| Config audit at session start | ✅ `SessionStart` | ✅ `SessionStart` | ✅ `session.created` |
| Live prompt coaching | ✅ `UserPromptSubmit` | ✅ `UserPromptSubmit` | ✅ `chat.message` |
| Context-pressure nudge | ✅ | ✅ | ✅ |
| Standalone CLI scripts | ✅ | ✅ | ✅ |
| How messages reach you | `systemMessage` | `systemMessage` | TUI toast |
| Terminal bell on urgent nudges | ✅ | ❔ untested | ❌ |
| Coloured status line | ✅ | ❌ no such feature | ❌ no such feature |

Claude Code and Codex are near-identical: both use `hooks.json`, the same event names,
the same `systemMessage` / `additionalContext` split, and the same exit-2 blocking
convention. The Python runs unmodified on both.

OpenCode is architecturally different — it has no shell-command hooks, and plugins are
JavaScript modules loaded in-process. The bundled plugin shells out to the same scripts
and renders their output as TUI toasts.

<details>
<summary>How the context-pressure nudge works everywhere, given no tool exposes usage
to hooks</summary>

**No tool passes context-window usage to hooks.** On Claude Code, `context_window` is a
status-line-only field, absent from every hook payload. Codex and OpenCode have no
equivalent at all. Hooks are told *what happened*, never *how full the window is*.

But every hook on all three receives **`transcript_path`**, and transcripts record
per-response token usage. So `scripts/context_usage.py` reads the tail of the
transcript, finds the most recent usage record, sums the input-side token counts, and
divides by the model's window. One mechanism, three tools, no per-tool bridge.

It degrades honestly: if the transcript is missing or unreadable the function returns
`None` and the nudge stays quiet rather than firing on a guess.

Two caveats:

- **It is an estimate.** Counts come from the last completed response, so the figure
  lags the current turn slightly.
- **The window size is inferred, and 1M is opt-in.** A model name alone does not imply
  extended context, so only an explicit marker earns the larger figure. Better still,
  if the session has ever compacted, its own compaction boundary is used as the real
  ceiling. Getting this wrong is not cosmetic: an earlier version reported a session at
  the brink of compaction as 17% full instead of 84%.
- **Formats differ.** The parser handles Claude Code's shape and tolerates variants. If
  a tool changes its transcript format the reading degrades to `None` rather than to a
  wrong number.

</details>

## Configuration

### Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `TUTOR_REVIEW` | unset | `1` enables assistant-judged prompt review. **Costs context.** |
| `TUTOR_REVIEW_MODE` | `teach` | `teach`, `brief`, or `off` |
| `TUTOR_STATE_DIR` | host tool's config dir | Where per-session state is kept |

Prompt review is the one part that is not free. To have judgement applied to a prompt,
the request must go where the judge can see it, which means the model's context. It is
capped at three notes per session, and saying "stop tutoring" turns it off for the rest
of the session.

### Thresholds

Every threshold is a named constant at the top of its script. If a nudge is too chatty,
raise its threshold or delete its block in `pick_nudge()`. To quieten things generally,
raise `CONTEXT_WARN_PCT` and `SESSION_TURN_HINT` in `scripts/coach.py`.

### Optional coloured status line (Claude Code only)

Hook messages cannot be coloured: hooks run with no controlling terminal, and the
`terminalSequence` field explicitly rejects colour sequences. The status line **does**
support ANSI colour, so the gauge is separate:

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.claude/plugins/tutor/scripts/statusline.sh"
  }
}
```

Add that to `~/.claude/settings.json` for a context bar coloured by pressure, cache hit
rate, model name, and the most recent tutor note. Remove it by deleting the key.

## Extending it

Adding a nudge takes two edits:

1. Add a block to `pick_nudge()` in `scripts/coach.py`, positioned by urgency, returning
   `(key, message)`.
2. Add the key to `SEVERITY` (1–3) and `SHORT` (the status-line one-liner).

If it needs a fact about the session rather than the prompt, add a counter to
`signals()` in `scripts/transcript.py`. Keep those strictly **countable**: that module
reports what happened and never interprets it, because a nudge built on a judgement call
will eventually fire wrongly and get the whole tool muted.

### Tests

```bash
python3 tests/test_robustness.py
```

Hooks run on every prompt, so a crash breaks the user's turn. This asserts that every
script survives malformed input — empty stdin, garbage, wrong JSON types, unreadable
transcripts, a 10,000-word prompt — always exits 0, writes nothing to stderr, and emits
only valid JSON. It found three real crashes on first run.

### Layout

```text
scripts/
  lint.py            size and budget checks
  advise.py          content-aware advice, with line numbers
  coach.py           the nudges, and the order they fire in
  transcript.py      counted behavioural signals from the transcript
  context_usage.py   context-window usage from a transcript
  review_prompt.py   opt-in assistant-judged prompt review
  statusline.sh      coloured gauge (Claude Code only)
tests/               robustness checks
.claude-plugin/      Claude Code plugin and marketplace manifests
hooks/hooks.json     Claude Code hook config
codex/hooks.json     Codex hook config
opencode/tutor.js    OpenCode plugin (shells out to scripts/)
SKILL.md             the on-demand /tutor review, read by all three hosts
```

## Status and limitations

This is young software. Being straight about that:

- **The thresholds are informed guesses.** 75% context, three corrections, seven words,
  twelve reads. They have not been validated against a real cohort, so expect to tune
  them. Every one is a named constant for exactly that reason.
- **Some nudges will misfire.** The hedging check will flag a politely-worded prompt
  that was perfectly clear. That is why nothing ever blocks, and why low-severity nudges
  are explicitly ignorable.
- **The session critique is the weakest part.** A model reviewing the session it is
  inside cannot see what it failed to notice, and has a stake in the verdict. `SKILL.md`
  says so, and offers a fresh subagent instead.
- **Windows is unsupported.** Hooks are cross-platform but `statusline.sh` is bash.
- **The terminal bell on Codex is untested.** `terminalSequence` is documented for
  Claude Code; Codex may ignore the unknown field or reject the payload. Hence the ❔.

Verified against Claude Code 2.1.218, Codex CLI 0.145.0, and OpenCode 1.18.4. Some
behaviour referenced in `SKILL.md` is gated on later Claude Code versions:
`/autocompact` needs 2.1.221+, cache TTL settings need 2.1.242+.

Bug reports and threshold data from real use are especially welcome.

## Sources

Thresholds and guidance come from the
[Claude Code docs](https://code.claude.com/docs/en/overview),
[Codex docs](https://learn.chatgpt.com/docs/hooks), and
[OpenCode docs](https://opencode.ai/docs/plugins/).

The reasoning about attention degradation draws on Liu et al.,
[*Lost in the Middle*](https://arxiv.org/abs/2307.03172) (2023) and Chroma Research,
[*Context Rot*](https://research.trychroma.com/context-rot) (2025).

Where this plugin and the official docs disagree, the docs win.

## Licence

Dual licensed under [MIT](LICENSE-MIT) and [Apache 2.0](LICENSE-APACHE), at your option.
