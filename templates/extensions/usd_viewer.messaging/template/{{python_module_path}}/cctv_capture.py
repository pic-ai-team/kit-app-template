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


# Resolution for CCTV captures
CCTV_WIDTH = 1920
CCTV_HEIGHT = 1080


class CCTVCapture:
    """
    Captures frames from all real camera prims under /World/_dstore/CCTV_Cameras/*/Camera
    using a dedicated off-screen HydraTexture render product.
    """

    CCTV_CAMERA_ROOT = "/World/_dstore/CCTV_Cameras"

    def __init__(self):
        self._hydra_texture = None
        self._drawable_sub = None
        self._hydra_size = (CCTV_WIDTH, CCTV_HEIGHT)
        self._capture_state = None  # {"future": asyncio.Future, "skip": int}


    def _get_camera_prims(self) -> List[dict]:
        """
        Enumerate all camera prims under CCTV_CAMERA_ROOT/*/Camera.
        Returns a list of dicts: {"prim_path", "camera_name", "camera_id", "location"}
        Adds debug logging for troubleshooting.
        """
        try:
            import omni.usd
            from pxr import Usd, UsdGeom, Sdf

            stage = omni.usd.get_context().get_stage()
            if not stage:
                carb.log_error("[CCTVCapture] No USD stage available")
                return []

            camera_infos = []
            root_prim = stage.GetPrimAtPath(self.CCTV_CAMERA_ROOT)
            if not root_prim or not root_prim.IsValid():
                carb.log_warn(f"[CCTVCapture] CCTV camera root {self.CCTV_CAMERA_ROOT} not found")
                return []

            carb.log_info(f"[CCTVCapture] Children of {self.CCTV_CAMERA_ROOT}:")
            for child in root_prim.GetChildren():
                carb.log_info(f"  - {child.GetPath().pathString} type={child.GetTypeName()} isXform={child.IsA(UsdGeom.Xform)}")
                if not child.IsA(UsdGeom.Xform):
                    continue
                xform_prim = child
                name_attr = xform_prim.GetAttribute("camera_name")
                id_attr = xform_prim.GetAttribute("camera_id")
                camera_name = None
                camera_id = None
                if name_attr and name_attr.HasAuthoredValue():
                    camera_name = name_attr.Get()
                if id_attr and id_attr.HasAuthoredValue():
                    camera_id = id_attr.Get()
                # Fallback logic for camera_id
                if not camera_id:
                    if camera_name:
                        camera_id = camera_name.lower().replace(' ', '_')
                    else:
                        camera_id = xform_prim.GetName()
                # Fallback logic for camera_name
                if not camera_name:
                    camera_name = xform_prim.GetName()

                cam_found = False
                for cam_child in xform_prim.GetChildren():
                    carb.log_info(f"    - child {cam_child.GetPath().pathString} type={cam_child.GetTypeName()}")
                    if cam_child.GetTypeName() == "Camera":
                        cam_found = True
                        try:
                            xform = UsdGeom.Xformable(cam_child)
                            world_xf = xform.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
                            translation = world_xf.ExtractTranslation()
                            location = [float(translation[0]), float(translation[1]), float(translation[2])]
                        except Exception as e:
                            carb.log_warn(f"[CCTVCapture] Failed to get world location for {cam_child.GetPath().pathString}: {e}")
                            location = [0, 0, 0]
                        camera_infos.append({
                            "prim_path": cam_child.GetPath().pathString,
                            "camera_name": camera_name,
                            "camera_id": camera_id,
                            "location": location
                        })
                if not cam_found:
                    carb.log_warn(f"[CCTVCapture] No Camera child found under {xform_prim.GetPath().pathString}")
            carb.log_info(f"[CCTVCapture] Found {len(camera_infos)} camera prim(s) under CCTV root")
            return camera_infos
        except Exception as e:
            carb.log_error(f"[CCTVCapture] Error enumerating camera prims: {e}")
            return []


    # No prim creation or pose logic needed anymore


    # ------------------------------------------------------------------
    # Headless HydraTexture + event subscription
    # ------------------------------------------------------------------

    def _ensure_hydra_texture(self, camera_prim_path: str, width: int = CCTV_WIDTH, height: int = CCTV_HEIGHT) -> bool:
        """Create the off-screen HydraTexture for a specific camera prim and subscribe to its events."""
        width = max(64, int(width))
        height = max(64, int(height))
        requested_size = (width, height)

        # Always recreate for each camera
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
                usd_camera_path=camera_prim_path,
                hydra_engine_name="rtx",
                is_async=True,
            )
            if ht is None:
                carb.log_error(f"[CCTVCapture] create_hydra_texture returned None for {camera_prim_path}")
                return False

            self._hydra_texture = ht
            self._hydra_size = requested_size

            self._drawable_sub = get_eventdispatcher().observe_event(
                observer_name="cctv_capture_drawable",
                event_name=GLOBAL_EVENT_DRAWABLE_CHANGED,
                on_event=self._on_drawable_changed,
                filter=ht.get_event_key(),
            )

            carb.log_info(f"[CCTVCapture] Created off-screen HydraTexture {width}x{height} for {camera_prim_path}")
            return True

        except ImportError as e:
            carb.log_error(f"[CCTVCapture] Import error creating HydraTexture: {e}")
            return False
        except Exception as e:
            carb.log_error(f"[CCTVCapture] Failed to create HydraTexture for {camera_prim_path}: {e}")
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
        Not used in new logic. Use _capture_single_frame_for_camera instead.
        """
        return None

    async def _capture_single_frame_for_camera(self, camera_prim_path: str) -> Optional[str]:
        """
        Request a single frame capture from the off-screen HydraTexture for a specific camera prim.
        """
        if not self._ensure_hydra_texture(camera_prim_path):
            return None
        await asyncio.sleep(0.1)  # Let USD propagate
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._capture_state = {"future": future, "skip": 3}
        try:
            result = await asyncio.wait_for(future, timeout=15.0)
            return result
        except asyncio.TimeoutError:
            carb.log_warn(f"[CCTVCapture] Frame capture timed out (15s) for {camera_prim_path}")
            return None
        finally:
            self._capture_state = None

    # No viewport fallback anymore


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
        Iterate over all real camera prims and capture a frame from each.
        Returns a list of dicts: {"camera_name", "camera_id", "prim_path", "location", "frame_data"}
        """
        cameras = self._get_camera_prims()
        if not cameras:
            carb.log_warn("[CCTVCapture] No camera prims found under CCTV root")
            return []

        carb.log_info(f"[CCTVCapture] Starting capture for {len(cameras)} camera(s)")
        results: List[Dict[str, Any]] = []
        for cam in cameras:
            frame_data = await self._capture_single_frame_for_camera(cam["prim_path"])
            if frame_data:
                results.append({
                    "camera_name": cam["camera_name"],
                    "camera_id": cam["camera_id"],
                    "prim_path": cam["prim_path"],
                    "location": cam["location"],
                    "frame_data": frame_data
                })
                carb.log_info(f"[CCTVCapture] Captured {cam['camera_name']} ({cam['prim_path']})")
            else:
                carb.log_warn(f"[CCTVCapture] Failed to capture {cam['camera_name']} ({cam['prim_path']})")
        carb.log_info(f"[CCTVCapture] Captured {len(results)}/{len(cameras)} camera feeds")
        return results


    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self):
        """Clean up render targets and camera resources."""
        try:
            self._drawable_sub = None
            self._capture_state = None
            self._hydra_texture = None
            self._hydra_size = (CCTV_WIDTH, CCTV_HEIGHT)
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
