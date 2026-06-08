"""Unit tests for workspace CRUD API endpoints."""
import json
import os
import uuid
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Lightweight FastAPI app that mirrors the real setup just enough for tests
# ---------------------------------------------------------------------------

def _make_client():
    """Create a TestClient with workspace routes and auth stubs."""
    from fastapi import FastAPI
    from routes.workspace_routes import setup_workspace_routes

    app = FastAPI()
    app.include_router(setup_workspace_routes())

    # Stub auth helpers so every request looks like user "alice" (admin)
    app.dependency_overrides = {}
    # We patch at module level instead of using Depends overrides because
    # the routes call the helpers directly.
    return TestClient(app)


@pytest.fixture(autouse=True)
def _auth_stubs():
    """Patch auth helpers so tests run without real auth."""
    with patch("routes.workspace_routes.get_current_user", return_value="alice"), \
         patch("routes.workspace_routes.effective_user", return_value="alice"), \
         patch("routes.workspace_routes.owner_is_admin_or_single_user", return_value=True):
        yield


@pytest.fixture
def client():
    return _make_client()


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------

class TestCreateWorkspace:
    def test_create_basic(self, client):
        resp = client.post("/api/workspace", json={"name": "Test WS"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test WS"
        assert data["id"]
        assert data["owner"] == "alice"
        assert data["path"]

    def test_create_with_sandbox_config(self, client):
        resp = client.post("/api/workspace", json={
            "name": "Sandbox WS",
            "description": "with overrides",
            "sandbox_image": "python:3.12",
            "sandbox_memory": "2g",
            "sandbox_network": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["sandbox_image"] == "python:3.12"
        assert data["sandbox_memory"] == "2g"
        assert data["sandbox_network"] is True


class TestListWorkspaces:
    def test_list_empty(self, client):
        # Clean up any workspaces from other tests first
        resp = client.get("/api/workspace")
        assert resp.status_code == 200
        # May have existing workspaces from other tests; just check shape
        assert isinstance(resp.json(), list)

    def test_list_includes_created(self, client):
        create = client.post("/api/workspace", json={"name": "List Test"})
        ws_id = create.json()["id"]
        resp = client.get("/api/workspace")
        ids = [ws["id"] for ws in resp.json()]
        assert ws_id in ids


class TestGetWorkspace:
    def test_get_existing(self, client):
        create = client.post("/api/workspace", json={"name": "Get Test"})
        ws_id = create.json()["id"]
        resp = client.get(f"/api/workspace/{ws_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Get Test"

    def test_get_not_found(self, client):
        resp = client.get("/api/workspace/nonexistent")
        assert resp.status_code == 404


class TestUpdateWorkspace:
    def test_update_name(self, client):
        create = client.post("/api/workspace", json={"name": "Old Name"})
        ws_id = create.json()["id"]
        resp = client.patch(f"/api/workspace/{ws_id}", json={"name": "New Name"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"

    def test_update_sandbox_config(self, client):
        create = client.post("/api/workspace", json={"name": "Config WS"})
        ws_id = create.json()["id"]
        resp = client.patch(f"/api/workspace/{ws_id}", json={
            "sandbox_image": "alpine:3.20",
            "sandbox_network": False,
        })
        assert resp.status_code == 200
        assert resp.json()["sandbox_image"] == "alpine:3.20"


class TestDeleteWorkspace:
    def test_delete_existing(self, client):
        create = client.post("/api/workspace", json={"name": "Delete Me"})
        ws_id = create.json()["id"]
        resp = client.delete(f"/api/workspace/{ws_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == ws_id
        # Verify it's gone
        get_resp = client.get(f"/api/workspace/{ws_id}")
        assert get_resp.status_code == 404

    def test_delete_not_found(self, client):
        resp = client.delete("/api/workspace/nonexistent")
        assert resp.status_code == 404


class TestSessionAttachment:
    def test_attach_and_detach(self, client):
        # Create workspace
        ws = client.post("/api/workspace", json={"name": "Attach WS"})
        ws_id = ws.json()["id"]

        # Create a session directly in the DB
        from core.database import Session as DbSession, SessionLocal
        db = SessionLocal()
        session_id = f"test-attach-{uuid.uuid4().hex[:8]}"
        db.add(DbSession(
            id=session_id,
            name="Test Session",
            endpoint_url="http://localhost",
            model="gpt-4",
            owner="alice",
        ))
        db.commit()
        db.close()

        # Attach
        resp = client.put(f"/api/workspace/{ws_id}/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["workspace_id"] == ws_id

        # Verify attachment
        db = SessionLocal()
        sess = db.query(DbSession).filter(DbSession.id == session_id).first()
        assert sess.workspace_id == ws_id
        db.close()

        # Detach
        resp = client.delete(f"/api/workspace/{ws_id}/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["workspace_id"] is None

        # Verify detachment
        db = SessionLocal()
        sess = db.query(DbSession).filter(DbSession.id == session_id).first()
        assert sess.workspace_id is None
        db.close()


class TestContainerManagement:
    """Tests for start/stop/status endpoints (mocked Docker)."""

    @pytest.fixture
    def client_with_sandbox(self):
        """Client with a mock sandbox_manager on app.state."""
        from fastapi import FastAPI
        from routes.workspace_routes import setup_workspace_routes
        from src.sandbox import SandboxContainer, SandboxConfig
        import time

        app = FastAPI()
        app.include_router(setup_workspace_routes())

        mock_mgr = MagicMock()
        mock_container = SandboxContainer(
            container_id="abc123container",
            workspace_id="",
            workspace_dir="/tmp/ws",
            container_workspace="/workspace",
            image="odysseus/sandbox:latest",
            created_at=time.time(),
            last_used_at=time.time(),
            config=SandboxConfig(),
        )
        mock_mgr.get_or_create = AsyncMock(return_value=mock_container)
        mock_mgr._cleanup_workspace = AsyncMock()
        mock_mgr._containers = {}

        app.state.sandbox_manager = mock_mgr
        return TestClient(app), mock_mgr, mock_container

    def test_status_stopped(self, client_with_sandbox):
        client, mgr, _ = client_with_sandbox
        ws = client.post("/api/workspace", json={"name": "Status WS"})
        ws_id = ws.json()["id"]
        resp = client.get(f"/api/workspace/{ws_id}/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"

    def test_status_running(self, client_with_sandbox):
        client, mgr, container = client_with_sandbox
        ws = client.post("/api/workspace", json={"name": "Running WS"})
        ws_id = ws.json()["id"]
        # Simulate a running container
        container.workspace_id = ws_id
        mgr._containers[ws_id] = container
        mgr._references = {ws_id: {"session-1"}}
        resp = client.get(f"/api/workspace/{ws_id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert data["container_id"] == "abc123contai"

    def test_start_container(self, client_with_sandbox):
        client, mgr, _ = client_with_sandbox
        ws = client.post("/api/workspace", json={"name": "Start WS"})
        ws_id = ws.json()["id"]
        resp = client.post(f"/api/workspace/{ws_id}/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
        mgr.get_or_create.assert_called_once()

    def test_stop_container(self, client_with_sandbox):
        client, mgr, container = client_with_sandbox
        ws = client.post("/api/workspace", json={"name": "Stop WS"})
        ws_id = ws.json()["id"]
        container.workspace_id = ws_id
        mgr._containers[ws_id] = container
        resp = client.post(f"/api/workspace/{ws_id}/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        mgr._cleanup_workspace.assert_called_once_with(ws_id)

    def test_start_no_sandbox_manager(self, client):
        """Start returns 503 when sandbox_manager is not available."""
        ws = client.post("/api/workspace", json={"name": "No SBMgr"})
        ws_id = ws.json()["id"]
        # The default test client doesn't set sandbox_manager on app.state
        resp = client.post(f"/api/workspace/{ws_id}/start")
        assert resp.status_code == 503
