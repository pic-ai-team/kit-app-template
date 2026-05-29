# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

"""
API HTTP Server — a lightweight aiohttp server running inside the Kit process
that exposes Kit functionalities to external services (e.g., Agent Backend).

Default port: 8100
"""

import asyncio
import json
import logging
from typing import Optional

import carb

# Default port — can be overridden via /app/cctv/port setting
DEFAULT_PORT = 8100

logger = logging.getLogger(__name__)


class APIServer:
    """
    HTTP server exposing Kit functionalities to external services.

    Endpoints:
        GET  /cctv/capture               — capture all CCTV feeds (returns JSON with base64 frames)
        GET  /cctv/positions             — list registered CCTV positions
        GET  /robot/shelf-analysis       — run automatic shelf analysis along a robot route
                                           (returns per-rack stock data)
        GET  /robot/shelf-analysis/routes — list available robot shelf analysis routes
        GET  /health                     — simple health check
    """

    def __init__(self, port: int = DEFAULT_PORT):
        self._port = port
        self._site: Optional[object] = None
        self._runner: Optional[object] = None
        self._started = False

    async def start(self):
        """Start the HTTP server."""
        if self._started:
            carb.log_warn("[APIServer] Already running")
            return

        try:
            from aiohttp import web
        except ImportError:
            carb.log_error(
                "[APIServer] aiohttp not available. Install with: "
                "pip install aiohttp"
            )
            return

        app = web.Application()
        app.router.add_get("/cctv/capture", self._handle_capture)
        app.router.add_get("/cctv/positions", self._handle_positions)
        app.router.add_get("/robot/shelf-analysis", self._handle_shelf_analysis)
        app.router.add_get("/robot/shelf-analysis/routes", self._handle_shelf_analysis_routes)
        app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await self._site.start()
        self._started = True
        carb.log_info(f"[APIServer] Started on port {self._port}")

    async def stop(self):
        """Stop the HTTP server."""
        if not self._started:
            return

        try:
            if self._site:
                await self._site.stop()
            if self._runner:
                await self._runner.cleanup()
        except Exception as e:
            carb.log_warn(f"[APIServer] Error during shutdown: {e}")
        finally:
            self._site = None
            self._runner = None
            self._started = False
            carb.log_info("[APIServer] Stopped")

    async def _handle_health(self, request):
        """Health check — just confirms the server is alive."""
        from aiohttp import web

        return web.json_response({"status": "ok", "service": "kit-cctv"})

    async def _handle_positions(self, request):
        """Return all real CCTV camera prims with camera_name, camera_id, and location."""
        from aiohttp import web
        from .cctv_capture import get_cctv_capture

        capture = get_cctv_capture()
        cameras = capture._get_camera_prims()
        return web.json_response({
            "positions": [
                {
                    "camera_name": cam["camera_name"],
                    "camera_id": cam["camera_id"],
                    "prim_path": cam["prim_path"],
                    "location": cam["location"]
                }
                for cam in cameras
            ],
            "count": len(cameras),
        })

    async def _handle_capture(self, request):
        """
        Capture frames from all real CCTV camera prims.

        Query params:
            cameras (optional): Comma-separated camera IDs to capture.
                                If omitted, captures ALL cameras.

        Returns JSON:
            {
                "feeds": [
                    {"camera_name": ..., "camera_id": ..., "prim_path": ..., "location": [...], "frame_data": "<base64>"}, ...
                ],
                "count": N
            }
        """
        from aiohttp import web
        from .cctv_capture import get_cctv_capture

        capture = get_cctv_capture()

        try:
            feeds = await capture.capture_all_feeds()

            # Optional filter by camera IDs
            cameras_param = request.query.get("cameras")
            if cameras_param:
                requested = set(c.strip() for c in cameras_param.split(","))
                feeds = [f for f in feeds if f["camera_id"] in requested]

            return web.json_response({
                "feeds": feeds,
                "count": len(feeds),
            })

        except Exception as e:
            carb.log_error(f"[APIServer] Capture error: {e}")
            import traceback
            carb.log_error(traceback.format_exc())
            return web.json_response(
                {"error": str(e), "feeds": [], "count": 0},
                status=500,
            )

    async def _handle_shelf_analysis(self, request):
        """
        Run automatic shelf analysis using the service robot.

        The robot navigates to each waypoint in the specified route, captures
        a high-resolution frame from its on-board camera, identifies products
        via the vision backend, and computes per-rack stock levels. The robot
        returns to its initial position after the analysis is complete.

        Query params:
            route (required): Name of a robot_shelf_analysis_* route.
            tolerance_cm (optional): Row clustering tolerance (default 8.0).
            model (optional): Vision model — "qwen" (default) or "cosmos".

        Returns JSON with per-rack analysis including rack_id, rack_name,
        stock_level, shelf_levels with product stock ratios.
        """
        from aiohttp import web

        route = request.query.get("route", "").strip()
        if not route:
            return web.json_response(
                {"success": False, "error": "'route' query parameter is required"},
                status=400,
            )

        tolerance_cm = float(request.query.get("tolerance_cm", "8.0"))
        model = request.query.get("model", "qwen")

        try:
            mgr = None
            try:
                from . import custom_messaging as _cm_mod
                mgr = getattr(_cm_mod, '_manager_instance', None)
            except Exception:
                pass

            if mgr is None:
                carb.log_warn("[APIServer] No CustomMessageManager found")
                return web.json_response(
                    {"success": False, "error": "CustomMessageManager not available"},
                    status=503,
                )

            result = await mgr._run_automatic_shelf_analysis(
                route, {"tolerance_cm": tolerance_cm, "model": model}
            )

            if not result or not result.get("success"):
                error_msg = (
                    result.get("error", "Analysis produced no results")
                    if result else "Analysis returned empty"
                )
                return web.json_response(
                    {"success": False, "error": error_msg},
                    status=422,
                )

            return web.json_response(result)

        except Exception as e:
            carb.log_error(f"[APIServer] Shelf analysis error: {e}")
            import traceback
            carb.log_error(traceback.format_exc())
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500,
            )

    async def _handle_shelf_analysis_routes(self, request):
        """
        List available robot shelf analysis routes.

        Returns JSON:
            {
                "routes": {
                    "robot_shelf_analysis_snacks": {"waypoints": [...]},
                    ...
                },
                "count": N
            }
        """
        from aiohttp import web

        try:
            mgr = None
            try:
                from . import custom_messaging as _cm_mod
                mgr = getattr(_cm_mod, '_manager_instance', None)
            except Exception:
                pass

            if mgr is None:
                return web.json_response(
                    {"error": "CustomMessageManager not available", "routes": {}, "count": 0},
                    status=503,
                )

            # Initialize robot controller and reload routes from disk
            mgr._init_robot()
            mgr._robot_controller.reload_from_disk()
            routes = mgr._robot_controller.get_shelf_analysis_routes()

            return web.json_response({
                "routes": {
                    name: {"waypoint_count": len(data.get("waypoints", []))}
                    for name, data in routes.items()
                },
                "count": len(routes),
            })

        except Exception as e:
            carb.log_error(f"[APIServer] Shelf analysis routes error: {e}")
            import traceback
            carb.log_error(traceback.format_exc())
            return web.json_response(
                {"error": str(e), "routes": {}, "count": 0},
                status=500,
            )


# Module-level singleton
_server: Optional[APIServer] = None


def get_api_server(port: int = DEFAULT_PORT) -> APIServer:
    """Get or create the singleton API server."""
    global _server
    if _server is None:
        _server = APIServer(port=port)
    return _server


async def start_api_server(port: int = DEFAULT_PORT):
    """Convenience: create and start the API server."""
    server = get_api_server(port)
    await server.start()
    return server


async def stop_api_server():
    """Convenience: stop the API server if running."""
    global _server
    if _server:
        await _server.stop()
        _server = None
