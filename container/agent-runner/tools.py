#!/usr/bin/env python3
"""
Tool implementations for the NanoClaw Python agent runner.

Provides Bash execution, file operations, web fetching, and IPC messaging
as Anthropic tool-use definitions + dispatcher.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SECRET_ENV_VARS = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
]

IPC_DIR = Path("/workspace/ipc")
MESSAGES_DIR = IPC_DIR / "messages"
TASKS_DIR = IPC_DIR / "tasks"
GROUPS_DIR = IPC_DIR / "groups"

WORKSPACE = Path("/workspace/group")

# ---------------------------------------------------------------------------
# Tool definitions (Anthropic tool-use JSON schema format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "Bash",
        "description": (
            "Execute a bash command. Returns combined stdout and stderr. "
            "Commands run in /workspace/group by default. "
            "API secrets are automatically removed from the environment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute",
                },
                "timeout": {
                    "type": "number",
                    "description": "Timeout in seconds (default 30)",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "Read",
        "description": "Read a file and return its contents. Relative paths are resolved from /workspace/group.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file"},
                "start_line": {
                    "type": "integer",
                    "description": "First line to read, 1-based (optional)",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to read, inclusive (optional)",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "Write",
        "description": "Write content to a file, creating parent directories as needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "Edit",
        "description": (
            "Replace the first occurrence of old_string with new_string in a file. "
            "Fails if old_string is not found."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string", "description": "String to replace"},
                "new_string": {"type": "string", "description": "Replacement string"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "Glob",
        "description": "Find files matching a glob pattern. Returns matching paths, one per line.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, e.g. '**/*.py'",
                },
                "path": {
                    "type": "string",
                    "description": "Base directory (defaults to /workspace/group)",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "Grep",
        "description": "Search for a regex pattern in files. Returns matching lines with file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex search pattern",
                },
                "path": {
                    "type": "string",
                    "description": "Directory or file to search (defaults to /workspace/group)",
                },
                "include": {
                    "type": "string",
                    "description": "Glob pattern for files to include (e.g. '*.py')",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Search recursively (default true)",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "LS",
        "description": "List directory contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path (defaults to /workspace/group)",
                },
            },
        },
    },
    {
        "name": "WebFetch",
        "description": "Fetch a URL and return its content as text (up to 10,000 chars).",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "timeout": {
                    "type": "number",
                    "description": "Timeout in seconds (default 15)",
                },
            },
            "required": ["url"],
        },
    },
    # IPC tools ---------------------------------------------------------------
    {
        "name": "send_message",
        "description": (
            "Send a message to the user or group immediately while still running. "
            "Use for progress updates or to send multiple messages. "
            "When running as a scheduled task, your final output is NOT automatically "
            "sent — use this tool to communicate with the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Message text to send"},
                "sender": {
                    "type": "string",
                    "description": "Your role/identity (e.g. 'Researcher'). "
                    "When set, messages appear from a dedicated bot in Telegram.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "schedule_task",
        "description": (
            "Schedule a recurring or one-time task.\n\n"
            "context_mode: 'group'=runs with chat history, 'isolated'=fresh session\n"
            "schedule_type: 'cron', 'interval', or 'once'\n"
            "schedule_value:\n"
            "  cron: '0 9 * * *' (daily 9 am)\n"
            "  interval: milliseconds e.g. '3600000'\n"
            "  once: local timestamp WITHOUT timezone suffix, e.g. '2026-02-01T15:30:00'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "schedule_type": {
                    "type": "string",
                    "enum": ["cron", "interval", "once"],
                },
                "schedule_value": {"type": "string"},
                "context_mode": {
                    "type": "string",
                    "enum": ["group", "isolated"],
                    "default": "group",
                },
                "target_group_jid": {
                    "type": "string",
                    "description": "(Main group only) JID of the target group.",
                },
            },
            "required": ["prompt", "schedule_type", "schedule_value"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List all scheduled tasks. Main group sees all; others see only their own.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "pause_task",
        "description": "Pause a scheduled task.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "resume_task",
        "description": "Resume a paused task.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "cancel_task",
        "description": "Cancel and delete a scheduled task.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "register_group",
        "description": (
            "Register a new chat/group. Main group only.\n"
            "folder format: '{channel}_{group-name}' e.g. 'whatsapp_family-chat'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "jid": {"type": "string", "description": "Chat JID"},
                "name": {"type": "string", "description": "Display name"},
                "folder": {
                    "type": "string",
                    "description": "Channel-prefixed folder name",
                },
                "trigger": {
                    "type": "string",
                    "description": "Trigger word (e.g. '@Andy')",
                },
            },
            "required": ["jid", "name", "folder", "trigger"],
        },
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_env() -> dict[str, str]:
    """Return os.environ with API secrets removed (safe for Bash subprocesses)."""
    env = os.environ.copy()
    for key in SECRET_ENV_VARS:
        env.pop(key, None)
    return env


def _resolve_path(raw: str) -> Path:
    """Resolve path; relative paths anchored to /workspace/group."""
    p = Path(raw)
    return p if p.is_absolute() else WORKSPACE / p


def _write_ipc_file(directory: Path, data: dict) -> str:
    """Atomically write a JSON IPC file. Returns the filename."""
    directory.mkdir(parents=True, exist_ok=True)
    ts_ms = int(time.time() * 1000)
    rand = os.urandom(3).hex()
    filename = f"{ts_ms}-{rand}.json"
    filepath = directory / filename
    tmp = directory / f"{filename}.tmp"
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(filepath)
    return filename


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _bash(command: str, timeout: int = 30) -> str:
    cwd = str(WORKSPACE) if WORKSPACE.exists() else None
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=_safe_env(),
        )
        output = result.stdout
        if result.stderr:
            output += result.stderr
        if result.returncode != 0:
            output += f"\n[Exit code: {result.returncode}]"
        return output.rstrip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"[Timeout after {timeout}s]"
    except Exception as exc:
        return f"[Error: {exc}]"


def _read(
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    try:
        p = _resolve_path(file_path)
        content = p.read_text(errors="replace")
        if start_line is not None or end_line is not None:
            lines = content.splitlines()
            s = (start_line or 1) - 1
            e = end_line if end_line is not None else len(lines)
            numbered = [f"{s + i + 1}: {line}" for i, line in enumerate(lines[s:e])]
            return "\n".join(numbered)
        return content
    except FileNotFoundError:
        return f"[File not found: {file_path}]"
    except Exception as exc:
        return f"[Error reading {file_path}: {exc}]"


def _write(file_path: str, content: str) -> str:
    try:
        p = _resolve_path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Written {len(content)} chars to {file_path}"
    except Exception as exc:
        return f"[Error writing {file_path}: {exc}]"


def _edit(file_path: str, old_string: str, new_string: str) -> str:
    try:
        p = _resolve_path(file_path)
        content = p.read_text()
        if old_string not in content:
            preview = old_string[:80]
            return f"[Error: old_string not found in {file_path}. Preview: '{preview}']"
        p.write_text(content.replace(old_string, new_string, 1))
        return f"Edited {file_path}: replaced 1 occurrence"
    except FileNotFoundError:
        return f"[File not found: {file_path}]"
    except Exception as exc:
        return f"[Error editing {file_path}: {exc}]"


def _glob(pattern: str, base_path: Optional[str] = None) -> str:
    try:
        base = Path(base_path) if base_path else WORKSPACE
        matches = sorted(base.glob(pattern))[:500]
        return "\n".join(str(m) for m in matches) or "(no matches)"
    except Exception as exc:
        return f"[Error: {exc}]"


def _grep(
    pattern: str,
    path: Optional[str] = None,
    include: Optional[str] = None,
    recursive: bool = True,
) -> str:
    try:
        search_path = path or str(WORKSPACE)
        args = ["grep", "-n"]
        if include:
            args += ["--include=" + include]
        if recursive:
            args.append("-r")
        args += ["-E", pattern, search_path]
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=15, env=_safe_env()
        )
        return result.stdout[:5000].rstrip() or "(no matches)"
    except subprocess.TimeoutExpired:
        return "[Timeout]"
    except Exception as exc:
        return f"[Error: {exc}]"


def _ls(path: Optional[str] = None) -> str:
    try:
        p = Path(path) if path else WORKSPACE
        if not p.exists():
            return f"[Path not found: {path}]"
        lines = []
        for entry in sorted(p.iterdir()):
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{entry.name}{suffix}")
        return "\n".join(lines) or "(empty directory)"
    except Exception as exc:
        return f"[Error: {exc}]"


def _web_fetch(url: str, timeout: int = 15) -> str:
    try:
        import httpx

        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        return f"[HTTP {resp.status_code}]\n{resp.text[:10000]}"
    except ImportError:
        # Fallback to curl if httpx not available
        result = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout), url],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            env=_safe_env(),
        )
        return (result.stdout[:10000] or result.stderr[:1000]).rstrip()
    except Exception as exc:
        return f"[Error fetching {url}: {exc}]"


def _send_message(
    chat_jid: str, text: str, sender: Optional[str], group_folder: str
) -> str:
    data: dict[str, Any] = {
        "type": "message",
        "chatJid": chat_jid,
        "text": text,
        "groupFolder": group_folder,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if sender:
        data["sender"] = sender
    _write_ipc_file(MESSAGES_DIR, data)
    return "Message sent."


def _schedule_task(
    chat_jid: str,
    prompt: str,
    schedule_type: str,
    schedule_value: str,
    context_mode: str,
    is_main: bool,
    group_folder: str,
    target_group_jid: Optional[str] = None,
) -> str:
    if schedule_type == "interval":
        try:
            ms = int(schedule_value)
            if ms <= 0:
                raise ValueError
        except ValueError:
            return f"[Error: Invalid interval '{schedule_value}'. Must be positive milliseconds.]"
    elif schedule_type == "once":
        if schedule_value.endswith("Z") or re.search(
            r"[+-]\d{2}:\d{2}$", schedule_value
        ):
            return f"[Error: Timestamp must be local time without timezone suffix. Got '{schedule_value}']"

    target_jid = chat_jid
    if is_main and target_group_jid:
        target_jid = target_group_jid

    data = {
        "type": "schedule_task",
        "prompt": prompt,
        "schedule_type": schedule_type,
        "schedule_value": schedule_value,
        "context_mode": context_mode or "group",
        "targetJid": target_jid,
        "createdBy": group_folder,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    filename = _write_ipc_file(TASKS_DIR, data)
    return f"Task scheduled ({filename}): {schedule_type} - {schedule_value}"


def _list_tasks(group_folder: str, is_main: bool) -> str:
    tasks_file = IPC_DIR / "current_tasks.json"
    if not tasks_file.exists():
        return "No scheduled tasks found."
    try:
        all_tasks: list[dict] = json.loads(tasks_file.read_text())
        tasks = (
            all_tasks
            if is_main
            else [t for t in all_tasks if t.get("groupFolder") == group_folder]
        )
        if not tasks:
            return "No scheduled tasks found."
        lines = [
            f"- [{t['id']}] {t['prompt'][:50]}... "
            f"({t['schedule_type']}: {t['schedule_value']}) "
            f"- {t['status']}, next: {t.get('next_run', 'N/A')}"
            for t in tasks
        ]
        return "Scheduled tasks:\n" + "\n".join(lines)
    except Exception as exc:
        return f"[Error reading tasks: {exc}]"


def _task_op(op: str, task_id: str, group_folder: str, is_main: bool) -> str:
    data = {
        "type": op,
        "taskId": task_id,
        "groupFolder": group_folder,
        "isMain": is_main,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _write_ipc_file(TASKS_DIR, data)
    return f"Task {task_id} {op} requested."


def _register_group(
    jid: str, name: str, folder: str, trigger: str, group_folder: str
) -> str:
    data = {
        "type": "register_group",
        "jid": jid,
        "name": name,
        "folder": folder,
        "trigger": trigger,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _write_ipc_file(GROUPS_DIR, data)
    return f'Group "{name}" registered. It will start receiving messages immediately.'


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


def execute_tool(name: str, input_data: dict, container_input: dict) -> str:
    """Dispatch a tool call and return the result as a string."""
    chat_jid: str = container_input.get("chatJid", "")
    group_folder: str = container_input.get("groupFolder", "")
    is_main: bool = bool(container_input.get("isMain", False))

    if name == "Bash":
        return _bash(input_data.get("command", ""), int(input_data.get("timeout", 30)))

    if name == "Read":
        return _read(
            input_data.get("file_path", ""),
            input_data.get("start_line"),
            input_data.get("end_line"),
        )

    if name == "Write":
        return _write(input_data.get("file_path", ""), input_data.get("content", ""))

    if name == "Edit":
        return _edit(
            input_data.get("file_path", ""),
            input_data.get("old_string", ""),
            input_data.get("new_string", ""),
        )

    if name == "Glob":
        return _glob(input_data.get("pattern", "*"), input_data.get("path"))

    if name == "Grep":
        return _grep(
            input_data.get("pattern", ""),
            input_data.get("path"),
            input_data.get("include"),
            input_data.get("recursive", True),
        )

    if name == "LS":
        return _ls(input_data.get("path"))

    if name == "WebFetch":
        return _web_fetch(input_data.get("url", ""), int(input_data.get("timeout", 15)))

    if name == "send_message":
        return _send_message(
            chat_jid,
            input_data.get("text", ""),
            input_data.get("sender"),
            group_folder,
        )

    if name == "schedule_task":
        return _schedule_task(
            chat_jid,
            input_data.get("prompt", ""),
            input_data.get("schedule_type", "once"),
            input_data.get("schedule_value", ""),
            input_data.get("context_mode", "group"),
            is_main,
            group_folder,
            input_data.get("target_group_jid"),
        )

    if name == "list_tasks":
        return _list_tasks(group_folder, is_main)

    if name in ("pause_task", "resume_task", "cancel_task"):
        return _task_op(name, input_data.get("task_id", ""), group_folder, is_main)

    if name == "register_group":
        if not is_main:
            return "[Error: Only the main group can register new groups.]"
        return _register_group(
            input_data.get("jid", ""),
            input_data.get("name", ""),
            input_data.get("folder", ""),
            input_data.get("trigger", ""),
            group_folder,
        )

    return f"[Unknown tool: {name}]"
