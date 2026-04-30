import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

import carb
import omni.usd
from pxr import Sdf, UsdGeom, Gf

DEFAULT_START_POSITION = [221.80, -508.34, 374.18]
DEFAULT_START_ROTATION = [70.70, 0.00, 60.14]
DEFAULT_FOV = 60.0


@dataclass
class UserSession:
    session_id: str
    position: List[float] = field(default_factory=lambda: list(DEFAULT_START_POSITION))
    rotation: List[float] = field(default_factory=lambda: list(DEFAULT_START_ROTATION))
    fov: float = DEFAULT_FOV
    ws: Any = None
    connected_at: float = field(default_factory=time.time)
    last_frame_time: float = 0.0
    frames_sent: int = 0
    viewport_window: Any = None
    viewport_api: Any = None
    camera_prim: Any = None
    camera_path: str = ""


class SessionManager:
    def __init__(self, max_sessions: int = 15, render_width: int = 1280, render_height: int = 720):
        self._max_sessions = max_sessions
        self._render_width = render_width
        self._render_height = render_height
        self._sessions: Dict[str, UserSession] = {}
        self._session_order: List[str] = []
        self._round_robin_index: int = 0
        carb.log_info(f"[SessionManager] Initialized (max={max_sessions}, res={render_width}x{render_height})")

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    def create_session(self, ws) -> Optional[UserSession]:
        if len(self._sessions) >= self._max_sessions:
            carb.log_warn("[SessionManager] At capacity")
            return None

        session_id = uuid.uuid4().hex[:12]
        camera_path = f"/MultiSession/cam_{session_id}"

        cam_prim = self._create_camera(
            camera_path,
            DEFAULT_START_POSITION,
            DEFAULT_START_ROTATION,
            DEFAULT_FOV,
        )
        if cam_prim is None:
            carb.log_error(f"[SessionManager] Failed to create camera for {session_id}")
            return None

        vp_window, vp_api = self._create_viewport(session_id, camera_path)
        if vp_api is None:
            carb.log_error(f"[SessionManager] Failed to create viewport for {session_id}")
            self._remove_camera(camera_path)
            return None

        session = UserSession(
            session_id=session_id,
            ws=ws,
            camera_prim=cam_prim,
            camera_path=camera_path,
            viewport_window=vp_window,
            viewport_api=vp_api,
        )

        self._sessions[session_id] = session
        self._session_order.append(session_id)

        xf = UsdGeom.Xformable(cam_prim)
        ops = xf.GetOrderedXformOps()
        op_names = [op.GetOpName() for op in ops]
        carb.log_info(
            f"[SessionManager] Session {session_id}: camera={camera_path}, "
            f"viewport=VP_{session_id}, xformOps={op_names}, "
            f"pos={session.position}, rot={session.rotation}, "
            f"({self.session_count}/{self._max_sessions})"
        )
        return session

    def remove_session(self, session_id: str):
        session = self._sessions.pop(session_id, None)
        if session is None:
            return

        if session_id in self._session_order:
            idx = self._session_order.index(session_id)
            self._session_order.remove(session_id)
            if self._round_robin_index > idx:
                self._round_robin_index = max(0, self._round_robin_index - 1)
            if self._session_order and self._round_robin_index >= len(self._session_order):
                self._round_robin_index = 0

        if session.viewport_window:
            try:
                session.viewport_window.destroy()
            except Exception:
                pass

        self._remove_camera(session.camera_path)

        carb.log_info(
            f"[SessionManager] Session removed: {session_id} "
            f"({self.session_count}/{self._max_sessions})"
        )

    def get_next_session(self) -> Optional[UserSession]:
        if not self._session_order:
            return None
        self._round_robin_index %= len(self._session_order)
        sid = self._session_order[self._round_robin_index]
        self._round_robin_index = (self._round_robin_index + 1) % len(self._session_order)
        return self._sessions.get(sid)

    def update_camera(self, session_id: str, position, rotation, fov=DEFAULT_FOV):
        session = self._sessions.get(session_id)
        if session is None:
            return

        if session.frames_sent < 3:
            carb.log_info(
                f"[SessionManager] Camera update #{session.frames_sent} for {session_id}: "
                f"pos=({position[0]:.1f},{position[1]:.1f},{position[2]:.1f}), "
                f"rot=({rotation[0]:.2f},{rotation[1]:.2f},{rotation[2]:.2f})"
            )

        session.position = list(position)
        session.rotation = list(rotation)
        session.fov = fov

        if session.camera_prim and session.camera_prim.IsValid():
            self._set_camera_transform(session.camera_prim, position, rotation, fov)

    def get_all_sessions(self) -> List[UserSession]:
        return list(self._sessions.values())

    def rebuild_cameras_on_new_stage(self):
        stage = omni.usd.get_context().get_stage()
        if not stage:
            carb.log_error("[SessionManager] No stage for camera rebuild")
            return

        for sid, session in self._sessions.items():
            cam_prim = self._create_camera(
                session.camera_path,
                session.position,
                session.rotation,
                session.fov,
            )
            if cam_prim is None:
                carb.log_error(f"[SessionManager] Failed to rebuild camera for {sid}")
                continue

            session.camera_prim = cam_prim

            if session.viewport_api:
                session.viewport_api.camera_path = Sdf.Path(session.camera_path)

            carb.log_info(
                f"[SessionManager] Rebuilt camera for {sid}: {session.camera_path}, "
                f"pos=({session.position[0]:.1f},{session.position[1]:.1f},{session.position[2]:.1f}), "
                f"rot=({session.rotation[0]:.2f},{session.rotation[1]:.2f},{session.rotation[2]:.2f})"
            )

        carb.log_info(f"[SessionManager] Rebuilt {len(self._sessions)} cameras on new stage")

    def cleanup_all(self):
        for sid in list(self._sessions.keys()):
            self.remove_session(sid)
        carb.log_info("[SessionManager] All sessions cleaned up")

    def _create_camera(self, path: str, position=None, rotation=None, fov=None):
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return None

        if not stage.GetPrimAtPath("/MultiSession"):
            UsdGeom.Scope.Define(stage, "/MultiSession")

        cam = UsdGeom.Camera.Define(stage, path)
        prim = cam.GetPrim()
        xf = UsdGeom.Xformable(prim)
        xf.ClearXformOpOrder()

        pos = position or DEFAULT_START_POSITION
        rot = rotation or DEFAULT_START_ROTATION
        f = fov or DEFAULT_FOV

        translate_op = xf.AddTranslateOp()
        translate_op.Set(Gf.Vec3d(pos[0], pos[1], pos[2]))

        rotate_op = xf.AddRotateXYZOp()
        rotate_op.Set(Gf.Vec3f(rot[0], rot[1], rot[2]))

        h_aperture = 20.955
        cam.GetHorizontalApertureAttr().Set(h_aperture)
        cam.GetVerticalApertureAttr().Set(15.2908)
        cam.GetClippingRangeAttr().Set(Gf.Vec2f(1.0, 100000.0))

        fov_rad = math.radians(f)
        focal = h_aperture / (2.0 * math.tan(fov_rad / 2.0))
        cam.GetFocalLengthAttr().Set(focal)

        carb.log_info(
            f"[SessionManager] Camera created: {path}, "
            f"pos=({pos[0]:.1f},{pos[1]:.1f},{pos[2]:.1f}), "
            f"rot=({rot[0]:.2f},{rot[1]:.2f},{rot[2]:.2f}), fov={f}"
        )
        return prim

    def _remove_camera(self, path: str):
        try:
            stage = omni.usd.get_context().get_stage()
            if stage:
                stage.RemovePrim(path)
        except Exception:
            pass

    def _create_viewport(self, session_id: str, camera_path: str):
        try:
            from omni.kit.viewport.window import ViewportWindow
            vp_name = f"VP_{session_id}"
            vp = ViewportWindow(
                vp_name,
                visible=True,
                width=self._render_width,
                height=self._render_height,
            )
            api = vp.viewport_api
            api.fill_frame = False
            api.resolution = (self._render_width, self._render_height)
            api.camera_path = Sdf.Path(camera_path)
            actual_cam = str(api.camera_path)
            carb.log_info(
                f"[SessionManager] Viewport {vp_name}: "
                f"set camera_path={camera_path}, actual={actual_cam}, "
                f"resolution={api.resolution}"
            )
            return vp, api
        except Exception as e:
            carb.log_error(f"[SessionManager] Viewport creation error: {e}")
            return None, None

    @staticmethod
    def _set_camera_transform(prim, position, rotation, fov):
        xf = UsdGeom.Xformable(prim)
        ops = xf.GetOrderedXformOps()
        for op in ops:
            name = op.GetOpName()
            if name == "xformOp:translate":
                op.Set(Gf.Vec3d(position[0], position[1], position[2]))
            elif "rotate" in name.lower():
                op.Set(Gf.Vec3f(rotation[0], rotation[1], rotation[2]))

        h_aperture = 20.955
        fov_rad = math.radians(fov)
        focal = h_aperture / (2.0 * math.tan(fov_rad / 2.0))
        UsdGeom.Camera(prim).GetFocalLengthAttr().Set(focal)
