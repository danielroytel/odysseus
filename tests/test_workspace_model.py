"""Unit tests for the Workspace model and Session.workspace_id binding."""
import time
import pytest

from core.database import (
    Workspace as DbWorkspace,
    Session as DbSession,
    SessionLocal,
    engine,
    init_db,
)
from core.models import Session


class TestWorkspaceModel:
    """Workspace SQLAlchemy model creation and serialization."""

    def test_to_dict_roundtrip(self):
        ws = DbWorkspace(
            id="ws-1",
            name="Project Alpha",
            path="/data/workspaces/ws-1",
            owner="alice",
            description="A test workspace",
            sandbox_image="python:3.12-slim",
            sandbox_memory="2g",
            sandbox_network=True,
        )
        d = ws.to_dict()
        assert d["id"] == "ws-1"
        assert d["name"] == "Project Alpha"
        assert d["path"] == "/data/workspaces/ws-1"
        assert d["owner"] == "alice"
        assert d["description"] == "A test workspace"
        assert d["sandbox_image"] == "python:3.12-slim"
        assert d["sandbox_memory"] == "2g"
        assert d["sandbox_network"] is True
        # Timestamps are None before persistence
        assert d["created_at"] is None
        assert d["updated_at"] is None

    def test_defaults(self):
        ws = DbWorkspace(id="ws-2", name="Minimal", path="/tmp/ws-2")
        d = ws.to_dict()
        assert d["owner"] is None
        assert d["description"] is None
        assert d["sandbox_image"] is None
        assert d["sandbox_memory"] is None
        assert d["sandbox_network"] is None


class TestSessionWorkspaceId:
    """Session dataclass and SQLAlchemy model workspace_id field."""

    def test_dataclass_workspace_id_default(self):
        s = Session(id="s1", name="Test", endpoint_url="http://x", model="gpt-4")
        assert s.workspace_id is None

    def test_dataclass_workspace_id_set(self):
        s = Session(
            id="s1", name="Test", endpoint_url="http://x", model="gpt-4",
            workspace_id="ws-42",
        )
        assert s.workspace_id == "ws-42"

    def test_sqlalchemy_model_has_workspace_id_column(self):
        cols = {c.name for c in DbSession.__table__.columns}
        assert "workspace_id" in cols


class TestWorkspaceMigration:
    """Verify migration functions are idempotent."""

    def test_migrations_run_without_error(self):
        """init_db should be safe to call multiple times."""
        from core.database import (
            _migrate_create_workspaces_table,
            _migrate_add_workspace_id_column,
        )
        # Running twice must not raise
        _migrate_create_workspaces_table()
        _migrate_add_workspace_id_column()
        _migrate_create_workspaces_table()
        _migrate_add_workspace_id_column()

    def test_workspaces_table_exists(self):
        """After migration the workspaces table must be present."""
        from sqlalchemy import text
        with engine.connect() as conn:
            tables = [r[0] for r in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='workspaces'"
            ))]
        assert "workspaces" in tables

    def test_sessions_has_workspace_id(self):
        """After migration sessions must have workspace_id column."""
        from sqlalchemy import text
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(sessions)"))]
        assert "workspace_id" in cols
