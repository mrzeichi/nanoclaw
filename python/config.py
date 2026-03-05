"""
NanoClaw Python — Configuration
Mirrors src/config.ts (Path A: container execution preserved).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .env import read_env_file

# ---------------------------------------------------------------------------
# Runtime config (read from .env, then process env)
# ---------------------------------------------------------------------------

_env = read_env_file(["ASSISTANT_NAME", "ASSISTANT_HAS_OWN_NUMBER"])

ASSISTANT_NAME: str = os.environ.get("ASSISTANT_NAME") or _env.get("ASSISTANT_NAME") or "Andy"
ASSISTANT_HAS_OWN_NUMBER: bool = (
    os.environ.get("ASSISTANT_HAS_OWN_NUMBER") or _env.get("ASSISTANT_HAS_OWN_NUMBER") or ""
) == "true"

POLL_INTERVAL: float = 2.0  # seconds
SCHEDULER_POLL_INTERVAL: float = 60.0  # seconds

# ---------------------------------------------------------------------------
# Absolute paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path.cwd()
_HOME_DIR = Path.home()

MOUNT_ALLOWLIST_PATH = _HOME_DIR / ".config" / "nanoclaw" / "mount-allowlist.json"
SENDER_ALLOWLIST_PATH = _HOME_DIR / ".config" / "nanoclaw" / "sender-allowlist.json"

STORE_DIR = _PROJECT_ROOT / "store"
GROUPS_DIR = _PROJECT_ROOT / "groups"
DATA_DIR = _PROJECT_ROOT / "data"

# ---------------------------------------------------------------------------
# Container settings
# ---------------------------------------------------------------------------

CONTAINER_IMAGE: str = os.environ.get("CONTAINER_IMAGE") or "nanoclaw-agent:latest"
CONTAINER_RUNTIME_BIN: str = os.environ.get("CONTAINER_RUNTIME", "docker")

CONTAINER_TIMEOUT: int = int(os.environ.get("CONTAINER_TIMEOUT") or 1_800_000)  # ms
CONTAINER_MAX_OUTPUT_SIZE: int = int(os.environ.get("CONTAINER_MAX_OUTPUT_SIZE") or 10_485_760)
IPC_POLL_INTERVAL: float = 1.0  # seconds
IDLE_TIMEOUT: int = int(os.environ.get("IDLE_TIMEOUT") or 1_800_000)  # ms
MAX_CONCURRENT_CONTAINERS: int = max(
    1, int(os.environ.get("MAX_CONCURRENT_CONTAINERS") or 5)
)

# ---------------------------------------------------------------------------
# Trigger pattern
# ---------------------------------------------------------------------------


def _escape_regex(s: str) -> str:
    return re.escape(s)


TRIGGER_PATTERN: re.Pattern[str] = re.compile(
    rf"^@{_escape_regex(ASSISTANT_NAME)}\b", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------

_tz_name = os.environ.get("TZ") or "UTC"
try:
    TIMEZONE: ZoneInfo = ZoneInfo(_tz_name)
except ZoneInfoNotFoundError:
    TIMEZONE = ZoneInfo("UTC")
