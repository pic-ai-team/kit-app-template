import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

import carb
import omni.usd
from pxr import Usd, UsdGeom, Gf, Sdf

DEFAULT_START_POSITION = [328.39, 268.79, -492.29]
DEFAULT_START_ROTATION = [0.43, -84.74, 4.67]
DEFAULT_FOV = 60.0

CAMERA_ROOT = "/MultiSession/Cameras"


@dataclass
class UserSession:
    session_id: str
    camera_prim_path: str
    position: List[float] = field(default_factory=lambda: list(DEFAULT_START_POSITION))
    rotation: List[float] = field(default_factory=lambda: list(DEFAULT_START_ROTATION))
    fov: float = DEFAULT_FOV
    ws: Any = None
    connected_at: float = field(default_factory=time.time)
    last_frame_time: float = 0.0
    frames_sent: int = 0


class SessionManager:
    def __init__(self, max_sessions: int = 15):
        self._max_sessions = max_sessions
        self._sessions: Dict[str, UserSession] = {}
        self._session_order: List[str] = []
        self._round_robin_index: int = 0
        self._root_created = False
        carb.log_info(f"[SessionManager] Initialized (max_sessions={max_sessions})")

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    def create_session(self, ws) -> Optional[UserSession]:
        if len(self._sessions) >= self._max_sessions:
            carb.log_warn("[SessionManager] At capacity, rejecting new session")
            return None

        session_id = uuid.uuid4().hex[:12]
        camera_path = f"{CAMERA_ROOT}/user_{session_id}"

        if not self._create_camera_prim(camera_path):
            carb.log_error(f"[SessionManager] Failed to create camera prim for {session_id}")
            return None

        session = UserSession(
            session_id=session_id,
            camera_prim_path=camera_path,
            ws=ws,
        )

        self._update_camera_prim(
            camera_path,
            session.position,
            session.rotation,
            session.fov,
        )

        self._sessions[session_id] = session
        self._session_order.append(session_id)

        carb.log_info(
            f"[SessionManager] Session created: {session_id} "
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

        self._remove_camera_prim(session.camera_prim_path)

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

    def update_camera(
        self,
        session_id: str,
        position: List[float],
        rotation: List[float],
        fov: float = DEFAULT_FOV,
    ):
        session = self._sessions.get(session_id)
        if session is None:
            return

        session.position = list(position)
        session.rotation = list(rotation)
        session.fov = fov

        self._update_camera_prim(
            session.camera_prim_path,
            position,
            rotation,
            fov,
        )

    def get_all_sessions(self) -> List[UserSession]:
        return list(self._sessions.values())

    def cleanup_all(self):
        for sid in list(self._sessions.keys()):
            self.remove_session(sid)

        stage = omni.usd.get_context().get_stage()
        if stage:
            root_prim = stage.GetPrimAtPath("/MultiSession")
            if root_prim and root_prim.IsValid():
                edit_ctx = Usd.EditContext(stage, Usd.EditTarget(stage.GetSessionLayer()))
                with edit_ctx:
                    stage.RemovePrim("/MultiSession")

        self._root_created = False
        carb.log_info("[SessionManager] All sessions cleaned up")

    def _ensure_root(self):
        if self._root_created:
            return

        stage = omni.usd.get_context().get_stage()
        if not stage:
            return

        edit_ctx = Usd.EditContext(stage, Usd.EditTarget(stage.GetSessionLayer()))
        with edit_ctx:
            if not stage.GetPrimAtPath("/MultiSession"):
                UsdGeom.Scope.Define(stage, "/MultiSession")
            if not stage.GetPrimAtPath(CAMERA_ROOT):
                UsdGeom.Scope.Define(stage, CAMERA_ROOT)

        self._root_created = True

    def _create_camera_prim(self, prim_path: str) -> bool:
        try:
            self._ensure_root()

            stage = omni.usd.get_context().get_stage()
            if not stage:
                return False

            edit_ctx = Usd.EditContext(stage, Usd.EditTarget(stage.GetSessionLayer()))
            with edit_ctx:
                camera = UsdGeom.Camera.Define(stage, prim_path)
                xformable = UsdGeom.Xformable(camera.GetPrim())
                xformable.ClearXformOpOrder()
                xformable.AddTranslateOp()
                xformable.AddRotateXYZOp()
                camera.GetHorizontalApertureAttr().Set(20.955)
                camera.GetVerticalApertureAttr().Set(15.2908)
                camera.GetClippingRangeAttr().Set(Gf.Vec2f(1.0, 100000.0))

            return True
        except Exception as e:
            carb.log_error(f"[SessionManager] Error creating camera prim: {e}")
            return False

    def _update_camera_prim(
        self,
        prim_path: str,
        position: List[float],
        rotation: List[float],
        fov: float,
    ):
        try:
            stage = omni.usd.get_context().get_stage()
            if not stage:
                return

            prim = stage.GetPrimAtPath(prim_path)
            if not prim or not prim.IsValid():
                return

            xformable = UsdGeom.Xformable(prim)
            ops = xformable.GetOrderedXformOps()

            translate_op = None
            rotate_op = None
            for op in ops:
                name = op.GetOpName()
                if name == "xformOp:translate":
                    translate_op = op
                elif "rotate" in name.lower():
                    rotate_op = op

            if translate_op:
                translate_op.Set(Gf.Vec3d(position[0], position[1], position[2]))
            if rotate_op:
                rotate_op.Set(Gf.Vec3f(rotation[0], rotation[1], rotation[2]))

            h_aperture = 20.955
            fov_rad = math.radians(fov)
            focal_length = h_aperture / (2.0 * math.tan(fov_rad / 2.0))

            camera = UsdGeom.Camera(prim)
            camera.GetFocalLengthAttr().Set(focal_length)

        except Exception as e:
            carb.log_error(f"[SessionManager] Error updating camera prim: {e}")

    def _remove_camera_prim(self, prim_path: str):
        try:
            stage = omni.usd.get_context().get_stage()
            if not stage:
                return

            edit_ctx = Usd.EditContext(stage, Usd.EditTarget(stage.GetSessionLayer()))
            with edit_ctx:
                stage.RemovePrim(prim_path)

        except Exception as e:
            carb.log_error(f"[SessionManager] Error removing camera prim: {e}")
