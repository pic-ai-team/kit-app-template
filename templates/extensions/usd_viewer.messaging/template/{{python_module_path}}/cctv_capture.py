"""
CCTV Capture Module — captures frames from predefined CCTV camera positions
using a dedicated HydraTexture (off-screen render product) so the user's
main viewport camera is never touched. Uses viewport capture as a fallback if HydraTexture setup fails.

Works in headless/Docker environments — no GLFW or windowing required.

Architecture:
    1. create_hydra_texture() — GPU render target bound to /World/CCTV_Camera
    2. GLOBAL_EVENT_DRAWABLE_CHANGED — fires when a frame is rendered
    3. omni.renderer_capture — reads pixels from the GPU resource
    4. No ViewportWindow, no GLFW, no UI dependency
"""

import asyncio
import base64
import io
from typing import Dict, Any, List, Optional

import carb
import omni.kit.app

# Prefix used to identify CCTV positions within the nav positions system
CCTV_PREFIX = "cctv_"

# Resolution for CCTV captures
CCTV_WIDTH = 1920
CCTV_HEIGHT = 1080


class CCTVCapture:
    """
    Captures frames from all registered CCTV positions using a dedicated
    off-screen HydraTexture render product. The user's viewport camera is
    never moved.
    """

    CCTV_CAMERA_PATH = "/World/CCTV_Camera"

    def __init__(self):
        self._camera_created = False
        self._hydra_texture = None
        self._drawable_sub = None
        self._hydra_size = (CCTV_WIDTH, CCTV_HEIGHT)
        # Capture coordination: set by _capture_single_frame, consumed by _on_drawable_changed
        self._capture_state = None  # {"future": asyncio.Future, "skip": int}


    # ------------------------------------------------------------------
    # CCTV position registry
    # ------------------------------------------------------------------

    def _get_cctv_positions(self) -> Dict[str, Dict[str, Any]]:
        """Get all positions whose key starts with 'cctv_'."""
        from .camera_navigation import get_camera_navigation

        nav = get_camera_navigation()
        all_positions = nav.get_positions()
        return {
            k: v for k, v in all_positions.items()
            if k.startswith(CCTV_PREFIX)
        }


    # ------------------------------------------------------------------
    # USD camera prim
    # ------------------------------------------------------------------

    def _ensure_cctv_camera(self):
        """Create the dedicated CCTV camera prim if it doesn't exist."""
        if self._camera_created:
            return True

        try:
            import omni.usd
            from pxr import UsdGeom

            stage = omni.usd.get_context().get_stage()
            if not stage:
                carb.log_error("[CCTVCapture] No USD stage available")
                return False

            prim = stage.GetPrimAtPath(self.CCTV_CAMERA_PATH)
            if not prim or not prim.IsValid():
                camera = UsdGeom.Camera.Define(stage, self.CCTV_CAMERA_PATH)
                camera.GetFocalLengthAttr().Set(18.14)
                camera.GetHorizontalApertureAttr().Set(20.955)
                camera.GetVerticalApertureAttr().Set(15.2908)
                camera.GetClippingRangeAttr().Set((1.0, 10000000.0))
                carb.log_info(f"[CCTVCapture] Created CCTV camera at {self.CCTV_CAMERA_PATH}")

            self._camera_created = True
            return True

        except Exception as e:
            carb.log_error(f"[CCTVCapture] Failed to create CCTV camera: {e}")
            return False


    def _set_cctv_camera_pose(self, location: List[float], rotation: List[float]):
        """Teleport the CCTV camera to the given pose."""
        try:
            import omni.usd
            from pxr import UsdGeom, Gf

            stage = omni.usd.get_context().get_stage()
            prim = stage.GetPrimAtPath(self.CCTV_CAMERA_PATH)
            if not prim or not prim.IsValid():
                carb.log_error("[CCTVCapture] CCTV camera prim not found")
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

            tx, ty, tz = location
            rx, ry, rz = rotation
            translate_op.Set(Gf.Vec3d(tx, ty, tz))
            rotate_op.Set(Gf.Vec3f(rx, ry, rz))
            return True

        except Exception as e:
            carb.log_error(f"[CCTVCapture] Failed to set camera pose: {e}")
            return False


    # ------------------------------------------------------------------
    # Headless HydraTexture + event subscription
    # ------------------------------------------------------------------

    def _ensure_hydra_texture(self, width: int = CCTV_WIDTH, height: int = CCTV_HEIGHT) -> bool:
        """Create the off-screen HydraTexture and subscribe to its events."""
        width = max(64, int(width))
        height = max(64, int(height))
        requested_size = (width, height)

        if self._hydra_texture is not None and self._hydra_size == requested_size:
            return True

        # Recreate texture if resolution changed
        if self._hydra_texture is not None and self._hydra_size != requested_size:
            carb.log_info(
                f"[CCTVCapture] Recreating HydraTexture due to size change "
                f"{self._hydra_size[0]}x{self._hydra_size[1]} -> {width}x{height}"
            )
            self._drawable_sub = None
            self._hydra_texture = None

        try:
            from omni.kit.hydra_texture import create_hydra_texture, GLOBAL_EVENT_DRAWABLE_CHANGED
            from carb.eventdispatcher import get_eventdispatcher

            ht = create_hydra_texture(
                name="cctv_offscreen",
                width=width,
                height=height,
                usd_context_name="",
                usd_camera_path=self.CCTV_CAMERA_PATH,
                hydra_engine_name="rtx",
                is_async=True,
            )
            if ht is None:
                carb.log_error("[CCTVCapture] create_hydra_texture returned None")
                return False

            self._hydra_texture = ht
            self._hydra_size = requested_size

            # Subscribe to frame-ready events from THIS specific HydraTexture
            self._drawable_sub = get_eventdispatcher().observe_event(
                observer_name="cctv_capture_drawable",
                event_name=GLOBAL_EVENT_DRAWABLE_CHANGED,
                on_event=self._on_drawable_changed,
                filter=ht.get_event_key(),
            )

            carb.log_info(
                f"[CCTVCapture] Created off-screen HydraTexture "
                f"{width}x{height} + event subscription"
            )
            return True

        except ImportError as e:
            carb.log_error(f"[CCTVCapture] Import error creating HydraTexture: {e}")
            return False
        except Exception as e:
            carb.log_error(f"[CCTVCapture] Failed to create HydraTexture: {e}")
            import traceback
            carb.log_error(traceback.format_exc())
            return False


    def _on_drawable_changed(self, event):
        """
        Called by Kit when the HydraTexture has rendered a new frame.
        If a capture is pending, reads pixels via omni.renderer_capture.
        """
        state = self._capture_state
        if state is None:
            return

        future = state["future"]
        if future.done():
            return

        # Skip initial frames to let the new camera pose propagate
        if state["skip"] > 0:
            state["skip"] -= 1
            return

        try:
            # Get result handle from event (dict-like access)
            try:
                result_handle = event["result_handle"]
            except (KeyError, TypeError):
                result_handle = 0

            # Get AOV info with GPU texture references
            aov_info = self._hydra_texture.get_aov_info(
                result_handle, include_texture=True
            )

            # Find the LdrColor AOV (standard color output)
            ldr_aov = None
            for info in aov_info:
                if info.get("name") == "LdrColor":
                    ldr_aov = info
                    break

            if not ldr_aov or "texture" not in ldr_aov:
                # AOV not ready yet — wait for next frame
                return

            rp_resource = ldr_aov["texture"]["rp_resource"]
            frame_info = self._hydra_texture.get_frame_info(result_handle)

            # Use omni.renderer_capture to read pixels from the GPU
            import omni.renderer_capture
            renderer = omni.renderer_capture.acquire_renderer_capture_interface()

            # Capture the reference to future before the callback scope
            capture_future = future

            def on_pixels_ready(buffer, buffer_size, width, height, pixel_format):
                try:
                    if buffer is None or buffer_size <= 0:
                        carb.log_warn("[CCTVCapture] Pixel callback: empty buffer")
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
                        b64 = CCTVCapture._raw_to_base64_jpeg(
                            data, width, height, pixel_format
                        )
                    else:
                        b64 = None

                    if not capture_future.done():
                        capture_future.set_result(b64)

                except Exception as e:
                    carb.log_error(f"[CCTVCapture] Pixel callback error: {e}")
                    if not capture_future.done():
                        capture_future.set_result(None)

            metadata = frame_info.get("metadata") if isinstance(frame_info, dict) else None
            renderer.capture_next_frame_rp_resource_callback(
                on_pixels_ready, rp_resource, metadata=metadata
            )

        except Exception as e:
            carb.log_error(f"[CCTVCapture] Error in drawable_changed handler: {e}")
            import traceback
            carb.log_error(traceback.format_exc())
            if not future.done():
                future.set_result(None)

    # ------------------------------------------------------------------
    # Single-frame capture (async)
    # ------------------------------------------------------------------

    async def _capture_single_frame(self) -> Optional[str]:
        """
        Request a single frame capture from the off-screen HydraTexture.
        Waits for the drawable changed event to fire and deliver pixels.
        """
        loop = asyncio.get_event_loop()
        future = loop.create_future()

        # Skip 3 rendered frames to let the new camera pose propagate
        self._capture_state = {"future": future, "skip": 3}

        try:
            result = await asyncio.wait_for(future, timeout=15.0)
            return result
        except asyncio.TimeoutError:
            carb.log_warn("[CCTVCapture] Frame capture timed out (15s)")
            return None
        finally:
            self._capture_state = None

    # ------------------------------------------------------------------
    # Active viewport fallback
    # ------------------------------------------------------------------

    async def _capture_via_active_viewport(self) -> Optional[str]:
        """
        Fallback: capture from the active viewport by temporarily switching
        its camera. Causes a brief visual glitch on the WebRTC stream.
        """
        try:
            from omni.kit.viewport.utility import (
                get_active_viewport,
                capture_viewport_to_buffer,
                next_viewport_frame_async,
            )

            viewport = get_active_viewport()
            if viewport is None:
                carb.log_error("[CCTVCapture] No active viewport for fallback capture")
                return None

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
                    b64 = self._raw_to_base64_jpeg(data, w, h, fmt) if data else None
                    if not future.done():
                        future.set_result(b64)
                except Exception as e:
                    carb.log_error(f"[CCTVCapture] Fallback callback error: {e}")
                    if not future.done():
                        future.set_result(None)

            capture_viewport_to_buffer(viewport, on_capture)
            return await asyncio.wait_for(future, timeout=10.0)

        except asyncio.TimeoutError:
            carb.log_warn("[CCTVCapture] Fallback capture timed out")
            return None
        except Exception as e:
            carb.log_warn(f"[CCTVCapture] Fallback capture failed: {e}")
            return None


    # ------------------------------------------------------------------
    # Image conversion
    # ------------------------------------------------------------------

    @staticmethod
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
            carb.log_error(f"[CCTVCapture] JPEG conversion failed: {e}")
            return None


    # ------------------------------------------------------------------
    # Main capture entry point
    # ------------------------------------------------------------------

    async def capture_all_feeds(self) -> List[Dict[str, Any]]:
        """
        Cycle through all CCTV positions and capture a frame from each.

        Tries the off-screen HydraTexture first (zero visual impact).
        Falls back to the active viewport approach if HydraTexture fails.
        """
        positions = self._get_cctv_positions()
        if not positions:
            carb.log_warn("[CCTVCapture] No CCTV positions registered (prefix 'cctv_')")
            return []

        carb.log_info(f"[CCTVCapture] Starting capture for {len(positions)} position(s)")

        if not self._ensure_cctv_camera():
            carb.log_warn("[CCTVCapture] Failed to ensure CCTV camera")
            return []

        # Try to set up the off-screen render path
        use_hydra = self._ensure_hydra_texture()
        if use_hydra:
            carb.log_info("[CCTVCapture] Using off-screen HydraTexture (no visual impact)")
        else:
            carb.log_info("[CCTVCapture] Falling back to active viewport (brief visual glitch)")

        # For fallback: save & restore original camera
        original_camera = None
        viewport = None
        if not use_hydra:
            from omni.kit.viewport.utility import get_active_viewport
            viewport = get_active_viewport()
            if viewport is None:
                carb.log_warn("[CCTVCapture] No active viewport — cannot capture")
                return []
            original_camera = viewport.camera_path
            viewport.camera_path = self.CCTV_CAMERA_PATH

        results: List[Dict[str, Any]] = []

        try:
            for cam_id, pos_data in positions.items():
                location = pos_data.get("location", [0, 0, 0])
                rotation = pos_data.get("rotation", [0, 0, 0])
                description = pos_data.get("description", cam_id)

                if not self._set_cctv_camera_pose(location, rotation):
                    carb.log_warn(f"[CCTVCapture] Skipping {cam_id} — pose set failed")
                    continue

                # Small delay to let USD propagation begin
                await asyncio.sleep(0.1)

                if use_hydra:
                    frame_data = await self._capture_single_frame()
                else:
                    frame_data = await self._capture_via_active_viewport()

                if frame_data:
                    results.append({
                        "camera_id": cam_id,
                        "frame_data": frame_data,
                        "location": location,
                        "rotation": rotation,
                        "description": description,
                    })
                    carb.log_warn(f"[CCTVCapture] Captured {cam_id}")
                else:
                    carb.log_warn(f"[CCTVCapture] Failed to capture {cam_id}")

        finally:
            if not use_hydra and viewport is not None and original_camera is not None:
                viewport.camera_path = original_camera
                carb.log_warn(f"[CCTVCapture] Restored camera to {original_camera}")

        carb.log_warn(f"[CCTVCapture] Captured {len(results)}/{len(positions)} CCTV feeds")
        return results


    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self):
        """Clean up render targets and camera resources."""
        try:
            # Unsubscribe from events
            self._drawable_sub = None
            self._capture_state = None

            # Drop HydraTexture reference
            self._hydra_texture = None
            self._hydra_size = (CCTV_WIDTH, CCTV_HEIGHT)

            # Remove the CCTV camera prim
            if self._camera_created:
                try:
                    import omni.usd
                    stage = omni.usd.get_context().get_stage()
                    if stage:
                        prim = stage.GetPrimAtPath(self.CCTV_CAMERA_PATH)
                        if prim and prim.IsValid():
                            stage.RemovePrim(self.CCTV_CAMERA_PATH)
                except Exception:
                    pass
                self._camera_created = False

            carb.log_info("[CCTVCapture] Shutdown complete")
        except Exception as e:
            carb.log_warn(f"[CCTVCapture] Shutdown error: {e}")


# Module-level singleton
_cctv_capture: Optional[CCTVCapture] = None


def get_cctv_capture() -> CCTVCapture:
    """Get the singleton CCTVCapture instance."""
    global _cctv_capture
    if _cctv_capture is None:
        _cctv_capture = CCTVCapture()
    return _cctv_capture
