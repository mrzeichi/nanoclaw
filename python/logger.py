"""
NanoClaw Python — Logging
Thin wrapper around loguru that mirrors the pino-style structured logging
used in the TypeScript version.
"""
from __future__ import annotations

import os
import sys

from loguru import logger as _loguru_logger

_level = (os.environ.get("LOG_LEVEL") or "info").upper()

# Remove default handler and install one with the desired level
_loguru_logger.remove()
_loguru_logger.add(
    sys.stderr,
    level=_level,
    format=(
        "<green>{time:YYYY-MM-DDTHH:mm:ss.SSSZ}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{line}</cyan> — "
        "<level>{message}</level>"
        " {extra}"
    ),
    colorize=True,
)

logger = _loguru_logger
