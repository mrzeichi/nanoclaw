"""
NanoClaw Python — BaseChannel
Mirrors nanobot's channels/base.py as described in PYTHONIZATION.md §2.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Callable, Optional

from ..types import NewMessage


OnInboundMessage = Callable[[str, NewMessage], None]
OnChatMetadata = Callable[..., None]


class BaseChannel(ABC):
    """Abstract base for all messaging channels."""

    name: str = ""

    def __init__(
        self,
        on_message: OnInboundMessage,
        on_chat_metadata: Optional[OnChatMetadata] = None,
        registered_groups: Optional[Callable[[], dict]] = None,
    ) -> None:
        self._on_message = on_message
        self._on_chat_metadata = on_chat_metadata
        self._registered_groups = registered_groups or (lambda: {})

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the messaging platform."""

    @abstractmethod
    async def send_message(self, jid: str, text: str) -> None:
        """Send a text message to the given JID/chat ID."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the channel is currently connected."""

    @abstractmethod
    def owns_jid(self, jid: str) -> bool:
        """Return True if this channel owns the given JID."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully disconnect from the platform."""

    # Optional overrides
    async def set_typing(self, jid: str, is_typing: bool) -> None:
        pass

    async def sync_groups(self, force: bool = False) -> None:
        pass

    def is_allowed(self, sender_id: str, allow_list: list[str]) -> bool:
        """Check whether sender_id is in the allow list.

        Mirrors nanobot's BaseChannel.is_allowed() (PYTHONIZATION.md §2).
        Empty list → deny all; '*' → allow all.
        """
        if not allow_list:
            return False
        if "*" in allow_list:
            return True
        return str(sender_id) in allow_list
