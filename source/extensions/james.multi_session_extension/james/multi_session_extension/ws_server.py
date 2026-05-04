import asyncio
import json
import time
from typing import Optional

import carb
import omni.client
import omni.usd

from .session_manager import SessionManager
from .agent_client import AgentClient, AgentAction, ChatRequest
from .camera_navigation import CameraNavigation


class WebSocketServer:
    def __init__(
        self,
        session_manager: SessionManager,
        port: int = 8211,
        agent_client: AgentClient = None,
        camera_nav: CameraNavigation = None,
    ):
        self._session_mgr = session_manager
        self._port = port
        self._agent_client = agent_client
        self._camera_nav = camera_nav
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

        elif msg_type == "open_stage":
            url = data.get("url", "")
            if url:
                carb.log_info(f"[WebSocketServer] open_stage request from {session.session_id}: {url}")
                await self._open_stage(session, url)

        elif msg_type == "chat_message":
            await self._handle_chat(session, data)

        elif msg_type == "get_nav_positions":
            await self._handle_get_nav_positions(session)

        elif msg_type == "navigate_to":
            await self._handle_navigate_to(session, data)

        else:
            carb.log_warn(f"[WebSocketServer] Unknown message type: {msg_type}")

    async def _handle_chat(self, session, data: dict):
        if not self._agent_client:
            if not session.ws.closed:
                await session.ws.send_json({
                    "type": "chat_response",
                    "message": "Chat is not available — agent backend not configured.",
                    "request_id": data.get("request_id", ""),
                })
            return

        request_id = data.get("request_id", "")
        message = data.get("message", "").strip()
        if not message:
            return

        try:
            if not session.ws.closed:
                await session.ws.send_json({"type": "chat_typing", "is_typing": True})

            request = ChatRequest(
                message=message,
                session_id=session.session_id,
                context={
                    "position": session.position,
                    "rotation": session.rotation,
                    "fov": session.fov,
                },
            )
            response = await self._agent_client.send_chat_message(request)

            if not session.ws.closed:
                await session.ws.send_json({"type": "chat_typing", "is_typing": False})

            if response.action == AgentAction.NAVIGATE_TO and response.action_params:
                dest = response.action_params.get("destination", "")
                if dest and self._camera_nav:
                    asyncio.ensure_future(self._camera_nav.navigate_to(session, dest))

            if not session.ws.closed:
                await session.ws.send_json({
                    "type": "chat_response",
                    "message": response.message,
                    "request_id": request_id,
                    "metadata": response.metadata,
                    "action": response.action.value if response.action else "none",
                    "action_params": response.action_params,
                })

        except Exception as e:
            carb.log_error(f"[WebSocketServer] Chat error for {session.session_id}: {e}")
            if not session.ws.closed:
                await session.ws.send_json({"type": "chat_typing", "is_typing": False})
                await session.ws.send_json({
                    "type": "chat_response",
                    "message": f"Error: {e}",
                    "request_id": request_id,
                })

    async def _handle_get_nav_positions(self, session):
        if not self._camera_nav:
            if not session.ws.closed:
                await session.ws.send_json({
                    "type": "nav_positions",
                    "positions": {},
                })
            return

        positions = self._camera_nav.get_all_positions()
        if not session.ws.closed:
            await session.ws.send_json({
                "type": "nav_positions",
                "positions": positions,
            })

    async def _handle_navigate_to(self, session, data: dict):
        if not self._camera_nav:
            return

        destination = data.get("destination", "")
        instant = data.get("instant", False)
        speed = data.get("speed", 1.0)

        if not destination:
            return

        self._camera_nav.stop_animation(session.session_id)

        success = await self._camera_nav.navigate_to(
            session, destination, speed=speed, instant=instant
        )
        if not success and not session.ws.closed:
            await session.ws.send_json({
                "type": "error",
                "message": f"Location '{destination}' not found",
            })

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

    async def _open_stage(self, session, url: str):
        try:
            import carb.tokens
            resolved_url = carb.tokens.acquire_tokens_interface().resolve(url)
            usd_context = omni.usd.get_context()

            stage = usd_context.get_stage()
            if stage:
                current = stage.GetRootLayer().identifier
                if omni.client.utils.equal_urls(resolved_url, current):
                    carb.log_info(f"[WebSocketServer] Stage already loaded: {resolved_url}")
                    if not session.ws.closed:
                        await session.ws.send_json({
                            "type": "stage_status",
                            "status": "already_loaded",
                            "url": url,
                        })
                    return

            if not session.ws.closed:
                await session.ws.send_json({
                    "type": "stage_status",
                    "status": "loading",
                    "url": url,
                })

            result, error = await usd_context.open_stage_async(
                resolved_url, omni.usd.UsdContextInitialLoadSet.LOAD_ALL
            )

            if result:
                carb.log_info(f"[WebSocketServer] Stage loaded: {resolved_url}")
                self._session_mgr.rebuild_cameras_on_new_stage()
                for s in self._session_mgr.get_all_sessions():
                    if not s.ws.closed:
                        await s.ws.send_json({
                            "type": "stage_status",
                            "status": "loaded",
                            "url": url,
                        })
            else:
                carb.log_error(f"[WebSocketServer] Failed to load stage: {error}")
                if not session.ws.closed:
                    await session.ws.send_json({
                        "type": "stage_status",
                        "status": "error",
                        "url": url,
                        "error": str(error),
                    })
        except Exception as e:
            carb.log_error(f"[WebSocketServer] open_stage error: {e}")
            if not session.ws.closed:
                await session.ws.send_json({
                    "type": "stage_status",
                    "status": "error",
                    "error": str(e),
                })

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
