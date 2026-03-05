"""
NanoClaw Python — IPC Watcher
Mirrors src/ipc.ts: polls per-group IPC directories for commands written by
running containers and dispatches them (send messages, register groups, etc.).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Set

from .config import DATA_DIR, IPC_POLL_INTERVAL
from .db import (
    create_task,
    delete_task,
    get_task_by_id,
    update_task,
)
from .group_folder import is_valid_group_folder
from .logger import logger
from .types import AvailableGroup, RegisteredGroup

_ipc_watcher_running = False


async def start_ipc_watcher(
    send_message: Callable[[str, str], asyncio.coroutine],
    registered_groups: Callable[[], dict[str, RegisteredGroup]],
    register_group: Callable[[str, RegisteredGroup], None],
    sync_groups: Callable[[bool], asyncio.coroutine],
    get_available_groups: Callable[[], list[AvailableGroup]],
    write_groups_snapshot: Callable[[str, bool, list, set], None],
) -> None:
    global _ipc_watcher_running
    if _ipc_watcher_running:
        logger.debug("IPC watcher already running, skipping duplicate start")
        return
    _ipc_watcher_running = True

    ipc_base = DATA_DIR / "ipc"
    ipc_base.mkdir(parents=True, exist_ok=True)

    async def _process():
        try:
            group_dirs = [
                d for d in ipc_base.iterdir() if d.is_dir() and d.name != "errors"
            ]
        except Exception as exc:
            logger.error(f"Error reading IPC base dir: {exc}")
            return

        groups = registered_groups()
        folder_is_main: dict[str, bool] = {
            g.folder: True for g in groups.values() if g.isMain
        }

        for source_dir in group_dirs:
            source_group = source_dir.name
            is_main = folder_is_main.get(source_group, False)
            messages_dir = source_dir / "messages"
            tasks_dir = source_dir / "tasks"
            groups_dir = source_dir / "groups"

            # Process outbound messages
            if messages_dir.exists():
                for fpath in sorted(messages_dir.glob("*.json")):
                    try:
                        data = json.loads(fpath.read_text())
                        if data.get("type") == "message" and data.get("chatJid") and data.get("text"):
                            chat_jid = data["chatJid"]
                            target = groups.get(chat_jid)
                            if is_main or (target and target.folder == source_group):
                                await send_message(chat_jid, data["text"])
                                logger.info(
                                    f"IPC message sent chatJid={chat_jid} source={source_group}"
                                )
                            else:
                                logger.warning(
                                    f"Unauthorized IPC message blocked chatJid={chat_jid} source={source_group}"
                                )
                        fpath.unlink(missing_ok=True)
                    except Exception as exc:
                        logger.error(
                            f"Error processing IPC message file={fpath.name} source={source_group}: {exc}"
                        )

            # Process task operations
            if tasks_dir.exists():
                for fpath in sorted(tasks_dir.glob("*.json")):
                    try:
                        data = json.loads(fpath.read_text())
                        op = data.get("op")
                        if op == "create":
                            if not is_valid_group_folder(data.get("groupFolder", "")):
                                logger.warning(
                                    f"IPC task create: invalid groupFolder source={source_group}"
                                )
                            elif not is_main and data.get("groupFolder") != source_group:
                                logger.warning(
                                    f"IPC task create: unauthorized cross-group source={source_group}"
                                )
                            else:
                                chat_jids = {
                                    jid
                                    for jid, g in groups.items()
                                    if g.folder == data.get("groupFolder")
                                }
                                if chat_jids:
                                    chat_jid = next(iter(chat_jids))
                                    task = await create_task(
                                        group_folder=data["groupFolder"],
                                        chat_jid=chat_jid,
                                        prompt=data.get("prompt", ""),
                                        schedule_type=data.get("scheduleType", "once"),
                                        schedule_value=data.get("scheduleValue", ""),
                                        context_mode=data.get("contextMode", "isolated"),
                                        next_run=data.get("nextRun"),
                                    )
                                    logger.info(
                                        f"IPC task created id={task.id} source={source_group}"
                                    )
                        elif op == "delete":
                            task_id = data.get("taskId")
                            if task_id:
                                existing = await get_task_by_id(task_id)
                                if existing and (
                                    is_main or existing.group_folder == source_group
                                ):
                                    await delete_task(task_id)
                                    logger.info(
                                        f"IPC task deleted id={task_id} source={source_group}"
                                    )
                                else:
                                    logger.warning(
                                        f"IPC task delete: unauthorized or not found id={task_id} source={source_group}"
                                    )
                        elif op == "update":
                            task_id = data.get("taskId")
                            updates = data.get("updates", {})
                            if task_id and updates:
                                existing = await get_task_by_id(task_id)
                                if existing and (
                                    is_main or existing.group_folder == source_group
                                ):
                                    await update_task(task_id, updates)
                                    logger.info(
                                        f"IPC task updated id={task_id} source={source_group}"
                                    )
                        fpath.unlink(missing_ok=True)
                    except Exception as exc:
                        logger.error(
                            f"Error processing IPC task file={fpath.name} source={source_group}: {exc}"
                        )

            # Process group registration commands (main only)
            if is_main and groups_dir.exists():
                for fpath in sorted(groups_dir.glob("*.json")):
                    try:
                        data = json.loads(fpath.read_text())
                        op = data.get("op")
                        if op == "register":
                            jid = data.get("jid")
                            group_data = data.get("group")
                            if jid and group_data:
                                group = RegisteredGroup(
                                    name=group_data.get("name", ""),
                                    folder=group_data.get("folder", ""),
                                    trigger=group_data.get("trigger", ""),
                                    added_at=group_data.get(
                                        "added_at",
                                        datetime.now(timezone.utc).isoformat(),
                                    ),
                                    requiresTrigger=group_data.get("requiresTrigger", True),
                                    isMain=group_data.get("isMain", False),
                                )
                                register_group(jid, group)
                                logger.info(
                                    f"IPC group registered jid={jid} folder={group.folder}"
                                )
                        elif op == "sync_groups":
                            force = data.get("force", False)
                            await sync_groups(force)
                            # Refresh snapshot after sync
                            ag = get_available_groups()
                            registered_jids = {
                                jid for jid, g in registered_groups().items()
                            }
                            write_groups_snapshot(
                                source_group, True, ag, registered_jids
                            )
                        fpath.unlink(missing_ok=True)
                    except Exception as exc:
                        logger.error(
                            f"Error processing IPC group file={fpath.name} source={source_group}: {exc}"
                        )

    async def _loop():
        while True:
            await _process()
            await asyncio.sleep(IPC_POLL_INTERVAL)

    asyncio.create_task(_loop())
    logger.info("IPC watcher started")
