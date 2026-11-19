"""Parse kiro-cli v3 headless output into structured telemetry.

The runner invokes `kiro-cli chat --no-interactive --agent-engine v3
--output-format stream-json ...`, which writes one ACP event per line to stdout:

  * `runStarted`            -- engine banner
  * `sessionUpdate`         -- streamed updates; the ones we use are
      - `agent_message_chunk`   assistant prose (streamed)
      - `tool_call`             a tool invocation (title, kind, rawInput, _meta)
      - `session_info_update`   with `_meta.kiro.kind == "turn_completion"`:
                                credits (`promptTurnSummaries[].usage`) and
                                `elapsedTime` (ms)
  * `runFinished`           -- `sessionId`, `status`, `stopReason`, `finalText`
                               (the full assistant text for the run)

Tool *names* are not in the stream events (titles are display strings such as
"Run Command"), so the runner also reads the run's session log,
`~/.kiro/sessions/<cwd-hash>/<sessionId>/messages.jsonl`, whose `tool_call`
payloads carry the exact `toolName` and `args`. MCP tools appear there as
`toolName == "tool_call"` with `args.tool_id == "tool::<server>::<tool>"`
(after a `load_resource` of the same id, which is plumbing and not counted).

This module extracts:
  * credits     (float)      -- platform credit cost, already rate-adjusted per model
  * time_s      (float)      -- elapsed seconds reported by the CLI
  * tool_calls  (list[str])  -- normalized tool names in call order
                               (MCP tools by their bare name, e.g. aws___search_documentation;
                                skill loads as load_resource(skill::<name>))
  * mcp_servers (list[str])  -- MCP servers that were actually called
  * answer      (str)        -- the assistant's text (what the judge grades)
  * transcript  (str)        -- answer interleaved with tool-call lines
  * errors      (list[str])  -- non-success status / error lines from stderr
  * session_id  (str)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

# Matches CSI sequences like \x1b[38;5;141m, \x1b[0m, \x1b[?25l, \x1b[1G
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# Display titles the v3 stream uses for built-in tools, mapped back to tool ids.
# Used only as a fallback when the session log is unavailable.
_TITLE_TO_TOOL = {
    "Run Command": "execute_bash",
    "Read File": "read_file",
    "List Directory": "list_directory",
    "File Search": "file_search",
    "Grep Search": "grep_search",
    "Web Search": "remote_web_search",
    "Web Fetch": "web_fetch",
    "Load Resource": "load_resource",
}

_MCP_TOOL_ID_RE = re.compile(r"^tool::([^:]+)::(.+)$")
_MCP_TITLE_RE = re.compile(r"^@([^/]+)/(.+)$")
_SKILL_TITLE_RE = re.compile(r"^Load skill:\s*(.+)$")

# stderr lines that are routine logging, not errors.
_STDERR_NOISE = ("[INFO]", "[DEBUG]", "[TRACE]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


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
    session_id: str | None = None
    status: str | None = None
    stop_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iter_events(stdout: str) -> Iterable[dict[str, Any]]:
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _normalize_tool(tool_name: str, args: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Map a session-log tool call to (normalized_name, mcp_server).

    Returns (None, None) for plumbing calls that should not be counted
    (loading an MCP tool definition before calling it)."""
    args = args or {}
    if tool_name == "tool_call":
        m = _MCP_TOOL_ID_RE.match(str(args.get("tool_id", "")))
        if m:
            return m.group(2), m.group(1)
        return "tool_call", None
    if tool_name == "load_resource":
        rid = str(args.get("id", ""))
        if rid.startswith("tool::"):
            return None, None  # tool-definition load: plumbing
        return f"load_resource({rid})" if rid else "load_resource", None
    return tool_name, None


def _tool_from_event(update: dict[str, Any]) -> tuple[str | None, str | None]:
    """Fallback normalization from a stream `tool_call` event."""
    title = str(update.get("title") or "")
    raw = update.get("rawInput") or {}
    meta = (update.get("_meta") or {}).get("kiro") or {}
    if title == "Tool Load":
        return None, None
    m = _MCP_TITLE_RE.match(title)
    if m:
        return m.group(2), meta.get("serverName") or m.group(1)
    m = _SKILL_TITLE_RE.match(title)
    if m:
        return f"load_resource(skill::{m.group(1).strip()})", None
    if title in _TITLE_TO_TOOL:
        name = _TITLE_TO_TOOL[title]
        if name == "load_resource":
            rid = str(raw.get("id", ""))
            if rid.startswith("tool::"):
                return None, None
            return f"load_resource({rid})" if rid else name, None
        return name, None
    tid = meta.get("toolId")
    if tid:
        return str(tid), None
    return title.lower().replace(" ", "_") or "unknown_tool", None


def parse_session_log(text: str) -> tuple[list[str], list[str], list[str]]:
    """Return (tool_calls, mcp_servers, transcript_lines) from messages.jsonl."""
    tool_calls: list[str] = []
    servers: list[str] = []
    lines: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            p = json.loads(line).get("payload") or {}
        except (json.JSONDecodeError, AttributeError):
            continue
        t = p.get("type")
        if t == "assistant" and p.get("operationType", "Say") == "Say":
            content = str(p.get("content") or "").strip()
            if content:
                lines.append(content)
        elif t == "tool_call":
            name, server = _normalize_tool(str(p.get("toolName") or ""), p.get("args"))
            if name is None:
                continue
            tool_calls.append(name)
            if server:
                servers.append(server)
            args = json.dumps(p.get("args") or {}, ensure_ascii=False)
            if len(args) > 300:
                args = args[:300] + "…"
            lines.append(f"[tool] {name} {args}")
    return tool_calls, sorted(set(servers)), lines


def parse_run(stdout: str, stderr: str = "", session_log: str | None = None) -> ParsedRun:
    """Parse a stream-json stdout capture (+ stderr, + optional session log)."""
    credits: float | None = None
    time_s: float | None = None
    session_id = None
    status = None
    stop_reason = None
    final_text: str | None = None
    chunks: list[str] = []
    event_tools: list[str] = []
    event_servers: list[str] = []

    for ev in _iter_events(stdout):
        et = ev.get("type")
        data = ev.get("data") or {}
        if et == "runFinished":
            session_id = data.get("sessionId") or session_id
            status = data.get("status")
            stop_reason = data.get("stopReason")
            final_text = data.get("finalText")
            continue
        if et != "sessionUpdate":
            continue
        session_id = data.get("sessionId") or session_id
        upd = data.get("update") or {}
        kind = upd.get("sessionUpdate")
        if kind == "agent_message_chunk":
            c = upd.get("content") or {}
            if c.get("type") == "text":
                chunks.append(str(c.get("text", "")))
        elif kind == "tool_call":
            name, server = _tool_from_event(upd)
            if name is not None:
                event_tools.append(name)
                if server:
                    event_servers.append(server)
        elif kind == "session_info_update":
            meta = (upd.get("_meta") or {}).get("kiro") or {}
            if meta.get("kind") == "turn_completion":
                total = 0.0
                seen = False
                for s in meta.get("promptTurnSummaries") or []:
                    try:
                        total += float(s.get("usage", 0))
                        seen = True
                    except (TypeError, ValueError):
                        pass
                if seen:
                    credits = round((credits or 0.0) + total, 6)
                if meta.get("elapsedTime") is not None:
                    try:
                        time_s = round((time_s or 0.0) + float(meta["elapsedTime"]) / 1000.0, 3)
                    except (TypeError, ValueError):
                        pass

    answer = (final_text if final_text is not None else "".join(chunks)).strip()

    # Prefer the session log for exact tool names and an ordered transcript.
    if session_log:
        tool_calls, mcp_servers, lines = parse_session_log(session_log)
        transcript = "\n".join(lines).strip() or answer
    else:
        tool_calls, mcp_servers = event_tools, sorted(set(event_servers))
        transcript = answer

    errors: list[str] = []
    if status is not None and status != "success":
        errors.append(f"status={status}" + (f" stop_reason={stop_reason}" if stop_reason else ""))
    for line in strip_ansi(stderr or "").splitlines():
        s = line.strip()
        if not s or s.startswith(_STDERR_NOISE):
            continue
        if "error" in s.lower() or "failed" in s.lower():
            errors.append(s[:300])

    return ParsedRun(
        credits=credits,
        time_s=time_s,
        tool_calls=tool_calls,
        tool_call_count=len(tool_calls),
        mcp_servers=mcp_servers,
        answer=answer,
        transcript=transcript,
        errors=errors,
        raw_len=len(stdout) + len(stderr or ""),
        session_id=session_id,
        status=status,
        stop_reason=stop_reason,
    )


# --- self-test against real captured samples (kiro-cli 3.5.0) ---------------

def _ev(update: dict[str, Any], sid: str = "sess_1") -> str:
    return json.dumps({"type": "sessionUpdate", "data": {"sessionId": sid, "update": update}})


def _turn(credits: float, elapsed_ms: int, used: list[str]) -> str:
    return _ev({"sessionUpdate": "session_info_update", "_meta": {"kiro": {
        "promptTurnSummaries": [{"unit": "credit", "unitPlural": "credits", "usage": credits, "usedTools": used}],
        "elapsedTime": elapsed_ms, "status": "success", "kind": "turn_completion"}}})


def _finished(text: str, sid: str = "sess_1", status: str = "success") -> str:
    return json.dumps({"type": "runFinished", "data": {
        "sessionId": sid, "status": status, "stopReason": "end_turn",
        "finalText": text, "finalTextTruncated": False}})


_SAMPLE_SIMPLE = "\n".join([
    json.dumps({"type": "runStarted", "data": {"payloadSchema": "acp", "acpProtocolVersion": 1, "engine": "v3"}}),
    _ev({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "p"}}),
    _ev({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ong"}}),
    _turn(0.015278539635157549, 926, []),
    _finished("pong"),
])

# Native shell tool (lift-baseline-native): title is a display string.
_SAMPLE_NATIVE = "\n".join([
    _ev({"sessionUpdate": "tool_call", "toolCallId": "tooluse_1", "title": "Run Command", "kind": "execute",
         "rawInput": {"command": "aws sts get-caller-identity"}, "_meta": {"kiro": {"toolOrigin": "default"}}}),
    _turn(0.03665, 3125, ["execute_bash"]),
    _finished("**Account ID:** `123456789012`"),
])
_LOG_NATIVE = "\n".join([
    json.dumps({"payload": {"type": "assistant", "content": "I'll run the AWS command.", "operationType": "Say"}}),
    json.dumps({"payload": {"type": "tool_call", "toolName": "execute_bash", "args": {"command": "aws sts get-caller-identity"}}}),
    json.dumps({"payload": {"type": "assistant", "content": "**Account ID:** `123456789012`", "operationType": "Say"}}),
    json.dumps({"payload": {"type": "usage_summary", "promptTurnSummaries": [{"usage": 0.03665, "usedTools": ["execute_bash"]}], "elapsedTime": 3125}}),
])

# Managed AWS MCP (lift-aws-mcp): a Tool Load (plumbing) then the real call.
_SAMPLE_MCP = "\n".join([
    _ev({"sessionUpdate": "tool_call", "title": "Tool Load", "rawInput": {"id": "tool::aws-mcp::aws___search_documentation"},
         "_meta": {"kiro": {"toolOrigin": "default"}}}),
    _ev({"sessionUpdate": "tool_call", "title": "@aws-mcp/aws___search_documentation",
         "rawInput": {"search_phrase": "Lambda maximum timeout limit"},
         "_meta": {"kiro": {"serverName": "aws-mcp", "toolOrigin": "default"}}}),
    _turn(0.066115, 5718, ["load_resource", "tool_call"]),
    _finished("The maximum timeout for AWS Lambda is 900 seconds (15 minutes)."),
])
_LOG_MCP = "\n".join([
    json.dumps({"payload": {"type": "tool_call", "toolName": "load_resource", "args": {"id": "tool::aws-mcp::aws___search_documentation"}}}),
    json.dumps({"payload": {"type": "tool_call", "toolName": "tool_call", "args": {
        "tool_id": "tool::aws-mcp::aws___search_documentation", "arguments": {"search_phrase": "Lambda maximum timeout limit"}}}}),
    json.dumps({"payload": {"type": "assistant", "content": "The maximum timeout for AWS Lambda is 900 seconds (15 minutes).", "operationType": "Say"}}),
])

# Local skill (lift-skill): loading a skill is a real (counted) augmentation use.
_SAMPLE_SKILL = "\n".join([
    _ev({"sessionUpdate": "tool_call", "title": "Load skill: aws-sdk-python-usage",
         "rawInput": {"id": "skill::aws-sdk-python-usage"},
         "_meta": {"kiro": {"toolOrigin": "default", "toolId": "disclose_context"}}}),
    _turn(0.044, 3360, ["load_resource"]),
    _finished("Use a paginator."),
])
_LOG_SKILL = json.dumps({"payload": {"type": "tool_call", "toolName": "load_resource", "args": {"id": "skill::aws-sdk-python-usage"}}})

if __name__ == "__main__":
    a = parse_run(_SAMPLE_SIMPLE)
    assert a.credits == 0.015279, a.credits
    assert a.time_s == 0.926, a.time_s
    assert a.tool_call_count == 0, a.tool_calls
    assert a.answer == "pong", repr(a.answer)
    assert a.session_id == "sess_1" and a.status == "success", (a.session_id, a.status)
    assert a.errors == [], a.errors

    # native: event-only fallback vs session log give the same tool name
    b0 = parse_run(_SAMPLE_NATIVE)
    assert b0.tool_calls == ["execute_bash"], b0.tool_calls
    b = parse_run(_SAMPLE_NATIVE, "", _LOG_NATIVE)
    assert b.tool_calls == ["execute_bash"], b.tool_calls
    assert b.credits == 0.03665 and b.time_s == 3.125, (b.credits, b.time_s)
    assert "123456789012" in b.answer and "[tool] execute_bash" in b.transcript, b.transcript
    assert "[tool]" not in b.answer

    m0 = parse_run(_SAMPLE_MCP)
    assert m0.tool_calls == ["aws___search_documentation"], m0.tool_calls
    assert m0.mcp_servers == ["aws-mcp"], m0.mcp_servers
    m = parse_run(_SAMPLE_MCP, "", _LOG_MCP)
    assert m.tool_calls == ["aws___search_documentation"], m.tool_calls  # Tool Load not counted
    assert m.mcp_servers == ["aws-mcp"], m.mcp_servers
    assert "900 seconds" in m.answer and "search_phrase" in m.transcript
    assert m.credits == 0.066115, m.credits

    s0 = parse_run(_SAMPLE_SKILL)
    assert s0.tool_calls == ["load_resource(skill::aws-sdk-python-usage)"], s0.tool_calls
    s = parse_run(_SAMPLE_SKILL, "", _LOG_SKILL)
    assert s.tool_calls == ["load_resource(skill::aws-sdk-python-usage)"], s.tool_calls

    e = parse_run(_finished("", status="error"), "Error: model unavailable\n[INFO] noise")
    assert e.status == "error" and len(e.errors) == 2, e.errors

    print("parse.py self-test OK")
    print("simple:", a.to_dict())
    print("native:", {k: b.to_dict()[k] for k in ("tool_calls", "credits", "time_s")})
    print("mcp:   ", {k: m.to_dict()[k] for k in ("tool_calls", "mcp_servers", "credits")})
    print("skill: ", {k: s.to_dict()[k] for k in ("tool_calls",)})
