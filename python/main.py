"""
NanoClaw Python — Main Entry Point (Path A)

Python host process that keeps container-based agent execution while
adopting nanobot-style asyncio architecture.

Mirrors src/index.ts.
"""
from __future__ import annotations

import asyncio
import json
import re
import signal
import sys
from pathlib import Path
from typing import Optional

from .config import (
    ASSISTANT_NAME,
    IDLE_TIMEOUT,
    POLL_INTERVAL,
    TRIGGER_PATTERN,
)
from .channels.registry import get_channel_factory, get_registered_channel_names
from .container_runner import (
    ContainerOutput,
    run_container_agent,
    write_groups_snapshot,
    write_tasks_snapshot,
)
from .container_runtime import cleanup_orphans, ensure_container_runtime_running
from .db import (
    get_all_chats,
    get_all_registered_groups,
    get_all_sessions,
    get_all_tasks,
    get_messages_since,
    get_new_messages,
    get_router_state,
    init_database,
    set_registered_group,
    set_router_state,
    set_session,
    store_chat_metadata,
    store_message,
)
from .group_folder import resolve_group_folder_path
from .group_queue import GroupQueue
from .ipc import start_ipc_watcher
from .logger import logger
from .router import find_channel, format_messages, format_outbound
from .sender_allowlist import (
    is_sender_allowed,
    is_trigger_allowed,
    load_sender_allowlist,
    should_drop_message,
)
from .task_scheduler import start_scheduler_loop
from .types import AvailableGroup, Channel, ContainerInput, NewMessage, RegisteredGroup

# ---------------------------------------------------------------------------
# Mutable state (mirrors the module-level vars in index.ts)
# ---------------------------------------------------------------------------

_last_timestamp: str = ""
_sessions: dict[str, str] = {}
_registered_groups: dict[str, RegisteredGroup] = {}
_last_agent_timestamp: dict[str, str] = {}
_message_loop_running = False

_channels: list[Channel] = []
_queue = GroupQueue()


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


async def _load_state() -> None:
    global _last_timestamp, _sessions, _registered_groups, _last_agent_timestamp
    _last_timestamp = (await get_router_state("last_timestamp")) or ""
    agent_ts_raw = await get_router_state("last_agent_timestamp")
    try:
        _last_agent_timestamp = json.loads(agent_ts_raw) if agent_ts_raw else {}
    except Exception:
        logger.warning("Corrupted last_agent_timestamp in DB, resetting")
        _last_agent_timestamp = {}
    _sessions = await get_all_sessions()
    _registered_groups = await get_all_registered_groups()
    logger.info(f"State loaded groups={len(_registered_groups)}")


async def _save_state() -> None:
    await set_router_state("last_timestamp", _last_timestamp)
    await set_router_state(
        "last_agent_timestamp", json.dumps(_last_agent_timestamp)
    )


# ---------------------------------------------------------------------------
# Group management
# ---------------------------------------------------------------------------


def _register_group(jid: str, group: RegisteredGroup) -> None:
    try:
        group_dir = resolve_group_folder_path(group.folder)
    except Exception as exc:
        logger.warning(
            f"Rejecting group registration with invalid folder jid={jid} folder={group.folder} err={exc}"
        )
        return
    _registered_groups[jid] = group
    asyncio.create_task(set_registered_group(jid, group))
    (group_dir / "logs").mkdir(parents=True, exist_ok=True)
    logger.info(f"Group registered jid={jid} name={group.name} folder={group.folder}")


def _get_available_groups() -> list[AvailableGroup]:
    # Resolved synchronously from cached state for snapshot writing
    registered_jids = set(_registered_groups.keys())
    # We don't have access to async db here so use a simple best-effort from
    # the registered groups we already know about
    return [
        AvailableGroup(
            jid=jid,
            name=g.name,
            lastActivity="",
            isRegistered=True,
        )
        for jid, g in _registered_groups.items()
    ]


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------


async def _process_group_messages(chat_jid: str) -> bool:
    group = _registered_groups.get(chat_jid)
    if not group:
        return True

    channel = find_channel(_channels, chat_jid)
    if not channel:
        logger.warning(f"No channel owns JID, skipping messages jid={chat_jid}")
        return True

    is_main_group = group.isMain is True
    since_ts = _last_agent_timestamp.get(chat_jid, "")
    missed = await get_messages_since(chat_jid, since_ts, ASSISTANT_NAME)

    if not missed:
        return True

    if not is_main_group and group.requiresTrigger is not False:
        allowlist_cfg = load_sender_allowlist()
        has_trigger = any(
            TRIGGER_PATTERN.match(m.content.strip())
            and (m.is_from_me or is_trigger_allowed(chat_jid, m.sender, allowlist_cfg))
            for m in missed
        )
        if not has_trigger:
            return True

    prompt = format_messages(missed)
    previous_cursor = _last_agent_timestamp.get(chat_jid, "")
    _last_agent_timestamp[chat_jid] = missed[-1].timestamp
    await _save_state()

    logger.info(f"Processing messages group={group.name} count={len(missed)}")

    idle_task: Optional[asyncio.Task] = None

    def _reset_idle_timer():
        nonlocal idle_task
        if idle_task:
            idle_task.cancel()

        async def _idle():
            await asyncio.sleep(IDLE_TIMEOUT / 1000)
            logger.debug(f"Idle timeout, closing container stdin group={group.name}")
            _queue.close_stdin(chat_jid)

        idle_task = asyncio.create_task(_idle())

    await channel.set_typing(chat_jid, True)
    had_error = False
    output_sent = False

    async def _on_output(result: ContainerOutput) -> None:
        nonlocal had_error, output_sent
        if result.result:
            raw = (
                result.result
                if isinstance(result.result, str)
                else json.dumps(result.result)
            )
            text = re.sub(r"<internal>[\s\S]*?</internal>", "", raw).strip()
            logger.info(f"Agent output group={group.name}: {raw[:200]}")
            if text:
                await channel.send_message(chat_jid, text)
                output_sent = True
            _reset_idle_timer()
        if result.status == "success":
            _queue.notify_idle(chat_jid)
        if result.status == "error":
            had_error = True

    output = await _run_agent(group, prompt, chat_jid, _on_output)

    await channel.set_typing(chat_jid, False)
    if idle_task:
        idle_task.cancel()

    if output == "error" or had_error:
        if output_sent:
            logger.warning(
                f"Agent error after output sent, skipping cursor rollback group={group.name}"
            )
            return True
        _last_agent_timestamp[chat_jid] = previous_cursor
        await _save_state()
        logger.warning(
            f"Agent error, rolled back message cursor group={group.name}"
        )
        return False

    return True


async def _run_agent(
    group: RegisteredGroup,
    prompt: str,
    chat_jid: str,
    on_output=None,
) -> str:
    is_main = group.isMain is True
    session_id = _sessions.get(group.folder)

    all_tasks = await get_all_tasks()
    write_tasks_snapshot(
        group.folder,
        is_main,
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

    available_groups = _get_available_groups()
    write_groups_snapshot(
        group.folder,
        is_main,
        available_groups,
        set(_registered_groups.keys()),
    )

    async def _wrapped_output(output: ContainerOutput) -> None:
        if output.newSessionId:
            _sessions[group.folder] = output.newSessionId
            await set_session(group.folder, output.newSessionId)
        if on_output:
            await on_output(output)

    try:
        output = await run_container_agent(
            group,
            ContainerInput(
                prompt=prompt,
                sessionId=session_id,
                groupFolder=group.folder,
                chatJid=chat_jid,
                isMain=is_main,
                assistantName=ASSISTANT_NAME,
            ),
            lambda proc, name: _queue.register_process(
                chat_jid, proc, name, group.folder
            ),
            _wrapped_output if on_output else None,
        )
        if output.newSessionId:
            _sessions[group.folder] = output.newSessionId
            await set_session(group.folder, output.newSessionId)
        if output.status == "error":
            logger.error(
                f"Container agent error group={group.name} error={output.error}"
            )
            return "error"
        return "success"
    except Exception as exc:
        logger.error(f"Agent error group={group.name} err={exc}")
        return "error"


# ---------------------------------------------------------------------------
# Message loop
# ---------------------------------------------------------------------------


async def _start_message_loop() -> None:
    global _message_loop_running, _last_timestamp
    if _message_loop_running:
        logger.debug("Message loop already running, skipping duplicate start")
        return
    _message_loop_running = True
    logger.info(f"NanoClaw running (trigger: @{ASSISTANT_NAME})")

    while True:
        try:
            jids = list(_registered_groups.keys())
            messages, new_ts = await get_new_messages(
                jids, _last_timestamp, ASSISTANT_NAME
            )
            if messages:
                logger.info(f"New messages count={len(messages)}")
                _last_timestamp = new_ts
                await _save_state()

                by_group: dict[str, list[NewMessage]] = {}
                for msg in messages:
                    by_group.setdefault(msg.chat_jid, []).append(msg)

                for chat_jid, group_messages in by_group.items():
                    group = _registered_groups.get(chat_jid)
                    if not group:
                        continue
                    channel = find_channel(_channels, chat_jid)
                    if not channel:
                        logger.warning(
                            f"No channel owns JID, skipping jid={chat_jid}"
                        )
                        continue

                    is_main_group = group.isMain is True
                    needs_trigger = not is_main_group and group.requiresTrigger is not False

                    if needs_trigger:
                        allowlist_cfg = load_sender_allowlist()
                        has_trigger = any(
                            TRIGGER_PATTERN.match(m.content.strip())
                            and (
                                m.is_from_me
                                or is_trigger_allowed(
                                    chat_jid, m.sender, allowlist_cfg
                                )
                            )
                            for m in group_messages
                        )
                        if not has_trigger:
                            continue

                    all_pending = await get_messages_since(
                        chat_jid,
                        _last_agent_timestamp.get(chat_jid, ""),
                        ASSISTANT_NAME,
                    )
                    msgs_to_send = all_pending if all_pending else group_messages
                    formatted = format_messages(msgs_to_send)

                    if _queue.send_message(chat_jid, formatted):
                        logger.debug(
                            f"Piped messages to active container jid={chat_jid} count={len(msgs_to_send)}"
                        )
                        _last_agent_timestamp[chat_jid] = msgs_to_send[-1].timestamp
                        await _save_state()
                        asyncio.create_task(
                            channel.set_typing(chat_jid, True)
                        )
                    else:
                        _queue.enqueue_message_check(chat_jid)
        except Exception as exc:
            logger.error(f"Error in message loop: {exc}")

        await asyncio.sleep(POLL_INTERVAL)


def _recover_pending_messages() -> None:
    for chat_jid, group in _registered_groups.items():
        asyncio.create_task(_do_recover(chat_jid, group))


async def _do_recover(chat_jid: str, group: RegisteredGroup) -> None:
    since = _last_agent_timestamp.get(chat_jid, "")
    pending = await get_messages_since(chat_jid, since, ASSISTANT_NAME)
    if pending:
        logger.info(
            f"Recovery: found unprocessed messages group={group.name} count={len(pending)}"
        )
        _queue.enqueue_message_check(chat_jid)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    ensure_container_runtime_running()
    cleanup_orphans()
    await init_database()
    logger.info("Database initialized")
    await _load_state()

    loop = asyncio.get_event_loop()

    def _shutdown(sig_name: str):
        logger.info(f"Shutdown signal received signal={sig_name}")
        asyncio.create_task(_do_shutdown())

    async def _do_shutdown():
        await _queue.shutdown(10_000)
        for ch in _channels:
            await ch.disconnect()
        sys.exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig.name: _shutdown(s))

    channel_opts = dict(
        on_message=lambda jid, msg: (
            _drop_or_store(jid, msg)
        ),
        on_chat_metadata=lambda jid, ts, name=None, ch=None, is_group=False: (
            asyncio.create_task(store_chat_metadata(jid, ts, name, ch, is_group))
        ),
        registered_groups=lambda: _registered_groups,
    )

    # Import channel plugins (they self-register on import)
    # Add your channels here, e.g.:
    #   import python.channels.whatsapp  # noqa: F401
    # or use the registry if you have a barrel import

    for name in get_registered_channel_names():
        factory = get_channel_factory(name)
        if factory is None:
            continue
        channel = factory(
            channel_opts["on_message"],
            channel_opts["on_chat_metadata"],
            channel_opts["registered_groups"],
        )
        if channel is None:
            logger.warning(
                f"Channel installed but credentials missing — skipping channel={name}"
            )
            continue
        _channels.append(channel)
        await channel.connect()

    if not _channels:
        logger.critical("No channels connected")
        sys.exit(1)

    await start_scheduler_loop(
        registered_groups=lambda: _registered_groups,
        get_sessions=lambda: _sessions,
        queue=_queue,
        on_process=lambda jid, proc, name, folder: _queue.register_process(
            jid, proc, name, folder
        ),
        send_message=_send_outbound,
    )
    await start_ipc_watcher(
        send_message=_send_outbound,
        registered_groups=lambda: _registered_groups,
        register_group=_register_group,
        sync_groups=_sync_groups,
        get_available_groups=_get_available_groups,
        write_groups_snapshot=write_groups_snapshot,
    )

    _queue.set_process_messages_fn(_process_group_messages)
    _recover_pending_messages()
    await _start_message_loop()


async def _send_outbound(jid: str, raw_text: str) -> None:
    channel = find_channel(_channels, jid)
    if not channel:
        logger.warning(f"No channel owns JID, cannot send message jid={jid}")
        return
    text = format_outbound(raw_text)
    if text:
        await channel.send_message(jid, text)


async def _sync_groups(force: bool) -> None:
    for ch in _channels:
        try:
            await ch.sync_groups(force)
        except Exception as exc:
            logger.warning(f"sync_groups failed channel={ch.name} err={exc}")


def _drop_or_store(jid: str, msg: NewMessage) -> None:
    if not msg.is_from_me and not msg.is_bot_message and jid in _registered_groups:
        cfg = load_sender_allowlist()
        if should_drop_message(jid, cfg) and not is_sender_allowed(
            jid, msg.sender, cfg
        ):
            logger.debug(
                f"sender-allowlist: dropping message (drop mode) jid={jid} sender={msg.sender}"
            )
            return
    asyncio.create_task(store_message(msg))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
