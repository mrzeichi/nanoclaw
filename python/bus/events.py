"""
NanoClaw Python — Message Bus
Implements the nanobot-style asyncio.Queue message bus described in
docs/PYTHONIZATION.md §1 (直接复用).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional


@dataclass
class InboundMessage:
    """A message received from a channel, ready for the agent loop."""

    chat_jid: str
    text: str  # XML-formatted <messages>…</messages> prompt
    group_folder: str
    session_id: Optional[str] = None
    is_main: bool = False
    is_scheduled_task: bool = False
    task_id: Optional[str] = None


@dataclass
class OutboundMessage:
    """A message to be sent out through a channel."""

    chat_jid: str
    text: str
