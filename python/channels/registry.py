"""
NanoClaw Python — Channel Registry
Mirrors src/channels/registry.ts: channels self-register at startup.
"""
from __future__ import annotations

from typing import Callable, Optional

from .base import BaseChannel, OnInboundMessage, OnChatMetadata

ChannelFactory = Callable[
    [OnInboundMessage, Optional[OnChatMetadata], Optional[Callable]],
    Optional[BaseChannel],
]

_registry: dict[str, ChannelFactory] = {}


def register_channel(name: str, factory: ChannelFactory) -> None:
    """Register a channel factory by name.  Called at module import time."""
    _registry[name] = factory


def get_registered_channel_names() -> list[str]:
    return list(_registry.keys())


def get_channel_factory(name: str) -> Optional[ChannelFactory]:
    return _registry.get(name)
