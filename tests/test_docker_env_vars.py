"""Tests for dynamic Docker env vars injected into sandbox containers."""
import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.sandbox import SandboxConfig, SandboxManager


def _mock_process(stdout=b"", stderr=b"", returncode=0):
    """Create a mock process with async communicate for _docker_cmd."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.stdout = None
    proc.stderr = None
    proc.wait = AsyncMock()
    proc.kill = MagicMock()
    return proc


class TestDynamicEnvVars:
    """Verify ODYSSEUS_APP_URL and ODYSSEUS_SEARXNG_URL are injected."""

    @pytest.mark.asyncio
    async def test_app_url_env_var_default_port(self):
        """Should inject ODYSSEUS_APP_URL with default port 7000."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig(image="alpine:3.20")

        docker_cmd_args = []

        def mock_exec(*args, **kwargs):
            docker_cmd_args.append(list(args))
            return _mock_process(stdout=b"abc123\n", returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec), \
             patch.dict(os.environ, {}, clear=False):
            await manager._create_container("ws-test", "/workspace", config)

        run_cmd = None
        for cmd in docker_cmd_args:
            if "run" in cmd:
                run_cmd = cmd
                break
        assert run_cmd is not None

        env_vars = [run_cmd[i + 1] for i, v in enumerate(run_cmd) if v == "-e"]
        app_url = [v for v in env_vars if v.startswith("ODYSSEUS_APP_URL")]
        assert len(app_url) == 1
        assert "7000" in app_url[0]

    @pytest.mark.asyncio
    async def test_app_url_env_var_custom_port(self):
        """Should use APP_PORT env var when set."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig(image="alpine:3.20")

        docker_cmd_args = []

        def mock_exec(*args, **kwargs):
            docker_cmd_args.append(list(args))
            return _mock_process(stdout=b"abc123\n", returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec), \
             patch.dict(os.environ, {"APP_PORT": "9000"}, clear=False):
            await manager._create_container("ws-test", "/workspace", config)

        run_cmd = None
        for cmd in docker_cmd_args:
            if "run" in cmd:
                run_cmd = cmd
                break
        env_vars = [run_cmd[i + 1] for i, v in enumerate(run_cmd) if v == "-e"]
        app_url = [v for v in env_vars if v.startswith("ODYSSEUS_APP_URL")]
        assert len(app_url) == 1
        assert "9000" in app_url[0]

    @pytest.mark.asyncio
    async def test_searxng_url_injected_when_set(self):
        """Should inject ODYSSEUS_SEARXNG_URL when SEARXNG_INSTANCE is set."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig(image="alpine:3.20")

        docker_cmd_args = []

        def mock_exec(*args, **kwargs):
            docker_cmd_args.append(list(args))
            return _mock_process(stdout=b"abc123\n", returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec), \
             patch.dict(os.environ, {"SEARXNG_INSTANCE": "http://searxng:8080"}, clear=False):
            await manager._create_container("ws-test", "/workspace", config)

        run_cmd = None
        for cmd in docker_cmd_args:
            if "run" in cmd:
                run_cmd = cmd
                break
        env_vars = [run_cmd[i + 1] for i, v in enumerate(run_cmd) if v == "-e"]
        searxng = [v for v in env_vars if v.startswith("ODYSSEUS_SEARXNG_URL")]
        assert len(searxng) == 1
        assert "http://searxng:8080" in searxng[0]

    @pytest.mark.asyncio
    async def test_searxng_url_omitted_when_unset(self):
        """Should NOT inject ODYSSEUS_SEARXNG_URL when SEARXNG_INSTANCE is empty."""
        manager = SandboxManager()
        manager._runtime = "docker"
        config = SandboxConfig(image="alpine:3.20")

        docker_cmd_args = []

        def mock_exec(*args, **kwargs):
            docker_cmd_args.append(list(args))
            return _mock_process(stdout=b"abc123\n", returncode=0)

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec), \
             patch.dict(os.environ, {"SEARXNG_INSTANCE": ""}, clear=False):
            await manager._create_container("ws-test", "/workspace", config)

        run_cmd = None
        for cmd in docker_cmd_args:
            if "run" in cmd:
                run_cmd = cmd
                break
        env_vars = [run_cmd[i + 1] for i, v in enumerate(run_cmd) if v == "-e"]
        searxng = [v for v in env_vars if v.startswith("ODYSSEUS_SEARXNG_URL")]
        assert len(searxng) == 0
