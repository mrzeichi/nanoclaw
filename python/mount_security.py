"""
NanoClaw Python — Mount Security
Mirrors src/mount-security.ts.

Validates additional mounts against an allowlist stored outside the project root,
making it tamper-proof from container agents.
Allowlist location: ~/.config/nanoclaw/mount-allowlist.json
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .config import MOUNT_ALLOWLIST_PATH
from .logger import logger
from .types import AdditionalMount, VolumeMount

_DEFAULT_BLOCKED_PATTERNS = [
    ".ssh", ".gnupg", ".gpg", ".aws", ".azure", ".gcloud", ".kube", ".docker",
    "credentials", ".env", ".netrc", ".npmrc", ".pypirc",
    "id_rsa", "id_ed25519", "private_key", ".secret",
]

_cached_allowlist: Optional[dict] = None
_allowlist_load_error: Optional[str] = None


def _load_mount_allowlist() -> Optional[dict]:
    global _cached_allowlist, _allowlist_load_error
    if _cached_allowlist is not None:
        return _cached_allowlist
    if _allowlist_load_error is not None:
        return None

    path = MOUNT_ALLOWLIST_PATH
    if not path.exists():
        _allowlist_load_error = f"Mount allowlist not found at {path}"
        logger.warning(
            f"Mount allowlist not found at {path} — additional mounts will be BLOCKED."
        )
        return None

    try:
        data = json.loads(path.read_text())
        if not isinstance(data.get("allowedRoots"), list):
            raise ValueError("allowedRoots must be a list")
        if not isinstance(data.get("blockedPatterns"), list):
            raise ValueError("blockedPatterns must be a list")
        if not isinstance(data.get("nonMainReadOnly"), bool):
            raise ValueError("nonMainReadOnly must be a bool")
        combined = list({*_DEFAULT_BLOCKED_PATTERNS, *data["blockedPatterns"]})
        data["blockedPatterns"] = combined
        _cached_allowlist = data
        return data
    except Exception as exc:
        _allowlist_load_error = str(exc)
        logger.error(
            f"Failed to load mount allowlist at {path}: {exc} — additional mounts will be BLOCKED"
        )
        return None


def _expand_path(p: str) -> Path:
    home = Path.home()
    if p.startswith("~/"):
        return home / p[2:]
    if p == "~":
        return home
    return Path(p).resolve()


def _real_path(p: Path) -> Optional[Path]:
    try:
        return p.resolve(strict=True)
    except OSError:
        return None


def _matches_blocked(real_path: Path, patterns: list[str]) -> Optional[str]:
    parts = real_path.parts
    path_str = str(real_path)
    for pat in patterns:
        for part in parts:
            if part == pat or pat in part:
                return pat
        if pat in path_str:
            return pat
    return None


def _find_allowed_root(real_path: Path, allowed_roots: list[dict]) -> Optional[dict]:
    for root in allowed_roots:
        expanded = _expand_path(root["path"])
        real_root = _real_path(expanded)
        if real_root is None:
            continue
        try:
            real_path.relative_to(real_root)
            return root
        except ValueError:
            continue
    return None


def validate_additional_mounts(
    mounts: list[AdditionalMount],
    group_name: str,
    is_main: bool,
) -> list[VolumeMount]:
    """Return validated VolumeMount entries (silently skip rejected ones)."""
    allowlist = _load_mount_allowlist()
    result: list[VolumeMount] = []

    for mount in mounts:
        if allowlist is None:
            logger.warning(
                f"[{group_name}] Mount rejected (no allowlist): {mount.hostPath}"
            )
            continue

        container_base = mount.containerPath or Path(mount.hostPath).name
        if ".." in container_base or container_base.startswith("/") or not container_base.strip():
            logger.warning(
                f"[{group_name}] Mount rejected (invalid container path): {container_base}"
            )
            continue

        expanded = _expand_path(mount.hostPath)
        real = _real_path(expanded)
        if real is None:
            logger.warning(
                f"[{group_name}] Mount rejected (path not found): {mount.hostPath}"
            )
            continue

        blocked = _matches_blocked(real, allowlist["blockedPatterns"])
        if blocked:
            logger.warning(
                f"[{group_name}] Mount rejected (blocked pattern '{blocked}'): {real}"
            )
            continue

        allowed_root = _find_allowed_root(real, allowlist["allowedRoots"])
        if allowed_root is None:
            logger.warning(
                f"[{group_name}] Mount rejected (not under allowed root): {real}"
            )
            continue

        requested_rw = mount.readonly is False
        effective_readonly = True
        if requested_rw:
            if not is_main and allowlist.get("nonMainReadOnly"):
                effective_readonly = True
            elif not allowed_root.get("allowReadWrite"):
                effective_readonly = True
            else:
                effective_readonly = False

        result.append(
            VolumeMount(
                hostPath=str(real),
                containerPath=f"/workspace/extra/{container_base}",
                readonly=effective_readonly,
            )
        )
        logger.debug(
            f"[{group_name}] Mount validated: {real} -> /workspace/extra/{container_base}"
        )

    return result
