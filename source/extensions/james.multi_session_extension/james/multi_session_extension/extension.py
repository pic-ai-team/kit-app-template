import asyncio
import math

import carb
import carb.settings
import omni.ext
import omni.usd

from .session_manager import SessionManager
from .multi_renderer import MultiRenderer
from .ws_server import WebSocketServer


class Extension(omni.ext.IExt):
    """Multi-session streaming extension.

    Serves multiple concurrent users from a single Kit instance using
    time-sliced rendering and WebSocket-based JPEG frame delivery.
    """

    def on_startup(self, _ext_id: str = ""):
        settings = carb.settings.get_settings()
        prefix = "/exts/james.multi_session_extension"

        self._ws_port = settings.get_as_int(f"{prefix}/ws_port") or 8211
        self._max_sessions = settings.get_as_int(f"{prefix}/max_sessions") or 15
        self._render_width = settings.get_as_int(f"{prefix}/render_width") or 1280
        self._render_height = settings.get_as_int(f"{prefix}/render_height") or 720
        self._jpeg_quality = settings.get_as_int(f"{prefix}/jpeg_quality") or 70
        self._settle_frames = settings.get_as_int(f"{prefix}/settle_frames") or 2

        carb.log_info(
            f"[MultiSessionExtension] Starting: "
            f"port={self._ws_port}, max={self._max_sessions}, "
            f"res={self._render_width}x{self._render_height}, "
            f"q={self._jpeg_quality}, settle={self._settle_frames}"
        )

        self._session_manager = None
        self._renderer = None
        self._ws_server = None
        self._renderer_started = False

        self._session_manager = SessionManager(
            max_sessions=self._max_sessions,
            render_width=self._render_width,
            render_height=self._render_height,
        )

        self._ws_server = WebSocketServer(
            session_manager=self._session_manager,
            port=self._ws_port,
        )
        self._ws_server.start()

        self._renderer = MultiRenderer(
            session_manager=self._session_manager,
            render_width=self._render_width,
            render_height=self._render_height,
            jpeg_quality=self._jpeg_quality,
            settle_frames=self._settle_frames,
        )

        event_stream = omni.usd.get_context().get_stage_event_stream()
        self._stage_event_sub = event_stream.create_subscription_to_pop(
            self._on_stage_event, name="MultiSessionExtension"
        )

        stage = omni.usd.get_context().get_stage()
        if stage:
            self._start_renderer()

        carb.log_info("[MultiSessionExtension] Waiting for stage to be ready...")

    def _on_stage_event(self, event):
        if event.type == int(omni.usd.StageEventType.OPENED):
            carb.log_info("[MultiSessionExtension] Stage opened")
            self._log_default_camera()
            self._start_renderer()
            if self._session_manager and self._session_manager.session_count > 0:
                carb.log_info("[MultiSessionExtension] Rebuilding cameras on new stage")
                self._session_manager.rebuild_cameras_on_new_stage()

    def _log_default_camera(self):
        try:
            from pxr import UsdGeom, Gf

            stage = omni.usd.get_context().get_stage()
            if not stage:
                return

            cam_paths = []
            try:
                from omni.kit.viewport.utility import get_active_viewport_camera_path
                active = get_active_viewport_camera_path()
                if active:
                    cam_paths.append(str(active))
            except Exception:
                pass

            cam_paths.extend(["/OmniverseKit_Persp", "/World/Camera"])

            for path in cam_paths:
                prim = stage.GetPrimAtPath(path)
                if not prim or not prim.IsValid():
                    continue

                xformable = UsdGeom.Xformable(prim)
                world_xform = xformable.ComputeLocalToWorldTransform(0)
                t = world_xform.ExtractTranslation()

                m = world_xform.ExtractRotationMatrix()
                sy = math.sqrt(m[0][0] ** 2 + m[0][1] ** 2)
                if sy > 1e-6:
                    rx = math.degrees(math.atan2(m[1][2], m[2][2]))
                    ry = math.degrees(math.atan2(-m[0][2], sy))
                    rz = math.degrees(math.atan2(m[0][1], m[0][0]))
                else:
                    rx = math.degrees(math.atan2(-m[2][1], m[1][1]))
                    ry = math.degrees(math.atan2(-m[0][2], sy))
                    rz = 0.0

                ops = xformable.GetOrderedXformOps()
                op_names = [op.GetOpName() for op in ops]

                carb.log_warn(
                    f"[MultiSession-DIAG] Camera '{path}': "
                    f"translate=({t[0]:.2f}, {t[1]:.2f}, {t[2]:.2f}), "
                    f"rotation=({rx:.2f}, {ry:.2f}, {rz:.2f}), "
                    f"xformOps={op_names}"
                )

        except Exception as e:
            carb.log_error(f"[MultiSession-DIAG] Error reading cameras: {e}")

    def _start_renderer(self):
        if self._renderer_started or self._renderer is None:
            return
        self._renderer_started = True
        self._renderer.start()
        carb.log_info("[MultiSessionExtension] Renderer started successfully")

    def on_shutdown(self):
        carb.log_info("[MultiSessionExtension] Shutting down...")

        self._stage_event_sub = None

        if self._renderer:
            self._renderer.stop()
            self._renderer = None

        if self._ws_server:
            asyncio.ensure_future(self._ws_server.stop())
            self._ws_server = None

        if self._session_manager:
            self._session_manager.cleanup_all()
            self._session_manager = None

        carb.log_info("[MultiSessionExtension] Shut down complete")
