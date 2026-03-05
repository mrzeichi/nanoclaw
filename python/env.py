"""
NanoClaw Python — Environment / .env reader
Mirrors src/env.ts
"""
from __future__ import annotations

import os
from pathlib import Path


def read_env_file(keys: list[str]) -> dict[str, str]:
    """Parse .env file and return values for the requested keys.

    Does NOT mutate os.environ — callers decide what to do with the values.
    This keeps secrets out of child process environments.
    """
    env_file = Path.cwd() / ".env"
    try:
        content = env_file.read_text(encoding="utf-8")
    except OSError:
        return {}

    wanted = set(keys)
    result: dict[str, str] = {}

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        eq_idx = stripped.find("=")
        if eq_idx == -1:
            continue
        key = stripped[:eq_idx].strip()
        if key not in wanted:
            continue
        value = stripped[eq_idx + 1 :].strip()
        if len(value) >= 2 and (
            (value.startswith('"') and value.endswith('"'))
            or (value.startswith("'") and value.endswith("'"))
        ):
            value = value[1:-1]
        if value:
            result[key] = value

    return result


def read_secrets() -> dict[str, str]:
    """Return allowed secrets for passing to containers via stdin.

    Secrets are never written to disk or mounted as files.
    """
    return read_env_file(
        [
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
        ]
    )
