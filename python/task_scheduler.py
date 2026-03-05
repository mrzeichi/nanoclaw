"""
NanoClaw Python — Task Scheduler
Mirrors src/task-scheduler.ts: polls due tasks and runs them in containers.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from croniter import croniter

from .config import ASSISTANT_NAME, CONTAINER_TIMEOUT, DATA_DIR, IDLE_TIMEOUT, SCHEDULER_POLL_INTERVAL
from .container_runner import run_container_agent, write_tasks_snapshot
from .db import (
    get_all_tasks,
    get_due_tasks,
    get_task_by_id,
    log_task_run,
    update_task,
    update_task_after_run,
)
from .group_folder import resolve_group_folder_path
from .group_queue import GroupQueue
from .logger import logger
from .types import ContainerInput, ContainerOutput, RegisteredGroup, ScheduledTask, TaskRunLog


def compute_next_run(task: ScheduledTask) -> Optional[str]:
    """Return ISO-8601 next run time or None for 'once' tasks."""
    if task.schedule_type == "once":
        return None
    now = time.time()
    if task.schedule_type == "cron":
        cron = croniter(task.schedule_value, datetime.now(timezone.utc))
        return cron.get_next(datetime).isoformat()
    if task.schedule_type == "interval":
        try:
            ms = int(task.schedule_value)
        except (ValueError, TypeError):
            logger.warning(
                f"Invalid interval value task_id={task.id} value={task.schedule_value}"
            )
            return datetime.fromtimestamp(now + 60, tz=timezone.utc).isoformat()
        if ms <= 0:
            return datetime.fromtimestamp(now + 60, tz=timezone.utc).isoformat()
        next_ts = datetime.fromisoformat(task.next_run).timestamp() * 1000 + ms  # still ms
        while next_ts / 1000 <= now:
            next_ts += ms
        return datetime.fromtimestamp(next_ts / 1000, tz=timezone.utc).isoformat()
    return None


async def _run_task(
    task: ScheduledTask,
    registered_groups: Callable[[], dict[str, RegisteredGroup]],
    get_sessions: Callable[[], dict[str, str]],
    queue: GroupQueue,
    on_process: Callable,
    send_message: Callable[[str, str], asyncio.coroutine],
) -> None:
    start_time = time.monotonic()
    try:
        resolve_group_folder_path(task.group_folder)
    except Exception as exc:
        error = str(exc)
        await update_task(task.id, {"status": "paused"})
        logger.error(
            f"Task has invalid group folder task_id={task.id} folder={task.group_folder} error={error}"
        )
        await log_task_run(
            TaskRunLog(
                task_id=task.id,
                run_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=int((time.monotonic() - start_time) * 1000),
                status="error",
                result=None,
                error=error,
            )
        )
        return

    groups = registered_groups()
    # Find the group by folder
    group: Optional[RegisteredGroup] = None
    chat_jid: Optional[str] = None
    for jid, g in groups.items():
        if g.folder == task.group_folder:
            group = g
            chat_jid = jid
            break

    if not group or not chat_jid:
        logger.warning(
            f"Task group not registered task_id={task.id} folder={task.group_folder}"
        )
        next_run = compute_next_run(task)
        status = "completed" if task.schedule_type == "once" else "active"
        await update_task_after_run(task.id, None, next_run, status)
        return

    session_id = get_sessions().get(task.group_folder)

    # For isolated tasks, use no session; for group tasks use the group session
    use_session = session_id if task.context_mode == "group" else None

    # Write snapshots
    all_tasks = await get_all_tasks()
    write_tasks_snapshot(
        task.group_folder,
        group.isMain or False,
        [
            {
                "id": t.id,
                "groupFolder": t.group_folder,
                "prompt": t.prompt,
                "schedule_type": t.schedule_type,
                "schedule_value": t.schedule_value,
                "status": t.status,
                "next_run": t.next_run,
            }
            for t in all_tasks
        ],
    )

    new_session_id: Optional[str] = None
    result_text: Optional[str] = None
    had_error = False

    async def on_output(output: ContainerOutput) -> None:
        nonlocal new_session_id, result_text, had_error
        if output.newSessionId:
            new_session_id = output.newSessionId
        if output.result:
            raw = output.result if isinstance(output.result, str) else str(output.result)
            text = re.sub(r"<internal>[\s\S]*?</internal>", "", raw).strip()
            if text:
                result_text = text
                await send_message(chat_jid, text)
        if output.status == "error":
            had_error = True

    try:
        output = await run_container_agent(
            group,
            ContainerInput(
                prompt=task.prompt,
                groupFolder=task.group_folder,
                chatJid=chat_jid,
                isMain=group.isMain or False,
                sessionId=use_session,
                isScheduledTask=True,
                assistantName=ASSISTANT_NAME,
            ),
            lambda proc, name: on_process(chat_jid, proc, name, task.group_folder),
            on_output,
        )
        if output.newSessionId:
            new_session_id = output.newSessionId
        if output.status == "error":
            had_error = True
    except Exception as exc:
        logger.error(f"Task container error task_id={task.id} err={exc}")
        had_error = True

    duration_ms = int((time.monotonic() - start_time) * 1000)
    status = "error" if had_error else "success"

    next_run = compute_next_run(task)
    task_status = "completed" if task.schedule_type == "once" else "active"

    await log_task_run(
        TaskRunLog(
            task_id=task.id,
            run_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=duration_ms,
            status=status,
            result=result_text,
            error=None if not had_error else "Container error",
        )
    )
    await update_task_after_run(task.id, result_text, next_run, task_status)

    logger.info(
        f"Task completed task_id={task.id} status={status} duration={duration_ms}ms"
    )


async def start_scheduler_loop(
    registered_groups: Callable[[], dict[str, RegisteredGroup]],
    get_sessions: Callable[[], dict[str, str]],
    queue: GroupQueue,
    on_process: Callable,
    send_message: Callable[[str, str], asyncio.coroutine],
) -> None:
    """Start the background scheduler poll loop."""

    async def _loop():
        while True:
            try:
                due = await get_due_tasks()
                for task in due:
                    groups = registered_groups()
                    chat_jid: Optional[str] = None
                    for jid, g in groups.items():
                        if g.folder == task.group_folder:
                            chat_jid = jid
                            break
                    if not chat_jid:
                        continue

                    async def _run(t=task):
                        await _run_task(
                            t,
                            registered_groups,
                            get_sessions,
                            queue,
                            on_process,
                            send_message,
                        )

                    queue.enqueue_task(chat_jid, task.id, _run)
            except Exception as exc:
                logger.error(f"Scheduler loop error: {exc}")
            await asyncio.sleep(SCHEDULER_POLL_INTERVAL)

    asyncio.create_task(_loop())
    logger.info("Scheduler loop started")
