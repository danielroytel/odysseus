import asyncio
import collections
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

PROGRESS_INTERVAL_S = 2.0
PROGRESS_TAIL_LINES = 12


class SandboxError(Exception):
    """Custom exception for sandbox-related failures."""
    pass


@dataclass
class SandboxConfig:
    """Configuration for sandbox containers."""
    image: str = "odysseus/sandbox:latest"
    memory: str = "4g"
    cpus: float = 2.0
    pids_limit: int = 256
    tmpfs_size: str = "512m"
    network: bool = False
    idle_timeout: int = 1800
    extra_bind_mounts: List[str] = field(default_factory=list)
    credential_passthrough: Dict[str, bool] = field(default_factory=lambda: {
        "git": True,
        "gh": True,
        "ssh": False
    })


@dataclass
class SandboxContainer:
    """Represents a running sandbox container."""
    container_id: str
    workspace_id: str
    workspace_dir: str
    container_workspace: str  # always "/workspace"
    image: str
    created_at: float
    last_used_at: float
    config: SandboxConfig


class SandboxManager:
    """Manages workspace-keyed Docker containers for agent tool isolation.

    Containers are keyed by ``workspace_id`` so multiple sessions can share
    the same container.  A reference set tracks which sessions are using each
    container; the container is only destroyed when the last session releases
    it (or it times out).
    """

    def __init__(self) -> None:
        # workspace_id → SandboxContainer
        self._containers: Dict[str, SandboxContainer] = {}
        # workspace_id → {session_id, ...}
        self._references: Dict[str, Set[str]] = {}
        # session_id → workspace_id  (reverse lookup for exec/release)
        self._session_to_workspace: Dict[str, str] = {}
        self._runtime: Optional[str] = None
        self._lock: asyncio.Lock = asyncio.Lock()

    async def detect_runtime(self) -> str:
        """Detect available container runtime (podman or docker).

        Prefers podman for rootless operation, falls back to docker.
        Raises SandboxError if neither is available.
        """
        for runtime in ["podman", "docker"]:
            try:
                proc = await asyncio.create_subprocess_exec(
                    runtime, "--version",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=5
                )
                if proc.returncode == 0:
                    self._runtime = runtime
                    logger.info(f"Detected container runtime: {runtime}")
                    return runtime
            except (asyncio.TimeoutError, FileNotFoundError):
                continue

        raise SandboxError("No container runtime available (podman or docker required)")

    async def get_or_create(
        self,
        session_id: str,
        workspace_dir: str,
        config: SandboxConfig,
        workspace_id: Optional[str] = None,
    ) -> SandboxContainer:
        """Get existing container or create new one for the session's workspace.

        If ``workspace_id`` is provided, multiple sessions sharing the same
        workspace will reuse the same container.  When ``workspace_id`` is
        ``None`` (legacy / per-session mode) a unique id is derived from the
        session id so the behaviour is identical to the old code.
        """
        wid = workspace_id or f"_session_{session_id}"

        async with self._lock:
            # Ensure runtime is detected
            if not self._runtime:
                await self.detect_runtime()

            # Register the session → workspace mapping
            self._session_to_workspace[session_id] = wid
            self._references.setdefault(wid, set()).add(session_id)

            existing = self._containers.get(wid)
            now = time.time()

            if existing:
                if existing.image == config.image:
                    existing.last_used_at = now
                    logger.debug(
                        "Reusing container %s for session %s (workspace %s)",
                        existing.container_id[:12], session_id, wid,
                    )
                    return existing
                else:
                    logger.info(
                        "Image changed for workspace %s, cleaning up old container",
                        wid,
                    )
                    await self._cleanup_workspace(wid)

            # Create new container
            container = await self._create_container(wid, workspace_dir, config)
            self._containers[wid] = container
            logger.info(
                "Created container %s for workspace %s (session %s)",
                container.container_id[:12], wid, session_id,
            )
            return container

    async def exec(
        self,
        session_id: str,
        command: str,
        timeout: int = 3600,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        progress_cb: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
    ) -> Tuple[int, str, str]:
        """Execute command in container, streaming output to progress callback.

        Returns (exit_code, stdout, stderr).
        Raises SandboxError if container is not running.
        """
        wid = self._session_to_workspace.get(session_id)
        if not wid:
            raise SandboxError(f"No container found for session {session_id}")
        container = self._containers.get(wid)
        if not container:
            raise SandboxError(f"No container found for session {session_id}")

        # Health check
        try:
            running = await self._docker_cmd([
                "inspect", "--format", "{{.State.Running}}",
                container.container_id
            ])
            if running.strip() != "true":
                # Container died, remove from tracking
                self._containers.pop(wid, None)
                self._session_to_workspace.pop(session_id, None)
                self._references.pop(wid, None)
                raise SandboxError(f"Container {container.container_id} is not running")
        except SandboxError:
            self._containers.pop(wid, None)
            self._session_to_workspace.pop(session_id, None)
            self._references.pop(wid, None)
            raise

        # Build exec command
        cmd = [self._runtime, "exec"]

        # User and workdir
        cmd.extend(["--user", "1000:1000"])
        workdir = cwd or container.container_workspace
        cmd.extend(["--workdir", workdir])

        # Environment variables
        if env:
            for key, value in env.items():
                cmd.extend(["-e", f"{key}={value}"])

        cmd.extend([container.container_id, "bash", "-c", command])

        started = time.time()
        stdout_lines: List[str] = []
        stderr_lines: List[str] = []
        tail = collections.deque(maxlen=PROGRESS_TAIL_LINES)

        async def _reader(stream, buf: List[str], label: str) -> None:
            if stream is None:
                return
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip("\n")
                buf.append(decoded)
                tail.append(f"! {decoded}" if label == "err" else decoded)

        async def _progress_emitter() -> None:
            await asyncio.sleep(PROGRESS_INTERVAL_S)
            while True:
                if progress_cb:
                    try:
                        await progress_cb({
                            "elapsed_s": round(time.time() - started, 1),
                            "tail": "\n".join(list(tail)),
                        })
                    except Exception:
                        pass
                await asyncio.sleep(PROGRESS_INTERVAL_S)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            tasks = [
                asyncio.create_task(_reader(proc.stdout, stdout_lines, "out")),
                asyncio.create_task(_reader(proc.stderr, stderr_lines, "err")),
                asyncio.create_task(_progress_emitter()),
            ]

            # Wait for process completion with timeout
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise SandboxError(f"Command timed out after {timeout}s")

            # Cancel reader and progress tasks
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            exit_code = proc.returncode or 0
            return (exit_code, "\n".join(stdout_lines), "\n".join(stderr_lines))

        except FileNotFoundError:
            raise SandboxError(f"Container runtime '{self._runtime}' not found")
        except Exception as e:
            raise SandboxError(f"Failed to execute command: {e}")

    async def read_file(self, session_id: str, path: str) -> str:
        """Read file content from container."""
        wid = self._session_to_workspace.get(session_id)
        if not wid:
            raise SandboxError(f"No container found for session {session_id}")
        container = self._containers.get(wid)
        if not container:
            raise SandboxError(f"No container found for session {session_id}")

        cat_cmd = f"cat {path}"
        exit_code, stdout, stderr = await self.exec(session_id, cat_cmd)

        if exit_code != 0:
            raise SandboxError(f"Failed to read file {path}: {stderr}")

        return stdout

    async def write_file(self, session_id: str, path: str, content: str) -> None:
        """Write content to file in container."""
        wid = self._session_to_workspace.get(session_id)
        if not wid:
            raise SandboxError(f"No container found for session {session_id}")
        container = self._containers.get(wid)
        if not container:
            raise SandboxError(f"No container found for session {session_id}")

        # Create parent directories first
        mkdir_cmd = f"mkdir -p $(dirname {path})"
        await self.exec(session_id, mkdir_cmd)

        # Write file content via stdin
        write_cmd = f"cat > {path}"
        exit_code, stdout, stderr = await self._exec_with_stdin(
            container.container_id, write_cmd, content
        )

        if exit_code != 0:
            raise SandboxError(f"Failed to write file {path}: {stderr}")

    async def release(self, session_id: str) -> None:
        """Release a session's reference to its workspace container.

        If this was the last session using the workspace the container is
        stopped and removed.
        """
        wid = self._session_to_workspace.pop(session_id, None)
        if not wid:
            return

        refs = self._references.get(wid)
        if refs:
            refs.discard(session_id)
            if refs:
                # Other sessions still using this container
                logger.debug(
                    "Session %s released workspace %s (%d session(s) remain)",
                    session_id, wid, len(refs),
                )
                return

        # Last session — destroy the container
        await self._cleanup_workspace(wid)

    async def cleanup(self, session_id: str) -> None:
        """Stop and remove container for session (legacy convenience wrapper).

        Equivalent to ``release()`` — destroys container only when the last
        session using the workspace is released.
        """
        await self.release(session_id)

    async def _cleanup_workspace(self, workspace_id: str) -> None:
        """Stop and remove a workspace container, purge all tracking."""
        container = self._containers.pop(workspace_id, None)
        self._references.pop(workspace_id, None)
        # Purge any session → workspace mappings pointing here
        self._session_to_workspace = {
            s: w for s, w in self._session_to_workspace.items()
            if w != workspace_id
        }
        if not container:
            return

        try:
            await self._docker_cmd(["stop", container.container_id], timeout=30)
            logger.debug(f"Stopped container {container.container_id}")
        except SandboxError as e:
            logger.warning(f"Failed to stop container {container.container_id}: {e}")

        try:
            await self._docker_cmd(["rm", container.container_id], timeout=30)
            logger.debug(f"Removed container {container.container_id}")
        except SandboxError as e:
            logger.warning(f"Failed to remove container {container.container_id}: {e}")

    async def cleanup_idle(self) -> int:
        """Clean up containers that have exceeded idle timeout.

        Only cleans up containers whose reference set is empty (no active
        sessions). Returns count of cleaned containers.
        """
        now = time.time()
        to_cleanup = []

        for wid, container in self._containers.items():
            refs = self._references.get(wid, set())
            if not refs and now - container.last_used_at > container.config.idle_timeout:
                to_cleanup.append(wid)

        for wid in to_cleanup:
            await self._cleanup_workspace(wid)

        if to_cleanup:
            logger.info(f"Cleaned up {len(to_cleanup)} idle containers")

        return len(to_cleanup)

    async def cleanup_all(self) -> int:
        """Clean up all containers.

        Returns total count of cleaned containers.
        """
        count = len(self._containers)
        workspace_ids = list(self._containers.keys())

        for wid in workspace_ids:
            await self._cleanup_workspace(wid)

        logger.info(f"Cleaned up all {count} containers")
        return count

    async def cleanup_orphans(self) -> int:
        """Clean up orphaned containers (those not in tracking dict).

        Returns count of cleaned containers.
        """
        if not self._runtime:
            return 0

        try:
            output = await self._docker_cmd([
                "ps", "-a",
                "--filter", "name=odysseus-ws-",
                "--format", "{{.ID}}"
            ])

            container_ids = [line.strip() for line in output.strip().split("\n") if line.strip()]
            count = 0

            for container_id in container_ids:
                # Skip if we're tracking this container
                if any(c.container_id == container_id for c in self._containers.values()):
                    continue

                try:
                    await self._docker_cmd(["stop", container_id], timeout=30)
                    await self._docker_cmd(["rm", container_id], timeout=30)
                    count += 1
                    logger.debug(f"Cleaned up orphaned container {container_id}")
                except SandboxError as e:
                    logger.warning(f"Failed to cleanup orphan {container_id}: {e}")

            if count:
                logger.info(f"Cleaned up {count} orphaned containers")

            return count
        except SandboxError as e:
            logger.warning(f"Failed to list orphaned containers: {e}")
            return 0

    async def _create_container(
        self,
        workspace_id: str,
        workspace_dir: str,
        config: SandboxConfig
    ) -> SandboxContainer:
        """Create new sandbox container."""
        container_name = f"odysseus-ws-{workspace_id}"
        cmd = [
            "run", "-d",
            "--name", container_name,
        ]

        if not config.network:
            cmd.extend(["--network", "none"])

        cmd.extend([
            "--read-only",
            "--tmpfs", f"/tmp:rw,nosuid,nodev,size={config.tmpfs_size}",
            "--tmpfs", "/home/sandbox:rw,nosuid,nodev,size=64m",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", "1000:1000",
            "--memory", config.memory,
            "--cpus", str(config.cpus),
            "--pids-limit", str(config.pids_limit),
            "-v", f"{workspace_dir}:/workspace:rw",
            "-w", "/workspace",
        ])

        # Credential passthrough
        home = os.path.expanduser("~")
        cp = config.credential_passthrough

        if cp.get("git") and os.path.isfile(os.path.join(home, ".gitconfig")):
            cmd.extend(["-v", f"{home}/.gitconfig:/home/sandbox/.gitconfig:ro"])

        if cp.get("gh") and os.path.isdir(os.path.join(home, ".config", "gh")):
            cmd.extend(["-v", f"{home}/.config/gh:/home/sandbox/.config/gh:ro"])

        gh_token = os.environ.get("GITHUB_TOKEN")
        if gh_token and cp.get("gh"):
            cmd.extend(["-e", f"GITHUB_TOKEN={gh_token}"])

        if cp.get("ssh") and os.path.isdir(os.path.join(home, ".ssh")):
            cmd.extend(["-v", f"{home}/.ssh:/home/sandbox/.ssh:ro"])

        # Extra bind mounts
        for mount in config.extra_bind_mounts:
            cmd.extend(["-v", mount])

        # Dynamic service env vars so tools inside the sandbox can reach
        # the Odysseus app and search engine.
        app_port = os.environ.get("APP_PORT", "7000")
        cmd.extend(["-e", f"ODYSSEUS_APP_URL=http://host.docker.internal:{app_port}"])
        searxng = os.environ.get("SEARXNG_INSTANCE", "")
        if searxng:
            # Inside Docker Compose the var is overridden to http://searxng:8080,
            # but the sandbox container is on the host network so use the
            # host-side address.
            cmd.extend(["-e", f"ODYSSEUS_SEARXNG_URL={searxng}"])

        cmd.extend([config.image, "sleep", "infinity"])

        try:
            stdout = await self._docker_cmd(cmd, timeout=120)
            container_id = stdout.strip()

            now = time.time()
            return SandboxContainer(
                container_id=container_id,
                workspace_id=workspace_id,
                workspace_dir=workspace_dir,
                container_workspace="/workspace",
                image=config.image,
                created_at=now,
                last_used_at=now,
                config=config,
            )
        except SandboxError as e:
            raise SandboxError(f"Failed to create container: {e}")

    async def _docker_cmd(self, args: List[str], timeout: int = 30) -> str:
        """Execute docker/podman command and return stdout.

        Raises SandboxError if command fails.
        """
        if not self._runtime:
            raise SandboxError("Container runtime not detected")

        try:
            proc = await asyncio.create_subprocess_exec(
                self._runtime, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )

            if proc.returncode != 0:
                raise SandboxError(
                    f"{self._runtime} {' '.join(args)} failed: "
                    f"{stderr.decode(errors='replace').strip()}"
                )

            return stdout.decode(errors="replace")
        except asyncio.TimeoutError:
            if proc:
                proc.kill()
                await proc.wait()
            raise SandboxError(f"Command timed out after {timeout}s: {' '.join(args)}")
        except FileNotFoundError:
            raise SandboxError(f"Container runtime '{self._runtime}' not found")

    async def container_stats(self) -> List[Dict]:
        """Collect resource stats for all active containers."""
        stats = []
        for wid, container in self._containers.items():
            refs = self._references.get(wid, set())
            stat = {
                "workspace_id": wid,
                "sessions": list(refs),
                "session_count": len(refs),
                "container_id": container.container_id[:12],
                "image": container.image,
                "workspace_dir": container.workspace_dir,
                "created_at": container.created_at,
                "last_used_at": container.last_used_at,
                "age_s": round(time.time() - container.created_at),
                "idle_s": round(time.time() - container.last_used_at),
            }

            # Try to get docker stats
            try:
                output = await self._docker_cmd([
                    "stats", "--no-stream", "--format",
                    "{{.MemUsage}}|{{.CPUPerc}}|{{.PIDs}}",
                    container.container_id
                ], timeout=10)
                parts = output.strip().split("|")
                if len(parts) == 3:
                    stat["mem_usage"] = parts[0].strip()
                    stat["cpu_pct"] = parts[1].strip()
                    stat["pids"] = parts[2].strip()
            except Exception:
                pass  # Stats are best-effort

            stats.append(stat)
        return stats

    async def _exec_with_stdin(
        self,
        container_id: str,
        command: str,
        stdin_content: str
    ) -> Tuple[int, str, str]:
        """Execute command with stdin content."""
        cmd = [self._runtime, "exec", "--interactive", container_id, "bash", "-c", command]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate(stdin_content.encode("utf-8"))

            exit_code = proc.returncode or 0
            return (
                exit_code,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace")
            )
        except Exception as e:
            raise SandboxError(f"Failed to execute command with stdin: {e}")
