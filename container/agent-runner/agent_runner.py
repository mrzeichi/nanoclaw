#!/usr/bin/env python3
"""
NanoClaw Agent Runner (Python)

Runs inside a Docker container.
Reads ContainerInput JSON from stdin, runs a Claude agent loop with tools,
and writes ContainerOutput JSON to stdout wrapped in sentinel markers.

Input protocol  (stdin):
  Full ContainerInput JSON (read until EOF)

IPC protocol (follow-up messages):
  Files written as {type:"message",text:"..."}.json in /workspace/ipc/input/
  Sentinel: /workspace/ipc/input/_close — signals session end

Output protocol (stdout):
  Each result is wrapped in OUTPUT_START/OUTPUT_END marker pairs.
  Multiple result blocks may be emitted (one per agent response).
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

try:
    import anthropic
except ImportError:
    print(
        "[agent-runner] ERROR: anthropic package not installed. Run: pip install anthropic",
        file=sys.stderr,
    )
    sys.exit(1)

from tools import TOOL_DEFINITIONS, execute_tool

# ---------------------------------------------------------------------------
# Protocol constants (must match host container_runner)
# ---------------------------------------------------------------------------

OUTPUT_START = "---NANOCLAW_OUTPUT_START---"
OUTPUT_END = "---NANOCLAW_OUTPUT_END---"

IPC_INPUT_DIR = Path("/workspace/ipc/input")
IPC_CLOSE_SENTINEL = IPC_INPUT_DIR / "_close"
IPC_POLL_S = 0.5

SESSIONS_DIR = Path("/workspace/group/.nanoclaw_sessions")

DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-5")
MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "8192"))
MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "50"))

SYSTEM_PROMPT_BASE = """\
You are a helpful AI assistant with access to tools for running shell commands, \
managing files, browsing the web, sending messages to users, and scheduling tasks.

Work within /workspace/group as your primary directory unless a path is absolute.
When asked to do something, use the available tools to accomplish it directly.
Be concise and efficient. Use tools proactively rather than asking for permission.\
"""

# ---------------------------------------------------------------------------
# Logging and output
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[agent-runner] {msg}", file=sys.stderr, flush=True)


def write_output(
    status: str,
    result: Optional[str] = None,
    new_session_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    output: dict[str, Any] = {"status": status, "result": result}
    if new_session_id:
        output["newSessionId"] = new_session_id
    if error:
        output["error"] = error
    print(OUTPUT_START, flush=True)
    print(json.dumps(output), flush=True)
    print(OUTPUT_END, flush=True)


# ---------------------------------------------------------------------------
# IPC helpers
# ---------------------------------------------------------------------------


def should_close() -> bool:
    if IPC_CLOSE_SENTINEL.exists():
        try:
            IPC_CLOSE_SENTINEL.unlink(missing_ok=True)
        except Exception:
            pass
        return True
    return False


def drain_ipc_input() -> list[str]:
    """Consume and return all pending IPC message files."""
    IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    messages: list[str] = []
    for f in sorted(IPC_INPUT_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            f.unlink(missing_ok=True)
            if data.get("type") == "message" and data.get("text"):
                messages.append(data["text"])
        except Exception as exc:
            log(f"IPC drain error for {f.name}: {exc}")
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
    return messages


def wait_for_ipc_message() -> Optional[str]:
    """Block until a message or _close sentinel arrives. Returns None on close."""
    while True:
        if should_close():
            return None
        msgs = drain_ipc_input()
        if msgs:
            return "\n".join(msgs)
        time.sleep(IPC_POLL_S)


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


def _content_to_dict(content: Any) -> list[dict]:
    """Convert SDK content blocks to plain JSON-serialisable dicts."""
    if isinstance(content, list):
        result = []
        for block in content:
            if isinstance(block, dict):
                result.append(block)
            elif hasattr(block, "type"):
                if block.type == "text":
                    result.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    result.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )
                else:
                    result.append({"type": block.type})
            else:
                result.append({"type": "unknown"})
        return result
    return []


def load_session(session_id: str) -> list[dict]:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session_file = SESSIONS_DIR / f"{session_id}.json"
    if session_file.exists():
        try:
            return json.loads(session_file.read_text())
        except Exception as exc:
            log(f"Failed to load session {session_id}: {exc}")
    return []


def save_session(session_id: str, messages: list[dict]) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        (SESSIONS_DIR / f"{session_id}.json").write_text(json.dumps(messages))
    except Exception as exc:
        log(f"Failed to save session {session_id}: {exc}")


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def build_system_prompt(container_input: dict) -> str:
    parts = [SYSTEM_PROMPT_BASE]

    # Global CLAUDE.md (shared memory, non-main groups only)
    if not container_input.get("isMain"):
        global_md = Path("/workspace/global/CLAUDE.md")
        if global_md.exists():
            content = global_md.read_text(errors="replace").strip()
            if content:
                parts.append(f"\n## Global Context\n{content}")

    # Group-level CLAUDE.md
    group_md = Path("/workspace/group/CLAUDE.md")
    if group_md.exists():
        content = group_md.read_text(errors="replace").strip()
        if content:
            parts.append(f"\n## Group Memory\n{content}")

    # Load additional directories CLAUDE.md (same as TypeScript version)
    extra_base = Path("/workspace/extra")
    if extra_base.exists():
        for extra_dir in sorted(extra_base.iterdir()):
            if extra_dir.is_dir():
                extra_md = extra_dir / "CLAUDE.md"
                if extra_md.exists():
                    content = extra_md.read_text(errors="replace").strip()
                    if content:
                        parts.append(f"\n## Extra Context ({extra_dir.name})\n{content}")

    assistant_name = container_input.get("assistantName") or "Assistant"
    parts.append(f"\nYour name is {assistant_name}.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Agent turn loop
# ---------------------------------------------------------------------------


def run_agent_turn(
    prompt: str,
    messages: list[dict],
    client: anthropic.Anthropic,
    system_prompt: str,
    container_input: dict,
    session_id: str,
) -> tuple[list[dict], bool]:
    """
    Push a user message and run the tool-use loop until the model stops.

    Returns (updated_messages, closed_during_turn).
    Emits ContainerOutput via write_output() as results arrive.
    """
    messages.append({"role": "user", "content": prompt})

    closed_during_turn = False
    turn_count = 0

    while turn_count < MAX_TURNS:
        turn_count += 1

        # Poll IPC between API calls
        if should_close():
            log("Close sentinel detected during tool loop")
            closed_during_turn = True
            break

        # Drain any queued IPC messages and append to history mid-turn
        pending = drain_ipc_input()
        if pending:
            log(f"Piping {len(pending)} IPC message(s) into active turn")
            for p in pending:
                messages.append({"role": "user", "content": p})

        log(f"API call (turn {turn_count}, {len(messages)} messages in history)")

        try:
            response = client.messages.create(
                model=DEFAULT_MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )
        except Exception as exc:
            log(f"Anthropic API error: {exc}")
            raise

        # Collect response blocks
        text_parts: list[str] = []
        tool_uses: list = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_uses.append(block)

        # Emit text output to host immediately
        if text_parts:
            text = "\n".join(text_parts).strip()
            if text:
                log(f"Result: {text[:200]}")
                write_output("success", text, session_id)

        # Store assistant message (convert blocks to plain dicts for JSON storage)
        messages.append(
            {"role": "assistant", "content": _content_to_dict(response.content)}
        )

        if response.stop_reason == "end_turn" or not tool_uses:
            log(f"Turn complete (stop_reason={response.stop_reason})")
            break

        # Execute all tool uses and collect results
        tool_results: list[dict] = []
        for tool_use in tool_uses:
            log(f"Tool: {tool_use.name} input={json.dumps(tool_use.input)[:200]}")
            try:
                result_text = execute_tool(
                    name=tool_use.name,
                    input_data=tool_use.input,
                    container_input=container_input,
                )
            except Exception as exc:
                result_text = f"Error executing {tool_use.name}: {exc}"
                log(f"Tool error: {exc}")

            log(f"Tool result: {result_text[:200]}")
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result_text,
                }
            )

        messages.append({"role": "user", "content": tool_results})

    return messages, closed_during_turn


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    # Read stdin
    try:
        raw = sys.stdin.read()
        container_input: dict = json.loads(raw)
        # Delete temp file written by entrypoint (contains secrets)
        try:
            Path("/tmp/input.json").unlink(missing_ok=True)
        except Exception:
            pass
        log(f"Received input for group: {container_input.get('groupFolder')}")
    except Exception as exc:
        write_output("error", error=f"Failed to parse input: {exc}")
        sys.exit(1)

    # Inject secrets into environment for the SDK (never leaked to Bash subprocesses)
    secrets: dict = container_input.get("secrets") or {}
    for key, value in secrets.items():
        os.environ[key] = value

    # Build Anthropic client
    api_key = secrets.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    base_url = (
        secrets.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL")
    )

    client_kwargs: dict[str, Any] = {}
    if api_key:
        client_kwargs["api_key"] = api_key
    if base_url:
        client_kwargs["base_url"] = base_url

    try:
        client = anthropic.Anthropic(**client_kwargs)
    except Exception as exc:
        write_output("error", error=f"Failed to create Anthropic client: {exc}")
        sys.exit(1)

    # Session
    session_id = container_input.get("sessionId") or str(uuid.uuid4())
    messages = load_session(session_id) if container_input.get("sessionId") else []

    # System prompt
    system_prompt = build_system_prompt(container_input)

    # Clean up stale IPC state from previous container runs
    IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        IPC_CLOSE_SENTINEL.unlink(missing_ok=True)
    except Exception:
        pass

    # Build initial prompt
    prompt = container_input.get("prompt", "")
    if container_input.get("isScheduledTask"):
        prompt = (
            "[SCHEDULED TASK - This message was sent automatically and is not "
            "coming directly from the user or group.]\n\n" + prompt
        )

    # Drain any pending IPC messages into the initial prompt
    pending = drain_ipc_input()
    if pending:
        log(f"Draining {len(pending)} pending IPC message(s) into initial prompt")
        prompt += "\n" + "\n".join(pending)

    # Main query loop: run agent → wait for IPC → repeat
    try:
        while True:
            log(f"Starting query (session: {session_id})...")

            messages, closed = run_agent_turn(
                prompt=prompt,
                messages=messages,
                client=client,
                system_prompt=system_prompt,
                container_input=container_input,
                session_id=session_id,
            )
            save_session(session_id, messages)

            if closed:
                log("Close sentinel consumed during query, exiting")
                break

            # Emit session update so the host tracks the session ID
            write_output("success", result=None, new_session_id=session_id)

            log("Query ended, waiting for next IPC message...")

            next_message = wait_for_ipc_message()
            if next_message is None:
                log("Close sentinel received, exiting")
                break

            log(f"Got new message ({len(next_message)} chars), starting new query")
            prompt = next_message

    except Exception as exc:
        log(f"Agent error: {exc}")
        write_output("error", error=str(exc), new_session_id=session_id)
        sys.exit(1)


if __name__ == "__main__":
    main()
