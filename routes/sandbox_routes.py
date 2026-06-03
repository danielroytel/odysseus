"""Routes for sandbox manager."""

import asyncio
import logging
import tempfile
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])


@router.get("/status")
async def sandbox_status(request: Request):
    """Get sandbox manager status and active containers with resource stats."""
    manager = getattr(request.app.state, "sandbox_manager", None)
    if not manager:
        return {
            "enabled": False,
            "runtime": None,
            "active_containers": 0,
            "containers": []
        }

    try:
        stats = await manager.container_stats()
    except Exception:
        stats = []

    return {
        "enabled": True,
        "runtime": manager._runtime,
        "active_containers": len(manager._containers),
        "containers": stats,
    }


@router.post("/cleanup")
async def sandbox_cleanup(request: Request):
    """Force cleanup of idle containers."""
    manager = getattr(request.app.state, "sandbox_manager", None)
    if not manager:
        return JSONResponse({"error": "Sandbox not enabled"}, status_code=400)
    cleaned = await manager.cleanup_idle()
    return {"cleaned": cleaned}


@router.post("/test")
async def sandbox_test(request: Request):
    """Test sandbox creation (create, exec uname -a, cleanup)."""
    manager = getattr(request.app.state, "sandbox_manager", None)
    if not manager:
        return JSONResponse({"error": "Sandbox not enabled"}, status_code=400)

    from src.sandbox import SandboxConfig

    test_session = f"test-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory() as tmpdir:
        config = SandboxConfig(image="alpine:3.20")  # Use tiny image for test
        try:
            container = await manager.get_or_create(test_session, tmpdir, config)
            exit_code, stdout, stderr = await manager.exec(test_session, "uname -a", timeout=30)
            await manager.cleanup(test_session)
            return {
                "success": exit_code == 0,
                "output": stdout.strip(),
                "container_id": container.container_id[:12],
                "cleaned_up": True,
            }
        except Exception as e:
            await manager.cleanup(test_session)
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)
