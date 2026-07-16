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
import omni.kit.app
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

        GET  /robot/camera/status        — get robot camera stream status
        GET  /robot/camera/latest        — get latest robot camera frame
        GET  /robot/navigation/stop      — stop the robot
        GET  /robot/navigation/return    — let the robot navigate back to its base position
        GET  /robot/navigation/route     — let the robot navigate along a route
        GET  /robot/incident-detection/routes  - receive all incident detection routes


        POST /simulation/incident        — spawn an incident
        POST /simulation/buy             — simulate someone buying items in the store
        POST /simulation/restock         — simulate a delivery truck restocking the store
                                           (also used for initial store population)

        GET  /store/populate-complete    — request a stage scan to inventory after store is populated

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
        app.router.add_get("/robot/status", self._handle_robot_status)
        app.router.add_post("/robot/navigation/stop", self._handle_stop_robot)
        app.router.add_post("/robot/navigation/return", self._handle_navigate_robot_to_base)
        app.router.add_post("/robot/navigation/route", self._handle_robot_navigate_route)
        app.router.add_get("/robot/shelf-analysis", self._handle_shelf_analysis)
        app.router.add_get("/robot/shelf-analysis/routes", self._handle_shelf_analysis_routes)
        app.router.add_get("/robot/camera/status", self._handle_robot_camera_status)
        app.router.add_get("/robot/camera/latest", self._handle_robot_camera_latest)
        app.router.add_get("/robot/incident-detection/routes", self._handle_incident_detection_routes)
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/simulation/incident", self._handle_spawn_incident)
        app.router.add_delete("/simulation/incident", self._handle_resolve_incident)
        app.router.add_post("/simulation/buy", self._handle_buy_product)
        app.router.add_post("/simulation/restock", self._handle_restock_product)
        app.router.add_post("/store/populate-complete", self._handle_store_populated)

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

    # ------------------------------------------------------------------
    # Robot Incident Detection Endpoints
    # ------------------------------------------------------------------
    async def _handle_incident_detection_routes(self, request):
        """
        List available robot incident detection routes.

        Returns JSON:
            {
                "routes": {
                    "robot_incident_detection_snacks": {"waypoints": [...]},
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
            routes = mgr._robot_controller.get_incident_detection_routes()

            return web.json_response({
                "routes": {
                    name: {"waypoint_count": len(data.get("waypoints", []))}
                    for name, data in routes.items()
                },
                "count": len(routes),
            })

        except Exception as e:
            carb.log_error(f"[APIServer] Incident detection routes error: {e}")
            import traceback
            carb.log_error(traceback.format_exc())
            return web.json_response(
                {"error": str(e), "routes": {}, "count": 0},
                status=500,
            )

    async def _handle_robot_navigate_route(self, request):
        """
        Command the robot to navigate a specified route.

        Expects JSON body:
            {
                "route": "robot_shelf_analysis_snacks"
            }
        """
        from aiohttp import web

        try:
            data = await request.json()
            route = data.get("route", "").strip()
            if not route:
                return web.json_response(
                    {"success": False, "error": "'route' field is required in JSON body"},
                    status=400,
                )

            from .robots.robot_controller import get_robot_controller

            rc = get_robot_controller()
            rc.initialize()
            navigation_result = rc.navigate_route(route)  # This will run in background and update robot state

            return web.json_response(navigation_result)

        except Exception as e:
            carb.log_error(f"[APIServer] Robot navigate route error: {e}")
            import traceback
            carb.log_error(traceback.format_exc())
            return web.json_response(
                {"success": False, "error": str(e)},
                status=500,
            )

    async def _handle_robot_status(self, request):
        """Return current robot status."""
        from aiohttp import web
        try:
            from .robots.robot_controller import get_robot_controller
            rc = get_robot_controller()
            rc.initialize()
            status = rc.get_status()
            return web.json_response(status)
        except Exception as e:
            carb.log_error(f"[APIServer] Robot status error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_stop_robot(self, request):
        """Stop robot movement."""
        from aiohttp import web
        try:
            from .robots.robot_controller import get_robot_controller
            rc = get_robot_controller()
            rc.initialize()
            result = rc.stop()
            return web.json_response(result)
        except Exception as e:
            carb.log_error(f"[APIServer] Robot couldn't be stopped: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_navigate_robot_to_base(self, request):
        """Let the robot navigate back to base."""
        from aiohttp import web
        try:
            from .robots.robot_controller import get_robot_controller
            rc = get_robot_controller()
            rc.initialize()
            rc.stop()
            result = rc.return_to_base()
            return web.json_response(result)
        except Exception as e:
            carb.log_error(f"[APIServer] Robot couldn't return to base: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_robot_camera_status(self, request):
        """Return Kit-side robot camera stream state."""
        from aiohttp import web
        try:
            from .robots.robot_controller import get_robot_controller
            rc = get_robot_controller()
            rc.initialize()
            status = rc.get_camera_stream_status()
            return web.json_response({"stream": status})
        except Exception as e:
            carb.log_error(f"[APIServer] Robot camera status error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def _handle_robot_camera_latest(self, request):
        """Return latest Kit-side robot frame (base64 JPEG + metadata)."""
        from aiohttp import web
        try:
            from .robots.robot_camera import get_robot_camera

            camera = get_robot_camera()
            frame = camera.get_latest_stream_frame()
            status = camera.get_stream_status()
            if not frame:
                return web.json_response(
                    {
                        "ok": False,
                        "error": "No frame available yet",
                        "stream": status,
                    },
                    status=404,
                )

            return web.json_response(
                {
                    "ok": True,
                    "robot_id": status.get("robot_id"),
                    "timestamp": status.get("last_frame_ts"),
                    "width": status.get("width"),
                    "height": status.get("height"),
                    "frame_data": frame,
                    "stream": status,
                }
            )
        except Exception as e:
            carb.log_error(f"[APIServer] Robot camera latest frame error: {e}")
            return web.json_response({"error": str(e)}, status=500)


    # -------------------------------------------------------------------------
    # Simulation Endpoints
    # -------------------------------------------------------------------------


    async def _handle_spawn_incident(self, request):
        """Spawn an incident in the store"""
        from aiohttp import web
        incident_types = ["fire", "trash", "spill", "random"]
        try:
            data = await request.json()
            incident_type = data.get("incident_type", "").strip()
            if incident_type not in incident_types:
                return web.json_response(
                    {
                        "success": False,
                        "error": f"Invalid incident type: Valid incident types: {incident_types}",
                    },
                    status=422,
                )

            # Get custom messaging object
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

            result = mgr._usd_spawner._on_incident_spawn_request({ "incident_type": incident_type})
            return web.json_response(result)
        except Exception as e:
            carb.log_error(f"[APIServer] Incident Spawner Error: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)


    async def _handle_resolve_incident(self, request):
        """Delete an incident in the store"""
        from aiohttp import web
        try:
            incident_id = request.query.get("incident_id")
            if not incident_id:
                return web.json_response(
                    {
                        "success": False,
                        "error": "Missing incident id.",
                    },
                    status=422,
                )

            # Get custom messaging object
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

            result = mgr._usd_spawner._on_incident_delete_request({ "incident_id": incident_id})
            return web.json_response(result)
        except Exception as e:
            carb.log_error(f"[APIServer] Incident Resolver Error: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)


    async def _handle_buy_product(self, request):
        """Remove a given quantity of a product from the store"""
        from aiohttp import web
        try:
            data = await request.json()
            asset_key = data.get("asset_key", "").strip()
            quantity = data.get("quantity", 1)

            # Get custom messaging object
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
            qty_removed = 0
            for i in range(quantity):
                success, info = mgr._usd_spawner._delete_usd(asset_key)
                if success:
                    qty_removed += 1
            success = True if qty_removed > 0 else False
            return web.json_response({"success": success, "qty_removed": qty_removed})
        except Exception as e:
            carb.log_error(f"[APIServer] Buy Products Error: {e}")
            return web.json_response(
                {"success": False, "error": f"Buying products failed: {e}", "qty_removed": 0},
                status=500,
            )


    async def _handle_restock_product(self, request):
        """Spawm a given quantity of a product in the store"""
        from aiohttp import web
        try:
            data = await request.json()
            asset_key = data.get("asset_key", "").strip()
            quantity = data.get("quantity", 1)
            rack_info = data.get("rack_info")
            skip_inventory_update = data.get("skip_inventory_update", False)

            # Get custom messaging object
            mgr = None
            try:
                from . import custom_messaging as _cm_mod
                mgr = getattr(_cm_mod, '_manager_instance', None)
            except Exception:
                pass

            if mgr is None:
                return web.json_response(
                    {"success": False, "error": "CustomMessageManager not available", "routes": {}, "count": 0},
                    status=503,
                )

            success, qty_restocked = mgr._usd_spawner._restock_product(rack_info, asset_key, quantity, skip_inventory_update=skip_inventory_update)
            return web.json_response({"success": success, "qty_restocked": qty_restocked})
        except Exception as e:
            carb.log_error(f"[APIServer] Restock Products Error: {e}")
            return web.json_response(
                {"success": False, "error": f"Restocking products failed: {e}", "qty_restocked": 0},
                status=500,
            )


    async def _handle_store_populated(self, request):
        """Spawm a given quantity of a product in the store"""
        from aiohttp import web
        try:

            # Get custom messaging object
            mgr = None
            try:
                from . import custom_messaging as _cm_mod
                mgr = getattr(_cm_mod, '_manager_instance', None)
            except Exception:
                pass

            if mgr is None:
                return web.json_response(
                    {"success": False, "error": "CustomMessageManager not available", "routes": {}, "count": 0},
                    status=503,
                )
            # Wait for 5 frames before scanning the stage (give items time to finish spawning)
            for _ in range(5):
                await omni.kit.app.get_app().next_update_async()

            mgr._usd_spawner._scan_stage_to_inventory()
            return web.json_response({"success": "True"})
        except Exception as e:
            carb.log_error(f"[APIServer] Scanning stage to inventory error: {e}")
            return web.json_response(
                {"success": False, "error": "Couldn't scan stage to inventory"},
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
