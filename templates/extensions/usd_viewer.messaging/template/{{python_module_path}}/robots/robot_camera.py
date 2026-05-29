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
from typing import Optional

import carb

from .robot_drive_controller import (
    ROBOT_PRIM_PATH,
)

# Resolution for robot camera captures
ROBOT_CAM_WIDTH = 250
ROBOT_CAM_HEIGHT = 1000


class RobotCamera:
    """Captures a snapshot from the robot's virtual camera viewpoint."""

    ROBOT_CAMERA_PATH = ROBOT_PRIM_PATH + "/Camera"

    def __init__(self):
        self._capturing = False
        self._hydra_texture = None
        self._drawable_sub = None
        self._hydra_size = (ROBOT_CAM_WIDTH, ROBOT_CAM_HEIGHT)
        self._capture_state = None  # {"future": asyncio.Future, "skip": int}

    # ------------------------------------------------------------------
    # USD camera prim
    # ------------------------------------------------------------------
    def _is_robot_camera(self) -> bool:
        """Check if the camera prim exists, and throw error if not."""
        try:
            import omni.usd

            stage = omni.usd.get_context().get_stage()
            if stage is None:
                carb.log_error("[RobotCamera] USD stage not available")
                return False

            prim = stage.GetPrimAtPath(self.ROBOT_CAMERA_PATH)
            if prim and prim.IsValid():
                return True
            else:
                carb.log_error(f"[RobotCamera] Camera prim at {self.ROBOT_CAMERA_PATH} is invalid")
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
                usd_camera_path=self.ROBOT_CAMERA_PATH,
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
    # Public capture API
    # ------------------------------------------------------------------

    async def capture_frame(
        self,
        width: int = 250,
        quality: int = 50,
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
            return await self._do_capture(width, quality)
        finally:
            self._capturing = False

    async def _do_capture(self, width: int, quality: int) -> Optional[str]:
        if not self._is_robot_camera():
            return None

        use_hydra = self._ensure_hydra_texture()
        if not use_hydra:
            carb.log_warn("[RobotCamera] HydraTexture unavailable")
            return None

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

        if not frame_b64:
            return None

        return _make_thumbnail(frame_b64, width, quality)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self):
        """Clean up render targets and camera resources."""
        self._drawable_sub = None
        self._capture_state = None
        self._hydra_texture = None
        self._hydra_size = (ROBOT_CAM_WIDTH, ROBOT_CAM_HEIGHT)

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


# Singleton
_robot_camera: Optional[RobotCamera] = None


def get_robot_camera() -> RobotCamera:
    global _robot_camera
    if _robot_camera is None:
        _robot_camera = RobotCamera()
    return _robot_camera
