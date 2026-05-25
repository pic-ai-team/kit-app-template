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
import math
from typing import Optional, Tuple

import carb

from .robot_drive_controller import (
    ROBOT_CAMERA_FORWARD,
    ROBOT_CAMERA_HEIGHT,
    get_robot_drive_controller,
)

# Resolution for robot camera captures
ROBOT_CAM_WIDTH = 640
ROBOT_CAM_HEIGHT = 480


class RobotCamera:
    """Captures a snapshot from the robot's virtual camera viewpoint."""

    ROBOT_CAMERA_PATH = "/World/Robot_Camera"

    def __init__(self):
        self._capturing = False
        self._camera_created = False
        self._hydra_texture = None
        self._drawable_sub = None
        self._hydra_size = (ROBOT_CAM_WIDTH, ROBOT_CAM_HEIGHT)
        self._capture_state = None  # {"future": asyncio.Future, "skip": int}

    def get_eye_position(self) -> Optional[Tuple[float, float, float, float]]:
        """
        Return (x, y, z, yaw) for the robot camera.
        Uses the drive controller's helper.
        """
        ctrl = get_robot_drive_controller()
        return ctrl.get_camera_world_pos()

    # ------------------------------------------------------------------
    # USD camera prim
    # ------------------------------------------------------------------

    def _ensure_robot_camera(self) -> bool:
        """Create the dedicated Robot_Camera prim if it doesn't exist."""
        if self._camera_created:
            return True
        try:
            import omni.usd
            from pxr import UsdGeom

            stage = omni.usd.get_context().get_stage()
            if not stage:
                carb.log_error("[RobotCamera] No USD stage available")
                return False

            prim = stage.GetPrimAtPath(self.ROBOT_CAMERA_PATH)
            if not prim or not prim.IsValid():
                camera = UsdGeom.Camera.Define(stage, self.ROBOT_CAMERA_PATH)
                camera.GetFocalLengthAttr().Set(18.14)
                camera.GetHorizontalApertureAttr().Set(20.955)
                camera.GetVerticalApertureAttr().Set(15.2908)
                camera.GetClippingRangeAttr().Set((1.0, 10000000.0))
                carb.log_info(f"[RobotCamera] Created camera at {self.ROBOT_CAMERA_PATH}")

            self._camera_created = True
            return True
        except Exception as e:
            carb.log_error(f"[RobotCamera] Failed to create camera: {e}")
            return False

    def _set_robot_camera_pose(self) -> bool:
        """Teleport the Robot_Camera to the robot's eye-point."""
        eye = self.get_eye_position()
        if eye is None:
            return False
        cam_x, cam_y, cam_z, yaw = eye

        try:
            import omni.usd
            from pxr import UsdGeom, Gf

            stage = omni.usd.get_context().get_stage()
            prim = stage.GetPrimAtPath(self.ROBOT_CAMERA_PATH)
            if not prim or not prim.IsValid():
                return False

            xformable = UsdGeom.Xformable(prim)

            translate_op = None
            rotate_op = None
            for op in xformable.GetOrderedXformOps():
                op_name = op.GetOpName()
                if op_name == "xformOp:translate":
                    translate_op = op
                elif "rotate" in op_name.lower():
                    rotate_op = op

            if translate_op is None or rotate_op is None:
                xformable.SetXformOpOrder([])
                translate_op = xformable.AddTranslateOp()
                rotate_op = xformable.AddRotateXYZOp()

            translate_op.Set(Gf.Vec3d(cam_x, cam_y, cam_z))
            # Z-up store: rx=80 tilts the camera to look nearly horizontal.
            # After 80° X-rotation the camera looks along +Y, but the robot
            # model faces +X at rz=0.  Subtracting 90° aligns the camera
            # with the robot's actual forward direction.
            rotate_op.Set(Gf.Vec3f(80.0, 0.0, yaw - 90.0))
            return True
        except Exception as e:
            carb.log_error(f"[RobotCamera] Failed to set camera pose: {e}")
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
        if not self._ensure_robot_camera():
            return None
        if not self._set_robot_camera_pose():
            return None

        use_hydra = self._ensure_hydra_texture()
        if not use_hydra:
            carb.log_warn("[RobotCamera] HydraTexture unavailable, falling back to viewport")
            return await self._capture_via_viewport(width, quality)

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

    async def _capture_via_viewport(self, width: int, quality: int) -> Optional[str]:
        """Fallback: capture from active viewport (brief visual glitch)."""
        try:
            from omni.kit.viewport.utility import (
                get_active_viewport,
                capture_viewport_to_buffer,
                next_viewport_frame_async,
            )

            viewport = get_active_viewport()
            if viewport is None:
                return None

            # Save & switch camera
            original_camera = viewport.camera_path
            viewport.camera_path = self.ROBOT_CAMERA_PATH

            await next_viewport_frame_async(viewport)
            await next_viewport_frame_async(viewport)

            loop = asyncio.get_event_loop()
            future = loop.create_future()

            def on_capture(buffer, buffer_size, w, h, fmt):
                try:
                    if buffer is None or buffer_size <= 0:
                        if not future.done():
                            future.set_result(None)
                        return
                    import ctypes
                    btype = type(buffer).__name__
                    if btype == 'PyCapsule':
                        ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p
                        ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
                        ptr = ctypes.pythonapi.PyCapsule_GetPointer(buffer, None)
                        data = ctypes.string_at(ptr, buffer_size) if ptr else None
                    elif isinstance(buffer, (bytes, bytearray)):
                        data = bytes(buffer)
                    elif isinstance(buffer, int):
                        data = ctypes.string_at(buffer, buffer_size)
                    else:
                        data = bytes(memoryview(buffer))
                    b64 = _raw_to_base64_jpeg(data, w, h, fmt) if data else None
                    if not future.done():
                        future.set_result(b64)
                except Exception as e:
                    carb.log_error(f"[RobotCamera] Fallback callback error: {e}")
                    if not future.done():
                        future.set_result(None)

            capture_viewport_to_buffer(viewport, on_capture)
            result = await asyncio.wait_for(future, timeout=10.0)

            # Restore original camera
            viewport.camera_path = original_camera

            if result:
                return _make_thumbnail(result, width, quality)
            return None
        except Exception as e:
            carb.log_warn(f"[RobotCamera] Fallback capture failed: {e}")
            return None

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
