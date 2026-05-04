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
        GET  /cctv/capture    — capture all CCTV feeds (returns JSON with base64 frames)
        GET  /cctv/positions  — list registered CCTV positions
        GET  /health          — simple health check
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
        """Return all registered CCTV positions."""
        from aiohttp import web
        from .cctv_capture import get_cctv_capture

        capture = get_cctv_capture()
        positions = capture._get_cctv_positions()

        return web.json_response({
            "positions": {
                k: {
                    "location": v.get("location", [0, 0, 0]),
                    "rotation": v.get("rotation", [0, 0, 0]),
                    "description": v.get("description", k),
                }
                for k, v in positions.items()
            },
            "count": len(positions),
        })

    async def _handle_capture(self, request):
        """
        Capture frames from all CCTV positions.

        Query params:
            cameras (optional): Comma-separated camera IDs to capture.
                                If omitted, captures ALL cctv_ positions.

        Returns JSON:
            {
                "feeds": [
                    {"camera_id": "cctv_entrance", "frame_data": "<base64>",
                     "location": [...], "rotation": [...], "description": "..."},
                    ...
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
