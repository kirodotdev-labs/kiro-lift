"""Parse raw kiro-cli headless output into structured telemetry.

Headless (`kiro-cli chat --no-interactive ...`) writes everything to stdout,
ANSI-colored, with three kinds of content we care about:

  * boilerplate banners (checkpoints note, trust-all-tools warning)
  * the assistant transcript (prose + tool-use event lines)
  * a footer of the form `▸ Credits: <float> • Time: <Ns>`

This module strips ANSI, removes known boilerplate, and extracts:
  * credits   (float)  -- platform credit cost, already rate-adjusted per model
  * time_s    (float)  -- wall-clock seconds reported by the CLI
  * tool_calls (list[str]) -- tool names invoked (incl. MCP tools)
  * answer    (str)    -- cleaned transcript with banners/footer/tool-lines removed
  * transcript (str)   -- cleaned transcript with tool-lines kept (for the judge)
  * errors    (list[str]) -- any "Tool validation failed" / error markers
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

# Matches CSI sequences like \x1b[38;5;141m, \x1b[0m, \x1b[?25l, \x1b[1G
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_CREDITS_RE = re.compile(r"Credits:\s*([0-9]+(?:\.[0-9]+)?)")
_TIME_RE = re.compile(r"Time:\s*([0-9hms.\s]+?)(?:\n|$|\u2022|•)")
# Tool invocation markers. Built-in tools print "(using tool: <name> ...)".
# MCP tools print "Running tool <name> with the param (from mcp server: <srv>)".
_TOOL_USING_RE = re.compile(r"using tool:\s*([A-Za-z0-9_.@/\-]+)")
_TOOL_RUNNING_RE = re.compile(r"Running tool\s+([A-Za-z0-9_.@/\-]+)\s+with the param")
_MCP_SERVER_RE = re.compile(r"from mcp server:\s*([A-Za-z0-9_.@/\-]+)")
_FOOTER_RE = re.compile(r"\u25b8?\s*Credits:.*?Time:[^\n]*")

# Lines that are pure boilerplate and should never reach the judge.
_BOILERPLATE_SUBSTRINGS = (
    "Checkpoints are not available",
    "Use '/checkpoint init'",
    "All tools are now trusted",
    "Agents can sometimes do unexpected things",
    "Learn more at",
    "kiro.dev/docs/cli/chat/security",
)

_ERROR_MARKERS = (
    "Tool validation failed",
    "Failed to validate tool parameters",
    "Amazon Q is having trouble",
    "Error:",
)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _parse_time_to_seconds(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    total = 0.0
    found = False
    for value, unit in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*([hms])", raw):
        found = True
        v = float(value)
        if unit == "h":
            total += v * 3600
        elif unit == "m":
            total += v * 60
        else:
            total += v
    if found:
        return total
    # bare number -> seconds
    try:
        return float(raw)
    except ValueError:
        return None


@dataclass
class ParsedRun:
    credits: float | None = None
    time_s: float | None = None
    tool_calls: list[str] = field(default_factory=list)
    tool_call_count: int = 0
    mcp_servers: list[str] = field(default_factory=list)
    answer: str = ""
    transcript: str = ""
    errors: list[str] = field(default_factory=list)
    raw_len: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_run(raw: str) -> ParsedRun:
    """Parse a raw headless stdout(+stderr) capture into telemetry."""
    clean = strip_ansi(raw)

    credits = None
    m = _CREDITS_RE.search(clean)
    if m:
        credits = float(m.group(1))

    time_s = None
    m = _TIME_RE.search(clean)
    if m:
        time_s = _parse_time_to_seconds(m.group(1))

    tool_calls = _TOOL_USING_RE.findall(clean) + _TOOL_RUNNING_RE.findall(clean)
    mcp_servers = _MCP_SERVER_RE.findall(clean)

    errors = [marker for marker in _ERROR_MARKERS if marker in clean]

    # Build cleaned transcript: drop boilerplate + footer lines.
    transcript_lines: list[str] = []
    answer_lines: list[str] = []
    for line in clean.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(b in stripped for b in _BOILERPLATE_SUBSTRINGS):
            continue
        if _FOOTER_RE.search(stripped):
            continue
        # Normalize the leading prompt marker "> " the CLI prints before prose.
        prose = stripped[2:].strip() if stripped.startswith("> ") else stripped
        transcript_lines.append(prose)
        # answer = prose only (exclude tool-event lines)
        is_tool_line = (
            "using tool:" in stripped
            or stripped.startswith("Running tool ")
            or "from mcp server:" in stripped
            or stripped.startswith("⋮")
            or stripped.startswith("✓")
            or stripped.startswith("- Completed in")
            or stripped.startswith("Reading directory:")
            or stripped.startswith("Reading file:")
        )
        if not is_tool_line:
            answer_lines.append(prose)

    return ParsedRun(
        credits=credits,
        time_s=time_s,
        tool_calls=tool_calls,
        tool_call_count=len(tool_calls),
        mcp_servers=sorted(set(mcp_servers)),
        answer="\n".join(answer_lines).strip(),
        transcript="\n".join(transcript_lines).strip(),
        errors=errors,
        raw_len=len(raw),
    )


# --- self-test against real captured samples -------------------------------
_SAMPLE_SIMPLE = (
    "\x1b[38;5;12mCheckpoints are not available in this directory. Use '/checkpoint init' to enable checkpoints.\n\n"
    "\x1b[39m\n\x1b[38;5;252m\x1b[0m\x1b[?25l\x1b[38;5;141m> \x1b[0mpong\x1b[0m\x1b[0m\n"
    "\x1b[38;5;8m\n \u25b8 Credits: 0.22 • Time: 1s\n\n\x1b[0m\x1b[1G\x1b[0m\x1b[0m\x1b[?25h"
)

_SAMPLE_TOOL = (
    "\x1b[32mAll tools are now trusted (\x1b[0m\x1b[31m!\x1b[0m\x1b[32m).\x1b[0m\n"
    "\x1b[38;5;12mCheckpoints are not available in this directory.\x1b[39m\n"
    "\x1b[1mTool validation failed: \x1b[0m\n"
    "\x1b[38;5;9mFailed to validate tool parameters: '/private/tmp/note.txt' does not exist\n"
    "\x1b[0m\x1b[38;5;141m> \x1b[0mThe file note.txt does not exist.\x1b[0m\n"
    "Reading directory: \x1b[38;5;141m/private/tmp\x1b[0m\x1b[38;5;244m (using tool: read, max depth: 0)\x1b[0m\n"
    "\x1b[38;5;10m ✓ \x1b[0mSuccessfully read directory /private/tmp (28 entries)\n"
    "\x1b[38;5;141m> \x1b[0mThere is no note.txt file.\x1b[0m\n"
    "\x1b[38;5;8m\n \u25b8 Credits: 0.52 • Time: 15s\n\n\x1b[0m"
)

# Real capture: an MCP tool call uses a different marker than built-in tools.
_SAMPLE_MCP = (
    "\x1b[38;5;141m> \x1b[0mI'll search the AWS Lambda documentation for the current limits.\n"
    "Running tool search_documentation with the param \x1b[38;5;244m(from mcp server: aws-docs)\x1b[0m\n"
    "⋮  {\n"
    "⋮    \"search_phrase\": \"AWS Lambda limits maximum memory timeout ephemeral storage\",\n"
    "⋮    \"limit\": 5\n"
    "⋮  }\n"
    "\x1b[38;5;141m> \x1b[0mAWS Lambda limits: memory up to 10,240 MB, timeout up to 900 seconds, /tmp up to 10,240 MB.\n"
    "\x1b[38;5;8m\n \u25b8 Credits: 0.06 • Time: 7s\n\n\x1b[0m"
)

# Real capture: the managed AWS MCP Server emits aws___-prefixed tool names via
# the same "Running tool ..." marker, from mcp server "aws-mcp".
_SAMPLE_MANAGED = (
    "\x1b[38;5;141m> \x1b[0mLet me check the role's permissions.\n"
    "Running tool aws___call_aws with the param \x1b[38;5;244m(from mcp server: aws-mcp)\x1b[0m\n"
    "⋮  {\n"
    "⋮    \"cli_command\": \"aws iam simulate-principal-policy ...\"\n"
    "⋮  }\n"
    "\x1b[38;5;141m> \x1b[0mYes, the role is allowed to perform s3:GetObject.\n"
    "\x1b[38;5;8m\n \u25b8 Credits: 0.11 • Time: 9s\n\n\x1b[0m"
)

if __name__ == "__main__":
    a = parse_run(_SAMPLE_SIMPLE)
    assert a.credits == 0.22, a.credits
    assert a.time_s == 1.0, a.time_s
    assert a.tool_call_count == 0, a.tool_calls
    assert a.answer == "pong", repr(a.answer)
    assert a.errors == [], a.errors

    b = parse_run(_SAMPLE_TOOL)
    assert b.credits == 0.52, b.credits
    assert b.time_s == 15.0, b.time_s
    assert b.tool_calls == ["read"], b.tool_calls
    assert "Tool validation failed" in b.errors, b.errors
    assert "note.txt does not exist" in b.answer
    assert "using tool" not in b.answer  # tool lines excluded from answer

    m = parse_run(_SAMPLE_MCP)
    assert m.tool_calls == ["search_documentation"], m.tool_calls
    assert m.mcp_servers == ["aws-docs"], m.mcp_servers
    assert "Running tool" not in m.answer and "search_phrase" not in m.answer, m.answer
    assert "10,240 MB" in m.answer, m.answer
    assert m.credits == 0.06, m.credits

    g = parse_run(_SAMPLE_MANAGED)
    assert g.tool_calls == ["aws___call_aws"], g.tool_calls
    assert g.mcp_servers == ["aws-mcp"], g.mcp_servers
    assert "Running tool" not in g.answer and "cli_command" not in g.answer, g.answer
    assert "s3:GetObject" in g.answer, g.answer
    assert g.credits == 0.11, g.credits

    print("parse.py self-test OK")
    print("simple:", a.to_dict())
    print("tool:  ", b.to_dict())
    print("mcp:   ", {k: m.to_dict()[k] for k in ("tool_calls", "mcp_servers", "credits", "answer")})
    print("managed:", {k: g.to_dict()[k] for k in ("tool_calls", "mcp_servers", "credits")})
