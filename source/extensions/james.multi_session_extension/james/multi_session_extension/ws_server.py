import asyncio
import json
import time
from typing import Optional

import carb
import omni.usd

from .session_manager import SessionManager


class WebSocketServer:
    def __init__(
        self,
        session_manager: SessionManager,
        port: int = 8211,
    ):
        self._session_mgr = session_manager
        self._port = port
        self._runner = None
        self._site = None
        self._app = None
        carb.log_info(f"[WebSocketServer] Initialized (port={port})")

    def start(self):
        asyncio.ensure_future(self._start_server())

    async def _start_server(self):
        try:
            import aiohttp.web as web
        except ImportError:
            carb.log_error(
                "[WebSocketServer] aiohttp not available. "
                "Multi-session streaming requires aiohttp."
            )
            return

        self._app = web.Application()
        self._app.router.add_get("/ws", self._handle_websocket)
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/status", self._handle_status)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await self._site.start()

        carb.log_info(f"[WebSocketServer] Listening on 0.0.0.0:{self._port}")

    async def _handle_health(self, request):
        import aiohttp.web as web
        return web.json_response({
            "status": "ok",
            "sessions": self._session_mgr.session_count,
            "max_sessions": self._session_mgr.max_sessions,
        })

    async def _handle_status(self, request):
        import aiohttp.web as web
        sessions_info = []
        for s in self._session_mgr.get_all_sessions():
            sessions_info.append({
                "session_id": s.session_id,
                "connected_at": s.connected_at,
                "frames_sent": s.frames_sent,
                "last_frame_time": s.last_frame_time,
                "position": s.position,
                "rotation": s.rotation,
            })

        return web.json_response({
            "status": "ok",
            "session_count": self._session_mgr.session_count,
            "max_sessions": self._session_mgr.max_sessions,
            "sessions": sessions_info,
        })

    async def _handle_websocket(self, request):
        import aiohttp.web as web
        import aiohttp

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        session = self._session_mgr.create_session(ws)
        if session is None:
            await ws.send_json({
                "type": "error",
                "message": f"Server at capacity ({self._session_mgr.max_sessions} max sessions)",
            })
            await ws.close()
            return ws

        await ws.send_json({
            "type": "session_info",
            "user_id": session.session_id,
            "user_count": self._session_mgr.session_count,
            "max_users": self._session_mgr.max_sessions,
        })

        carb.log_info(
            f"[WebSocketServer] Client connected: {session.session_id} "
            f"(from {request.remote})"
        )

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        await self._handle_message(session, data)
                    except json.JSONDecodeError:
                        await ws.send_json({
                            "type": "error",
                            "message": "Invalid JSON",
                        })
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    carb.log_warn(
                        f"[WebSocketServer] WS error for {session.session_id}: "
                        f"{ws.exception()}"
                    )
                    break

        except Exception as e:
            carb.log_error(f"[WebSocketServer] Session error: {e}")

        finally:
            self._session_mgr.remove_session(session.session_id)
            carb.log_info(f"[WebSocketServer] Client disconnected: {session.session_id}")

        return ws

    async def _handle_message(self, session, data: dict):
        msg_type = data.get("type", "")

        if msg_type == "camera_update":
            position = data.get("position", session.position)
            rotation = data.get("rotation", session.rotation)
            fov = data.get("fov", session.fov)

            self._session_mgr.update_camera(
                session.session_id,
                position,
                rotation,
                fov,
            )

        elif msg_type == "marker_click":
            marker_id = data.get("marker_id", "")
            carb.log_info(
                f"[WebSocketServer] Marker click: {marker_id} "
                f"by {session.session_id}"
            )
            try:
                from carb.eventdispatcher import get_eventdispatcher
                get_eventdispatcher().dispatch_event(
                    "markerClick",
                    payload={
                        "marker_id": marker_id,
                        "session_id": session.session_id,
                    },
                )
            except Exception:
                pass

        elif msg_type == "ping":
            if not session.ws.closed:
                await session.ws.send_json({"type": "pong"})

        elif msg_type == "get_markers":
            markers = self._collect_markers()
            if not session.ws.closed:
                await session.ws.send_json({
                    "type": "markers",
                    "data": markers,
                })

        elif msg_type == "get_scene_info":
            info = self._get_scene_info()
            if not session.ws.closed:
                await session.ws.send_json({
                    "type": "scene_info",
                    "data": info,
                })

        else:
            carb.log_warn(f"[WebSocketServer] Unknown message type: {msg_type}")

    def _collect_markers(self) -> list:
        markers = []
        try:
            stage = omni.usd.get_context().get_stage()
            if not stage:
                return markers

            from pxr import UsdGeom

            markers_prim = stage.GetPrimAtPath("/World/Markers")
            if not markers_prim or not markers_prim.IsValid():
                return markers

            for child in markers_prim.GetChildren():
                xformable = UsdGeom.Xformable(child)
                local_transform = xformable.ComputeLocalToWorldTransform(0)
                pos = local_transform.ExtractTranslation()

                label_attr = child.GetAttribute("label")
                label = label_attr.Get() if label_attr else child.GetName()

                markers.append({
                    "id": child.GetName(),
                    "position": [float(pos[0]), float(pos[1]), float(pos[2])],
                    "label": str(label),
                    "path": str(child.GetPath()),
                })

        except Exception as e:
            carb.log_error(f"[WebSocketServer] Error collecting markers: {e}")

        return markers

    def _get_scene_info(self) -> dict:
        try:
            stage = omni.usd.get_context().get_stage()
            if not stage:
                return {"error": "No stage loaded"}

            root_layer = stage.GetRootLayer()
            return {
                "root_layer": root_layer.identifier if root_layer else None,
                "up_axis": str(stage.GetMetadata("upAxis") or "Y"),
                "meters_per_unit": stage.GetMetadata("metersPerUnit") or 0.01,
            }
        except Exception as e:
            return {"error": str(e)}

    async def stop(self):
        for session in self._session_mgr.get_all_sessions():
            try:
                if not session.ws.closed:
                    await session.ws.close()
            except Exception:
                pass

        if self._site:
            await self._site.stop()
            self._site = None

        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        self._app = None
        carb.log_info("[WebSocketServer] Server stopped")
