"""
robot_drive_controller.py — State-machine drive controller for the Dingo robot.

Movement rules (V1):
  • Forward / Backward — straight-line translation along the robot's facing.
  • Turn Left / Turn Right — standing rotation (no translation).
  • Coordinate navigation — decomposed into turn → drive → turn sequences
    via the nav mesh A* planner.

Animation is driven by an **omni.kit.app update event stream** so it keeps
running smoothly even when the main thread is busy.  No ``asyncio.sleep()``
is used.

State machine:
    IDLE → TURNING → MOVING → TURNING → … → IDLE
"""

import math
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple

import carb
import omni.kit.app

from .robot_nav_mesh import RobotNavMesh, get_robot_nav_mesh


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Movement speeds (scene units per second / degrees per second)
DEFAULT_MOVE_SPEED = 150.0   # cm/s
DEFAULT_TURN_SPEED = 90.0    # deg/s

# Per-tick step sizes at 60 FPS (used as fallback when dt is unavailable)
_FALLBACK_DT = 1.0 / 60.0

# Robot prim path in the stage
ROBOT_PRIM_PATH = "/World/Robots/service_robotV1"


class DriveState(Enum):
    IDLE = auto()
    TURNING = auto()
    MOVING = auto()


class RobotDriveController:
    """
    Event-stream-driven state machine that moves the Dingo robot prim
    on the stage along straight-line segments with standing turns.
    """

    def __init__(
        self,
        robot_prim_path: str = ROBOT_PRIM_PATH,
        move_speed: float = DEFAULT_MOVE_SPEED,
        turn_speed: float = DEFAULT_TURN_SPEED,
    ):
        self._robot_path = robot_prim_path
        self._move_speed = move_speed
        self._turn_speed = turn_speed

        # State machine
        self._state = DriveState.IDLE
        self._command_queue: List[Dict[str, Any]] = []

        # Current command data
        self._target_yaw: Optional[float] = None      # degrees
        self._target_pos: Optional[Tuple[float, float]] = None  # (x, z) world
        self._move_dir: int = 1  # +1 forward, -1 backward

        # Event stream subscription
        self._update_sub = None

        # Callback fired when robot arrives at final destination
        self._on_arrive: Optional[Callable[[], None]] = None
        # Callback fired every tick with (x, y, z, yaw) for UI status
        self._on_status: Optional[Callable[[float, float, float, float, str], None]] = None

        # Nav mesh reference (lazy init)
        self._nav_mesh: Optional[RobotNavMesh] = None

        # Initial position for reset (stored on first _get_xform_ops call)
        self._initial_position: Optional[Tuple[float, float, float, float, float, float]] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Subscribe to the Kit update event stream."""
        if self._update_sub is not None:
            return
        update_stream = omni.kit.app.get_app().get_update_event_stream()
        self._update_sub = update_stream.create_subscription_to_pop(
            self._on_update, name="RobotDriveController"
        )
        carb.log_info("[RobotDrive] Started update subscription")

    def shutdown(self) -> None:
        """Unsubscribe and reset."""
        self._state = DriveState.IDLE
        self._command_queue.clear()
        if self._update_sub is not None:
            self._update_sub = None
            carb.log_info("[RobotDrive] Stopped update subscription")

    # ------------------------------------------------------------------
    # Public API — enqueue commands
    # ------------------------------------------------------------------

    def set_on_arrive(self, cb: Callable[[], None]) -> None:
        self._on_arrive = cb

    def set_on_status(self, cb: Callable[[float, float, float, float, str], None]) -> None:
        self._on_status = cb

    def stop(self) -> None:
        """Immediately stop all movement and clear the queue."""
        self._command_queue.clear()
        self._state = DriveState.IDLE
        self._target_yaw = None
        self._target_pos = None
        carb.log_info("[RobotDrive] Stopped")
        if self._on_status:
            pos = self.get_position()
            if pos:
                self._on_status(pos[0], pos[1], pos[2], pos[3], "idle")

    def move_forward(self, distance: float = 50.0) -> None:
        """Queue a forward movement of *distance* scene units."""
        self._command_queue.append({"type": "move", "distance": abs(distance), "direction": 1})
        carb.log_info(f"[RobotDrive] Queued forward {distance}")

    def move_backward(self, distance: float = 50.0) -> None:
        """Queue a backward movement of *distance* scene units."""
        self._command_queue.append({"type": "move", "distance": abs(distance), "direction": -1})
        carb.log_info(f"[RobotDrive] Queued backward {distance}")

    def turn_left(self, degrees: float = 90.0) -> None:
        """Queue a standing left turn (counter-clockwise)."""
        self._command_queue.append({"type": "turn", "degrees": abs(degrees), "direction": 1})
        carb.log_info(f"[RobotDrive] Queued turn left {degrees}°")

    def turn_right(self, degrees: float = 90.0) -> None:
        """Queue a standing right turn (clockwise)."""
        self._command_queue.append({"type": "turn", "degrees": -abs(degrees), "direction": -1})
        carb.log_info(f"[RobotDrive] Queued turn right {degrees}°")

    def navigate_to(self, x: float, y: float, target_yaw: Optional[float] = None) -> bool:
        """
        Plan a path to (x, y) using the nav mesh and queue the
        turn-move-turn sequence.

        Args:
            x, y: Target world coordinates on the ground plane (Z-up store).
            target_yaw: Optional final heading in degrees.  If None the robot
                        will face the direction of the last segment.

        Returns True if a path was found and commands were queued.
        """
        # Clear any pending commands so the path is planned from the
        # actual current position, not some future expected position.
        self._command_queue.clear()
        self._state = DriveState.IDLE
        self._target_yaw = None
        self._target_pos = None

        pos = self.get_position()
        if pos is None:
            carb.log_error("[RobotDrive] Cannot navigate — robot prim not found")
            return False

        cur_x, cur_y, _, cur_yaw = pos

        # If already at target, skip pathfinding (avoids random atan2 from
        # near-zero deltas that would cause spurious rotations).
        dist_to_target = math.sqrt((x - cur_x) ** 2 + (y - cur_y) ** 2)
        if dist_to_target < 5.0:
            if target_yaw is not None:
                final_turn = self._shortest_angle(cur_yaw, target_yaw)
                if abs(final_turn) > 1.0:
                    d = 1 if final_turn > 0 else -1
                    self._command_queue.append({"type": "turn", "degrees": final_turn, "direction": d})
            carb.log_info("[RobotDrive] Already at target — skipping navigation")
            return True

        # Lazy-init nav mesh
        if self._nav_mesh is None:  
            self._nav_mesh = get_robot_nav_mesh(cell_size=10.0, robot_radius=50.0)
        if not self._nav_mesh.is_built:
            carb.log_warn("[RobotDrive] Nav mesh not built — driving direct")
            self._queue_direct_drive(cur_x, cur_y, cur_yaw, x, y, target_yaw)
            return True

        path = self._nav_mesh.find_path((cur_x, cur_y), (x, y))
        if path is None:
            carb.log_warn("[RobotDrive] No path found — attempting direct drive")
            self._queue_direct_drive(cur_x, cur_y, cur_yaw, x, y, target_yaw)
            return True

        # Convert path waypoints into turn+move commands.
        # The robot model faces +X at rz=0, which matches atan2 convention
        # (atan2 returns 0° for +X direction).  All yaw values are world rz.
        prev_x, prev_y = cur_x, cur_y
        heading = cur_yaw  # world rz from get_position()

        for wx, wy in path[1:]:  # skip start (current pos)
            dx = wx - prev_x
            dy = wy - prev_y
            dist = math.sqrt(dx * dx + dy * dy)
            if dist < 1.0:
                continue

            # atan2 gives angle-from-+X which matches the robot model convention
            desired_yaw = math.degrees(math.atan2(dy, dx))
            turn_delta = self._shortest_angle(heading, desired_yaw)

            if abs(turn_delta) > 1.0:
                if turn_delta > 0:
                    self._command_queue.append({"type": "turn", "degrees": turn_delta, "direction": 1})
                else:
                    self._command_queue.append({"type": "turn", "degrees": turn_delta, "direction": -1})

            self._command_queue.append({"type": "move", "distance": dist, "direction": 1})
            heading = desired_yaw
            prev_x, prev_y = wx, wy

        # Final yaw adjustment
        if target_yaw is not None:
            final_turn = self._shortest_angle(heading, target_yaw)
            if abs(final_turn) > 1.0:
                d = 1 if final_turn > 0 else -1
                self._command_queue.append({"type": "turn", "degrees": final_turn, "direction": d})

        carb.log_info(
            f"[RobotDrive] Queued {len(self._command_queue)} commands for path "
            f"({cur_x:.0f},{cur_y:.0f}) → ({x:.0f},{y:.0f})"
        )
        return True

    def _queue_direct_drive(
        self, cx: float, cy: float, cyaw: float,
        tx: float, ty: float, tyaw: Optional[float]
    ) -> None:
        """Fallback: turn toward target and drive straight."""
        dx = tx - cx
        dy = ty - cy
        dist = math.sqrt(dx * dx + dy * dy)
        if dist < 1.0:
            return
        # atan2 gives angle-from-+X which matches the robot model convention
        desired_yaw = math.degrees(math.atan2(dy, dx))
        turn_delta = self._shortest_angle(cyaw, desired_yaw)
        if abs(turn_delta) > 1.0:
            d = 1 if turn_delta > 0 else -1
            self._command_queue.append({"type": "turn", "degrees": turn_delta, "direction": d})
        self._command_queue.append({"type": "move", "distance": dist, "direction": 1})
        if tyaw is not None:
            final_turn = self._shortest_angle(desired_yaw, tyaw)
            if abs(final_turn) > 1.0:
                d = 1 if final_turn > 0 else -1
                self._command_queue.append({"type": "turn", "degrees": final_turn, "direction": d})

    # ------------------------------------------------------------------
    # Prim helpers
    # ------------------------------------------------------------------

    def get_position(self) -> Optional[Tuple[float, float, float, float]]:
        """
        Return (x, y, z, yaw_degrees) of the robot prim, or None.
        yaw is the rotation around the Z axis (Z-up store).
        """
        try:
            import omni.usd
            from pxr import UsdGeom, Gf

            stage = omni.usd.get_context().get_stage()
            if not stage:
                return None

            prim = stage.GetPrimAtPath(self._robot_path)
            if not prim or not prim.IsValid():
                carb.log_warn(f"[RobotDrive] Robot prim not found: {self._robot_path}")
                return None

            xformable = UsdGeom.Xformable(prim)
            world_xform = xformable.ComputeLocalToWorldTransform(0)
            t = world_xform.ExtractTranslation()

            # Extract yaw: transform the robot's +X forward direction to
            # world space.  USD matrices use row-vector convention (v' = v*M),
            # so m[0] = transformed +X basis.  Using TransformDir avoids
            # sign errors from raw matrix element access (m[1][0] gives -θ).
            forward = world_xform.TransformDir(Gf.Vec3d(1, 0, 0))
            yaw = math.degrees(math.atan2(forward[1], forward[0]))

            return (float(t[0]), float(t[1]), float(t[2]), yaw)

        except Exception as e:
            carb.log_error(f"[RobotDrive] get_position failed: {e}")
            return None

    def _get_xform_ops(self):
        """Get translate and rotateY ops for the robot, creating if needed."""
        try:
            import omni.usd
            from pxr import UsdGeom, Gf

            stage = omni.usd.get_context().get_stage()
            if not stage:
                return None, None, None

            prim = stage.GetPrimAtPath(self._robot_path)
            if not prim or not prim.IsValid():
                return None, None, None

            xformable = UsdGeom.Xformable(prim)

            translate_op = None
            rotate_op = None

            for op in xformable.GetOrderedXformOps():
                name = op.GetOpName()
                if name == "xformOp:translate":
                    translate_op = op
                elif "rotate" in name.lower():
                    rotate_op = op

            if translate_op is None or rotate_op is None:
                carb.log_error(
                    "[RobotDrive] Robot prim is missing xformOp:translate and/or "
                    "xformOp:rotateXYZ. Quaternion (xformOp:orient) is not "
                    "supported — convert to rotateXYZ in the USD editor first."
                )
                return None, None, None

            # Store initial position on first successful access (for reset)
            if self._initial_position is None:
                pos = translate_op.Get()
                rot = rotate_op.Get()
                self._initial_position = (
                    float(pos[0]), float(pos[1]), float(pos[2]),
                    float(rot[0]), float(rot[1]), float(rot[2]),
                )
                carb.log_info(
                    f"[RobotDrive] Stored initial position: "
                    f"({self._initial_position[0]:.1f}, {self._initial_position[1]:.1f}, {self._initial_position[2]:.1f}) "
                    f"rot=({self._initial_position[3]:.1f}, {self._initial_position[4]:.1f}, {self._initial_position[5]:.1f})"
                )

            return xformable, translate_op, rotate_op

        except Exception as e:
            carb.log_error(f"[RobotDrive] _get_xform_ops failed: {e}")
            return None, None, None

    # ------------------------------------------------------------------
    # Update tick (event stream driven)
    # ------------------------------------------------------------------

    def _on_update(self, event) -> None:
        """Called every frame by the Kit update event stream."""
        dt = getattr(event, "payload", {}).get("dt", _FALLBACK_DT)
        if isinstance(dt, dict):
            dt = _FALLBACK_DT
        dt = float(dt) if dt else _FALLBACK_DT

        if self._state == DriveState.IDLE:
            if not self._command_queue:
                return
            self._begin_next_command()

        elif self._state == DriveState.TURNING:
            self._tick_turn(dt)

        elif self._state == DriveState.MOVING:
            self._tick_move(dt)

    def _begin_next_command(self) -> None:
        """Pop the next command from the queue and start executing it."""
        if not self._command_queue:
            self._state = DriveState.IDLE
            carb.log_info("[RobotDrive] All commands complete")
            if self._on_arrive:
                self._on_arrive()
            return

        cmd = self._command_queue.pop(0)
        cmd_type = cmd["type"]

        if cmd_type == "turn":
            degrees = cmd["degrees"]
            # Use LOCAL rotation op value (not world yaw) so target and
            # _tick_turn comparison are in the same coordinate space.
            _, _, rotate_op = self._get_xform_ops()
            if rotate_op is None:
                self._state = DriveState.IDLE
                return
            current_rot = rotate_op.Get()
            local_yaw = float(current_rot[2])
            self._target_yaw = local_yaw + degrees
            self._state = DriveState.TURNING
            carb.log_info(f"[RobotDrive] Begin turn: {degrees:.1f}° (local {local_yaw:.1f} → {self._target_yaw:.1f})")

        elif cmd_type == "move":
            distance = cmd["distance"]
            direction = cmd.get("direction", 1)
            # Use LOCAL ops so translate and rotate are in the same space.
            _, translate_op, rotate_op = self._get_xform_ops()
            if translate_op is None or rotate_op is None:
                self._state = DriveState.IDLE
                return
            current_pos = translate_op.Get()
            current_rot = rotate_op.Get()
            x, y = float(current_pos[0]), float(current_pos[1])
            yaw = float(current_rot[2])
            # Model forward = +X at rz 0, matching atan2 convention
            rad = math.radians(yaw)
            dx = math.cos(rad) * distance * direction
            dy = math.sin(rad) * distance * direction
            self._target_pos = (x + dx, y + dy)
            self._move_dir = direction
            self._state = DriveState.MOVING
            carb.log_info(f"[RobotDrive] Begin move: {distance:.1f} (dir={direction})")

    def _tick_turn(self, dt: float) -> None:
        """Rotate the robot toward _target_yaw."""
        _, translate_op, rotate_op = self._get_xform_ops()
        if rotate_op is None or self._target_yaw is None:
            self._state = DriveState.IDLE
            self._begin_next_command()
            return

        from pxr import Gf

        current_rot = rotate_op.Get()
        # Z-up store: yaw = rotation around Z axis = index 2
        current_yaw = float(current_rot[2])
        delta = self._shortest_angle(current_yaw, self._target_yaw)

        step = self._turn_speed * dt
        if abs(delta) <= step:
            # Snap to target
            rotate_op.Set(Gf.Vec3f(float(current_rot[0]), float(current_rot[1]), self._target_yaw))
            self._target_yaw = None
            self._state = DriveState.IDLE
            self._emit_status("turning")
            self._begin_next_command()
        else:
            sign = 1.0 if delta > 0 else -1.0
            new_yaw = current_yaw + sign * step
            rotate_op.Set(Gf.Vec3f(float(current_rot[0]), float(current_rot[1]), new_yaw))
            self._emit_status("turning")

    def _tick_move(self, dt: float) -> None:
        """Translate the robot toward _target_pos along its facing direction."""
        _, translate_op, rotate_op = self._get_xform_ops()
        if translate_op is None or self._target_pos is None:
            self._state = DriveState.IDLE
            self._begin_next_command()
            return

        from pxr import Gf

        current_pos = translate_op.Get()
        cx, cy, cz = float(current_pos[0]), float(current_pos[1]), float(current_pos[2])
        tx, ty = self._target_pos

        dx = tx - cx
        dy = ty - cy
        remaining = math.sqrt(dx * dx + dy * dy)

        step = self._move_speed * dt
        if remaining <= step:
            # Snap to target
            translate_op.Set(Gf.Vec3d(tx, ty, cz))
            self._target_pos = None
            self._state = DriveState.IDLE
            self._emit_status("moving")
            self._begin_next_command()
        else:
            # Normalize and step
            nx = dx / remaining * step
            ny = dy / remaining * step
            translate_op.Set(Gf.Vec3d(cx + nx, cy + ny, cz))
            self._emit_status("moving")

    def reset_position(self) -> bool:
        """Teleport the robot back to its initial position and rotation."""
        self.stop()
        if self._initial_position is None:
            carb.log_warn("[RobotDrive] No initial position stored — cannot reset")
            return False
        _, translate_op, rotate_op = self._get_xform_ops()
        if translate_op is None or rotate_op is None:
            return False
        from pxr import Gf
        ix, iy, iz, irx, iry, irz = self._initial_position
        translate_op.Set(Gf.Vec3d(ix, iy, iz))
        rotate_op.Set(Gf.Vec3f(irx, iry, irz))
        carb.log_info(f"[RobotDrive] Reset to initial position ({ix:.1f}, {iy:.1f}, {iz:.1f})")
        return True

    def _emit_status(self, action: str) -> None:
        """Fire the status callback with current robot state."""
        if self._on_status:
            pos = self.get_position()
            if pos:
                state_str = action if self._command_queue or self._state != DriveState.IDLE else "idle"
                self._on_status(pos[0], pos[1], pos[2], pos[3], state_str)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _shortest_angle(current_deg: float, target_deg: float) -> float:
        """Compute shortest rotation delta in [-180, 180]."""
        d = (target_deg - current_deg) % 360.0
        if d > 180.0:
            d -= 360.0
        return d

    @property
    def is_idle(self) -> bool:
        return self._state == DriveState.IDLE and not self._command_queue

    @property
    def state_name(self) -> str:
        return self._state.name.lower()

    @property
    def queue_length(self) -> int:
        return len(self._command_queue)


# Singleton
_drive_controller: Optional[RobotDriveController] = None


def get_robot_drive_controller() -> RobotDriveController:
    global _drive_controller
    if _drive_controller is None:
        _drive_controller = RobotDriveController()
    return _drive_controller
