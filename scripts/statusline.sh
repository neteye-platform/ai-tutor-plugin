#!/usr/bin/env bash
# Tutor statusline: coloured context/cache gauge plus the latest tutor nudge.
#
# Why this exists: hook `systemMessage` output cannot be coloured (hooks run with
# no controlling terminal, and `terminalSequence` explicitly rejects colour
# sequences). The statusline IS documented to support ANSI colour, so hooks write
# state to a file and this script renders it in colour.
#
# Install: "statusLine": {"type": "command", "command": "~/.claude/skills/tutor/scripts/statusline.sh"}

set -uo pipefail

BLUE=$'\033[34m'; CYAN=$'\033[36m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; RED=$'\033[31m'; DIM=$'\033[2m'; RESET=$'\033[0m'

input=$(cat)

# jq is optional; fall back to python3 which we already depend on.
if command -v jq >/dev/null 2>&1; then
  read -r used cache_warm hit_ratio model < <(printf '%s' "$input" | jq -r '
    [ (.context_window.used_percentage // 0 | floor),
      (.prompt_cache.warm // false),
      ((.prompt_cache.hit_ratio // 0) * 100 | floor),
      (.model.display_name // .model.id // "?") ] | @tsv' 2>/dev/null | tr '\t' ' ')
else
  read -r used cache_warm hit_ratio model < <(printf '%s' "$input" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
cw=d.get("context_window") or {}
pc=d.get("prompt_cache") or {}
m=d.get("model") or {}
print(int(cw.get("used_percentage") or 0),
      str(pc.get("warm", False)).lower(),
      int((pc.get("hit_ratio") or 0)*100),
      m.get("display_name") or m.get("id") or "?")' 2>/dev/null)
fi

used=${used:-0}; hit_ratio=${hit_ratio:-0}; model=${model:-?}

# Context gauge, coloured by pressure. Thresholds match the coach hook.
if   (( used >= 85 )); then ctx_col=$RED
elif (( used >= 75 )); then ctx_col=$YELLOW
elif (( used >= 50 )); then ctx_col=$CYAN
else                        ctx_col=$GREEN
fi

filled=$(( used / 10 )); (( filled > 10 )) && filled=10
bar=""
for ((i=0; i<10; i++)); do
  if (( i < filled )); then bar+="█"; else bar+="░"; fi
done

# Cache state. Cold means the next turn re-reads the whole prefix.
if [[ "${cache_warm:-false}" == "true" ]]; then
  cache="${GREEN}cache ${hit_ratio}%${RESET}"
else
  cache="${DIM}cache cold${RESET}"
fi

line1="${ctx_col}${bar} ${used}%${RESET}  ${cache}  ${DIM}${model}${RESET}"

# Must match _state_dir() in coach.py exactly, or the two write to different places
# and the note row silently never appears: CLAUDE_PLUGIN_DATA, then TUTOR_STATE_DIR,
# then the first host config dir that exists, then a neutral cache dir.
if [[ -n "${CLAUDE_PLUGIN_DATA:-}" ]]; then
  state_dir="$CLAUDE_PLUGIN_DATA"
elif [[ -n "${TUTOR_STATE_DIR:-}" ]]; then
  state_dir="$TUTOR_STATE_DIR"
elif [[ -d "$HOME/.claude" ]]; then
  state_dir="$HOME/.claude/tutor-state"
elif [[ -d "$HOME/.codex" ]]; then
  state_dir="$HOME/.codex/tutor-state"
elif [[ -d "$HOME/.config/opencode" ]]; then
  state_dir="$HOME/.config/opencode/tutor-state"
else
  state_dir="$HOME/.cache/ai-tutor"
fi

# Publish context usage for coach.py. Hook payloads do NOT include context_window
# (it is a statusline-only field), so this file is the only way the prompt coach can
# know how full the window is. The statusline runs on every render, so it stays fresh.
mkdir -p "$state_dir" 2>/dev/null && printf '%s' "$used" > "$state_dir/used-pct" 2>/dev/null

# Second row: most recent tutor nudge, if any, written by the coach hook.
note_file="$state_dir/latest-note"
if [[ -f "$note_file" ]]; then
  # Only show notes from the last 30 minutes; stale advice is noise.
  if [[ -n "$(find "$note_file" -newermt '-30 minutes' 2>/dev/null)" ]]; then
    note=$(head -c 150 "$note_file" 2>/dev/null | tr -d '\n')
    [[ -n "$note" ]] && printf '%s\n%s\n' "$line1" "${BLUE}tutor${RESET} ${DIM}${note}${RESET}" && exit 0
  fi
fi

printf '%s\n' "$line1"
