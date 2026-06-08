"""Integration flow test: complete workspace + sandbox lifecycle.

This test exercises the full path:
1. Enable sandbox via settings
2. Create workspace
3. Start container for workspace
4. Create session, attach to workspace
5. Verify tool execution routes through sandbox
6. Detach session, verify container still alive
7. Stop container
8. Delete workspace
"""
import time
import uuid
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.workspace_routes import setup_workspace_routes
from routes.sandbox_routes import router as sandbox_router
from src.sandbox import SandboxContainer, SandboxConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_app():
    app = FastAPI()
    app.include_router(setup_workspace_routes())
    app.include_router(sandbox_router)
    return app


@pytest.fixture(autouse=True)
def _auth_stubs():
    with patch("routes.workspace_routes.get_current_user", return_value="alice"), \
         patch("routes.workspace_routes.effective_user", return_value="alice"), \
         patch("routes.workspace_routes.owner_is_admin_or_single_user", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _settings_tmp(tmp_path, monkeypatch):
    """Redirect settings to a temp file."""
    import src.settings as s
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    monkeypatch.setattr(s, "SETTINGS_FILE", str(settings_file))
    s._invalidate_caches()


@pytest.fixture
def client():
    app = _make_app()
    c = TestClient(app)
    return c


@pytest.fixture
def client_with_sandbox():
    """Client with a mock SandboxManager on app.state."""
    app = _make_app()

    mock_mgr = MagicMock()
    mock_mgr._runtime = "docker"
    mock_mgr._containers = {}
    mock_mgr._references = {}
    mock_mgr._session_to_workspace = {}
    mock_mgr._lock = MagicMock()

    # Wire get_or_create to actually register sessions
    async def _get_or_create(session_id, workspace_dir, config, workspace_id=None):
        wid = workspace_id or f"_session_{session_id}"
        container = SandboxContainer(
            container_id=f"container-{wid[:8]}",
            workspace_id=wid,
            workspace_dir=workspace_dir,
            container_workspace="/workspace",
            image=config.image,
            created_at=time.time(),
            last_used_at=time.time(),
            config=config,
        )
        mock_mgr._containers[wid] = container
        mock_mgr._session_to_workspace[session_id] = wid
        mock_mgr._references.setdefault(wid, set()).add(session_id)
        return container

    async def _exec(session_id, command, **kwargs):
        wid = mock_mgr._session_to_workspace.get(session_id)
        if not wid or wid not in mock_mgr._containers:
            from src.sandbox import SandboxError
            raise SandboxError(f"No container for session {session_id}")
        return (0, f"mock output for: {command[:50]}", "")

    async def _cleanup_workspace(workspace_id):
        mock_mgr._containers.pop(workspace_id, None)
        mock_mgr._references.pop(workspace_id, None)
        mock_mgr._session_to_workspace = {
            s: w for s, w in mock_mgr._session_to_workspace.items()
            if w != workspace_id
        }

    async def _cleanup(session_id):
        wid = mock_mgr._session_to_workspace.pop(session_id, None)
        if wid:
            refs = mock_mgr._references.get(wid, set())
            refs.discard(session_id)
            if not refs:
                mock_mgr._containers.pop(wid, None)
                mock_mgr._references.pop(wid, None)

    async def _container_stats():
        stats = []
        for wid, container in mock_mgr._containers.items():
            refs = mock_mgr._references.get(wid, set())
            stats.append({
                "workspace_id": wid,
                "sessions": list(refs),
                "session_count": len(refs),
                "container_id": container.container_id[:12],
                "image": container.image,
            })
        return stats

    async def _cleanup_all():
        count = len(mock_mgr._containers)
        mock_mgr._containers.clear()
        mock_mgr._references.clear()
        mock_mgr._session_to_workspace.clear()
        return count

    mock_mgr.get_or_create = _get_or_create
    mock_mgr.exec = _exec
    mock_mgr._cleanup_workspace = _cleanup_workspace
    mock_mgr.cleanup = _cleanup
    mock_mgr.release = _cleanup
    mock_mgr.container_stats = _container_stats
    mock_mgr.cleanup_all = _cleanup_all

    app.state.sandbox_manager = mock_mgr
    return TestClient(app), mock_mgr


# ---------------------------------------------------------------------------
# Flow tests
# ---------------------------------------------------------------------------

class TestCompleteFlow:
    """Full lifecycle: enable sandbox → create workspace → start → attach → stop → delete."""

    def test_full_lifecycle(self, client_with_sandbox):
        client, mgr = client_with_sandbox

        # Note: The client_with_sandbox fixture already has a mock SandboxManager
        # on app.state. We don't enable via settings (which would replace the mock
        # with a real manager that tries podman/docker).

        # Step 1: Create workspace
        resp = client.post("/api/workspace", json={
            "name": "E2E Test WS",
            "description": "Full lifecycle test",
        })
        assert resp.status_code == 200
        ws = resp.json()
        ws_id = ws["id"]
        assert ws["name"] == "E2E Test WS"

        # Step 2: Check status (should be stopped)
        resp = client.get(f"/api/workspace/{ws_id}/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"

        # Step 3: Start container
        resp = client.post(f"/api/workspace/{ws_id}/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
        container_id = resp.json()["container_id"]

        # Step 4: Verify status now shows running
        resp = client.get(f"/api/workspace/{ws_id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["container_id"] == container_id

        # Step 5: Create a session and attach
        from core.database import Session as DbSession, SessionLocal
        db = SessionLocal()
        session_id = f"e2e-test-{uuid.uuid4().hex[:8]}"
        db.add(DbSession(
            id=session_id,
            name="E2E Test Session",
            endpoint_url="http://localhost",
            model="gpt-4",
            owner="alice",
        ))
        db.commit()
        db.close()

        resp = client.put(f"/api/workspace/{ws_id}/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["workspace_id"] == ws_id

        # Step 6: Verify session count in workspace list
        resp = client.get("/api/workspace")
        workspaces = resp.json()
        ws_data = [w for w in workspaces if w["id"] == ws_id][0]
        assert ws_data["session_count"] >= 1

        # Step 7: Note — attaching a session to a workspace only updates the DB.
        # The session gets registered in the sandbox manager only when a tool
        # actually executes (via _direct_fallback's lazy init) or when the
        # workspace container is started (which registers a pseudo session).
        # The prestart pseudo session should be registered:
        assert f"_prestart_{ws_id}" in mgr._session_to_workspace

        # Step 8: Detach session
        resp = client.delete(f"/api/workspace/{ws_id}/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["workspace_id"] is None

        # Step 9: Container should still be running (pre-started via Start)
        resp = client.get(f"/api/workspace/{ws_id}/status")
        assert resp.json()["status"] == "running"

        # Step 10: Stop container
        resp = client.post(f"/api/workspace/{ws_id}/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"

        # Step 11: Verify stopped
        resp = client.get(f"/api/workspace/{ws_id}/status")
        assert resp.json()["status"] == "stopped"

        # Step 12: Delete workspace
        resp = client.delete(f"/api/workspace/{ws_id}")
        assert resp.status_code == 200

        # Step 13: Verify gone
        resp = client.get(f"/api/workspace/{ws_id}")
        assert resp.status_code == 404

    def test_sandbox_status_reflects_settings(self, client):
        """Status endpoint reads sandbox_enabled from settings, not just app.state."""
        from src.settings import save_settings

        # Default: disabled
        resp = client.get("/api/sandbox/status")
        assert resp.json()["enabled"] is False

        # Enable via settings
        save_settings({"sandbox_enabled": True})
        resp = client.get("/api/sandbox/status")
        assert resp.json()["enabled"] is True

    def test_sandbox_settings_persist_config(self, client):
        """All sandbox config fields persist through the settings endpoint."""
        resp = client.post("/api/sandbox/settings", json={
            "image": "python:3.12",
            "memory": "8g",
            "cpus": "4",
            "idle_timeout": "3600",
            "network_access": True,
            "cred_git": False,
            "cred_gh": True,
            "cred_ssh": True,
        })
        assert resp.status_code == 200

        # Verify via status endpoint
        resp = client.get("/api/sandbox/status")
        data = resp.json()
        assert data["image"] == "python:3.12"
        assert data["memory"] == "8g"
        assert data["cpus"] == "4"
        assert data["idle_timeout"] == "3600"
        assert data["network_access"] is True
        assert data["cred_git"] is False
        assert data["cred_gh"] is True
        assert data["cred_ssh"] is True


class TestToolExecutionSandboxInit:
    """Verify _direct_fallback lazily initializes sandbox containers."""

    @pytest.mark.asyncio
    async def test_lazy_init_registers_session(self):
        """When sandbox_manager is given but session not registered,
        _direct_fallback should call get_or_create() first."""
        from src.tool_execution import _direct_fallback

        mock_mgr = MagicMock()
        mock_mgr._session_to_workspace = {}
        mock_mgr.get_or_create = AsyncMock(return_value=SandboxContainer(
            container_id="abc123",
            workspace_id="ws-1",
            workspace_dir="/tmp/ws",
            container_workspace="/workspace",
            image="alpine:3.20",
            created_at=time.time(),
            last_used_at=time.time(),
            config=SandboxConfig(),
        ))
        mock_mgr.exec = AsyncMock(return_value=(0, "hello", ""))

        result = await _direct_fallback(
            "bash",
            "echo hello",
            workspace="/tmp/ws",
            sandbox_manager=mock_mgr,
            workspace_id="ws-1",
            session_id="test-session-1",
        )

        assert result is not None
        assert result.get("exit_code") == 0
        mock_mgr.get_or_create.assert_called_once()
        mock_mgr.exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_registered_skips_init(self):
        """If session is already in _session_to_workspace, skip get_or_create."""
        from src.tool_execution import _direct_fallback

        mock_mgr = MagicMock()
        mock_mgr._session_to_workspace = {"test-session-2": "ws-2"}
        mock_mgr.exec = AsyncMock(return_value=(0, "hello", ""))

        result = await _direct_fallback(
            "bash",
            "echo hello",
            workspace="/tmp/ws",
            sandbox_manager=mock_mgr,
            workspace_id="ws-2",
            session_id="test-session-2",
        )

        assert result is not None
        assert result.get("exit_code") == 0
        mock_mgr.get_or_create.assert_not_called()
        mock_mgr.exec.assert_called_once()
