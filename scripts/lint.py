#!/usr/bin/env python3
"""Audit AI coding agent configuration against documented limits.

Supports Claude Code, Codex, and OpenCode. Runs standalone (`python3 lint.py`) or as
a hook: Claude Code and Codex both invoke it as a SessionStart hook with JSON on
stdin, and the OpenCode plugin shells out to it.

In hook mode it emits `systemMessage`, which both Claude Code and Codex surface to
the user without adding it to the model's context.

Every threshold comes from official documentation. Findings explain the mechanism
rather than just stating the rule, because a rule alone does not transfer to the next
situation.
"""

import json
import os
import sys
from pathlib import Path

# --- documented limits ------------------------------------------------------
# Claude Code docs: "aim to keep CLAUDE.md under 200 lines".
CONTEXT_FILE_LINES = 200
# Claude Code skills docs recommendation.
SKILL_MD_LINES = 500
# Claude Code: per-skill cap when re-injected after compaction.
SKILL_TRUNCATE_TOKENS = 5000
# Claude Code: combined subagent description total that warns at startup.
AGENT_DESC_TOKENS = 15000
# Agent SDK: tool-selection accuracy degrades past roughly 30-50 loaded tools.
MCP_TOOL_CEILING = 30
# Local heuristics, not from docs.
MCP_SERVER_HINT = 5
NESTED_CONTEXT_FILE_HINT = 3
CHARS_PER_TOKEN = 4  # deliberately conservative

SIREN = "\N{POLICE CARS REVOLVING LIGHT}"

# Severity is cost-weighted: three sirens for what you pay on every single request,
# fewer for costs that are occasional or merely untidy.
SEV_HIGH = 3
SEV_MED = 2
SEV_LOW = 1

Finding = tuple[int, str]


# --- tool detection ---------------------------------------------------------
# Each tool keeps its context files, skills, and agents in different places, so the
# checks are parameterised rather than duplicated. Names of tool-specific commands
# differ too: recommending /doctor to a Codex user would just be wrong.
TOOLS = {
    "claude": {
        "label": "Claude Code",
        "home": ".claude",
        "context_files": ["CLAUDE.md"],
        "skill_dirs": ["skills"],
        "agent_dirs": ["agents"],
        "audit_hint": "Run /doctor for suggested cuts.",
        "context_cmd": "/context",
    },
    "codex": {
        "label": "Codex",
        "home": ".codex",
        "context_files": ["AGENTS.md"],
        # Codex docs list .agents/skills; the Codex repo itself ships .codex/skills.
        # Both are checked because the precedence rule is not documented.
        "skill_dirs": ["skills", "../.agents/skills"],
        "agent_dirs": [],
        "audit_hint": "Review with /hooks and /skills.",
        "context_cmd": None,
    },
    "opencode": {
        "label": "OpenCode",
        "home": ".config/opencode",
        "context_files": ["AGENTS.md"],
        "skill_dirs": ["skills"],
        "agent_dirs": ["agents"],
        "audit_hint": "Check opencode.json for unused plugins.",
        "context_cmd": None,
    },
}


def detect_tools(root: Path, home: Path) -> list[str]:
    """Which agents are in use here? Absence of evidence means we skip the check."""
    present = []
    for key, spec in TOOLS.items():
        tool_home = home / spec["home"]
        project_dir = root / f".{key}" if key != "opencode" else root / ".opencode"
        context_here = any((root / name).is_file() for name in spec["context_files"])
        if tool_home.is_dir() or project_dir.is_dir() or context_here:
            present.append(key)
    return present or ["claude"]


def sirens(level: int) -> str:
    return SIREN * level


def tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def line_count(path: Path) -> int:
    try:
        return len(path.read_text(errors="replace").splitlines())
    except OSError:
        return 0


def check_context_files(root: Path, home: Path, tool: str) -> list[Finding]:
    """The context file is the one thing loaded on *every* request."""
    spec = TOOLS[tool]
    out: list[Finding] = []
    seen: set[Path] = set()

    candidates: list[Path] = []
    for name in spec["context_files"]:
        candidates += [
            home / spec["home"] / name,
            root / name,
            root / f".{tool}" / name,
        ]

    for path in candidates:
        if not path.is_file() or path in seen:
            continue
        seen.add(path)
        lines = line_count(path)
        if lines > CONTEXT_FILE_LINES:
            scope = (
                "your global" if str(path).startswith(str(home)) else "this project's"
            )
            msg = (
                f"[{spec['label']}] {scope} {path.name} is {lines} lines (target: under "
                f"{CONTEXT_FILE_LINES}). It loads on every single request, so every line "
                "above the target is a recurring cost. Ask of each line: would removing "
                f"this cause a mistake? If not, cut it. {spec['audit_hint']}"
            )
            out.append((SEV_HIGH, msg))

    # The hierarchy is the invisible cost: several files merge silently.
    for name in spec["context_files"]:
        nested = [
            p
            for p in root.rglob(name)
            if ".git" not in p.parts and p not in seen and "node_modules" not in p.parts
        ]
        if len(nested) >= NESTED_CONTEXT_FILE_HINT:
            msg = (
                f"[{spec['label']}] {len(nested)} nested {name} files below the project "
                "root. They merge silently, so the total cost is invisible unless you "
                "audit every level. Prefer one root file with imports."
            )
            out.append((SEV_MED, msg))
    return out


def check_skills(root: Path, home: Path, tool: str) -> list[Finding]:
    """Skill bodies persist in context for the rest of the session once invoked."""
    spec = TOOLS[tool]
    out: list[Finding] = []
    bases = [home / spec["home"] / d for d in spec["skill_dirs"]]
    bases += [root / f".{tool}" / d for d in spec["skill_dirs"]]
    for base in bases:
        if not base.is_dir():
            continue
        for skill in sorted(base.glob("*/SKILL.md")):
            lines = line_count(skill)
            body = skill.read_text(errors="replace")
            if lines > SKILL_MD_LINES:
                msg = (
                    f"skill '{skill.parent.name}' is {lines} lines (target: under "
                    f"{SKILL_MD_LINES}). Once invoked, the whole body stays in context "
                    "for the rest of the session, so every line is a recurring cost. "
                    "Move detail into supporting files, or better, into scripts that "
                    "execute instead of load."
                )
                out.append((SEV_LOW, msg))
            elif tokens(body) > SKILL_TRUNCATE_TOKENS:
                msg = (
                    f"skill '{skill.parent.name}' is over roughly "
                    f"{SKILL_TRUNCATE_TOKENS} tokens, so after compaction it may be "
                    "truncated. Truncation keeps the START of the file, so put the most "
                    "important instructions near the top."
                )
                out.append((SEV_LOW, msg))
    return out


def check_agents(root: Path, home: Path, tool: str) -> list[Finding]:
    """Agent descriptions always load, because they drive delegation choices."""
    spec = TOOLS[tool]
    if not spec["agent_dirs"]:
        return []
    out: list[Finding] = []
    total = 0
    count = 0
    bases = [home / spec["home"] / d for d in spec["agent_dirs"]]
    bases += [root / f".{tool}" / d for d in spec["agent_dirs"]]
    for base in bases:
        if not base.is_dir():
            continue
        for agent in base.rglob("*.md"):
            text = agent.read_text(errors="replace")
            # The description lives in frontmatter; approximate with the header block.
            if text.startswith("---") and "---" in text[3:]:
                head = text.split("---")[1]
            else:
                head = text[:600]
            total += tokens(head)
            count += 1
    if total > AGENT_DESC_TOKENS:
        msg = (
            f"[{spec['label']}] {count} agent definitions total roughly {total:,} tokens "
            f"of frontmatter (warning threshold: {AGENT_DESC_TOKENS:,}). Descriptions "
            "load on every request, because the model needs them to choose what to "
            "delegate. Shorten the descriptions and move detail into each agent's system "
            "prompt, which loads only when that agent actually runs."
        )
        out.append((SEV_HIGH, msg))
    return out


def _mcp_servers(root: Path, home: Path, tool: str) -> dict:
    """Each tool stores MCP config differently, so normalise to a name->config dict."""
    if tool == "claude":
        cfg = root / ".mcp.json"
        if cfg.is_file():
            try:
                return json.loads(cfg.read_text()).get("mcpServers", {})
            except (json.JSONDecodeError, OSError):
                return {}
    elif tool == "opencode":
        for cfg in (root / "opencode.json", home / ".config/opencode/opencode.json"):
            if cfg.is_file():
                try:
                    return json.loads(cfg.read_text()).get("mcp", {})
                except (json.JSONDecodeError, OSError):
                    continue
    # Codex keeps MCP config in TOML. Parsing TOML needs tomllib (3.11+); count
    # [mcp_servers.*] table headers instead, which is enough for a threshold check.
    elif tool == "codex":
        found = {}
        for cfg in (home / ".codex/config.toml", root / ".codex/config.toml"):
            if not cfg.is_file():
                continue
            try:
                for line in cfg.read_text(errors="replace").splitlines():
                    stripped = line.strip()
                    if stripped.startswith("[mcp_servers."):
                        name = (
                            stripped[len("[mcp_servers.") :].rstrip("]").split(".")[0]
                        )
                        found[name] = {}
            except OSError:
                continue
        return found
    return {}


def check_mcp(root: Path, home: Path, tool: str) -> list[Finding]:
    """MCP tool schemas cost context; a CLI equivalent costs nothing."""
    spec = TOOLS[tool]
    out: list[Finding] = []
    servers = _mcp_servers(root, home, tool)
    if not servers:
        return out

    if len(servers) >= MCP_SERVER_HINT:
        where = (
            f" Check per-tool cost with {spec['context_cmd']} all."
            if spec["context_cmd"]
            else ""
        )
        msg = (
            f"[{spec['label']}] {len(servers)} MCP servers configured. Tool-selection "
            f"accuracy degrades past roughly {MCP_TOOL_CEILING} to 50 loaded tools."
            f"{where} Prefer CLI tools (gh, aws, gcloud) where possible, since they add "
            "no per-tool listing at all."
        )
        out.append((SEV_MED, msg))

    # alwaysLoad defeats deferred loading, which is the main protection here.
    eager = [
        n for n, s in servers.items() if isinstance(s, dict) and s.get("alwaysLoad")
    ]
    if eager:
        msg = (
            f"MCP server(s) {', '.join(eager)} set alwaysLoad, so their full schemas load "
            "at startup instead of on demand. That is right for a small, high-frequency "
            "toolset and wrong for a broad catalog."
        )
        out.append((SEV_MED, msg))
    return out


def audit(cwd: str, tools: list[str] | None = None) -> list[Finding]:
    root = Path(cwd)
    home = Path.home()
    active = tools or detect_tools(root, home)
    findings: list[Finding] = []
    for tool in active:
        findings += check_context_files(root, home, tool)
        findings += check_skills(root, home, tool)
        findings += check_agents(root, home, tool)
        findings += check_mcp(root, home, tool)
    # Deduplicate: shared skill dirs mean the same finding can surface twice.
    seen: set[str] = set()
    unique: list[Finding] = []
    for sev, text in findings:
        if text not in seen:
            seen.add(text)
            unique.append((sev, text))
    return unique


def main() -> None:
    # Hook mode is signalled by real JSON on stdin. Detect by parsing, not by
    # isatty(), which is unreliable under pipes and CI. Both Claude Code and Codex
    # send a `cwd` field.
    cwd = os.getcwd()
    hook_mode = False
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    # An explicit null must not replace a usable default.
                    candidate = payload.get("cwd")
                    if isinstance(candidate, str) and candidate:
                        cwd = candidate
                    hook_mode = True
            except (json.JSONDecodeError, ValueError):
                pass

    # Most urgent first, so the top line is the one worth acting on.
    findings = sorted(audit(cwd), key=lambda f: -f[0])

    if hook_mode:
        if findings:
            body = "\n".join(f"  {sirens(sev)} {text}" for sev, text in findings)
            print(json.dumps({"systemMessage": f"tutor: setup notes\n{body}"}))
        sys.exit(0)

    if not findings:
        print("tutor: no config issues found.")
        return
    print(f"tutor: {len(findings)} finding(s)\n")
    for sev, text in findings:
        print(f"  {sirens(sev)} {text}\n")


if __name__ == "__main__":
    main()
