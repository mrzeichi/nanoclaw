"""
NanoClaw Python — Sender Allowlist
Mirrors src/sender-allowlist.ts
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, Union

from .config import SENDER_ALLOWLIST_PATH
from .logger import logger


@dataclass
class ChatAllowlistEntry:
    allow: Union[Literal["*"], list[str]]
    mode: Literal["trigger", "drop"]


@dataclass
class SenderAllowlistConfig:
    default: ChatAllowlistEntry
    chats: dict[str, ChatAllowlistEntry] = field(default_factory=dict)
    logDenied: bool = True


_DEFAULT_CONFIG = SenderAllowlistConfig(
    default=ChatAllowlistEntry(allow="*", mode="trigger"),
)


def _is_valid_entry(data: dict) -> bool:
    allow = data.get("allow")
    mode = data.get("mode")
    valid_allow = allow == "*" or (
        isinstance(allow, list) and all(isinstance(v, str) for v in allow)
    )
    valid_mode = mode in ("trigger", "drop")
    return valid_allow and valid_mode


def load_sender_allowlist(path_override=None) -> SenderAllowlistConfig:
    file_path = path_override or SENDER_ALLOWLIST_PATH
    try:
        raw = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _DEFAULT_CONFIG
    except Exception as exc:
        logger.warning(f"sender-allowlist: cannot read config at {file_path}: {exc}")
        return _DEFAULT_CONFIG

    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"sender-allowlist: invalid JSON at {file_path}")
        return _DEFAULT_CONFIG

    if not isinstance(obj, dict) or not _is_valid_entry(obj.get("default", {})):
        logger.warning(f"sender-allowlist: invalid or missing default entry at {file_path}")
        return _DEFAULT_CONFIG

    default = ChatAllowlistEntry(
        allow=obj["default"]["allow"],
        mode=obj["default"]["mode"],
    )
    chats: dict[str, ChatAllowlistEntry] = {}
    for jid, entry in (obj.get("chats") or {}).items():
        if _is_valid_entry(entry):
            chats[jid] = ChatAllowlistEntry(allow=entry["allow"], mode=entry["mode"])
        else:
            logger.warning(
                f"sender-allowlist: skipping invalid chat entry jid={jid}"
            )

    return SenderAllowlistConfig(
        default=default,
        chats=chats,
        logDenied=obj.get("logDenied", True),
    )


def _get_entry(chat_jid: str, cfg: SenderAllowlistConfig) -> ChatAllowlistEntry:
    return cfg.chats.get(chat_jid, cfg.default)


def is_sender_allowed(chat_jid: str, sender: str, cfg: SenderAllowlistConfig) -> bool:
    entry = _get_entry(chat_jid, cfg)
    if entry.allow == "*":
        return True
    return sender in entry.allow


def should_drop_message(chat_jid: str, cfg: SenderAllowlistConfig) -> bool:
    return _get_entry(chat_jid, cfg).mode == "drop"


def is_trigger_allowed(chat_jid: str, sender: str, cfg: SenderAllowlistConfig) -> bool:
    allowed = is_sender_allowed(chat_jid, sender, cfg)
    if not allowed and cfg.logDenied:
        logger.debug(
            f"sender-allowlist: trigger denied jid={chat_jid} sender={sender}"
        )
    return allowed
