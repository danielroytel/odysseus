"""Tests for sandbox settings API endpoints."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from fastapi.testclient import TestClient
from fastapi import FastAPI

from routes.sandbox_routes import router as sandbox_router


def _make_app():
    app = FastAPI()
    app.include_router(sandbox_router)
    return app


@pytest.fixture
def app():
    return _make_app()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture(autouse=True)
def _settings_tmp(tmp_path, monkeypatch):
    """Redirect settings to a temp file so tests don't pollute real config."""
    import src.settings as s
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{}")
    monkeypatch.setattr(s, "SETTINGS_FILE", str(settings_file))
    s._invalidate_caches()


class TestSandboxStatus:
    def test_status_defaults(self, client):
        resp = client.get("/api/sandbox/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["runtime"] is None
        assert data["containers"] == []
        assert data["image"] == "odysseus/sandbox:latest"

    def test_status_returns_saved_config(self, client):
        from src.settings import save_settings
        save_settings({
            "sandbox_enabled": True,
            "sandbox_image": "python:3.12",
            "sandbox_memory": "8g",
        })
        resp = client.get("/api/sandbox/status")
        data = resp.json()
        assert data["enabled"] is True
        assert data["image"] == "python:3.12"
        assert data["memory"] == "8g"


class TestSandboxSettingsUpdate:
    def test_enable_sandbox(self, client):
        """Enabling sandbox should persist and attempt to create manager."""
        resp = client.post("/api/sandbox/settings", json={"enabled": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["enabled"] is True
        # Verify persisted
        from src.settings import get_setting
        assert get_setting("sandbox_enabled") is True

    def test_disable_sandbox(self, client):
        """Disabling sandbox should persist and clear manager."""
        from src.settings import save_settings
        save_settings({"sandbox_enabled": True})

        resp = client.post("/api/sandbox/settings", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        from src.settings import get_setting
        assert get_setting("sandbox_enabled") is False

    def test_save_image_setting(self, client):
        resp = client.post("/api/sandbox/settings", json={"image": "alpine:3.20"})
        assert resp.status_code == 200
        from src.settings import get_setting
        assert get_setting("sandbox_image") == "alpine:3.20"

    def test_save_memory_setting(self, client):
        resp = client.post("/api/sandbox/settings", json={"memory": "2g"})
        assert resp.status_code == 200
        from src.settings import get_setting
        assert get_setting("sandbox_memory") == "2g"

    def test_save_network_access(self, client):
        resp = client.post("/api/sandbox/settings", json={"network_access": True})
        assert resp.status_code == 200
        from src.settings import get_setting
        assert get_setting("sandbox_network_access") is True

    def test_save_credential_passthrough(self, client):
        resp = client.post("/api/sandbox/settings", json={
            "cred_git": False,
            "cred_gh": True,
            "cred_ssh": True,
        })
        assert resp.status_code == 200
        from src.settings import get_setting
        assert get_setting("sandbox_cred_git") is False
        assert get_setting("sandbox_cred_gh") is True
        assert get_setting("sandbox_cred_ssh") is True

    def test_enable_creates_manager(self, client):
        """When Docker is available, enabling creates a SandboxManager."""
        mock_mgr = MagicMock()
        mock_mgr._runtime = "docker"
        mock_mgr.detect_runtime = AsyncMock()
        mock_mgr.cleanup_orphans = AsyncMock()

        with patch("src.sandbox.SandboxManager", return_value=mock_mgr):
            resp = client.post("/api/sandbox/settings", json={"enabled": True})

        assert resp.status_code == 200
        assert resp.json()["enabled"] is True
        mock_mgr.detect_runtime.assert_called_once()

    def test_enable_handles_no_docker(self, client):
        """When Docker is unavailable, enabling returns 500."""
        with patch("src.sandbox.SandboxManager", side_effect=Exception("No docker")):
            resp = client.post("/api/sandbox/settings", json={"enabled": True})

        assert resp.status_code == 500
        assert "Failed to activate" in resp.json()["error"]
