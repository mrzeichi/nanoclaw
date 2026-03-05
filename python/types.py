"""
NanoClaw Python — Type definitions
Mirrors src/types.ts
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass
class NewMessage:
    id: str
    chat_jid: str
    sender: str
    sender_name: str
    content: str
    timestamp: str
    is_from_me: bool = False
    is_bot_message: bool = False


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@dataclass
class AdditionalMount:
    hostPath: str
    containerPath: Optional[str] = None
    readonly: bool = True


@dataclass
class ContainerConfig:
    additionalMounts: list[AdditionalMount] = field(default_factory=list)
    timeout: Optional[int] = None  # ms; None → use CONTAINER_TIMEOUT


@dataclass
class RegisteredGroup:
    name: str
    folder: str
    trigger: str
    added_at: str
    containerConfig: Optional[ContainerConfig] = None
    requiresTrigger: Optional[bool] = None  # default True for groups
    isMain: Optional[bool] = None


# ---------------------------------------------------------------------------
# Container I/O
# ---------------------------------------------------------------------------


@dataclass
class ContainerInput:
    prompt: str
    groupFolder: str
    chatJid: str
    isMain: bool
    sessionId: Optional[str] = None
    isScheduledTask: bool = False
    assistantName: Optional[str] = None
    secrets: Optional[dict[str, str]] = None


@dataclass
class ContainerOutput:
    status: Literal["success", "error"]
    result: Optional[str]
    newSessionId: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------


@dataclass
class ScheduledTask:
    id: str
    group_folder: str
    chat_jid: str
    prompt: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    context_mode: Literal["group", "isolated"]
    next_run: Optional[str]
    last_run: Optional[str]
    last_result: Optional[str]
    status: Literal["active", "paused", "completed"]
    created_at: str


@dataclass
class TaskRunLog:
    task_id: str
    run_at: str
    duration_ms: int
    status: Literal["success", "error"]
    result: Optional[str]
    error: Optional[str]


# ---------------------------------------------------------------------------
# Channel abstraction
# ---------------------------------------------------------------------------


class Channel:
    """Abstract base for messaging channels."""

    name: str

    async def connect(self) -> None:
        raise NotImplementedError

    async def send_message(self, jid: str, text: str) -> None:
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError

    def owns_jid(self, jid: str) -> bool:
        raise NotImplementedError

    async def disconnect(self) -> None:
        raise NotImplementedError

    # Optional
    async def set_typing(self, jid: str, is_typing: bool) -> None:
        pass

    async def sync_groups(self, force: bool = False) -> None:
        pass


# ---------------------------------------------------------------------------
# Volume mount (container runner internal)
# ---------------------------------------------------------------------------


@dataclass
class VolumeMount:
    hostPath: str
    containerPath: str
    readonly: bool


# ---------------------------------------------------------------------------
# Available group snapshot entry
# ---------------------------------------------------------------------------


@dataclass
class AvailableGroup:
    jid: str
    name: str
    lastActivity: str
    isRegistered: bool
