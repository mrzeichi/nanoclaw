"""
NanoClaw Python — Container Runner (Path A)
Translates src/container-runner.ts to Python.

Spawns agent execution inside Docker/Apple-Container containers and streams
structured output back to the caller using the sentinel markers that the
existing Node.js agent-runner already emits.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .config import (
    CONTAINER_IMAGE,
    CONTAINER_MAX_OUTPUT_SIZE,
    CONTAINER_RUNTIME_BIN,
    CONTAINER_TIMEOUT,
    DATA_DIR,
    GROUPS_DIR,
    IDLE_TIMEOUT,
    TIMEZONE,
)
from .env import read_secrets
from .group_folder import resolve_group_folder_path, resolve_group_ipc_path
from .logger import logger
from .types import (
    AvailableGroup,
    ContainerInput,
    ContainerOutput,
    RegisteredGroup,
    VolumeMount,
)

# Sentinel markers — must match agent-runner/src/index.ts
OUTPUT_START_MARKER = "---NANOCLAW_OUTPUT_START---"
OUTPUT_END_MARKER = "---NANOCLAW_OUTPUT_END---"

# Readonly mount flag (Docker syntax)
_READONLY_SUFFIX = ":ro"


# ---------------------------------------------------------------------------
# Volume mount helpers
# ---------------------------------------------------------------------------


def _readonly_mount_args(host_path: Path, container_path: str) -> list[str]:
    return ["-v", f"{host_path}:{container_path}{_READONLY_SUFFIX}"]


def _build_volume_mounts(group: RegisteredGroup, is_main: bool) -> list[VolumeMount]:
    mounts: list[VolumeMount] = []
    project_root = Path.cwd()
    group_dir = resolve_group_folder_path(group.folder)

    if is_main:
        mounts.append(
            VolumeMount(
                hostPath=str(project_root),
                containerPath="/workspace/project",
                readonly=True,
            )
        )
        env_file = project_root / ".env"
        if env_file.exists():
            mounts.append(
                VolumeMount(
                    hostPath="/dev/null",
                    containerPath="/workspace/project/.env",
                    readonly=True,
                )
            )
        mounts.append(
            VolumeMount(
                hostPath=str(group_dir),
                containerPath="/workspace/group",
                readonly=False,
            )
        )
    else:
        mounts.append(
            VolumeMount(
                hostPath=str(group_dir),
                containerPath="/workspace/group",
                readonly=False,
            )
        )
        global_dir = GROUPS_DIR / "global"
        if global_dir.exists():
            mounts.append(
                VolumeMount(
                    hostPath=str(global_dir),
                    containerPath="/workspace/global",
                    readonly=True,
                )
            )

    # Per-group Claude sessions (.claude/)
    group_sessions_dir = DATA_DIR / "sessions" / group.folder / ".claude"
    group_sessions_dir.mkdir(parents=True, exist_ok=True)
    settings_file = group_sessions_dir / "settings.json"
    if not settings_file.exists():
        settings_file.write_text(
            json.dumps(
                {
                    "env": {
                        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
                        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
                        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0",
                    }
                },
                indent=2,
            )
            + "\n"
        )

    # Sync skills
    skills_src = project_root / "container" / "skills"
    skills_dst = group_sessions_dir / "skills"
    if skills_src.exists():
        for skill_dir in skills_src.iterdir():
            if skill_dir.is_dir():
                dst = skills_dst / skill_dir.name
                shutil.copytree(skill_dir, dst, dirs_exist_ok=True)

    mounts.append(
        VolumeMount(
            hostPath=str(group_sessions_dir),
            containerPath="/home/node/.claude",
            readonly=False,
        )
    )

    # Per-group IPC namespace
    group_ipc_dir = resolve_group_ipc_path(group.folder)
    (group_ipc_dir / "messages").mkdir(parents=True, exist_ok=True)
    (group_ipc_dir / "tasks").mkdir(parents=True, exist_ok=True)
    (group_ipc_dir / "input").mkdir(parents=True, exist_ok=True)
    mounts.append(
        VolumeMount(
            hostPath=str(group_ipc_dir),
            containerPath="/workspace/ipc",
            readonly=False,
        )
    )

    # Per-group agent-runner source
    agent_runner_src = project_root / "container" / "agent-runner" / "src"
    group_agent_runner_dir = DATA_DIR / "sessions" / group.folder / "agent-runner-src"
    if not group_agent_runner_dir.exists() and agent_runner_src.exists():
        shutil.copytree(agent_runner_src, group_agent_runner_dir)
    mounts.append(
        VolumeMount(
            hostPath=str(group_agent_runner_dir),
            containerPath="/app/src",
            readonly=False,
        )
    )

    # Additional validated mounts from group config
    if group.containerConfig and group.containerConfig.additionalMounts:
        from .mount_security import validate_additional_mounts

        extra = validate_additional_mounts(
            group.containerConfig.additionalMounts, group.name, is_main
        )
        mounts.extend(extra)

    return mounts


def _build_container_args(
    mounts: list[VolumeMount], container_name: str
) -> list[str]:
    args = [CONTAINER_RUNTIME_BIN, "run", "-i", "--rm", "--name", container_name]

    tz_name = getattr(TIMEZONE, "key", str(TIMEZONE))
    args += ["-e", f"TZ={tz_name}"]

    try:
        host_uid = os.getuid()  # type: ignore[attr-defined]
        host_gid = os.getgid()  # type: ignore[attr-defined]
        if host_uid not in (0, 1000):
            args += ["--user", f"{host_uid}:{host_gid}", "-e", "HOME=/home/node"]
    except AttributeError:
        pass  # Windows

    for mount in mounts:
        if mount.readonly:
            args += _readonly_mount_args(Path(mount.hostPath), mount.containerPath)
        else:
            args += ["-v", f"{mount.hostPath}:{mount.containerPath}"]

    args.append(CONTAINER_IMAGE)
    return args


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


async def run_container_agent(
    group: RegisteredGroup,
    inp: ContainerInput,
    on_process: Callable[[subprocess.Popen, str], None],
    on_output: Optional[Callable[[ContainerOutput], asyncio.Future]] = None,
) -> ContainerOutput:
    """Spawn a container, stream output, and return the final ContainerOutput.

    Mirrors TypeScript runContainerAgent().
    """
    start_time = time.monotonic()
    group_dir = resolve_group_folder_path(group.folder)
    group_dir.mkdir(parents=True, exist_ok=True)

    mounts = _build_volume_mounts(group, inp.isMain)
    safe_name = re.sub(r"[^a-zA-Z0-9-]", "-", group.folder)
    container_name = f"nanoclaw-{safe_name}-{int(time.time() * 1000)}"
    container_args = _build_container_args(mounts, container_name)

    logger.info(
        f"Spawning container agent group={group.name} container={container_name} "
        f"mounts={len(mounts)} isMain={inp.isMain}"
    )

    logs_dir = group_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Inject secrets into stdin payload (never on disk)
    inp.secrets = read_secrets()
    stdin_payload = json.dumps(
        {
            k: v
            for k, v in {
                "prompt": inp.prompt,
                "sessionId": inp.sessionId,
                "groupFolder": inp.groupFolder,
                "chatJid": inp.chatJid,
                "isMain": inp.isMain,
                "isScheduledTask": inp.isScheduledTask,
                "assistantName": inp.assistantName,
                "secrets": inp.secrets,
            }.items()
            if v is not None
        }
    ).encode()
    inp.secrets = None  # Remove from memory

    loop = asyncio.get_event_loop()

    proc = await asyncio.create_subprocess_exec(
        *container_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    on_process(proc, container_name)  # type: ignore[arg-type]

    proc.stdin.write(stdin_payload)
    await proc.stdin.drain()
    proc.stdin.close()

    stdout_chunks: list[str] = []
    stdout_size = 0
    stdout_truncated = False

    new_session_id: Optional[str] = None
    had_streaming_output = False
    timed_out = False
    parse_buffer = ""

    timeout_ms = max(
        group.containerConfig.timeout if group.containerConfig and group.containerConfig.timeout else CONTAINER_TIMEOUT,
        IDLE_TIMEOUT + 30_000,
    )

    async def _read_stderr() -> None:
        assert proc.stderr is not None
        async for line in proc.stderr:
            text = line.decode(errors="replace").rstrip()
            if text:
                logger.debug(f"[container:{group.folder}] {text}")

    async def _read_stdout() -> ContainerOutput:
        nonlocal stdout_size, stdout_truncated, new_session_id, had_streaming_output, parse_buffer, timed_out

        assert proc.stdout is not None
        result: Optional[ContainerOutput] = None

        async for raw in proc.stdout:
            chunk = raw.decode(errors="replace")

            if not stdout_truncated:
                remaining = CONTAINER_MAX_OUTPUT_SIZE - stdout_size
                if len(chunk) > remaining:
                    stdout_chunks.append(chunk[:remaining])
                    stdout_size += remaining
                    stdout_truncated = True
                    logger.warning(
                        f"Container stdout truncated size={stdout_size} group={group.name}"
                    )
                else:
                    stdout_chunks.append(chunk)
                    stdout_size += len(chunk)

            if on_output:
                parse_buffer += chunk
                while True:
                    start_idx = parse_buffer.find(OUTPUT_START_MARKER)
                    if start_idx == -1:
                        break
                    end_idx = parse_buffer.find(OUTPUT_END_MARKER, start_idx)
                    if end_idx == -1:
                        break
                    json_str = parse_buffer[
                        start_idx + len(OUTPUT_START_MARKER) : end_idx
                    ].strip()
                    parse_buffer = parse_buffer[end_idx + len(OUTPUT_END_MARKER) :]
                    try:
                        parsed: ContainerOutput = _parse_output(json_str)
                        if parsed.newSessionId:
                            new_session_id = parsed.newSessionId
                        had_streaming_output = True
                        await on_output(parsed)
                    except Exception as exc:
                        logger.warning(f"Failed to parse streamed output: {exc}")

        return ContainerOutput(status="success", result=None, newSessionId=new_session_id)

    # Run stdout reader and stderr reader concurrently with a hard timeout
    try:
        stdout_task = asyncio.create_task(_read_stdout())
        stderr_task = asyncio.create_task(_read_stderr())

        await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task),
            timeout=timeout_ms / 1000,
        )
        exit_code = await proc.wait()
    except asyncio.TimeoutError:
        timed_out = True
        logger.error(f"Container timeout group={group.name} container={container_name}")
        try:
            subprocess.run(
                [CONTAINER_RUNTIME_BIN, "stop", container_name],
                timeout=15,
                capture_output=True,
            )
        except Exception:
            proc.kill()
        exit_code = await proc.wait()

    duration_ms = int((time.monotonic() - start_time) * 1000)
    stdout = "".join(stdout_chunks)

    # Write log file
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f")
    log_file = logs_dir / f"container-{ts}.log"
    verbose = os.environ.get("LOG_LEVEL", "info").lower() in ("debug", "trace")
    log_lines = [
        "=== Container Run Log ===",
        f"Timestamp: {datetime.now(timezone.utc).isoformat()}",
        f"Group: {group.name}",
        f"IsMain: {inp.isMain}",
        f"Duration: {duration_ms}ms",
        f"Exit Code: {exit_code}",
        f"Stdout Truncated: {stdout_truncated}",
        f"TimedOut: {timed_out}",
        "",
    ]
    if verbose or exit_code != 0:
        log_lines += [
            "=== Container Args ===",
            " ".join(container_args),
            "",
            "=== Mounts ===",
            *[
                f"{m.hostPath} -> {m.containerPath}{' (ro)' if m.readonly else ''}"
                for m in mounts
            ],
            "",
            "=== Stdout ===",
            stdout,
        ]
    log_file.write_text("\n".join(log_lines))

    if timed_out:
        if had_streaming_output:
            logger.info(
                f"Container timed out after output (idle cleanup) "
                f"group={group.name} duration={duration_ms}ms"
            )
            return ContainerOutput(
                status="success", result=None, newSessionId=new_session_id
            )
        logger.error(
            f"Container timed out with no output group={group.name} duration={duration_ms}ms"
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Container timed out after {timeout_ms}ms",
        )

    if exit_code != 0:
        logger.error(
            f"Container exited with error group={group.name} code={exit_code} duration={duration_ms}ms"
        )
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Container exited with code {exit_code}",
        )

    if on_output:
        logger.info(
            f"Container completed (streaming) group={group.name} duration={duration_ms}ms"
        )
        return ContainerOutput(
            status="success", result=None, newSessionId=new_session_id
        )

    # Legacy (non-streaming): parse last sentinel pair from accumulated stdout
    start_idx = stdout.find(OUTPUT_START_MARKER)
    end_idx = stdout.find(OUTPUT_END_MARKER)
    try:
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = stdout[start_idx + len(OUTPUT_START_MARKER) : end_idx].strip()
        else:
            lines = stdout.strip().splitlines()
            json_str = lines[-1] if lines else ""
        output = _parse_output(json_str)
        logger.info(
            f"Container completed group={group.name} duration={duration_ms}ms status={output.status}"
        )
        return output
    except Exception as exc:
        logger.error(f"Failed to parse container output group={group.name} error={exc}")
        return ContainerOutput(
            status="error",
            result=None,
            error=f"Failed to parse container output: {exc}",
        )


def _parse_output(json_str: str) -> ContainerOutput:
    data = json.loads(json_str)
    return ContainerOutput(
        status=data.get("status", "error"),
        result=data.get("result"),
        newSessionId=data.get("newSessionId"),
        error=data.get("error"),
    )


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def write_tasks_snapshot(
    group_folder: str,
    is_main: bool,
    tasks: list[dict],
) -> None:
    group_ipc_dir = resolve_group_ipc_path(group_folder)
    group_ipc_dir.mkdir(parents=True, exist_ok=True)
    filtered = tasks if is_main else [t for t in tasks if t.get("groupFolder") == group_folder]
    (group_ipc_dir / "current_tasks.json").write_text(json.dumps(filtered, indent=2))


def write_groups_snapshot(
    group_folder: str,
    is_main: bool,
    groups: list[AvailableGroup],
    registered_jids: set[str],
) -> None:
    group_ipc_dir = resolve_group_ipc_path(group_folder)
    group_ipc_dir.mkdir(parents=True, exist_ok=True)
    visible = (
        [
            {
                "jid": g.jid,
                "name": g.name,
                "lastActivity": g.lastActivity,
                "isRegistered": g.isRegistered,
            }
            for g in groups
        ]
        if is_main
        else []
    )
    (group_ipc_dir / "available_groups.json").write_text(
        json.dumps({"groups": visible, "lastSync": datetime.now(timezone.utc).isoformat()}, indent=2)
    )
