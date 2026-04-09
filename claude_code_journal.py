#!/usr/bin/env python3
"""claude_code_journal.py — end-of-day local journal of Claude Code activity.

Scans ~/.claude/projects/*/*.jsonl for sessions modified in the last 24 hours,
asks `claude -p` to produce a topic-level synthesis (no credentials, no
verbatim quotes, no personal info), and writes the result to a timestamped
markdown file in ~/claude-code-journal/.

Privacy properties:
  - The output stays ON YOUR MACHINE. Never emailed, never uploaded.
  - The only network call is to `claude -p` (your Max subscription / OAuth),
    same trust boundary you already accepted for normal Claude Code use.
  - The prompt has hard rules against leaking credentials, personal data,
    file contents, or verbatim user messages into the summary.

Scheduled by launchd to run at 23:59 daily
(see the launchd plist template in this repo).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# --- config -----------------------------------------------------------------

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
JOURNAL_DIR = Path.home() / "claude-code-journal"
LOG_DIR = JOURNAL_DIR / "logs"

MODEL = "claude-opus-4-6"  # via Max OAuth through `claude -p`
CLAUDE_CLI_TIMEOUT = 300   # seconds

MAX_CHARS_PER_SESSION = 4000   # cap per-session content fed to LLM
MAX_TOTAL_CHARS = 40000        # hard ceiling on total input across all sessions
SNIPPET_MAX_CHARS = 500        # per-message snippet cap
LOOKBACK_HOURS = 24


# --- utilities --------------------------------------------------------------

def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] journal · {msg}"
    print(line)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{datetime.now():%Y-%m}.log"
    with open(log_file, "a") as f:
        f.write(line + "\n")


def _decode_project_dir(encoded: str) -> str:
    """Claude Code encodes project paths with dashes: '-Users-you-myproject' → '/Users/you/myproject'."""
    if encoded == "-":
        return "(root / no project)"
    if encoded.startswith("-"):
        return "/" + encoded[1:].replace("-", "/")
    return encoded


def _extract_user_text_from_line(obj: dict) -> str:
    """Pull the user's actual text from a jsonl message line, filtering
    system reminders, tool results, and command caveats (all noise)."""
    if obj.get("type") != "user":
        return ""
    msg = obj.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        text = "\n".join(parts)
    else:
        return ""
    if not text or len(text.strip()) < 3:
        return ""
    if "<system-reminder>" in text:
        return ""
    if "<local-command-caveat>" in text:
        return ""
    if text.strip().startswith("<command-"):
        return ""
    return text.strip()


# --- session scanning -------------------------------------------------------

def scan_recent_sessions(hours: int = LOOKBACK_HOURS) -> list[dict]:
    """Return a list of sessions (across all projects) modified in the last
    N hours. Each session has: project, session_id, mtime, user_messages[]."""
    if not CLAUDE_PROJECTS_DIR.exists():
        return []
    cutoff = datetime.now().timestamp() - hours * 3600
    sessions = []
    for proj_dir in sorted(CLAUDE_PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        project_name = _decode_project_dir(proj_dir.name)
        for jsonl_file in proj_dir.glob("*.jsonl"):
            try:
                mtime = jsonl_file.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            user_messages: list[str] = []
            try:
                with open(jsonl_file, errors="ignore") as f:
                    for line in f:
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        text = _extract_user_text_from_line(obj)
                        if text:
                            user_messages.append(text)
            except OSError:
                continue
            if not user_messages:
                continue
            sessions.append({
                "project": project_name,
                "session_id": jsonl_file.stem,
                "mtime": datetime.fromtimestamp(mtime),
                "user_messages": user_messages,
            })
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    return sessions


# --- prompt building --------------------------------------------------------

def build_prompt(sessions: list[dict]) -> str:
    by_project: dict[str, list[dict]] = {}
    for s in sessions:
        by_project.setdefault(s["project"], []).append(s)

    total_chars = 0
    lines: list[str] = []
    for project, project_sessions in by_project.items():
        lines.append(f"\n### PROJECT: {project}")
        for s in project_sessions:
            header = (
                f"  Session {s['session_id'][:8]} · "
                f"{s['mtime']:%Y-%m-%d %H:%M} · "
                f"{len(s['user_messages'])} user messages"
            )
            lines.append(header)
            session_chars = 0
            for i, msg in enumerate(s["user_messages"]):
                if session_chars >= MAX_CHARS_PER_SESSION:
                    lines.append(f"    [... {len(s['user_messages']) - i} more messages truncated ...]")
                    break
                snippet = msg[:SNIPPET_MAX_CHARS].replace("\n", " ")
                lines.append(f"    [{i+1}] {snippet}")
                session_chars += len(snippet)
                total_chars += len(snippet)
                if total_chars > MAX_TOTAL_CHARS:
                    lines.append("\n[global cap reached, further sessions truncated]")
                    break
            if total_chars > MAX_TOTAL_CHARS:
                break
        if total_chars > MAX_TOTAL_CHARS:
            break

    sessions_block = "\n".join(lines)
    today_date = datetime.now().strftime("%A, %B %-d, %Y")

    return f"""You are writing a private end-of-day journal entry for {today_date}, summarizing what the user worked on in Claude Code today. This is for the user's own records — it stays on their machine, never emailed.

## SECURITY — HARD RULES (violating these is a critical failure)

- **NEVER include credentials, API keys, tokens, passwords, private keys, mnemonics, signatures, or hashes in your output.** If you see any in the input, ignore that content entirely.
- **NEVER include personal information** (email addresses, phone numbers, physical addresses, legal names, SSNs, bank/account numbers) that appears in the session content.
- **NEVER quote user messages verbatim.** Always summarize at the topic/intent level. No direct quotes.
- **NEVER include file contents, code snippets, commands, or config values from the input** in your output. Describe what was worked on, not how.
- **NEVER reveal URLs from the input** unless they are clearly public reference URLs (GitHub repos, standard docs sites). Prefer summarizing the destination.
- If a session contains content that can't be safely summarized without leaking, omit it silently.

## Output format — markdown journal entry

Write a reflective but factual journal entry for today. Structure:

```
## Overview

1-3 sentences describing the overall shape of the day's work — what was the theme, what got shipped, what was explored.

## Projects

- **<project-name>**: 1-3 sentences on what was done in this project today. Focus on decisions, what was built/debugged/designed, and any notable outcomes.
- **<project-name>**: ...

## Loose Ends

Optional section — only include if relevant. 1-3 bullets on unfinished work, unresolved questions, or things the user explicitly said they wanted to come back to.

## Tomorrow's Considerations

Optional section — only include if the user hinted at what's next. 1-3 bullets.
```

Tone: plain, factual, written to the user's future self. No hype words ("amazing", "successful"). No filler ("Today was productive..."). Just what happened.

Length: typically 150-400 words total. Scale to the amount of actual work — a quiet day is a 3-line Overview and 2 project bullets. A heavy day is fuller but still tight.

Output ONLY the markdown. No preamble, no meta-commentary, no "Here's your journal entry for today". Start with `## Overview`.

If there are fewer than 2 meaningful projects worth logging, write:

```
## Overview

_No substantive Claude Code activity today._
```

and nothing else.

## Sessions ({len(sessions)} total)

{sessions_block}
"""


# --- main -------------------------------------------------------------------

def main() -> int:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log("=" * 50)
    log("journal run start")

    sessions = scan_recent_sessions(hours=LOOKBACK_HOURS)
    if not sessions:
        log("no sessions in last 24h — writing empty journal entry")
        projects_count = 0
        body = "## Overview\n\n_No Claude Code activity in the last 24 hours._\n"
    else:
        project_set = {s["project"] for s in sessions}
        projects_count = len(project_set)
        log(f"found {len(sessions)} sessions across {projects_count} projects")

        prompt = build_prompt(sessions)
        (JOURNAL_DIR / "last-prompt.md").write_text(prompt)

        start = datetime.now()
        try:
            result = subprocess.run(
                [
                    "claude", "-p",
                    "--model", MODEL,
                    "--allowedTools", "",  # no tools — pure summarization
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=CLAUDE_CLI_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            log(f"ERROR: claude -p timed out after {CLAUDE_CLI_TIMEOUT}s")
            return 1

        dur = (datetime.now() - start).total_seconds()

        if result.returncode != 0:
            err = (result.stderr or "").strip()[:500]
            log(f"ERROR: claude -p exited {result.returncode}: {err or '<no stderr>'}")
            return 2

        body = result.stdout.strip()
        # Drop any preamble before the first `## ` heading as a safety net
        idx = body.find("## ")
        if idx > 0:
            body = body[idx:]
        log(f"generated {len(body)} chars in {dur:.1f}s")

    # Build final markdown file with proper timestamped header
    now = datetime.now()
    tz_name = now.astimezone().tzname() or ""
    file_date = now.strftime("%Y-%m-%d")
    display_date = now.strftime("%A, %B %-d, %Y")
    generated_at = now.strftime("%Y-%m-%d %H:%M:%S ").rstrip() + (f" {tz_name}" if tz_name else "")

    session_metadata = ""
    if sessions:
        by_project: dict[str, list[dict]] = {}
        for s in sessions:
            by_project.setdefault(s["project"], []).append(s)
        lines = []
        for proj in sorted(by_project.keys()):
            ps = by_project[proj]
            total_msgs = sum(len(s["user_messages"]) for s in ps)
            lines.append(f"- `{proj}` — {len(ps)} session(s), {total_msgs} user message(s)")
        session_metadata = "\n".join(lines)

    full_content = (
        f"# Claude Code Journal — {display_date}\n"
        f"\n"
        f"_Generated: {generated_at}_  \n"
        f"_Sessions scanned: {len(sessions)} across {projects_count} project(s)_  \n"
        f"_Model: {MODEL} via `claude -p`_\n"
        f"\n"
        f"---\n"
        f"\n"
        f"{body}\n"
    )
    if session_metadata:
        full_content += (
            f"\n"
            f"---\n"
            f"\n"
            f"## Session Metadata\n"
            f"\n"
            f"_(For audit/debug — what the journal scanned, not what it contains)_\n"
            f"\n"
            f"{session_metadata}\n"
        )

    # Write to dated file. If the file already exists (re-run on same day),
    # overwrite it — the run is deterministic-ish and we want latest-wins.
    out_file = JOURNAL_DIR / f"{file_date}.md"
    out_file.write_text(full_content)
    log(f"wrote {out_file}")
    log("journal run end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
