import asyncio
import io
import time
from typing import Optional

import carb

from .session_manager import SessionManager, UserSession


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
        self._running = False
        self._loop_task = None

        self._has_pil = False
        self._first_capture_logged = False
        try:
            from PIL import Image  # noqa: F401
            self._has_pil = True
        except ImportError:
            carb.log_warn("[MultiRenderer] PIL not available, sending raw frames.")

        carb.log_info(
            f"[MultiRenderer] Init ({render_width}x{render_height}, "
            f"q={jpeg_quality}, settle={settle_frames})"
        )

    def start(self):
        self._running = True
        self._loop_task = asyncio.ensure_future(self._render_loop())
        carb.log_info("[MultiRenderer] Render loop started")

    def stop(self):
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
        carb.log_info("[MultiRenderer] Stopped")

    async def _render_loop(self):
        while self._running:
            try:
                session = self._session_mgr.get_next_session()
                if session is None:
                    await asyncio.sleep(0.1)
                    continue

                if session.ws is None or session.ws.closed:
                    continue

                if session.viewport_api is None:
                    continue

                # Wait for this user's viewport to render
                await session.viewport_api.wait_for_rendered_frames(self._settle_frames)

                # Capture from this user's dedicated viewport
                capture_result = await self._capture_frame(session.viewport_api)
                if capture_result is None:
                    continue

                frame_data, cap_width, cap_height = capture_result

                if not self._first_capture_logged:
                    self._first_capture_logged = True
                    carb.log_info(
                        f"[MultiRenderer] First capture: {cap_width}x{cap_height}, "
                        f"expected {self._render_width}x{self._render_height}, "
                        f"buffer={len(frame_data)} bytes, "
                        f"session={session.session_id}"
                    )

                frame_bytes = self._encode_frame(frame_data, cap_width, cap_height)
                if frame_bytes is None:
                    continue

                try:
                    if not session.ws.closed:
                        await session.ws.send_bytes(frame_bytes)
                        session.frames_sent += 1
                        session.last_frame_time = time.time()
                except Exception:
                    pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                carb.log_error(f"[MultiRenderer] Loop error: {e}")
                await asyncio.sleep(0.1)

    async def _capture_frame(self, viewport_api):
        try:
            from omni.kit.widget.viewport.capture import ByteCapture

            loop = asyncio.get_event_loop()
            future = loop.create_future()
            result = [None, 0, 0]

            def on_capture(buffer, buffer_size, width, height, fmt):
                try:
                    if buffer is not None and buffer_size > 0:
                        import ctypes
                        buf_type = type(buffer).__name__
                        if buf_type == 'PyCapsule':
                            ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
                            ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [
                                ctypes.py_object, ctypes.c_char_p
                            ]
                            ptr = ctypes.pythonapi.PyCapsule_GetPointer(buffer, None)
                            if ptr:
                                result[0] = ctypes.string_at(ptr, buffer_size)
                        elif isinstance(buffer, (bytes, bytearray)):
                            result[0] = bytes(buffer)
                        elif isinstance(buffer, int):
                            result[0] = ctypes.string_at(buffer, buffer_size)
                        elif hasattr(buffer, '__array_interface__'):
                            import numpy as np
                            result[0] = np.array(buffer, copy=False).tobytes()
                        else:
                            result[0] = bytes(memoryview(buffer))
                        result[1] = width
                        result[2] = height
                except Exception as e:
                    carb.log_error(f"[MultiRenderer] Capture cb error: {e}")
                finally:
                    if not future.done():
                        future.set_result(True)

            viewport_api.schedule_capture(ByteCapture(on_capture))
            await asyncio.wait_for(future, timeout=5.0)

            if result[0] is None:
                return None
            return (result[0], result[1], result[2])

        except asyncio.TimeoutError:
            carb.log_warn("[MultiRenderer] Capture timed out")
            return None
        except Exception as e:
            carb.log_error(f"[MultiRenderer] Capture error: {e}")
            return None

    def _encode_frame(self, raw_buffer: bytes, width: int, height: int) -> Optional[bytes]:
        try:
            if self._has_pil:
                from PIL import Image
                expected_size = width * height * 4
                if len(raw_buffer) != expected_size:
                    carb.log_warn(
                        f"[MultiRenderer] Buffer size mismatch: got {len(raw_buffer)}, "
                        f"expected {expected_size} for {width}x{height} RGBA"
                    )
                    return None
                img = Image.frombytes("RGBA", (width, height), raw_buffer)
                img = img.convert("RGB")
                output = io.BytesIO()
                img.save(output, format="JPEG", quality=self._jpeg_quality)
                return output.getvalue()
            else:
                return raw_buffer
        except Exception as e:
            carb.log_error(f"[MultiRenderer] Encode error: {e}")
            return None
