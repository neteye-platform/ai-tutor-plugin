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
 * User-facing output goes through `client.tui.showToast`, because OpenCode has no
 * `systemMessage` equivalent. Toasts are transient, so a long audit is trimmed to
 * the single most urgent finding rather than dumped in full.
 *
 * Install: copy to ~/.config/opencode/plugins/tutor.js (global) or
 * .opencode/plugins/tutor.js (project).
 */

const SCRIPTS = new URL("../scripts/", import.meta.url).pathname

/** Run a tutor script, feeding it a Claude-Code-shaped payload on stdin. */
async function runScript($, name, payload) {
  try {
    const result = await $`python3 ${SCRIPTS + name}`
      .stdin(new Response(JSON.stringify(payload)))
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

/**
 * Estimate how full the context window is, as `{ used_percentage }`.
 *
 * OpenCode passes no `transcript_path` to plugins, so the shared Python helper has
 * nothing to read. But assistant messages carry a `tokens` object, so fetch the most
 * recent one and compute it here. Returns undefined when unknown, which keeps the
 * coach quiet rather than warning on a guess.
 */
async function usedPercentage(client, sessionID, model) {
  try {
    const res = await client.session.messages({ path: { id: sessionID } })
    const messages = res?.data ?? res ?? []
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
function firstLine(message) {
  const lines = message
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
  // Prefer the first finding over the "tutor: setup notes" header.
  return lines.find((l) => l.includes("\u{1F6A8}")) ?? lines[0] ?? ""
}

export const TutorPlugin = async ({ client, directory, $ }) => {
  const seen = new Set()

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
      const key = out.systemMessage.slice(0, 40)
      if (seen.has(key)) return
      seen.add(key)

      await client.tui.showToast({
        body: {
          title: "tutor",
          message: firstLine(out.systemMessage),
          variant: "info",
        },
      })
    },
  }
}

export default TutorPlugin
