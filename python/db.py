"""
NanoClaw Python — SQLite database layer
Mirrors src/db.ts using aiosqlite for async access.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from .config import ASSISTANT_NAME, DATA_DIR, STORE_DIR
from .group_folder import is_valid_group_folder
from .logger import logger
from .types import NewMessage, RegisteredGroup, ScheduledTask, TaskRunLog

_DB_PATH: Path = DATA_DIR / "nanoclaw.db"
_db: Optional[aiosqlite.Connection] = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    jid TEXT PRIMARY KEY,
    name TEXT,
    last_message_time TEXT,
    channel TEXT,
    is_group INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT,
    chat_jid TEXT,
    sender TEXT,
    sender_name TEXT,
    content TEXT,
    timestamp TEXT,
    is_from_me INTEGER,
    is_bot_message INTEGER DEFAULT 0,
    PRIMARY KEY (id, chat_jid),
    FOREIGN KEY (chat_jid) REFERENCES chats(jid)
);
CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id TEXT PRIMARY KEY,
    group_folder TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    prompt TEXT NOT NULL,
    schedule_type TEXT NOT NULL,
    schedule_value TEXT NOT NULL,
    context_mode TEXT DEFAULT 'isolated',
    next_run TEXT,
    last_run TEXT,
    last_result TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_next_run ON scheduled_tasks(next_run);
CREATE INDEX IF NOT EXISTS idx_status ON scheduled_tasks(status);

CREATE TABLE IF NOT EXISTS task_run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_run_logs ON task_run_logs(task_id, run_at);

CREATE TABLE IF NOT EXISTS router_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    group_folder TEXT PRIMARY KEY,
    session_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS registered_groups (
    jid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    folder TEXT NOT NULL UNIQUE,
    trigger_pattern TEXT NOT NULL,
    added_at TEXT NOT NULL,
    container_config TEXT,
    requires_trigger INTEGER DEFAULT 1,
    is_main INTEGER DEFAULT 0
);
"""


async def init_database() -> None:
    global _db
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STORE_DIR.mkdir(parents=True, exist_ok=True)

    _db = await aiosqlite.connect(_DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(_SCHEMA)
    # Migration: add context_mode if missing
    try:
        await _db.execute(
            "ALTER TABLE scheduled_tasks ADD COLUMN context_mode TEXT DEFAULT 'isolated'"
        )
        await _db.commit()
    except Exception:
        pass
    # Migration: add is_bot_message if missing
    try:
        await _db.execute(
            "ALTER TABLE messages ADD COLUMN is_bot_message INTEGER DEFAULT 0"
        )
        await _db.commit()
    except Exception:
        pass
    # Migration: add is_main if missing
    try:
        await _db.execute(
            "ALTER TABLE registered_groups ADD COLUMN is_main INTEGER DEFAULT 0"
        )
        await _db.commit()
    except Exception:
        pass
    logger.info("Database initialized", path=str(_DB_PATH))


def _get_db() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("Database not initialised — call init_database() first")
    return _db


# ---------------------------------------------------------------------------
# Router state
# ---------------------------------------------------------------------------


async def get_router_state(key: str) -> Optional[str]:
    async with _get_db().execute(
        "SELECT value FROM router_state WHERE key = ?", (key,)
    ) as cur:
        row = await cur.fetchone()
        return row["value"] if row else None


async def set_router_state(key: str, value: str) -> None:
    await _get_db().execute(
        "INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)", (key, value)
    )
    await _get_db().commit()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def get_all_sessions() -> dict[str, str]:
    result: dict[str, str] = {}
    async with _get_db().execute("SELECT group_folder, session_id FROM sessions") as cur:
        async for row in cur:
            result[row["group_folder"]] = row["session_id"]
    return result


async def set_session(group_folder: str, session_id: str) -> None:
    await _get_db().execute(
        "INSERT OR REPLACE INTO sessions (group_folder, session_id) VALUES (?, ?)",
        (group_folder, session_id),
    )
    await _get_db().commit()


# ---------------------------------------------------------------------------
# Registered groups
# ---------------------------------------------------------------------------


def _row_to_registered_group(row: aiosqlite.Row) -> RegisteredGroup:
    cfg = None
    if row["container_config"]:
        try:
            cfg_data = json.loads(row["container_config"])
            from .types import ContainerConfig, AdditionalMount

            mounts = [
                AdditionalMount(**m)
                for m in cfg_data.get("additionalMounts", [])
            ]
            cfg = ContainerConfig(
                additionalMounts=mounts,
                timeout=cfg_data.get("timeout"),
            )
        except Exception:
            pass
    return RegisteredGroup(
        name=row["name"],
        folder=row["folder"],
        trigger=row["trigger_pattern"],
        added_at=row["added_at"],
        containerConfig=cfg,
        requiresTrigger=bool(row["requires_trigger"]),
        isMain=bool(row["is_main"]) if "is_main" in row.keys() else False,
    )


async def get_all_registered_groups() -> dict[str, RegisteredGroup]:
    result: dict[str, RegisteredGroup] = {}
    async with _get_db().execute("SELECT * FROM registered_groups") as cur:
        async for row in cur:
            result[row["jid"]] = _row_to_registered_group(row)
    return result


async def set_registered_group(jid: str, group: RegisteredGroup) -> None:
    cfg_json = None
    if group.containerConfig:
        cfg_json = json.dumps(
            {
                "additionalMounts": [
                    {
                        "hostPath": m.hostPath,
                        "containerPath": m.containerPath,
                        "readonly": m.readonly,
                    }
                    for m in group.containerConfig.additionalMounts
                ],
                "timeout": group.containerConfig.timeout,
            }
        )
    await _get_db().execute(
        """
        INSERT OR REPLACE INTO registered_groups
        (jid, name, folder, trigger_pattern, added_at, container_config, requires_trigger, is_main)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            jid,
            group.name,
            group.folder,
            group.trigger,
            group.added_at,
            cfg_json,
            1 if group.requiresTrigger else 0,
            1 if group.isMain else 0,
        ),
    )
    await _get_db().commit()


# ---------------------------------------------------------------------------
# Chats / Messages
# ---------------------------------------------------------------------------


async def store_chat_metadata(
    jid: str,
    timestamp: str,
    name: Optional[str] = None,
    channel: Optional[str] = None,
    is_group: bool = False,
) -> None:
    await _get_db().execute(
        """
        INSERT OR REPLACE INTO chats (jid, name, last_message_time, channel, is_group)
        VALUES (?, COALESCE(?, (SELECT name FROM chats WHERE jid = ?)), ?, ?, ?)
        """,
        (jid, name, jid, timestamp, channel, 1 if is_group else 0),
    )
    await _get_db().commit()


async def store_message(msg: NewMessage) -> None:
    await store_chat_metadata(msg.chat_jid, msg.timestamp)
    await _get_db().execute(
        """
        INSERT OR IGNORE INTO messages
        (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            msg.id,
            msg.chat_jid,
            msg.sender,
            msg.sender_name,
            msg.content,
            msg.timestamp,
            1 if msg.is_from_me else 0,
            1 if msg.is_bot_message else 0,
        ),
    )
    await _get_db().commit()


async def get_new_messages(
    jids: list[str],
    since_timestamp: str,
    assistant_name: str,
) -> tuple[list[NewMessage], str]:
    """Return messages newer than since_timestamp for the given JIDs.

    Excludes messages from the assistant itself.
    Returns (messages, new_timestamp).
    """
    if not jids:
        return [], since_timestamp

    placeholders = ",".join("?" * len(jids))
    params: list = list(jids)
    query = f"""
        SELECT * FROM messages
        WHERE chat_jid IN ({placeholders})
        AND sender != ?
        AND is_from_me = 0
    """
    params.append(assistant_name)
    if since_timestamp:
        query += " AND timestamp > ?"
        params.append(since_timestamp)
    query += " ORDER BY timestamp ASC"

    messages: list[NewMessage] = []
    async with _get_db().execute(query, params) as cur:
        async for row in cur:
            messages.append(
                NewMessage(
                    id=row["id"],
                    chat_jid=row["chat_jid"],
                    sender=row["sender"],
                    sender_name=row["sender_name"],
                    content=row["content"],
                    timestamp=row["timestamp"],
                    is_from_me=bool(row["is_from_me"]),
                    is_bot_message=bool(row["is_bot_message"]),
                )
            )

    new_ts = messages[-1].timestamp if messages else since_timestamp
    return messages, new_ts


async def get_messages_since(
    chat_jid: str,
    since_timestamp: str,
    assistant_name: str,
) -> list[NewMessage]:
    """Return all messages for chat_jid newer than since_timestamp."""
    params: list = [chat_jid, assistant_name]
    query = """
        SELECT * FROM messages
        WHERE chat_jid = ?
        AND sender != ?
        AND is_from_me = 0
    """
    if since_timestamp:
        query += " AND timestamp > ?"
        params.append(since_timestamp)
    query += " ORDER BY timestamp ASC"

    messages: list[NewMessage] = []
    async with _get_db().execute(query, params) as cur:
        async for row in cur:
            messages.append(
                NewMessage(
                    id=row["id"],
                    chat_jid=row["chat_jid"],
                    sender=row["sender"],
                    sender_name=row["sender_name"],
                    content=row["content"],
                    timestamp=row["timestamp"],
                    is_from_me=bool(row["is_from_me"]),
                    is_bot_message=bool(row["is_bot_message"]),
                )
            )
    return messages


async def get_all_chats() -> list[dict]:
    result = []
    async with _get_db().execute("SELECT * FROM chats ORDER BY last_message_time DESC") as cur:
        async for row in cur:
            result.append(dict(row))
    return result


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------


def _row_to_task(row: aiosqlite.Row) -> ScheduledTask:
    return ScheduledTask(
        id=row["id"],
        group_folder=row["group_folder"],
        chat_jid=row["chat_jid"],
        prompt=row["prompt"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        context_mode=row["context_mode"] or "isolated",
        next_run=row["next_run"],
        last_run=row["last_run"],
        last_result=row["last_result"],
        status=row["status"],
        created_at=row["created_at"],
    )


async def get_all_tasks() -> list[ScheduledTask]:
    result = []
    async with _get_db().execute(
        "SELECT * FROM scheduled_tasks WHERE status != 'completed'"
    ) as cur:
        async for row in cur:
            result.append(_row_to_task(row))
    return result


async def get_due_tasks() -> list[ScheduledTask]:
    now = datetime.now(timezone.utc).isoformat()
    result = []
    async with _get_db().execute(
        """
        SELECT * FROM scheduled_tasks
        WHERE status = 'active' AND next_run IS NOT NULL AND next_run <= ?
        ORDER BY next_run ASC
        """,
        (now,),
    ) as cur:
        async for row in cur:
            result.append(_row_to_task(row))
    return result


async def get_task_by_id(task_id: str) -> Optional[ScheduledTask]:
    async with _get_db().execute(
        "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
    ) as cur:
        row = await cur.fetchone()
        return _row_to_task(row) if row else None


async def create_task(
    group_folder: str,
    chat_jid: str,
    prompt: str,
    schedule_type: str,
    schedule_value: str,
    context_mode: str = "isolated",
    next_run: Optional[str] = None,
) -> ScheduledTask:
    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    await _get_db().execute(
        """
        INSERT INTO scheduled_tasks
        (id, group_folder, chat_jid, prompt, schedule_type, schedule_value,
         context_mode, next_run, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (
            task_id,
            group_folder,
            chat_jid,
            prompt,
            schedule_type,
            schedule_value,
            context_mode,
            next_run,
            now,
        ),
    )
    await _get_db().commit()
    task = await get_task_by_id(task_id)
    assert task is not None
    return task


async def update_task(task_id: str, updates: dict) -> None:
    set_clauses = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [task_id]
    await _get_db().execute(
        f"UPDATE scheduled_tasks SET {set_clauses} WHERE id = ?", values
    )
    await _get_db().commit()


async def update_task_after_run(
    task_id: str,
    result: Optional[str],
    next_run: Optional[str],
    status: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await update_task(
        task_id,
        {
            "last_run": now,
            "last_result": result,
            "next_run": next_run,
            "status": status,
        },
    )


async def delete_task(task_id: str) -> None:
    await _get_db().execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    await _get_db().commit()


async def log_task_run(log: TaskRunLog) -> None:
    await _get_db().execute(
        """
        INSERT INTO task_run_logs
        (task_id, run_at, duration_ms, status, result, error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            log.task_id,
            log.run_at,
            log.duration_ms,
            log.status,
            log.result,
            log.error,
        ),
    )
    await _get_db().commit()
