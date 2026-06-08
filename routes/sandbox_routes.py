"""Routes for sandbox manager."""

import asyncio
import logging
import tempfile
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])


class SandboxSettingUpdate(BaseModel):
    """Single sandbox setting update."""
    key: str  # Not used directly — the body IS {key: value}
    pass


@router.get("/status")
async def sandbox_status(request: Request):
    """Get sandbox manager status and active containers with resource stats."""
    from src.settings import get_setting, SANDBOX_DEFAULTS

    enabled = get_setting("sandbox_enabled", False)
    manager = getattr(request.app.state, "sandbox_manager", None)

    # Sandbox config from persisted settings (fall back to defaults)
    image = get_setting("sandbox_image", SANDBOX_DEFAULTS["image"])
    memory = get_setting("sandbox_memory", SANDBOX_DEFAULTS["memory"])
    cpus = get_setting("sandbox_cpus", SANDBOX_DEFAULTS["cpus"])
    idle_timeout = get_setting("sandbox_idle_timeout", SANDBOX_DEFAULTS["idle_timeout"])
    network_access = get_setting("sandbox_network_access", SANDBOX_DEFAULTS["network"])
    cred_git = get_setting("sandbox_cred_git", True)
    cred_gh = get_setting("sandbox_cred_gh", True)
    cred_ssh = get_setting("sandbox_cred_ssh", False)

    # Ensure cpus/idle_timeout are strings for frontend select elements
    cpus = str(cpus)
    idle_timeout = str(idle_timeout)

    result = {
        "enabled": enabled,
        "image": image,
        "memory": memory,
        "cpus": cpus,
        "idle_timeout": idle_timeout,
        "network_access": network_access,
        "cred_git": cred_git,
        "cred_gh": cred_gh,
        "cred_ssh": cred_ssh,
        "runtime": None,
        "active_containers": 0,
        "containers": [],
    }

    if manager:
        result["runtime"] = manager._runtime
        result["active_containers"] = len(manager._containers)
        try:
            result["containers"] = await manager.container_stats()
        except Exception:
            pass

    return result


@router.post("/settings")
async def sandbox_update_settings(request: Request):
    """Update sandbox settings and activate/deactivate the manager at runtime."""
    body = await request.json()
    from src.settings import load_settings, save_settings

    settings = load_settings()
    changed_enabled = False
    new_enabled = None

    # Map frontend keys to settings keys
    key_map = {
        "enabled": "sandbox_enabled",
        "image": "sandbox_image",
        "memory": "sandbox_memory",
        "cpus": "sandbox_cpus",
        "idle_timeout": "sandbox_idle_timeout",
        "network_access": "sandbox_network_access",
        "cred_git": "sandbox_cred_git",
        "cred_gh": "sandbox_cred_gh",
        "cred_ssh": "sandbox_cred_ssh",
    }

    for frontend_key, value in body.items():
        settings_key = key_map.get(frontend_key, f"sandbox_{frontend_key}")
        settings[settings_key] = value
        if frontend_key == "enabled":
            changed_enabled = True
            new_enabled = value

    save_settings(settings)

    # Activate/deactivate SandboxManager at runtime
    if changed_enabled:
        if new_enabled:
            # Create and initialize SandboxManager
            try:
                from src.sandbox import SandboxManager
                mgr = SandboxManager()
                await mgr.detect_runtime()
                try:
                    await mgr.cleanup_orphans()
                except Exception:
                    pass
                request.app.state.sandbox_manager = mgr
                logger.info(f"Sandbox manager activated at runtime (runtime: {mgr._runtime})")
            except Exception as e:
                logger.warning(f"Failed to activate sandbox: {e}")
                request.app.state.sandbox_manager = None
                return JSONResponse(
                    {"error": f"Failed to activate sandbox: {e}"},
                    status_code=500,
                )
        else:
            # Deactivate: cleanup all containers
            mgr = getattr(request.app.state, "sandbox_manager", None)
            if mgr:
                try:
                    await mgr.cleanup_all()
                except Exception:
                    pass
                request.app.state.sandbox_manager = None
                logger.info("Sandbox manager deactivated at runtime")

    # Return current status so the frontend can sync
    manager = getattr(request.app.state, "sandbox_manager", None)
    return {
        "ok": True,
        "enabled": new_enabled if changed_enabled else settings.get("sandbox_enabled", False),
        "runtime": manager._runtime if manager else None,
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
