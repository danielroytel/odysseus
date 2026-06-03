import asyncio
import os
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call, mock_open
from src.sandbox import SandboxError, SandboxConfig, SandboxContainer, SandboxManager


def _mock_process(stdout=b"", stderr=b"", returncode=0):
    """Create a mock subprocess.Process for _docker_cmd (uses .communicate())."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.stdout = None
    proc.stderr = None
    proc.wait = AsyncMock()
    proc.kill = MagicMock()
    return proc


class _MockStream:
    """Async stream that yields lines from a bytes payload, then EOF."""
    def __init__(self, data: bytes):
        if data:
            self._lines = data.split(b"\n")
            # Remove trailing empty element produced by trailing \n
            if self._lines and self._lines[-1] == b"":
                self._lines.pop()
        else:
            self._lines = []
        self._idx = 0

    async def readline(self):
        if self._idx < len(self._lines):
            line = self._lines[self._idx]
            self._idx += 1
            return line + b"\n"
        return b""  # EOF


def _mock_streaming_process(stdout=b"", stderr=b"", returncode=0):
    """Create a mock subprocess.Process for exec() (uses readline streaming)."""
    async def _slow_wait():
        # Yield to event loop so reader tasks can consume streams
        # before wait() completes (mimics real subprocess timing)
        await asyncio.sleep(0)
        return returncode

    proc = MagicMock()
    proc.stdout = _MockStream(stdout)
    proc.stderr = _MockStream(stderr)
    proc.returncode = returncode
    proc.wait = AsyncMock(side_effect=_slow_wait)
    proc.kill = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


class TestSandboxConfig:
    def test_defaults(self):
        config = SandboxConfig()
        assert config.image == "odysseus/sandbox:latest"
        assert config.memory == "4g"
        assert config.cpus == 2.0
        assert config.pids_limit == 256
        assert config.network is False
        assert config.idle_timeout == 1800
        assert config.credential_passthrough["git"] is True
        assert config.credential_passthrough["gh"] is True
        assert config.credential_passthrough["ssh"] is False

    def test_custom_values(self):
        config = SandboxConfig(image="python:3.12-slim", memory="2g", network=True)
        assert config.image == "python:3.12-slim"
        assert config.memory == "2g"
        assert config.network is True
        # Defaults should remain for unspecified values
        assert config.cpus == 2.0
        assert config.pids_limit == 256


class TestDetectRuntime:
    @pytest.mark.asyncio
    async def test_detect_podman(self):
        """Should detect podman when available."""
        manager = SandboxManager()

        with patch("asyncio.create_subprocess_exec", side_effect=[
            _mock_process(stdout=b"podman version 4.0.0", returncode=0)
        ]):
            runtime = await manager.detect_runtime()
            assert runtime == "podman"
            assert manager._runtime == "podman"

    @pytest.mark.asyncio
    async def test_detect_docker_fallback(self):
        """Should detect docker when podman is not available."""
        manager = SandboxManager()

        with patch("asyncio.create_subprocess_exec", side_effect=[
            _mock_process(returncode=1, stderr=b"not found"),
            _mock_process(stdout=b"Docker version 24.0.0", returncode=0)
        ]):
            runtime = await manager.detect_runtime()
            assert runtime == "docker"
            assert manager._runtime == "docker"

    @pytest.mark.asyncio
    async def test_detect_no_runtime(self):
        """Should raise SandboxError when no runtime available."""
        manager = SandboxManager()

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
            with pytest.raises(SandboxError, match="No container runtime available"):
                await manager.detect_runtime()

    @pytest.mark.asyncio
    async def test_detect_timeout(self):
        """Should fallback when runtime times out."""
        manager = SandboxManager()

        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", side_effect=[
            proc,
            _mock_process(stdout=b"Docker version 24.0.0", returncode=0)
        ]):
            runtime = await manager.detect_runtime()
            assert runtime == "docker"


class TestCreateContainer:
    @pytest.mark.asyncio
    async def test_create_container_basic(self):
        """Should create container with basic configuration."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig(image="python:3.12-slim")

        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(
            stdout=b"abc123\n",
            returncode=0
        )):
            container = await manager._create_container("test-session", "/workspace", config)

            assert container.container_id == "abc123"
            assert container.session_id == "test-session"
            assert container.workspace_dir == "/workspace"
            assert container.image == "python:3.12-slim"
            assert container.container_workspace == "/workspace"
            assert isinstance(container.created_at, float)
            assert isinstance(container.last_used_at, float)

    @pytest.mark.asyncio
    async def test_create_container_no_network(self):
        """Should create container without network when network=False."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig(network=False)

        docker_cmd_called = []

        def mock_exec(*args, **kwargs):
            docker_cmd_called.append(list(args))
            return _mock_process(stdout=b"abc123\n", returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await manager._create_container("test-session", "/workspace", config)

            # Find the docker run command
            run_cmd = None
            for cmd in docker_cmd_called:
                if len(cmd) > 1 and cmd[1] == "run":
                    run_cmd = cmd
                    break

            assert run_cmd is not None
            assert "--network" in run_cmd
            network_idx = run_cmd.index("--network")
            assert run_cmd[network_idx + 1] == "none"

    @pytest.mark.asyncio
    async def test_create_container_with_network(self):
        """Should create container with network when network=True."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig(network=True)

        docker_cmd_called = []

        def mock_exec(*args, **kwargs):
            docker_cmd_called.append(list(args))
            return _mock_process(stdout=b"abc123\n", returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await manager._create_container("test-session", "/workspace", config)

            # Find the docker run command
            run_cmd = None
            for cmd in docker_cmd_called:
                if len(cmd) > 1 and cmd[1] == "run":
                    run_cmd = cmd
                    break

            assert run_cmd is not None
            assert "--network" not in run_cmd

    @pytest.mark.asyncio
    async def test_create_container_credential_passthrough(self):
        """Should pass through credentials when configured."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig(
            credential_passthrough={"git": True, "gh": True, "ssh": False}
        )

        docker_cmd_called = []

        def mock_exec(*args, **kwargs):
            docker_cmd_called.append(list(args))
            return _mock_process(stdout=b"abc123\n", returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            with patch("os.path.isfile", return_value=True):
                with patch("os.path.isdir", return_value=False):
                    await manager._create_container("test-session", "/workspace", config)

            # Find the docker run command
            run_cmd = None
            for cmd in docker_cmd_called:
                if len(cmd) > 1 and cmd[1] == "run":
                    run_cmd = cmd
                    break

            assert run_cmd is not None
            # Should have git config mount
            assert any("/.gitconfig" in str(arg) for arg in run_cmd)

    @pytest.mark.asyncio
    async def test_create_container_extra_mounts(self):
        """Should include extra bind mounts."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig(
            extra_bind_mounts=["/host/path:/container/path:rw", "/another:/mnt:ro"]
        )

        docker_cmd_called = []

        def mock_exec(*args, **kwargs):
            docker_cmd_called.append(list(args))
            return _mock_process(stdout=b"abc123\n", returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await manager._create_container("test-session", "/workspace", config)

            # Find the docker run command
            run_cmd = None
            for cmd in docker_cmd_called:
                if len(cmd) > 1 and cmd[1] == "run":
                    run_cmd = cmd
                    break

            assert run_cmd is not None
            assert "/host/path:/container/path:rw" in run_cmd
            assert "/another:/mnt:ro" in run_cmd


class TestGetOrCreate:
    @pytest.mark.asyncio
    async def test_get_existing(self):
        """Should return existing container when image matches."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig(image="test:latest")

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["test-session"] = container

        result = await manager.get_or_create("test-session", "/workspace", config)

        assert result.container_id == "abc123"
        assert result.session_id == "test-session"

    @pytest.mark.asyncio
    async def test_create_new(self):
        """Should create new container when none exists."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig(image="test:latest")

        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(
            stdout=b"abc123\n",
            returncode=0
        )):
            container = await manager.get_or_create("test-session", "/workspace", config)

            assert container.container_id == "abc123"
            assert container.session_id == "test-session"
            assert container.image == "test:latest"
            assert "test-session" in manager._containers

    @pytest.mark.asyncio
    async def test_recreate_on_image_change(self):
        """Should cleanup and recreate when image changes."""
        manager = SandboxManager()
        manager._runtime = "docker"

        old_config = SandboxConfig(image="test:v1")
        new_config = SandboxConfig(image="test:v2")

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:v1",
            created_at=time.time(),
            last_used_at=time.time(),
            config=old_config
        )
        manager._containers["test-session"] = container

        cleanup_called = []

        async def mock_cleanup(session_id):
            cleanup_called.append(session_id)

        with patch.object(manager, "cleanup", side_effect=mock_cleanup):
            with patch("asyncio.create_subprocess_exec", return_value=_mock_process(
                stdout=b"xyz789\n",
                returncode=0
            )):
                result = await manager.get_or_create("test-session", "/workspace", new_config)

                assert cleanup_called == ["test-session"]
                assert result.container_id == "xyz789"
                assert result.image == "test:v2"

    @pytest.mark.asyncio
    async def test_auto_detect_runtime(self):
        """Should auto-detect runtime when not set."""
        manager = SandboxManager()
        config = SandboxConfig()

        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(
            stdout=b"podman version 4.0.0",
            returncode=0
        )):
            await manager.get_or_create("test-session", "/workspace", config)

            assert manager._runtime == "podman"


class TestExecCommand:
    @pytest.mark.asyncio
    async def test_exec_success(self):
        """Should execute command successfully and return output."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["test-session"] = container

        calls = []

        def mock_create(*args, **kwargs):
            calls.append(list(args))
            if "inspect" in args:
                return _mock_process(stdout=b"true", returncode=0)
            else:
                return _mock_streaming_process(stdout=b"line1\nline2\n", returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            exit_code, stdout, stderr = await manager.exec("test-session", "echo test")

            assert exit_code == 0
            assert "line1" in stdout
            assert "line2" in stdout

    @pytest.mark.asyncio
    async def test_exec_container_not_running(self):
        """Should raise SandboxError when container is not running."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["test-session"] = container

        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(
            stdout=b"false",
            returncode=0
        )):
            with pytest.raises(SandboxError, match="is not running"):
                await manager.exec("test-session", "echo test")

            # Container should be removed from tracking
            assert "test-session" not in manager._containers

    @pytest.mark.asyncio
    async def test_exec_no_container(self):
        """Should raise SandboxError when container doesn't exist."""
        manager = SandboxManager()

        with pytest.raises(SandboxError, match="No container found"):
            await manager.exec("nonexistent", "echo test")

    @pytest.mark.asyncio
    async def test_exec_with_timeout(self):
        """Should timeout command after specified duration."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["test-session"] = container

        def mock_create(*args, **kwargs):
            if "inspect" in args:
                return _mock_process(stdout=b"true", returncode=0)
            else:
                proc = MagicMock()
                # First wait() raises TimeoutError (triggers kill path),
                # second wait() succeeds (after kill)
                proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError, None])
                proc.kill = MagicMock()
                proc.stdout = MagicMock()
                proc.stdout.readline = AsyncMock(return_value=b"")
                proc.stderr = MagicMock()
                proc.stderr.readline = AsyncMock(return_value=b"")
                return proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            with pytest.raises(SandboxError, match="timed out"):
                await manager.exec("test-session", "sleep 100", timeout=1)

    @pytest.mark.asyncio
    async def test_exec_with_cwd(self):
        """Should use custom working directory when specified."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["test-session"] = container

        calls = []

        def mock_create(*args, **kwargs):
            calls.append(list(args))
            if "inspect" in args:
                return _mock_process(stdout=b"true", returncode=0)
            else:
                return _mock_streaming_process(returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            await manager.exec("test-session", "pwd", cwd="/custom/path")

            # Find the exec command (not inspect)
            exec_cmd = None
            for cmd in calls:
                if len(cmd) > 1 and cmd[1] == "exec":
                    exec_cmd = cmd
                    break

            assert exec_cmd is not None
            assert "--workdir" in exec_cmd
            workdir_idx = exec_cmd.index("--workdir")
            assert exec_cmd[workdir_idx + 1] == "/custom/path"

    @pytest.mark.asyncio
    async def test_exec_with_env(self):
        """Should pass environment variables."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["test-session"] = container

        calls = []

        def mock_create(*args, **kwargs):
            calls.append(list(args))
            if "inspect" in args:
                return _mock_process(stdout=b"true", returncode=0)
            else:
                return _mock_streaming_process(returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            await manager.exec("test-session", "env", env={"FOO": "bar", "BAZ": "qux"})

            # Find the exec command
            exec_cmd = None
            for cmd in calls:
                if len(cmd) > 1 and cmd[1] == "exec":
                    exec_cmd = cmd
                    break

            assert exec_cmd is not None
            assert "-e" in exec_cmd
            assert "FOO=bar" in exec_cmd
            assert "BAZ=qux" in exec_cmd


class TestReadWriteFile:
    @pytest.mark.asyncio
    async def test_read_file_success(self):
        """Should read file content successfully."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["test-session"] = container

        def mock_create(*args, **kwargs):
            if "inspect" in args:
                return _mock_process(stdout=b"true", returncode=0)
            else:
                return _mock_streaming_process(stdout=b"file content\n", returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            content = await manager.read_file("test-session", "/test/file.txt")

            assert content == "file content"

    @pytest.mark.asyncio
    async def test_read_file_not_found(self):
        """Should raise SandboxError when file not found."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["test-session"] = container

        def mock_create(*args, **kwargs):
            if "inspect" in args:
                return _mock_process(stdout=b"true", returncode=0)
            else:
                return _mock_streaming_process(
                    stderr=b"No such file\n",
                    returncode=1
                )

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            with pytest.raises(SandboxError, match="Failed to read file"):
                await manager.read_file("test-session", "/nonexistent/file.txt")

    @pytest.mark.asyncio
    async def test_read_file_no_container(self):
        """Should raise SandboxError when container doesn't exist."""
        manager = SandboxManager()

        with pytest.raises(SandboxError, match="No container found"):
            await manager.read_file("nonexistent", "/test/file.txt")

    @pytest.mark.asyncio
    async def test_write_file_success(self):
        """Should write file content successfully."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["test-session"] = container

        exec_calls = []

        def mock_create(*args, **kwargs):
            exec_calls.append(("create", list(args)))
            if "inspect" in args:
                return _mock_process(stdout=b"true", returncode=0)
            else:
                return _mock_streaming_process(returncode=0)

        def mock_stdin(*args, **kwargs):
            exec_calls.append(("stdin", list(args)))
            # _exec_with_stdin returns (exit_code, stdout, stderr)
            return (0, "", "")

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            with patch.object(manager, "_exec_with_stdin", side_effect=mock_stdin):
                await manager.write_file("test-session", "/test/file.txt", "test content")

                assert any(cmd[0] == "create" for cmd in exec_calls)
                assert any(cmd[0] == "stdin" for cmd in exec_calls)

    @pytest.mark.asyncio
    async def test_write_file_no_container(self):
        """Should raise SandboxError when container doesn't exist."""
        manager = SandboxManager()

        with pytest.raises(SandboxError, match="No container found"):
            await manager.write_file("nonexistent", "/test/file.txt", "content")


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_session(self):
        """Should cleanup specific session container."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["test-session"] = container

        docker_calls = []

        async def mock_exec(*args, **kwargs):
            docker_calls.append(list(args))
            return _mock_process(returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            await manager.cleanup("test-session")

            assert "test-session" not in manager._containers

            # Should have stop and rm commands
            stop_cmds = [cmd for cmd in docker_calls if "stop" in cmd]
            rm_cmds = [cmd for cmd in docker_calls if "rm" in cmd]

            assert len(stop_cmds) == 1
            assert len(rm_cmds) == 1
            assert "abc123" in stop_cmds[0]
            assert "abc123" in rm_cmds[0]

    @pytest.mark.asyncio
    async def test_cleanup_idle(self):
        """Should cleanup idle containers that exceeded timeout."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig(idle_timeout=60)

        now = time.time()

        # Add containers with different last_used_at times
        idle_container = SandboxContainer(
            container_id="idle123",
            session_id="idle-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=now - 200,
            last_used_at=now - 100,  # 100 seconds ago
            config=config
        )

        active_container = SandboxContainer(
            container_id="active123",
            session_id="active-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=now - 200,
            last_used_at=now - 30,  # 30 seconds ago
            config=config
        )

        manager._containers["idle-session"] = idle_container
        manager._containers["active-session"] = active_container

        docker_calls = []

        async def mock_exec(*args, **kwargs):
            docker_calls.append(list(args))
            return _mock_process(returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            count = await manager.cleanup_idle()

            assert count == 1
            assert "idle-session" not in manager._containers
            assert "active-session" in manager._containers

    @pytest.mark.asyncio
    async def test_cleanup_all(self):
        """Should cleanup all containers."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        now = time.time()

        for i in range(3):
            container = SandboxContainer(
                container_id=f"container{i}",
                session_id=f"session{i}",
                workspace_dir="/workspace",
                container_workspace="/workspace",
                image="test:latest",
                created_at=now,
                last_used_at=now,
                config=config
            )
            manager._containers[f"session{i}"] = container

        async def mock_exec(*args, **kwargs):
            return _mock_process(returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            count = await manager.cleanup_all()

            assert count == 3
            assert len(manager._containers) == 0

    @pytest.mark.asyncio
    async def test_cleanup_orphans(self):
        """Should cleanup orphaned containers not in tracking dict."""
        manager = SandboxManager()
        manager._runtime = "docker"

        def mock_create(*args, **kwargs):
            cmd = list(args)
            if len(cmd) > 1 and cmd[1] == "ps":
                return _mock_process(
                    stdout=b"orphan1\norphan2\ntracked123\n",
                    returncode=0
                )
            return _mock_process(returncode=0)

        # Add a tracked container
        config = SandboxConfig()
        container = SandboxContainer(
            container_id="tracked123",
            session_id="tracked-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["tracked-session"] = container

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            count = await manager.cleanup_orphans()

            # Should cleanup 2 orphans (orphan1, orphan2)
            # tracked123 should be skipped
            assert count == 2

    @pytest.mark.asyncio
    async def test_cleanup_orphans_no_runtime(self):
        """Should return 0 when runtime not detected."""
        manager = SandboxManager()
        manager._runtime = None

        count = await manager.cleanup_orphans()

        assert count == 0

    @pytest.mark.asyncio
    async def test_cleanup_nonexistent_session(self):
        """Should silently cleanup nonexistent session."""
        manager = SandboxManager()
        manager._runtime = "docker"

        # Should not raise error
        await manager.cleanup("nonexistent")

        assert len(manager._containers) == 0


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_exec_when_runtime_unavailable(self):
        """Should raise SandboxError when runtime becomes unavailable."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["test-session"] = container

        def mock_create(*args, **kwargs):
            if "inspect" in args:
                return _mock_process(stdout=b"true", returncode=0)
            else:
                raise FileNotFoundError("docker not found")

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create):
            with pytest.raises(SandboxError, match="not found"):
                await manager.exec("test-session", "echo test")

    @pytest.mark.asyncio
    async def test_create_when_docker_fails(self):
        """Should raise SandboxError when docker run fails."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(
            returncode=1,
            stderr=b"docker run failed"
        )):
            with pytest.raises(SandboxError, match="Failed to create container"):
                await manager._create_container("test-session", "/workspace", config)

    @pytest.mark.asyncio
    async def test_inspect_fails(self):
        """Should remove container from tracking when inspect fails."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["test-session"] = container

        with patch("asyncio.create_subprocess_exec", return_value=_mock_process(
            returncode=1,
            stderr=b"container not found"
        )):
            # inspect returns non-zero → SandboxError from _docker_cmd
            # Container gets removed from tracking
            with pytest.raises(SandboxError):
                await manager.exec("test-session", "echo test")

            # Container should be removed from tracking
            assert "test-session" not in manager._containers


class TestProgressCallback:
    @pytest.mark.asyncio
    async def test_exec_with_progress_callback(self):
        """Should call progress callback periodically."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig()

        container = SandboxContainer(
            container_id="abc123",
            session_id="test-session",
            workspace_dir="/workspace",
            container_workspace="/workspace",
            image="test:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=config
        )
        manager._containers["test-session"] = container

        progress_calls = []

        async def progress_cb(data):
            progress_calls.append(data)

        async def mock_exec(*args, **kwargs):
            if len(args) > 1 and args[1] == "inspect":
                return _mock_process(stdout=b"true", returncode=0)
            else:
                proc = MagicMock()

                async def mock_wait():
                    # Simulate a command that takes time
                    await asyncio.sleep(0.1)

                proc.wait = AsyncMock(side_effect=mock_wait)
                proc.returncode = 0
                proc.stdout = MagicMock()
                proc.stdout.readline = AsyncMock(
                    side_effect=[b"output line\n", b""]
                )
                proc.stderr = MagicMock()
                proc.stderr.readline = AsyncMock(return_value=b"")
                proc.kill = MagicMock()
                return proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            # Use very short interval for testing
            with patch("src.sandbox.PROGRESS_INTERVAL_S", 0.01):
                await manager.exec(
                    "test-session",
                    "sleep 0.1",
                    progress_cb=progress_cb
                )

                # Should have at least one progress call
                assert len(progress_calls) >= 1
                assert "elapsed_s" in progress_calls[0]
                assert "tail" in progress_calls[0]


# Integration tests (only run with ODYSSEUS_TEST_DOCKER=1)

@pytest.mark.skipif(
    not os.environ.get("ODYSSEUS_TEST_DOCKER"),
    reason="Requires Docker (set ODYSSEUS_TEST_DOCKER=1)"
)
@pytest.mark.integration
class TestIntegration:
    """Integration tests requiring actual Docker."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self):
        """Create container, exec command, read/write file, cleanup."""
        if not os.environ.get("ODYSSEUS_TEST_DOCKER"):
            pytest.skip("Requires Docker")

        manager = SandboxManager()

        # Create a temporary workspace
        import tempfile
        with tempfile.TemporaryDirectory() as workspace:
            config = SandboxConfig()
            session_id = "test-lifecycle"

            # Detect runtime
            runtime = await manager.detect_runtime()
            assert runtime in ["docker", "podman"]

            # Create container
            container = await manager.get_or_create(session_id, workspace, config)
            assert container.session_id == session_id
            assert container.container_id

            # Exec command
            exit_code, stdout, stderr = await manager.exec(session_id, "echo hello")
            assert exit_code == 0
            assert "hello" in stdout

            # Write file
            await manager.write_file(session_id, "/test.txt", "test content")

            # Read file
            content = await manager.read_file(session_id, "/test.txt")
            assert content == "test content"

            # Cleanup
            count = await manager.cleanup(session_id)
            assert session_id not in manager._containers

    @pytest.mark.asyncio
    async def test_cleanup_orphans_integration(self):
        """Test cleanup of orphaned containers."""
        if not os.environ.get("ODYSSEUS_TEST_DOCKER"):
            pytest.skip("Requires Docker")

        manager = SandboxManager()

        # Detect runtime
        await manager.detect_runtime()

        # Cleanup any existing orphans
        await manager.cleanup_orphans()

        # The test itself creates containers, so we're just verifying
        # the method doesn't error out
        count = await manager.cleanup_orphans()
        assert isinstance(count, int)
