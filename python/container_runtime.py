"""
NanoClaw Python — Container Runtime
Mirrors src/container-runtime.ts: runtime detection, orphan cleanup.
"""
from __future__ import annotations

import subprocess

from .config import CONTAINER_RUNTIME_BIN
from .logger import logger


def ensure_container_runtime_running() -> None:
    try:
        subprocess.run(
            [CONTAINER_RUNTIME_BIN, "info"],
            capture_output=True,
            timeout=10,
            check=True,
        )
        logger.debug("Container runtime reachable")
    except Exception as exc:
        logger.error(f"Failed to reach container runtime: {exc}")
        print(
            "\n╔════════════════════════════════════════════════════════════════╗\n"
            "║  FATAL: Container runtime failed to start                      ║\n"
            "║                                                                ║\n"
            "║  Agents cannot run without a container runtime. To fix:        ║\n"
            "║  1. Ensure Docker is installed and running                     ║\n"
            "║  2. Run: docker info                                           ║\n"
            "║  3. Restart NanoClaw                                           ║\n"
            "╚════════════════════════════════════════════════════════════════╝\n"
        )
        raise RuntimeError("Container runtime is required but failed to start") from exc


def cleanup_orphans() -> None:
    try:
        result = subprocess.run(
            [
                CONTAINER_RUNTIME_BIN,
                "ps",
                "--filter",
                "name=nanoclaw-",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        orphans = [n for n in result.stdout.strip().splitlines() if n]
        for name in orphans:
            try:
                subprocess.run(
                    [CONTAINER_RUNTIME_BIN, "stop", name],
                    capture_output=True,
                    timeout=15,
                )
            except Exception:
                pass
        if orphans:
            logger.info(f"Cleaned up orphaned containers count={len(orphans)} names={orphans}")
    except Exception as exc:
        logger.warning(f"Could not clean up orphaned containers: {exc}")
