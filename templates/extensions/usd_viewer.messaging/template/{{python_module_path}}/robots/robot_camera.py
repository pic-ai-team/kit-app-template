"""
robot_camera.py — Virtual camera attached to the robot.

Creates a dedicated hidden ``/World/Robot_Camera`` USD camera prim and uses
a HydraTexture (off-screen render product) to capture frames without touching
the user's main viewport.  Follows the same pattern as cctv_capture.py.

The camera pose is derived from the robot prim's position + its
``camera_height_cm`` attribute (or the fallback ``ROBOT_CAMERA_HEIGHT``).
"""

import asyncio
import base64
import io
import time
from typing import Optional

import carb

from .robot_drive_controller import (
    ROBOT_PRIM_PATH,
)

# Default low-impact robot camera stream resolution
ROBOT_STREAM_WIDTH = 300
ROBOT_STREAM_HEIGHT = 300
ROBOT_STREAM_FPS = 0.5
ROBOT_STREAM_QUALITY = 60


class RobotCamera:
    """Captures a snapshot from the robot's virtual camera viewpoint."""

    

    def __init__(self, robot_prim_path: str = ROBOT_PRIM_PATH):
        self._capturing = False
        self._hydra_texture = None
        self._drawable_sub = None
        self.robot_camera_path = robot_prim_path + "/Camera"
        self._hydra_size = (ROBOT_STREAM_WIDTH, ROBOT_STREAM_HEIGHT)
        self._capture_state = None  # {"future": asyncio.Future, "skip": int}
        self._camera_created = False

        # Continuous robot camera stream (Kit -> backend)
        self._stream_task: Optional[asyncio.Task] = None
        self._stream_running = False
        self._stream_backend_url = "http://localhost:8000"
        self._stream_camera_id = "robot_cam_1"
        self._stream_width = ROBOT_STREAM_WIDTH
        self._stream_height = ROBOT_STREAM_HEIGHT
        self._stream_quality = ROBOT_STREAM_QUALITY
        self._stream_fps = ROBOT_STREAM_FPS
        self._last_stream_frame_b64: Optional[str] = None
        self._last_stream_frame_ts: Optional[float] = None
        self._last_stream_push_ok = False
        self._last_stream_error: Optional[str] = None

    # ------------------------------------------------------------------
    # USD camera prim
    # ------------------------------------------------------------------
    def _is_robot_camera(self) -> bool:
        """Check if the camera prim exists, and throw error if not."""
        try:
            import omni.usd

            stage = omni.usd.get_context().get_stage()
            if stage is None or stage.GetRootLayer().anonymous:
                carb.log_warn("[RobotCamera] USD stage not available")
                return False

            prim = stage.GetPrimAtPath(self.robot_camera_path)
            if prim and prim.IsValid():
                return True
            else:
                carb.log_error(f"[RobotCamera] Camera prim at {self.robot_camera_path} is invalid")
                return False
        except Exception as e:
            carb.log_error(f"[RobotCamera] Error accessing USD stage when checking robot camera prim: {e}")
            return False

    # ------------------------------------------------------------------
    # HydraTexture (off-screen render product)
    # ------------------------------------------------------------------

    def _ensure_hydra_texture(self) -> bool:
        """Create the off-screen HydraTexture and subscribe to events."""
        if self._hydra_texture is not None:
            return True
        try:
            from omni.kit.hydra_texture import create_hydra_texture, GLOBAL_EVENT_DRAWABLE_CHANGED
            from carb.eventdispatcher import get_eventdispatcher

            ht = create_hydra_texture(
                name="robot_cam_offscreen",
                width=self._hydra_size[0],
                height=self._hydra_size[1],
                usd_context_name="",
                usd_camera_path=self.robot_camera_path,
                hydra_engine_name="rtx",
                is_async=True,
            )
            if ht is None:
                carb.log_error("[RobotCamera] create_hydra_texture returned None")
                return False

            self._hydra_texture = ht

            self._drawable_sub = get_eventdispatcher().observe_event(
                observer_name="robot_cam_drawable",
                event_name=GLOBAL_EVENT_DRAWABLE_CHANGED,
                on_event=self._on_drawable_changed,
                filter=ht.get_event_key(),
            )

            carb.log_info(
                f"[RobotCamera] Created HydraTexture "
                f"{self._hydra_size[0]}x{self._hydra_size[1]}"
            )
            return True
        except ImportError as e:
            carb.log_error(f"[RobotCamera] Import error: {e}")
            return False
        except Exception as e:
            carb.log_error(f"[RobotCamera] HydraTexture setup failed: {e}")
            return False

    def _on_drawable_changed(self, event):
        """Called when the HydraTexture renders a new frame."""
        state = self._capture_state
        if state is None:
            return

        future = state["future"]
        if future.done():
            return

        if state["skip"] > 0:
            state["skip"] -= 1
            return

        try:
            try:
                result_handle = event["result_handle"]
            except (KeyError, TypeError):
                result_handle = 0

            aov_info = self._hydra_texture.get_aov_info(
                result_handle, include_texture=True
            )

            ldr_aov = None
            for info in aov_info:
                if info.get("name") == "LdrColor":
                    ldr_aov = info
                    break

            if not ldr_aov or "texture" not in ldr_aov:
                return

            rp_resource = ldr_aov["texture"]["rp_resource"]
            frame_info = self._hydra_texture.get_frame_info(result_handle)

            import omni.renderer_capture
            renderer = omni.renderer_capture.acquire_renderer_capture_interface()

            capture_future = future

            def on_pixels_ready(buffer, buffer_size, width, height, pixel_format):
                try:
                    if buffer is None or buffer_size <= 0:
                        if not capture_future.done():
                            capture_future.set_result(None)
                        return

                    import ctypes
                    btype = type(buffer).__name__
                    if btype == 'PyCapsule':
                        ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
                        ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [
                            ctypes.py_object, ctypes.c_char_p
                        ]
                        ptr = ctypes.pythonapi.PyCapsule_GetPointer(buffer, None)
                        data = ctypes.string_at(ptr, buffer_size) if ptr else None
                    elif isinstance(buffer, (bytes, bytearray)):
                        data = bytes(buffer)
                    elif isinstance(buffer, int):
                        data = ctypes.string_at(buffer, buffer_size)
                    else:
                        try:
                            data = bytes(memoryview(buffer))
                        except TypeError:
                            data = None

                    if data:
                        b64 = _raw_to_base64_jpeg(data, width, height, pixel_format)
                    else:
                        b64 = None

                    if not capture_future.done():
                        capture_future.set_result(b64)
                except Exception as e:
                    carb.log_error(f"[RobotCamera] pixel callback error: {e}")
                    if not capture_future.done():
                        capture_future.set_result(None)

            metadata = frame_info.get("metadata") if isinstance(frame_info, dict) else None
            renderer.capture_next_frame_rp_resource_callback(
                on_pixels_ready, rp_resource, metadata=metadata
            )
        except Exception as e:
            carb.log_error(f"[RobotCamera] drawable_changed error: {e}")
            if not future.done():
                future.set_result(None)

    # ------------------------------------------------------------------
    # HydraTexture resolution management
    # ------------------------------------------------------------------

    def _recreate_hydra_texture(self, width: int, height: int) -> bool:
        """Tear down and recreate the HydraTexture at a new resolution."""
        if self._hydra_texture is not None:
            self._drawable_sub = None
            self._hydra_texture = None
        self._hydra_size = (width, height)
        return self._ensure_hydra_texture()

    # ------------------------------------------------------------------
    # Public capture API
    # ------------------------------------------------------------------

    async def capture_frame_low_res(
        self,
        width: int = ROBOT_STREAM_WIDTH,
        quality: int = ROBOT_STREAM_QUALITY,
    ) -> Optional[str]:
        """
        Capture a frame from the robot's viewpoint using off-screen rendering.

        Returns:
            Base64-encoded JPEG thumbnail, or None on failure.
        """
        if self._capturing:
            carb.log_warn("[RobotCamera] Capture already in progress")
            return None

        self._capturing = True
        try:
            return await self._do_capture_low_res(width, quality)
        finally:
            self._capturing = False

    async def capture_frame_full_res(
        self,
        width: int = 2000,
        height: int = 2000,
        quality: int = 90,
    ) -> Optional[str]:
        """
        Capture a high-resolution frame without thumbnail downscaling.

        Recreates the HydraTexture at the requested resolution if needed,
        captures the raw frame, and returns a base64 JPEG at full size.
        After capture, restores the default thumbnail resolution.

        Args:
            width:   Render width in pixels.
            height:  Render height in pixels.
            quality: JPEG quality (1-100).

        Returns:
            Base64-encoded JPEG at the requested resolution, or None.
        """
        if self._capturing:
            carb.log_warn("[RobotCamera] Capture already in progress")
            return None

        self._capturing = True
        try:
            return await self._do_capture_full_res(width, height, quality)
        finally:
            self._capturing = False

    async def _wait_for_next_frame(self) -> Optional[str]:
        """Core async loop to wait for the next valid Hydra frame."""
        # Wait briefly for USD pose propagation
        await asyncio.sleep(0.1)

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._capture_state = {"future": future, "skip": 3}

        try:
            frame_b64 = await asyncio.wait_for(future, timeout=15.0)
        except asyncio.TimeoutError:
            carb.log_warn("[RobotCamera] Capture timed out (15s)")
            frame_b64 = None
        finally:
            self._capture_state = None

        return frame_b64

    async def _do_capture_full_res(
        self, width: int, height: int, quality: int
    ) -> Optional[str]:
        if not self._is_robot_camera():
            return None

        # 1. Setup requested resolution
        if self._hydra_size != (width, height):
            if not self._recreate_hydra_texture(width, height):
                carb.log_warn("[RobotCamera] HydraTexture unavailable at requested resolution")
                return None
        else:
            if not self._ensure_hydra_texture():
                carb.log_warn("[RobotCamera] HydraTexture unavailable")
                return None

        # 2. Wait for the frame
        frame_b64 = await self._wait_for_next_frame()

        # 3. Teardown / Restore default thumbnail resolution
        if self._hydra_size != (ROBOT_STREAM_WIDTH, ROBOT_STREAM_HEIGHT):
            self._recreate_hydra_texture(ROBOT_STREAM_WIDTH, ROBOT_STREAM_HEIGHT)

        if not frame_b64:
            return None

        # 4. Post-process
        return _reencode_jpeg(frame_b64, quality)


    async def _do_capture_low_res(self, width: int, quality: int) -> Optional[str]:
        if not self._is_robot_camera():
            return None

        # 1. Setup standard resolution
        if not self._ensure_hydra_texture():
            carb.log_warn("[RobotCamera] HydraTexture unavailable")
            return None

        # 2. Wait for the frame
        frame_b64 = await self._wait_for_next_frame()

        if not frame_b64:
            return None

        # 3. Post-process
        return _make_thumbnail(frame_b64, width, quality)
    
    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self):
        """Clean up render targets and camera resources."""
        self.stop_backend_stream()
        self._drawable_sub = None
        self._capture_state = None
        self._hydra_texture = None
        self._hydra_size = (ROBOT_STREAM_WIDTH, ROBOT_STREAM_HEIGHT)

        if self._camera_created:
            try:
                import omni.usd
                stage = omni.usd.get_context().get_stage()
                if stage:
                    prim = stage.GetPrimAtPath(self.ROBOT_CAMERA_PATH)
                    if prim and prim.IsValid():
                        stage.RemovePrim(self.ROBOT_CAMERA_PATH)
            except Exception:
                pass
            self._camera_created = False

    # ------------------------------------------------------------------
    # Continuous stream (Kit -> backend)
    # ------------------------------------------------------------------

    def configure_backend_stream(
        self,
        backend_url: str,
        fps: float = ROBOT_STREAM_FPS,
        width: int = ROBOT_STREAM_WIDTH,
        height: int = ROBOT_STREAM_HEIGHT,
        quality: int = ROBOT_STREAM_QUALITY,
        camera_id: str = "robot_cam_1",
    ) -> None:
        """Configure stream target and capture parameters."""
        if backend_url:
            self._stream_backend_url = backend_url.rstrip("/")
        self._stream_fps = max(0.1, float(fps))
        self._stream_width = max(64, int(width))
        self._stream_height = max(64, int(height))
        self._stream_quality = max(1, min(100, int(quality)))
        self._stream_camera_id = camera_id or "robot_cam_1"

    def start_backend_stream(self) -> bool:
        """Start continuous robot camera streaming to the backend."""
        if self._stream_running:
            return True

        self._stream_running = True
        self._stream_task = asyncio.ensure_future(self._run_backend_stream_loop())
        carb.log_info(
            "[RobotCamera] Backend stream started: "
            f"{self._stream_width}x{self._stream_height} @ {self._stream_fps:.2f}fps -> "
            f"{self._stream_backend_url}/api/robot/camera-frame"
        )
        return True

    def stop_backend_stream(self) -> None:
        """Stop continuous robot camera streaming."""
        self._stream_running = False
        if self._stream_task is not None:
            self._stream_task.cancel()
            self._stream_task = None

    def get_stream_status(self) -> dict:
        """Return current streaming health/state for diagnostics."""
        age = None
        if self._last_stream_frame_ts is not None:
            age = max(0.0, time.time() - self._last_stream_frame_ts)

        return {
            "running": self._stream_running,
            "backend_url": self._stream_backend_url,
            "camera_id": self._stream_camera_id,
            "fps": self._stream_fps,
            "width": self._stream_width,
            "height": self._stream_height,
            "quality": self._stream_quality,
            "last_frame_ts": self._last_stream_frame_ts,
            "last_frame_age_sec": age,
            "last_push_ok": self._last_stream_push_ok,
            "last_error": self._last_stream_error,
        }

    def get_latest_stream_frame(self) -> Optional[str]:
        """Return latest streamed frame for optional local preview endpoints."""
        return self._last_stream_frame_b64

    async def _run_backend_stream_loop(self) -> None:
        """Capture/push loop designed to avoid queue buildup and render contention."""
        interval = 1.0 / self._stream_fps

        # capture_frame() creates the hydra texture one time
        if self._hydra_texture is not None and self._hydra_size != (self._stream_width, self._stream_height):
            self._recreate_hydra_texture(self._stream_width, self._stream_height)
        else:
            self._hydra_size = (self._stream_width, self._stream_height)

        while self._stream_running:
            tick_start = time.time()
            try:
                # Reuse existing capture path, but at low cadence and fixed resolution.
                frame_b64 = await self.capture_frame_low_res(
                    width=self._stream_width,
                    quality=self._stream_quality,
                )

                if frame_b64:
                    self._last_stream_frame_b64 = frame_b64
                    self._last_stream_frame_ts = time.time()
                    await self._push_frame_to_backend(frame_b64)
                else:
                    self._last_stream_push_ok = False
                    self._last_stream_error = "capture returned empty frame"

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._last_stream_push_ok = False
                self._last_stream_error = str(e)
                carb.log_warn(f"[RobotCamera] stream loop error: {e}")

            elapsed = time.time() - tick_start
            await asyncio.sleep(max(0.0, interval - elapsed))

        carb.log_info("[RobotCamera] Backend stream loop stopped")

    async def _push_frame_to_backend(self, frame_b64: str) -> None:
        """Push one frame to backend over HTTP JSON (robust + low-overhead)."""
        payload = {
            "camera_id": self._stream_camera_id,
            "timestamp": time.time(),
            "width": self._stream_width,
            "height": self._stream_height,
            "frame_data": frame_b64,
            "source": "kit_robot_camera",
        }
        url = f"{self._stream_backend_url}/api/robot/camera-frame"

        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=5.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as response:
                    if response.status != 200:
                        text = await response.text()
                        self._last_stream_push_ok = False
                        self._last_stream_error = f"backend push failed ({response.status}): {text}"
                        carb.log_warn(f"[RobotCamera] {self._last_stream_error}")
                        return

            self._last_stream_push_ok = True
            self._last_stream_error = None
            return

        except ImportError:
            # Fallback for Kit runtimes without aiohttp.
            import json as _json
            import urllib.request

            try:
                data = _json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: urllib.request.urlopen(req, timeout=5.0),
                )
                self._last_stream_push_ok = response.status == 200
                if not self._last_stream_push_ok:
                    self._last_stream_error = f"backend push failed ({response.status})"
                else:
                    self._last_stream_error = None
            except Exception as e:
                self._last_stream_push_ok = False
                self._last_stream_error = str(e)
                carb.log_warn(f"[RobotCamera] backend push fallback failed: {e}")

        except Exception as e:
            self._last_stream_push_ok = False
            self._last_stream_error = str(e)
            carb.log_warn(f"[RobotCamera] backend push failed: {e}")


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _raw_to_base64_jpeg(data: bytes, width: int, height: int, img_format) -> Optional[str]:
    """Convert raw RGBA/BGRA buffer to base64-encoded JPEG."""
    try:
        from PIL import Image

        format_str = ""
        if img_format is not None:
            if hasattr(img_format, 'name'):
                format_str = img_format.name.upper()
            elif isinstance(img_format, str):
                format_str = img_format.upper()
            else:
                format_str = str(img_format).upper()

        if 'BGRA' in format_str:
            img = Image.frombytes('RGBA', (width, height), data)
            r, g, b, a = img.split()
            img = Image.merge('RGB', (b, g, r))
        elif 'RGBA' in format_str:
            img = Image.frombytes('RGBA', (width, height), data)
            img = img.convert('RGB')
        else:
            try:
                img = Image.frombytes('RGBA', (width, height), data)
                img = img.convert('RGB')
            except ValueError:
                img = Image.frombytes('RGB', (width, height), data)

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=90)
        buf.seek(0)
        return base64.b64encode(buf.getvalue()).decode('utf-8')
    except Exception as e:
        carb.log_error(f"[RobotCamera] JPEG conversion failed: {e}")
        return None


def _make_thumbnail(b64_jpeg: str, max_width: int, quality: int) -> Optional[str]:
    """Downscale a base64 JPEG to a smaller JPEG thumbnail."""
    try:
        from PIL import Image

        raw = base64.b64decode(b64_jpeg)
        img = Image.open(io.BytesIO(raw))

        w, h = img.size
        if w > max_width:
            ratio = max_width / w
            new_h = int(h * ratio)
            img = img.resize((max_width, new_h), Image.LANCZOS)

        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except ImportError:
        return b64_jpeg
    except Exception:
        return b64_jpeg


def _reencode_jpeg(b64_jpeg: str, quality: int) -> Optional[str]:
    """Re-encode a base64 JPEG at the given quality without resizing."""
    try:
        from PIL import Image

        raw = base64.b64decode(b64_jpeg)
        img = Image.open(io.BytesIO(raw))

        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except ImportError:
        return b64_jpeg
    except Exception:
        return b64_jpeg


# Singleton
_robot_camera: Optional[RobotCamera] = None


def get_robot_camera(robot_prim_path: str = ROBOT_PRIM_PATH) -> RobotCamera:
    global _robot_camera
    if _robot_camera is None:
        _robot_camera = RobotCamera(robot_prim_path=robot_prim_path)
    return _robot_camera
