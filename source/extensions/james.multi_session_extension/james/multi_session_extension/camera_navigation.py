import asyncio
import json
import math
import os
from typing import Dict, Any, Optional

import carb
from pxr import UsdGeom, Gf

from .session_manager import UserSession


class CameraNavigation:
    def __init__(self, presets_path: str):
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._active_animations: Dict[str, bool] = {}
        self._load_presets(presets_path)
        carb.log_info(
            f"[CameraNavigation] Loaded {len(self._positions)} positions: "
            f"{list(self._positions.keys())}"
        )

    def _load_presets(self, path: str):
        if not os.path.exists(path):
            carb.log_warn(f"[CameraNavigation] Presets not found: {path}")
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
            for key, value in data.items():
                self._positions[key.lower()] = value
        except Exception as e:
            carb.log_error(f"[CameraNavigation] Failed to load presets: {e}")

    def get_all_positions(self) -> Dict[str, Dict[str, Any]]:
        return {k: {**v} for k, v in self._positions.items()}

    def find_position(self, name: str) -> Optional[str]:
        name_lower = name.lower().strip()

        if name_lower in self._positions:
            return name_lower

        for key in self._positions:
            if name_lower in key or key in name_lower:
                return key

        for key in self._positions:
            key_words = key.replace("_", " ").split()
            name_words = name_lower.replace("_", " ").split()
            if any(w in key_words for w in name_words):
                return key

        return None

    async def navigate_to(
        self,
        session: UserSession,
        destination: str,
        speed: float = 1.0,
        instant: bool = False,
    ) -> bool:
        matched = self.find_position(destination)
        if not matched:
            carb.log_warn(f"[CameraNavigation] Unknown destination: {destination}")
            return False

        self._active_animations[session.session_id] = False
        await asyncio.sleep(0.05)

        if not session.camera_prim or not session.camera_prim.IsValid():
            carb.log_error(f"[CameraNavigation] Invalid camera for {session.session_id}")
            return False

        xf = UsdGeom.Xformable(session.camera_prim)
        translate_op = None
        rotate_op = None
        for op in xf.GetOrderedXformOps():
            op_name = op.GetOpName()
            if op_name == "xformOp:translate":
                translate_op = op
            elif "rotate" in op_name.lower():
                rotate_op = op

        if not translate_op or not rotate_op:
            carb.log_error(f"[CameraNavigation] Missing xform ops for {session.session_id}")
            return False

        target = self._positions[matched]
        tx, ty, tz = target["location"]
        rx, ry, rz = target["rotation"]

        if instant:
            translate_op.Set(Gf.Vec3d(tx, ty, tz))
            rotate_op.Set(Gf.Vec3f(rx, ry, rz))
            session.position = [tx, ty, tz]
            session.rotation = [rx, ry, rz]
            carb.log_info(f"[CameraNavigation] {session.session_id} teleported to {matched}")
            await self._send_navigate_complete(session, matched, tx, ty, tz, rx, ry, rz)
            return True

        current_pos = translate_op.Get()
        current_rot = rotate_op.Get()

        dist = math.sqrt(
            (tx - current_pos[0]) ** 2
            + (ty - current_pos[1]) ** 2
            + (tz - current_pos[2]) ** 2
        )
        frames = max(60, min(180, int(dist / 4.0 / speed)))

        carb.log_info(
            f"[CameraNavigation] {session.session_id} → {matched} ({frames} frames)"
        )

        self._active_animations[session.session_id] = True

        for i in range(frames + 1):
            if not self._active_animations.get(session.session_id, False):
                carb.log_info(f"[CameraNavigation] Animation cancelled for {session.session_id}")
                return False

            t = i / frames
            t = 1.0 - math.pow(2.0, -10.0 * t) if t < 1.0 else 1.0

            new_pos = Gf.Vec3d(
                current_pos[0] + (tx - current_pos[0]) * t,
                current_pos[1] + (ty - current_pos[1]) * t,
                current_pos[2] + (tz - current_pos[2]) * t,
            )
            translate_op.Set(new_pos)

            new_rot = Gf.Vec3f(
                current_rot[0] + (rx - current_rot[0]) * t,
                current_rot[1] + (ry - current_rot[1]) * t,
                current_rot[2] + (rz - current_rot[2]) * t,
            )
            rotate_op.Set(new_rot)

            session.position = [float(new_pos[0]), float(new_pos[1]), float(new_pos[2])]
            session.rotation = [float(new_rot[0]), float(new_rot[1]), float(new_rot[2])]

            await asyncio.sleep(1 / 60)

        translate_op.Set(Gf.Vec3d(tx, ty, tz))
        rotate_op.Set(Gf.Vec3f(rx, ry, rz))
        session.position = [tx, ty, tz]
        session.rotation = [rx, ry, rz]

        self._active_animations.pop(session.session_id, None)
        carb.log_info(f"[CameraNavigation] {session.session_id} arrived at {matched}")

        await self._send_navigate_complete(session, matched, tx, ty, tz, rx, ry, rz)
        return True

    async def _send_navigate_complete(self, session, destination, tx, ty, tz, rx, ry, rz):
        try:
            if session.ws and not session.ws.closed:
                await session.ws.send_json({
                    "type": "navigate_complete",
                    "destination": destination,
                    "position": [tx, ty, tz],
                    "rotation": [rx, ry, rz],
                    "fov": session.fov,
                })
        except Exception:
            pass

    def stop_animation(self, session_id: str):
        self._active_animations[session_id] = False

    def is_animating(self, session_id: str) -> bool:
        return self._active_animations.get(session_id, False)
