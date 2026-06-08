"""Workspace API — browse server directories + named workspace CRUD."""
import logging
import os
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel

from core.database import SessionLocal, Workspace as DbWorkspace, Session as DbSession, get_db_session
from sqlalchemy import func
from src.auth_helpers import get_current_user, effective_user
from src.tool_security import owner_is_admin_or_single_user

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic request/response models
# ---------------------------------------------------------------------------

class WorkspaceCreate(BaseModel):
    name: str
    description: Optional[str] = None
    sandbox_image: Optional[str] = None
    sandbox_memory: Optional[str] = None
    sandbox_network: Optional[bool] = None


class WorkspaceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    sandbox_image: Optional[str] = None
    sandbox_memory: Optional[str] = None
    sandbox_network: Optional[bool] = None


class WorkspaceShare(BaseModel):
    username: str


class SessionWorkspace(BaseModel):
    workspace_id: Optional[str] = None  # null = detach


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORKSPACE_BASE_DIR = os.path.join("data", "workspaces")


def _ensure_workspace_dir(workspace_id: str) -> str:
    """Create and return the filesystem directory for a workspace."""
    path = os.path.join(WORKSPACE_BASE_DIR, workspace_id)
    os.makedirs(path, exist_ok=True)
    return path


def _workspace_to_dict(ws: DbWorkspace, session_count: int = 0) -> dict:
    """Serialize a workspace row with extra computed fields."""
    d = ws.to_dict()
    d["session_count"] = session_count
    return d


# ---------------------------------------------------------------------------
# Route setup
# ---------------------------------------------------------------------------

def setup_workspace_routes():
    router = APIRouter(prefix="/api/workspace", tags=["workspace"])

    # ----- Directory browser (admin-only, existing) -----

    @router.get("/browse")
    def browse(request: Request, path: str = Query(default="")):
        """List subdirectories of `path` (default: home) so the UI can navigate
        the server filesystem and pick a workspace folder. Directories only.

        ADMIN-ONLY: this enumerates the server filesystem, so it is gated the
        same way the file/shell tools are (read_file/write_file/bash are in
        NON_ADMIN_BLOCKED_TOOLS). A non-admin who can't use those tools must not
        be able to map the host's directory tree either.
        """
        owner = get_current_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Workspace browsing is admin-only")

        target = os.path.realpath(os.path.expanduser(path.strip() or "~"))
        if not os.path.isdir(target):
            target = os.path.realpath(os.path.expanduser("~"))

        dirs = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                            dirs.append({"name": entry.name, "path": os.path.join(target, entry.name)})
                    except OSError:
                        continue
        except (PermissionError, OSError):
            dirs = []

        parent = os.path.dirname(target)
        return {
            "path": target,
            "parent": parent if parent and parent != target else None,
            "dirs": sorted(dirs, key=lambda d: d["name"].lower()),
        }

    # ----- CRUD -----

    @router.post("")
    def create_workspace(body: WorkspaceCreate, request: Request):
        """Create a new named workspace."""
        owner = effective_user(request)
        ws_id = uuid.uuid4().hex
        ws_path = _ensure_workspace_dir(ws_id)

        db = SessionLocal()
        try:
            ws = DbWorkspace(
                id=ws_id,
                name=body.name,
                path=ws_path,
                owner=owner,
                description=body.description,
                sandbox_image=body.sandbox_image,
                sandbox_memory=body.sandbox_memory,
                sandbox_network=body.sandbox_network,
            )
            db.add(ws)
            db.commit()
            db.refresh(ws)
            return _workspace_to_dict(ws)
        except Exception as e:
            db.rollback()
            logger.error("Failed to create workspace: %s", e)
            raise HTTPException(500, "Failed to create workspace")
        finally:
            db.close()

    @router.get("")
    def list_workspaces(request: Request):
        """List workspaces owned by or shared with the current user."""
        owner = effective_user(request)
        db = SessionLocal()
        try:
            workspaces = db.query(DbWorkspace).filter(
                (DbWorkspace.owner == owner) | (DbWorkspace.owner == None)
            ).all()

            # Count active sessions per workspace
            ws_ids = [ws.id for ws in workspaces]
            counts = {}
            if ws_ids:
                rows = db.query(
                    DbSession.workspace_id,
                    func.count(DbSession.id)
                ).filter(
                    DbSession.workspace_id.in_(ws_ids),
                    DbSession.archived == False,
                ).group_by(DbSession.workspace_id).all()
                counts = {r[0]: r[1] for r in rows}

            return [_workspace_to_dict(ws, session_count=counts.get(ws.id, 0))
                    for ws in workspaces]
        finally:
            db.close()

    @router.get("/{workspace_id}")
    def get_workspace(workspace_id: str, request: Request):
        """Get workspace details."""
        owner = effective_user(request)
        db = SessionLocal()
        try:
            ws = db.query(DbWorkspace).filter(DbWorkspace.id == workspace_id).first()
            if not ws:
                raise HTTPException(404, "Workspace not found")
            if ws.owner and ws.owner != owner:
                raise HTTPException(403, "Not your workspace")
            session_count = db.query(DbSession).filter(
                DbSession.workspace_id == workspace_id,
                DbSession.archived == False,
            ).count()
            return _workspace_to_dict(ws, session_count=session_count)
        finally:
            db.close()

    @router.patch("/{workspace_id}")
    def update_workspace(workspace_id: str, body: WorkspaceUpdate, request: Request):
        """Update workspace metadata and sandbox config."""
        owner = effective_user(request)
        db = SessionLocal()
        try:
            ws = db.query(DbWorkspace).filter(DbWorkspace.id == workspace_id).first()
            if not ws:
                raise HTTPException(404, "Workspace not found")
            if ws.owner and ws.owner != owner:
                raise HTTPException(403, "Not your workspace")

            if body.name is not None:
                ws.name = body.name
            if body.description is not None:
                ws.description = body.description
            if body.sandbox_image is not None:
                ws.sandbox_image = body.sandbox_image
            if body.sandbox_memory is not None:
                ws.sandbox_memory = body.sandbox_memory
            if body.sandbox_network is not None:
                ws.sandbox_network = body.sandbox_network

            db.commit()
            db.refresh(ws)
            return _workspace_to_dict(ws)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to update workspace: %s", e)
            raise HTTPException(500, "Failed to update workspace")
        finally:
            db.close()

    @router.delete("/{workspace_id}")
    def delete_workspace(workspace_id: str, request: Request):
        """Delete workspace and its on-disk directory."""
        owner = effective_user(request)
        db = SessionLocal()
        try:
            ws = db.query(DbWorkspace).filter(DbWorkspace.id == workspace_id).first()
            if not ws:
                raise HTTPException(404, "Workspace not found")
            if ws.owner and ws.owner != owner:
                raise HTTPException(403, "Not your workspace")

            # Detach all sessions
            db.query(DbSession).filter(
                DbSession.workspace_id == workspace_id
            ).update({"workspace_id": None}, synchronize_session=False)

            # Remove DB record
            db.delete(ws)
            db.commit()

            # Remove on-disk directory
            if ws.path and os.path.isdir(ws.path):
                import shutil
                shutil.rmtree(ws.path, ignore_errors=True)

            return {"deleted": workspace_id}
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to delete workspace: %s", e)
            raise HTTPException(500, "Failed to delete workspace")
        finally:
            db.close()

    # ----- Session attachment -----

    @router.put("/{workspace_id}/sessions/{session_id}")
    def attach_session(workspace_id: str, session_id: str, request: Request):
        """Attach a session to a workspace."""
        owner = effective_user(request)
        db = SessionLocal()
        try:
            ws = db.query(DbWorkspace).filter(DbWorkspace.id == workspace_id).first()
            if not ws:
                raise HTTPException(404, "Workspace not found")
            if ws.owner and ws.owner != owner:
                raise HTTPException(403, "Not your workspace")

            session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if not session:
                raise HTTPException(404, "Session not found")
            if session.owner and session.owner != owner:
                raise HTTPException(403, "Not your session")

            session.workspace_id = workspace_id
            db.commit()
            return {"session_id": session_id, "workspace_id": workspace_id}
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(500, str(e))
        finally:
            db.close()

    @router.delete("/{workspace_id}/sessions/{session_id}")
    def detach_session(workspace_id: str, session_id: str, request: Request):
        """Detach a session from its workspace."""
        owner = effective_user(request)
        db = SessionLocal()
        try:
            session = db.query(DbSession).filter(DbSession.id == session_id).first()
            if not session:
                raise HTTPException(404, "Session not found")
            if session.owner and session.owner != owner:
                raise HTTPException(403, "Not your session")
            if session.workspace_id != workspace_id:
                raise HTTPException(400, "Session not in this workspace")

            session.workspace_id = None
            db.commit()
            return {"session_id": session_id, "workspace_id": None}
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(500, str(e))
        finally:
            db.close()

    # ----- Container management -----

    @router.post("/{workspace_id}/start")
    async def start_container(workspace_id: str, request: Request):
        """Pre-start a Docker container for the workspace."""
        import asyncio
        from src.sandbox import SandboxConfig

        owner = effective_user(request)
        db = SessionLocal()
        try:
            ws = db.query(DbWorkspace).filter(DbWorkspace.id == workspace_id).first()
            if not ws:
                raise HTTPException(404, "Workspace not found")
            if ws.owner and ws.owner != owner:
                raise HTTPException(403, "Not your workspace")

            sandbox_mgr = getattr(request.app.state, "sandbox_manager", None)
            if not sandbox_mgr:
                raise HTTPException(503, "Sandbox manager not available")

            # Build config from workspace overrides
            from src import settings as app_settings
            config = SandboxConfig(
                image=ws.sandbox_image or app_settings.SANDBOX_DEFAULTS["image"],
                memory=ws.sandbox_memory or app_settings.SANDBOX_DEFAULTS["memory"],
                network=ws.sandbox_network if ws.sandbox_network is not None else app_settings.SANDBOX_DEFAULTS["network"],
            )

            # Use a pseudo session id for pre-started containers
            pseudo_session = f"_prestart_{workspace_id}"
            container = await sandbox_mgr.get_or_create(
                session_id=pseudo_session,
                workspace_dir=ws.path,
                config=config,
                workspace_id=workspace_id,
            )
            return {
                "status": "running",
                "container_id": container.container_id[:12],
                "workspace_id": workspace_id,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to start workspace container: %s", e)
            raise HTTPException(500, f"Failed to start container: {e}")
        finally:
            db.close()

    @router.post("/{workspace_id}/stop")
    async def stop_container(workspace_id: str, request: Request):
        """Stop the Docker container for the workspace."""
        owner = effective_user(request)
        db = SessionLocal()
        try:
            ws = db.query(DbWorkspace).filter(DbWorkspace.id == workspace_id).first()
            if not ws:
                raise HTTPException(404, "Workspace not found")
            if ws.owner and ws.owner != owner:
                raise HTTPException(403, "Not your workspace")

            sandbox_mgr = getattr(request.app.state, "sandbox_manager", None)
            if not sandbox_mgr:
                raise HTTPException(503, "Sandbox manager not available")

            await sandbox_mgr._cleanup_workspace(workspace_id)
            return {"status": "stopped", "workspace_id": workspace_id}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to stop workspace container: %s", e)
            raise HTTPException(500, f"Failed to stop container: {e}")
        finally:
            db.close()

    @router.get("/{workspace_id}/status")
    async def container_status(workspace_id: str, request: Request):
        """Get container status for a workspace."""
        owner = effective_user(request)
        db = SessionLocal()
        try:
            ws = db.query(DbWorkspace).filter(DbWorkspace.id == workspace_id).first()
            if not ws:
                raise HTTPException(404, "Workspace not found")
            if ws.owner and ws.owner != owner:
                raise HTTPException(403, "Not your workspace")

            sandbox_mgr = getattr(request.app.state, "sandbox_manager", None)
            container = sandbox_mgr._containers.get(workspace_id) if sandbox_mgr else None

            if container:
                refs = sandbox_mgr._references.get(workspace_id, set())
                return {
                    "status": "running",
                    "container_id": container.container_id[:12],
                    "workspace_id": workspace_id,
                    "sessions": list(refs),
                    "session_count": len(refs),
                    "image": container.image,
                    "created_at": container.created_at,
                    "idle_s": round(time.time() - container.last_used_at),
                }
            return {"status": "stopped", "workspace_id": workspace_id}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to get container status: %s", e)
            raise HTTPException(500, str(e))
        finally:
            db.close()

    return router
