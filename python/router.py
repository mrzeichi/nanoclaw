"""
NanoClaw Python — Message Router
Mirrors src/router.ts
"""
from __future__ import annotations

import re
from typing import Optional

from .types import Channel, NewMessage


def escape_xml(s: str) -> str:
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def format_messages(messages: list[NewMessage]) -> str:
    lines = [
        f'<message sender="{escape_xml(m.sender_name)}" time="{m.timestamp}">'
        f"{escape_xml(m.content)}</message>"
        for m in messages
    ]
    return "<messages>\n" + "\n".join(lines) + "\n</messages>"


def strip_internal_tags(text: str) -> str:
    return re.sub(r"<internal>[\s\S]*?</internal>", "", text).strip()


def format_outbound(raw_text: str) -> str:
    text = strip_internal_tags(raw_text)
    return text  # empty string if nothing left


def find_channel(
    channels: list[Channel], jid: str
) -> Optional[Channel]:
    for ch in channels:
        if ch.owns_jid(jid):
            return ch
    return None
