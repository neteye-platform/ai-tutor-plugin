#!/usr/bin/env python3
"""Content-aware advice on context files, skills, and agent definitions.

`lint.py` measures size. This reads what is actually written and names specific lines
worth changing, because "your file is too long" tells you nothing about which parts to
cut.

Every rule here is mechanical: a pattern in the text, not a judgement about meaning.
Anything requiring real judgement is left to the `/tutor` skill, where a model can read
the file properly. The aim is advice specific enough to act on without a second opinion.

Run directly:  python3 advise.py [path]
"""

import re
import sys
from pathlib import Path

# --- patterns ---------------------------------------------------------------
# Things the model does anyway. Telling it again spends context on nothing.
OBVIOUS = re.compile(
    r"^\s*[-*]?\s*(write clean|write good|use best practice|be careful|"
    r"don'?t break|make sure (the )?code works|follow (the )?conventions?|"
    r"write readable|keep it simple|use meaningful names|add comments where)",
    re.IGNORECASE,
)

# Rules a hook enforces deterministically, so they do not belong in prose.
HOOKABLE = re.compile(
    r"\b(always|never|every time|after (each|every)|before (each|every))\b.{0,60}"
    r"\b(run|format|lint|test|build|commit|prettier|eslint|ruff|black|gofmt)\b",
    re.IGNORECASE,
)

# Emphasis that has stopped being emphasis.
SHOUTING = re.compile(r"\b(IMPORTANT|CRITICAL|MUST|ALWAYS|NEVER|DO NOT)\b")

# Content the model can read from the repo on demand.
DERIVABLE = re.compile(
    r"^\s*[-*]?\s*(this (project|repo|file)|the \S+ (folder|directory|file) contains|"
    r"we use (react|vue|django|flask|express|next|rails|spring)|"
    r"the (stack|dependencies|structure) (is|are|includes))",
    re.IGNORECASE,
)

# A skill step written as a one-off instruction. Skill bodies persist for the whole
# session, so "now do X" reads oddly ten turns later.
ONE_SHOT_STEP = re.compile(
    r"^\s*(\d+[.)]\s*)?(now|next|then|first|finally|after (this|that))\b", re.IGNORECASE
)

CODE_FENCE = re.compile(r"^\s*```")


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()
    except OSError:
        return []


def _outside_code(lines: list[str]):
    """Yield (line_number, text) skipping fenced code, where prose rules do not apply."""
    in_fence = False
    for i, line in enumerate(lines, 1):
        if CODE_FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield i, line


def advise_context_file(path: Path) -> list[str]:
    """Specific, quotable advice about a CLAUDE.md or AGENTS.md."""
    lines = _lines(path)
    if not lines:
        return []
    out: list[str] = []
    name = path.name

    obvious: list[int] = []
    hookable: list[tuple[int, str]] = []
    derivable: list[int] = []
    shouting: list[int] = []

    for num, text in _outside_code(lines):
        if OBVIOUS.match(text):
            obvious.append(num)
        if HOOKABLE.search(text):
            hookable.append((num, text.strip()[:70]))
        if DERIVABLE.match(text):
            derivable.append(num)
        if SHOUTING.search(text):
            shouting.append(num)

    if obvious:
        where = ", ".join(f"L{n}" for n in obvious[:4])
        out.append(
            f"{name}: {len(obvious)} line(s) restate things the model already does "
            f"({where}). Every line here loads on every request, so instructions like "
            f"'write clean code' cost tokens without changing behaviour. Delete them and "
            f"keep only what is specific to this repo."
        )

    if hookable:
        num, sample = hookable[0]
        out.append(
            f'{name} L{num} reads like a rule a hook should enforce: "{sample}". '
            f"Written as prose it is advisory, and competes for attention with "
            f"everything else. As a PostToolUse hook it is deterministic and costs zero "
            f"context. That is the single highest-leverage move available here."
        )

    if derivable:
        where = ", ".join(f"L{n}" for n in derivable[:4])
        out.append(
            f"{name}: {len(derivable)} line(s) describe things readable from the repo "
            f"itself ({where}). Structure and stack can be discovered on demand; this "
            f"file should hold only what changes how the agent operates. Move that "
            f"material to your README, which the model can read when it matters."
        )

    if len(shouting) > 1:
        where = ", ".join(f"L{n}" for n in shouting[:5])
        out.append(
            f"{name}: emphasis markers on {len(shouting)} lines ({where}). Emphasis "
            f"everywhere is emphasis nowhere. Keep it on the one line that genuinely "
            f"matters most and remove the rest."
        )

    # Structural check: a long file with no headings is hard to attend to selectively.
    if len(lines) > 120 and sum(1 for line in lines if line.startswith("#")) < 3:
        out.append(
            f"{name} is {len(lines)} lines with almost no headings. Sections let the "
            f"model locate the relevant rule instead of weighing all of it equally, and "
            f"they make it obvious to you which parts have grown stale."
        )

    return out


def advise_skill(path: Path) -> list[str]:
    """Advice about a SKILL.md body."""
    lines = _lines(path)
    if not lines:
        return []
    out: list[str] = []
    name = path.parent.name

    steps = [num for num, text in _outside_code(lines) if ONE_SHOT_STEP.match(text)]
    if len(steps) >= 4:
        where = ", ".join(f"L{n}" for n in steps[:4])
        out.append(
            f"skill '{name}': {len(steps)} lines are written as sequential one-off steps "
            f"({where}). A skill body stays in context for the rest of the session, so "
            f"'now do step 3' still sits there long after step 3 is done. Write standing "
            f"instructions describing how to do the thing, not a script to walk through."
        )

    # A skill that bundles a long reference section is paying for it on every later turn.
    body = "\n".join(lines)
    fenced = body.count("```") // 2
    if len(lines) > 200 and fenced >= 4:
        out.append(
            f"skill '{name}' is {len(lines)} lines with {fenced} code blocks. Long "
            f"reference material costs its full length on every turn after invocation. "
            f"Move the examples into a supporting file the model reads only when needed, "
            f"or into a script that executes instead of loading."
        )

    # Only worth mentioning on files big enough to be truncated. Every skill opens with
    # YAML frontmatter, so checking the literal first lines would fire on all of them.
    if len(lines) > 120:
        after_frontmatter = lines
        if lines and lines[0].strip() == "---":
            for idx in range(1, len(lines)):
                if lines[idx].strip() == "---":
                    after_frontmatter = lines[idx + 1 :]
                    break
        head = [line for line in after_frontmatter[:12] if line.strip()]
        if head and not any(line.startswith("#") for line in head):
            out.append(
                f"skill '{name}' has no heading in its first lines. Truncation after "
                f"compaction keeps the START of the body, so the opening should carry the "
                f"most important instructions rather than preamble."
            )

    return out


def advise_agent(path: Path) -> list[str]:
    """Advice about a subagent definition."""
    # Join with newlines: the `^description:` matcher below is multiline, so
    # concatenating without separators would make it match nothing.
    text = "\n".join(_lines(path)[:40])
    out: list[str] = []
    if not text.startswith("---"):
        return out

    # A long description is the expensive part: descriptions load on every request.
    match = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
    if match and len(match.group(1)) > 300:
        out.append(
            f"agent '{path.stem}': the description is {len(match.group(1))} characters. "
            f"Descriptions load on every single request, because the model needs them to "
            f"choose what to delegate. Keep it to one sentence about when to use this "
            f"agent, and move the detail into the body, which loads only when it runs."
        )

    if "tools:" not in text:
        # Tagged so the caller can group these: twenty copies of the same sentence
        # crowds out more varied advice.
        out.append(f"@unrestricted-tools:{path.stem}")
    return out


def collect(root: Path, home: Path) -> list[str]:
    out: list[str] = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        for candidate in (root / name, home / ".claude" / name, home / ".codex" / name):
            if candidate.is_file():
                out += advise_context_file(candidate)

    seen_skills: set[Path] = set()
    for base in (
        home / ".claude" / "skills",
        root / ".claude" / "skills",
        home / ".config" / "opencode" / "skills",
    ):
        if not base.is_dir():
            continue
        for skill in sorted(base.glob("*/SKILL.md")):
            if skill in seen_skills:
                continue
            seen_skills.add(skill)
            out += advise_skill(skill)

    for base in (home / ".claude" / "agents", root / ".claude" / "agents"):
        if base.is_dir():
            for agent in sorted(base.rglob("*.md"))[:200]:
                out += advise_agent(agent)

    return _group(out)


def _group(findings: list[str]) -> list[str]:
    """Collapse repeated findings into one line, so variety survives the cut."""
    unrestricted = [
        f.split(":", 1)[1] for f in findings if f.startswith("@unrestricted-tools:")
    ]
    rest = [f for f in findings if not f.startswith("@")]

    if unrestricted:
        names = ", ".join(sorted(unrestricted)[:3])
        more = f" and {len(unrestricted) - 3} others" if len(unrestricted) > 3 else ""
        rest.append(
            f"{len(unrestricted)} agent definitions do not restrict `tools:` ({names}"
            f"{more}), so each inherits every tool available. Restricting them is worth "
            f"doing for two reasons: a read-only reviewer cannot 'helpfully' edit while "
            f"you are reading it, and a narrower tool set means a smaller prompt for that "
            f"agent."
        )
    return rest


# A wall of advice is as useless as none: nobody acts on fifty items. Show the most
# valuable few and say how many were held back.
MAX_SHOWN = 8


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    findings = collect(root, Path.home())
    if not findings:
        print("tutor: nothing specific to suggest. Your files read cleanly.")
        return

    shown = findings[:MAX_SHOWN]
    print(f"tutor: {len(shown)} suggestion(s)\n")
    for item in shown:
        print(f"  - {item}\n")
    if len(findings) > MAX_SHOWN:
        print(
            f"  ({len(findings) - MAX_SHOWN} more suppressed. Fix these first, then "
            f"re-run; a list nobody finishes teaches nothing.)"
        )


if __name__ == "__main__":
    main()
