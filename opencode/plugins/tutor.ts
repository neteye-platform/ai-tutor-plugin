/**
 * Tutor plugin for OpenCode.
 *
 * OpenCode has no shell-command hook system: plugins are JS/TS modules loaded
 * in-process. So rather than reimplementing the checks, this shells out to the same
 * Python scripts the Claude Code and Codex integrations use, keeping one source of
 * truth for the thresholds and the wording.
 *
 * Hooks used, both verified against @opencode-ai/plugin's exported types:
 *   - `chat.message`  fires when a new user message is received, so prompts can be
 *                     coached before the model works on them.
 *   - `event`         catch-all on the event bus; `session.created` triggers the
 *                     config audit once per session.
 *
 * User-facing output is a hybrid: every nudge is appended to the just-submitted
 * user message as an extra text part with `ignored: true` — OpenCode's own flag for
 * "shown to the user, excluded from what's replayed to the model" (see
 * `partAudience` in opencode's acp/content.ts) — so it's a permanent, scrollable
 * record even if you miss it in the moment. `client.tui.showToast` (the only
 * `systemMessage`-like primitive OpenCode exposes to plugins) additionally fires
 * as an immediate, harder-to-miss cue, with a longer-than-default `duration` since
 * the built-in 5s default disappears fast and the toast itself has a fixed
 * top-right position plugins can't move (see TOAST_DURATION_MS below). The config
 * audit has no message yet to attach a part to, so it's toast-only.
 *
 * Install: copy this file plus the sibling `tutor/scripts/` directory to
 * ~/.config/opencode/plugins/ (global) or .opencode/plugins/ (project). Only files
 * directly inside `plugins/` are auto-loaded, so this must not live in a subfolder.
 */

import type { Plugin, PluginInput } from "@opencode-ai/plugin"

// Resolved relative to this file, which lives directly in <config>/plugins/, so the
// scripts directory is one level down at <config>/plugins/tutor/scripts/.
const SCRIPTS = new URL("tutor/scripts/", import.meta.url).pathname

/** Run a tutor script, feeding it a Claude-Code-shaped payload on stdin. */
async function runScript($: PluginInput["$"], name: string, payload: unknown): Promise<{ systemMessage?: string } | null> {
  try {
    // @opencode-ai/plugin types `stdin` as a WritableStream property on the shell
    // promise, but on the installed Bun runtime it is neither callable nor present
    // (undefined) — a `.stdin(...)` call or `.stdin.getWriter()` both throw. Bun's
    // shell redirect operator, piping a Response/Blob in via `<`, is what actually
    // works at runtime, so feed the payload that way instead.
    const result = await $`python3 ${SCRIPTS + name} < ${new Response(JSON.stringify(payload))}`
      .quiet()
      .nothrow()
    const text = result.stdout.toString().trim()
    if (!text.startsWith("{")) return null
    return JSON.parse(text)
  } catch {
    // A coach that breaks the session is worse than no coach.
    return null
  }
}

// Context windows by model slug. Conservative default: warning early beats never.
const DEFAULT_WINDOW = 200_000
const LARGE_WINDOW = 1_000_000
const LARGE_HINTS = ["sonnet-4-5", "sonnet-5", "gemini", "gpt-5", "opus-4-6", "opus-5"]

// OpenCode's toast defaults to 5s and sits in a fixed top-right corner a plugin
// can't reposition. Tripling the duration is the only lever available to make it
// less likely to be missed or covered before the reader notices it.
const TOAST_DURATION_MS = 15_000

/**
 * Estimate how full the context window is, as `{ used_percentage }`.
 *
 * OpenCode passes no `transcript_path` to plugins, so the shared Python helper has
 * nothing to read. But assistant messages carry a `tokens` object, so fetch the most
 * recent one and compute it here. Returns undefined when unknown, which keeps the
 * coach quiet rather than warning on a guess.
 */
async function usedPercentage(
  client: PluginInput["client"],
  sessionID: string,
  model: { modelID?: string } | undefined,
): Promise<{ used_percentage: number } | undefined> {
  try {
    const res = await client.session.messages({ path: { id: sessionID } })
    const messages = (res as any)?.data ?? res ?? []
    for (let i = messages.length - 1; i >= 0; i--) {
      const info = messages[i]?.info ?? messages[i]
      const t = info?.tokens
      if (!t) continue
      const total = (t.input ?? 0) + (t.cache?.read ?? 0) + (t.cache?.write ?? 0)
      if (!total) continue
      const slug = `${info.modelID ?? model?.modelID ?? ""}`.toLowerCase()
      const window = LARGE_HINTS.some((h) => slug.includes(h))
        ? LARGE_WINDOW
        : DEFAULT_WINDOW
      return { used_percentage: Math.min(100, Math.round((total / window) * 100)) }
    }
  } catch {
    // Session may have no messages yet, or the API may have moved. Stay quiet.
  }
  return undefined
}

/** Toasts are transient and narrow, so show one line rather than a wall of text. */
function firstLine(message: string): string {
  const lines = message
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
  // Prefer the first finding over the "tutor: setup notes" header.
  return lines.find((l) => l.includes("\u{1F6A8}")) ?? lines[0] ?? ""
}

export const TutorPlugin: Plugin = async ({ client, directory, $ }) => {
  // Keyed by sessionID: a nudge already shown in one session must still be available
  // in the next. A single flat Set would suppress it for the lifetime of the process.
  const seenBySession = new Map<string, Set<string>>()

  return {
    /** Config audit, once per session. */
    event: async ({ event }) => {
      if (event.type !== "session.created") return
      const out = await runScript($, "lint.py", {
        cwd: directory,
        tutor_host: "opencode",
        hook_event_name: "SessionStart",
      })
      if (!out?.systemMessage) return
      await client.tui.showToast({
        body: {
          title: "tutor",
          message: firstLine(out.systemMessage),
          variant: "warning",
          duration: TOAST_DURATION_MS,
        },
      })
    },

    /** Prompt coaching, on each new user message. */
    "chat.message": async (input, output) => {
      const text = (output.parts ?? [])
        .filter((p) => p.type === "text")
        .map((p) => p.text ?? "")
        .join(" ")
        .trim()
      if (!text || text.startsWith("/")) return

      const sessionID = input.sessionID ?? "default"
      const out = await runScript($, "coach.py", {
        prompt: text,
        sessionID,
        tutor_host: "opencode",
        hook_event_name: "UserPromptSubmit",
        // OpenCode gives no transcript_path, but assistant messages carry token
        // counts, so compute the percentage here and hand it over in the same shape
        // the scripts already understand.
        context_window: await usedPercentage(client, sessionID, input.model),
      })
      if (!out?.systemMessage) return

      // Never repeat a nudge within a session; repetition is what gets it muted.
      let seen = seenBySession.get(sessionID)
      if (!seen) {
        seen = new Set()
        seenBySession.set(sessionID, seen)
      }
      const key = out.systemMessage.slice(0, 40)
      if (seen.has(key)) return
      seen.add(key)

      // Immediate, harder-to-miss cue. Best-effort: a permanent record still lands
      // below via output.parts even if this fails or gets missed/covered.
      await client.tui.showToast({
        body: {
          title: "tutor",
          message: firstLine(out.systemMessage),
          variant: "info",
          duration: TOAST_DURATION_MS,
        },
      })

      // input.messageID is undefined for a normal new message (it's meant for a
      // different case entirely — editing/resuming a specific existing message);
      // the message actually being created is output.message, so its id is the one
      // to attach a part to. Guard anyway in case that ever changes upstream.
      const messageID = output.message?.id
      if (!messageID) return

      output.parts.push({
        // OpenCode's PartID schema requires the "prt" prefix (Schema.isStartsWith
        // in opencode's session/schema.ts) and throws — not just logs — if it's
        // missing, which fails the whole turn. A bare crypto.randomUUID() doesn't
        // qualify.
        id: `prt_${crypto.randomUUID()}`,
        sessionID,
        messageID,
        type: "text",
        // coach.py's own messages already read as "🚨 tutor: ...", so no extra
        // prefix is needed here.
        text: out.systemMessage,
        // Visible to the user in the transcript, but excluded from what's replayed
        // to the model on this and future turns (OpenCode's "user-only audience"
        // flag) — a coaching aside, not something the model should react to.
        ignored: true,
      })
    },
  }
}

export default TutorPlugin
