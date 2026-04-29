import asyncio
import io
import time
from enum import Enum, auto
from typing import Optional

import carb
import omni.kit.app

from .session_manager import SessionManager, UserSession


class _RenderState(Enum):
    IDLE = auto()
    SETTLING = auto()
    CAPTURE = auto()


class MultiRenderer:
    def __init__(
        self,
        session_manager: SessionManager,
        render_width: int = 1280,
        render_height: int = 720,
        jpeg_quality: int = 70,
        settle_frames: int = 2,
    ):
        self._session_mgr = session_manager
        self._render_width = render_width
        self._render_height = render_height
        self._jpeg_quality = jpeg_quality
        self._settle_frames = settle_frames

        self._state = _RenderState.IDLE
        self._settle_counter = 0
        self._current_session: Optional[UserSession] = None
        self._capture_in_progress = False
        self._update_sub = None
        self._hidden_vp = None
        self._viewport_api = None
        self._running = False

        self._has_pil = False
        try:
            from PIL import Image  # noqa: F401
            self._has_pil = True
        except ImportError:
            carb.log_warn(
                "[MultiRenderer] PIL not available, will send PNG frames (larger). "
                "Install Pillow for JPEG encoding."
            )

        carb.log_info(
            f"[MultiRenderer] Initialized ({render_width}x{render_height}, "
            f"q={jpeg_quality}, settle={settle_frames}, pil={self._has_pil})"
        )

    def start(self):
        try:
            from omni.kit.viewport.window import ViewportWindow

            self._hidden_vp = ViewportWindow(
                "MultiSessionVP",
                visible=False,
                width=self._render_width,
                height=self._render_height,
            )
            self._viewport_api = self._hidden_vp.viewport_api
            carb.log_info("[MultiRenderer] Hidden viewport created: MultiSessionVP")
        except Exception as e:
            carb.log_error(f"[MultiRenderer] Failed to create hidden viewport: {e}")
            carb.log_info("[MultiRenderer] Falling back to main Viewport")
            from omni.kit.viewport.utility import get_viewport_from_window_name
            self._viewport_api = get_viewport_from_window_name("Viewport")

        self._running = True
        self._state = _RenderState.IDLE

        self._update_sub = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(self._on_update, name="MultiRenderer")
        )

        carb.log_info("[MultiRenderer] Render loop started")

    def stop(self):
        self._running = False

        if self._update_sub:
            self._update_sub = None

        if self._hidden_vp:
            try:
                self._hidden_vp.destroy()
            except Exception:
                pass
            self._hidden_vp = None
            self._viewport_api = None

        carb.log_info("[MultiRenderer] Render loop stopped")

    def _on_update(self, event):
        if not self._running:
            return

        if self._state == _RenderState.IDLE:
            session = self._session_mgr.get_next_session()
            if session is None:
                return

            self._current_session = session

            try:
                if self._viewport_api:
                    self._viewport_api.camera_path = session.camera_prim_path
            except Exception as e:
                carb.log_error(f"[MultiRenderer] Failed to set camera path: {e}")
                return

            self._settle_counter = self._settle_frames
            self._state = _RenderState.SETTLING

        elif self._state == _RenderState.SETTLING:
            self._settle_counter -= 1
            if self._settle_counter <= 0:
                self._state = _RenderState.CAPTURE

        elif self._state == _RenderState.CAPTURE:
            if not self._capture_in_progress and self._current_session:
                self._capture_in_progress = True
                asyncio.ensure_future(
                    self._capture_and_send(self._current_session)
                )
            self._state = _RenderState.IDLE

    async def _capture_and_send(self, session: UserSession):
        try:
            if session.ws is None or session.ws.closed:
                return

            from omni.kit.viewport.utility import capture_viewport_to_buffer

            vp_name = "MultiSessionVP" if self._hidden_vp else "Viewport"

            buffer = await capture_viewport_to_buffer(
                viewport_api_name=vp_name,
                width=self._render_width,
                height=self._render_height,
            )

            if buffer is None:
                return

            frame_bytes = self._encode_frame(buffer)
            if frame_bytes is None:
                return

            if not session.ws.closed:
                await session.ws.send_bytes(frame_bytes)
                session.frames_sent += 1
                session.last_frame_time = time.time()

        except ConnectionResetError:
            pass
        except Exception as e:
            carb.log_error(f"[MultiRenderer] Capture/send error for {session.session_id}: {e}")
        finally:
            self._capture_in_progress = False

    def _encode_frame(self, png_buffer: bytes) -> Optional[bytes]:
        try:
            if self._has_pil:
                from PIL import Image
                img = Image.open(io.BytesIO(png_buffer))
                img = img.convert("RGB")
                output = io.BytesIO()
                img.save(output, format="JPEG", quality=self._jpeg_quality)
                return output.getvalue()
            else:
                return png_buffer
        except Exception as e:
            carb.log_error(f"[MultiRenderer] Frame encode error: {e}")
            return None
