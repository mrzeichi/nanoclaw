"""
NanoClaw Python — Group Queue
Mirrors src/group-queue.ts: per-group serialised container execution with
concurrency cap and retry/backoff.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Coroutine, Optional

from .config import DATA_DIR, MAX_CONCURRENT_CONTAINERS
from .logger import logger

MAX_RETRIES = 5
BASE_RETRY_S = 5.0


@dataclass
class _QueuedTask:
    id: str
    group_jid: str
    fn: Callable[[], Coroutine]


@dataclass
class _GroupState:
    active: bool = False
    idle_waiting: bool = False
    is_task_container: bool = False
    running_task_id: Optional[str] = None
    pending_messages: bool = False
    pending_tasks: list[_QueuedTask] = field(default_factory=list)
    process: object = None  # asyncio.subprocess.Process
    container_name: Optional[str] = None
    group_folder: Optional[str] = None
    retry_count: int = 0


class GroupQueue:
    """Per-group serialised task/message execution with concurrency cap."""

    def __init__(self) -> None:
        self._groups: dict[str, _GroupState] = {}
        self._active_count = 0
        self._waiting_groups: list[str] = []
        self._process_messages_fn: Optional[
            Callable[[str], Coroutine[None, None, bool]]
        ] = None
        self._shutting_down = False

    def _get_group(self, group_jid: str) -> _GroupState:
        if group_jid not in self._groups:
            self._groups[group_jid] = _GroupState()
        return self._groups[group_jid]

    def set_process_messages_fn(
        self, fn: Callable[[str], Coroutine[None, None, bool]]
    ) -> None:
        self._process_messages_fn = fn

    def enqueue_message_check(self, group_jid: str) -> None:
        if self._shutting_down:
            return
        state = self._get_group(group_jid)
        if state.active:
            state.pending_messages = True
            logger.debug(f"Container active, message queued jid={group_jid}")
            return
        if self._active_count >= MAX_CONCURRENT_CONTAINERS:
            state.pending_messages = True
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            logger.debug(
                f"At concurrency limit, message queued jid={group_jid} active={self._active_count}"
            )
            return
        asyncio.create_task(self._run_for_group(group_jid, "messages"))

    def enqueue_task(
        self, group_jid: str, task_id: str, fn: Callable[[], Coroutine]
    ) -> None:
        if self._shutting_down:
            return
        state = self._get_group(group_jid)
        if state.running_task_id == task_id:
            logger.debug(f"Task already running jid={group_jid} task_id={task_id}")
            return
        if any(t.id == task_id for t in state.pending_tasks):
            logger.debug(f"Task already queued jid={group_jid} task_id={task_id}")
            return
        if state.active:
            state.pending_tasks.append(_QueuedTask(task_id, group_jid, fn))
            if state.idle_waiting:
                self.close_stdin(group_jid)
            return
        if self._active_count >= MAX_CONCURRENT_CONTAINERS:
            state.pending_tasks.append(_QueuedTask(task_id, group_jid, fn))
            if group_jid not in self._waiting_groups:
                self._waiting_groups.append(group_jid)
            return
        asyncio.create_task(
            self._run_task(group_jid, _QueuedTask(task_id, group_jid, fn))
        )

    def register_process(
        self,
        group_jid: str,
        proc: object,
        container_name: str,
        group_folder: Optional[str] = None,
    ) -> None:
        state = self._get_group(group_jid)
        state.process = proc
        state.container_name = container_name
        if group_folder:
            state.group_folder = group_folder

    def notify_idle(self, group_jid: str) -> None:
        state = self._get_group(group_jid)
        state.idle_waiting = True
        if state.pending_tasks:
            self.close_stdin(group_jid)

    def send_message(self, group_jid: str, text: str) -> bool:
        """Write a follow-up message IPC file for the active container.

        Returns True if written, False if no active container.
        """
        state = self._get_group(group_jid)
        if not state.active or not state.group_folder or state.is_task_container:
            return False
        state.idle_waiting = False
        input_dir = DATA_DIR / "ipc" / state.group_folder / "input"
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{int(time.time() * 1000)}-{id(text) & 0xFFFF:04x}.json"
            final_path = input_dir / filename
            tmp_path = input_dir / f"{filename}.tmp"
            tmp_path.write_text(json.dumps({"type": "message", "text": text}))
            tmp_path.rename(final_path)
            return True
        except Exception as exc:
            logger.debug(f"send_message failed jid={group_jid} err={exc}")
            return False

    def close_stdin(self, group_jid: str) -> None:
        state = self._get_group(group_jid)
        if not state.active or not state.group_folder:
            return
        input_dir = DATA_DIR / "ipc" / state.group_folder / "input"
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            (input_dir / "_close").write_text("")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal

    async def _run_for_group(self, group_jid: str, reason: str) -> None:
        state = self._get_group(group_jid)
        state.active = True
        state.idle_waiting = False
        state.is_task_container = False
        state.pending_messages = False
        self._active_count += 1
        logger.debug(
            f"Starting container for group jid={group_jid} reason={reason} active={self._active_count}"
        )
        try:
            if self._process_messages_fn:
                success = await self._process_messages_fn(group_jid)
                if success:
                    state.retry_count = 0
                else:
                    self._schedule_retry(group_jid, state)
        except Exception as exc:
            logger.error(f"Error processing messages jid={group_jid} err={exc}")
            self._schedule_retry(group_jid, state)
        finally:
            state.active = False
            state.process = None
            state.container_name = None
            state.group_folder = None
            self._active_count -= 1
            self._drain_group(group_jid)

    async def _run_task(self, group_jid: str, task: _QueuedTask) -> None:
        state = self._get_group(group_jid)
        state.active = True
        state.idle_waiting = False
        state.is_task_container = True
        state.running_task_id = task.id
        self._active_count += 1
        logger.debug(
            f"Running queued task jid={group_jid} task_id={task.id} active={self._active_count}"
        )
        try:
            await task.fn()
        except Exception as exc:
            logger.error(f"Error running task jid={group_jid} task_id={task.id} err={exc}")
        finally:
            state.active = False
            state.is_task_container = False
            state.running_task_id = None
            state.process = None
            state.container_name = None
            state.group_folder = None
            self._active_count -= 1
            self._drain_group(group_jid)

    def _schedule_retry(self, group_jid: str, state: _GroupState) -> None:
        state.retry_count += 1
        if state.retry_count > MAX_RETRIES:
            logger.error(
                f"Max retries exceeded jid={group_jid} — dropping (will retry on next message)"
            )
            state.retry_count = 0
            return
        delay = BASE_RETRY_S * (2 ** (state.retry_count - 1))
        logger.info(
            f"Scheduling retry jid={group_jid} count={state.retry_count} delay={delay:.1f}s"
        )

        async def _retry():
            await asyncio.sleep(delay)
            if not self._shutting_down:
                self.enqueue_message_check(group_jid)

        asyncio.create_task(_retry())

    def _drain_group(self, group_jid: str) -> None:
        if self._shutting_down:
            return
        state = self._get_group(group_jid)
        if state.pending_tasks:
            task = state.pending_tasks.pop(0)
            asyncio.create_task(self._run_task(group_jid, task))
            return
        if state.pending_messages:
            asyncio.create_task(self._run_for_group(group_jid, "drain"))
            return
        self._drain_waiting()

    def _drain_waiting(self) -> None:
        while self._waiting_groups and self._active_count < MAX_CONCURRENT_CONTAINERS:
            next_jid = self._waiting_groups.pop(0)
            state = self._get_group(next_jid)
            if state.pending_tasks:
                task = state.pending_tasks.pop(0)
                asyncio.create_task(self._run_task(next_jid, task))
            elif state.pending_messages:
                asyncio.create_task(self._run_for_group(next_jid, "drain"))

    async def shutdown(self, grace_period_ms: int = 10_000) -> None:
        self._shutting_down = True
        active = [
            state.container_name
            for state in self._groups.values()
            if state.container_name
        ]
        logger.info(
            f"GroupQueue shutting down active={self._active_count} detached_containers={active}"
        )
