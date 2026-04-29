import asyncio

import carb
import carb.settings
import omni.ext

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

        ws_port = settings.get_as_int(f"{prefix}/ws_port") or 8211
        max_sessions = settings.get_as_int(f"{prefix}/max_sessions") or 15
        render_width = settings.get_as_int(f"{prefix}/render_width") or 1280
        render_height = settings.get_as_int(f"{prefix}/render_height") or 720
        jpeg_quality = settings.get_as_int(f"{prefix}/jpeg_quality") or 70
        settle_frames = settings.get_as_int(f"{prefix}/settle_frames") or 2

        carb.log_info(
            f"[MultiSessionExtension] Starting: "
            f"port={ws_port}, max={max_sessions}, "
            f"res={render_width}x{render_height}, "
            f"q={jpeg_quality}, settle={settle_frames}"
        )

        self._session_manager = SessionManager(max_sessions=max_sessions)

        self._renderer = MultiRenderer(
            session_manager=self._session_manager,
            render_width=render_width,
            render_height=render_height,
            jpeg_quality=jpeg_quality,
            settle_frames=settle_frames,
        )

        self._ws_server = WebSocketServer(
            session_manager=self._session_manager,
            port=ws_port,
        )

        self._ws_server.start()
        self._renderer.start()

        carb.log_info("[MultiSessionExtension] Started successfully")

    def on_shutdown(self):
        carb.log_info("[MultiSessionExtension] Shutting down...")

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
