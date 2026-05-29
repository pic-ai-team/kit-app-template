"""
robot_controller.py — Orchestrator for the V1 Robot system.

Wires together:
  • RobotNavMesh    — 2D occupancy grid + A* pathfinding
  • RobotDriveController — event-stream state machine for movement
  • RobotCamera     — virtual camera snapshots

Exposes a single ``RobotController`` class consumed by the extension and
by ``CustomMessageManager`` for WebRTC messaging.

Robot prim: /World/Robots/create_3  (Clearpath Dingo)

Navigation positions and routes are read from the *existing*
``nav_presets.json`` / ``nav_routes.json`` used by the camera navigation
system, filtered to entries whose key starts with ``robot_``.
The ``rotation`` field ``[rx, ry, rz]`` stores Euler XYZ; ``rz`` is
the yaw (rotation around Z in the Z-up store).
"""

from typing import Any, Callable, Dict, List, Optional

import asyncio
import carb

from .robot_nav_mesh import RobotNavMesh, get_robot_nav_mesh
from .robot_drive_controller import (
    RobotDriveController,
    get_robot_drive_controller,
)
from .robot_camera import RobotCamera, get_robot_camera

# Prefix used to identify robot nav presets / routes
ROBOT_PREFIX = "robot_"

# Prefix for robot shelf analysis routes
ROBOT_SHELF_ANALYSIS_PREFIX = "robot_shelf_analysis_"

class RobotController:
    """
    High-level robot API consumed by the messaging layer.

    Lifecycle:
        controller = RobotController()
        controller.initialize()   # builds nav mesh, starts drive loop
        ...
        controller.shutdown()
    """

    def __init__(self):
        self._nav_mesh: Optional[RobotNavMesh] = None
        self._drive: Optional[RobotDriveController] = None
        self._camera: Optional[RobotCamera] = None

        # Robot navigation presets (robot_xxx)
        self._nav_positions: Dict[str, Dict[str, Any]] = {}
        # Robot routes
        self._routes: Dict[str, Dict[str, Any]] = {}

        # Status callback (pushed to frontend via messaging)
        self._on_status: Optional[Callable[[Dict[str, Any]], None]] = None

        # Sequential route navigation state
        self._route_queue: List[Dict[str, Any]] = []
        self._navigating_route: bool = False

        self._initialized = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> bool:
        """Build the nav mesh and start the drive controller loop."""
        if self._initialized:
            return True

        carb.log_info("[RobotController] Initializing...")

        # Nav mesh
        self._nav_mesh = get_robot_nav_mesh(cell_size=10.0, robot_radius=50.0)
        # Build is deferred until the stage is loaded — see build_nav_mesh()

        # Drive controller
        self._drive = get_robot_drive_controller()
        self._drive.start()
        self._drive.set_on_arrive(self._on_robot_arrived)
        self._drive.set_on_status(self._on_drive_status)

        # Camera
        self._camera = get_robot_camera()

        # Load saved presets
        self._load_nav_positions()
        self._load_routes()

        self._initialized = True
        carb.log_info("[RobotController] Initialized successfully")
        return True

    def build_nav_mesh(self) -> Dict[str, Any]:
        """
        Build (or rebuild) the navigation mesh from the current stage.
        Should be called after the USD stage is fully loaded.
        Returns grid info dict.
        """
        if self._nav_mesh is None:
            self._nav_mesh = get_robot_nav_mesh(cell_size=10.0, robot_radius=50.0)

        ok = self._nav_mesh.build_from_stage()
        info = self._nav_mesh.get_grid_info()
        if ok:
            carb.log_info(f"[RobotController] Nav mesh built: {info}")
        else:
            carb.log_error("[RobotController] Nav mesh build failed")
        return info

    def shutdown(self) -> None:
        """Stop the drive controller and clean up."""
        if self._drive:
            self._drive.shutdown()
        self._initialized = False
        carb.log_info("[RobotController] Shut down")

    # ------------------------------------------------------------------
    # Status callbacks
    # ------------------------------------------------------------------

    def set_on_status(self, cb: Callable[[Dict[str, Any]], None]) -> None:
        """Set callback for status updates (pushed to frontend)."""
        self._on_status = cb

    def _on_robot_arrived(self) -> None:
        # Continue route navigation if waypoints remain
        if self._navigating_route and self._route_queue:
            self._navigate_next_route_wp()
            return
        self._navigating_route = False
        if self._on_status:
            pos = self._drive.get_position() if self._drive else None
            self._on_status({
                "event": "arrived",
                "position": list(pos[:3]) if pos else None,
                "yaw": pos[3] if pos else None,
                "state": "idle",
            })

    def _on_drive_status(self, x: float, y: float, z: float, yaw: float, state: str) -> None:
        if self._on_status:
            self._on_status({
                "event": "status",
                "position": [x, y, z],
                "yaw": yaw,
                "state": state,
            })

    # ------------------------------------------------------------------
    # Direct input navigation
    # ------------------------------------------------------------------

    def move_forward(self, distance: float = 50.0) -> Dict[str, Any]:
        self._ensure_initialized()
        self._drive.move_forward(distance)
        return {"ok": True, "command": "forward", "distance": distance}

    def move_backward(self, distance: float = 50.0) -> Dict[str, Any]:
        self._ensure_initialized()
        self._drive.move_backward(distance)
        return {"ok": True, "command": "backward", "distance": distance}

    def turn_left(self, degrees: float = 90.0) -> Dict[str, Any]:
        self._ensure_initialized()
        self._drive.turn_left(degrees)
        return {"ok": True, "command": "turn_left", "degrees": degrees}

    def turn_right(self, degrees: float = 90.0) -> Dict[str, Any]:
        self._ensure_initialized()
        self._drive.turn_right(degrees)
        return {"ok": True, "command": "turn_right", "degrees": degrees}

    def stop(self) -> Dict[str, Any]:
        self._ensure_initialized()
        self._route_queue = []
        self._navigating_route = False
        self._drive.stop()
        return {"ok": True, "command": "stop"}

    def reset(self) -> Dict[str, Any]:
        """Stop the robot and teleport it back to its initial position."""
        self._ensure_initialized()
        self._route_queue = []
        self._navigating_route = False
        ok = self._drive.reset_position()
        return {"ok": ok, "command": "reset"}

    # ------------------------------------------------------------------
    # Coordinate / point navigation
    # ------------------------------------------------------------------

    def navigate_to_point(self, name: str) -> Dict[str, Any]:
        """Navigate to a named robot_* nav point."""
        self._ensure_initialized()
        # Cancel any in-progress route navigation
        self._route_queue = []
        self._navigating_route = False

        key = name.lower().strip().replace(" ", "_")
        if key not in self._nav_positions:
            return {"ok": False, "error": f"Unknown nav point: {key}"}

        point = self._nav_positions[key]
        loc = point["location"]
        yaw = point.get("yaw", None)
        # Ground plane is X,Y (indices 0, 1)
        ok = self._drive.navigate_to(loc[0], loc[1], target_yaw=yaw)
        return {"ok": ok, "destination": key}

    def navigate_to_coords(self, x: float, y: float, yaw: Optional[float] = None) -> Dict[str, Any]:
        """Navigate to arbitrary world coordinates on the XY ground plane."""
        self._ensure_initialized()
        self._route_queue = []
        self._navigating_route = False
        ok = self._drive.navigate_to(x, y, target_yaw=yaw)
        return {"ok": ok, "target": [x, y], "yaw": yaw}

    # ------------------------------------------------------------------
    # Route navigation
    # ------------------------------------------------------------------

    def navigate_route(self, route_name: str) -> Dict[str, Any]:
        """Execute a robot route (sequential waypoints)."""
        self._ensure_initialized()
        key = route_name.lower().strip().replace(" ", "_")
        if key not in self._routes:
            return {"ok": False, "error": f"Unknown route: {key}"}

        route = self._routes[key]
        waypoints = route.get("waypoints", [])
        if not waypoints:
            return {"ok": False, "error": "Route has no waypoints"}

        # Store waypoints and navigate to the first one.
        # Subsequent waypoints are triggered sequentially in _on_robot_arrived.
        self._route_queue = list(waypoints)
        self._navigating_route = True
        self._navigate_next_route_wp()

        return {"ok": True, "route": key, "waypoint_count": len(waypoints)}

    def _navigate_next_route_wp(self) -> None:
        """Pop the next route waypoint and navigate to it."""
        if not self._route_queue:
            self._navigating_route = False
            return
        wp = self._route_queue.pop(0)
        loc = wp["location"]
        yaw = wp.get("yaw", None)
        carb.log_info(
            f"[RobotController] Route: navigating to waypoint "
            f"({loc[0]:.0f}, {loc[1]:.0f}), {len(self._route_queue)} remaining"
        )
        self._drive.navigate_to(loc[0], loc[1], target_yaw=yaw)

    # ------------------------------------------------------------------
    # Await-able navigation (for automated workflows)
    # ------------------------------------------------------------------

    async def navigate_to_and_wait(
        self, x: float, y: float, target_yaw: Optional[float] = None, timeout: float = 120.0
    ) -> bool:
        """
        Navigate to (x, y) and block until the robot arrives or times out.

        Returns True if the robot arrived, False on timeout.
        """
        self._ensure_initialized()
        self._route_queue = []
        self._navigating_route = False

        arrived_event = asyncio.Event()
        original_on_arrive = self._drive._on_arrive

        def _on_arrive_signal():
            arrived_event.set()
            if original_on_arrive:
                original_on_arrive()

        self._drive.set_on_arrive(_on_arrive_signal)

        ok = self._drive.navigate_to(x, y, target_yaw=target_yaw)
        if not ok:
            self._drive.set_on_arrive(self._on_robot_arrived)
            return False

        try:
            await asyncio.wait_for(arrived_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            carb.log_warn(f"[RobotController] navigate_to_and_wait timed out ({timeout}s)")
            return False
        finally:
            self._drive.set_on_arrive(self._on_robot_arrived)

    # ------------------------------------------------------------------
    # Shelf analysis routes
    # ------------------------------------------------------------------

    def get_shelf_analysis_routes(self) -> Dict[str, Dict[str, Any]]:
        """Return only routes with the robot_shelf_analysis_ prefix."""
        return {
            k: v for k, v in self._routes.items()
            if k.startswith(ROBOT_SHELF_ANALYSIS_PREFIX)
        }

    # ------------------------------------------------------------------
    # Camera
    # ------------------------------------------------------------------

    async def capture_frame(self, width: int = 250, quality: int = 50) -> Dict[str, Any]:
        """Take a snapshot from the robot's virtual camera."""
        self._ensure_initialized()
        frame = await self._camera.capture_frame(width=width, quality=quality)
        if frame:
            return {"ok": True, "frame": frame}
        return {"ok": False, "error": "Capture failed"}

    async def capture_frame_full_res(
        self, width: int = 400, height: int = 1000, quality: int = 90
    ) -> Dict[str, Any]:
        """Take a high-resolution snapshot (no thumbnail compression)."""
        self._ensure_initialized()
        frame = await self._camera.capture_frame_full_res(
            width=width, height=height, quality=quality
        )
        if frame:
            return {"ok": True, "frame": frame}
        return {"ok": False, "error": "Full-res capture failed"}

    # ------------------------------------------------------------------
    # Robot status
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Return current robot state for the UI."""
        if not self._initialized or not self._drive:
            return {"initialized": False}

        pos = self._drive.get_position()
        return {
            "initialized": True,
            "state": self._drive.state_name,
            "is_idle": self._drive.is_idle,
            "queue_length": self._drive.queue_length,
            "position": list(pos[:3]) if pos else None,
            "yaw": pos[3] if pos else None,
            "nav_mesh_built": self._nav_mesh.is_built if self._nav_mesh else False,
        }

    # ------------------------------------------------------------------
    # Nav position presets (robot_* from shared nav_presets.json)
    # ------------------------------------------------------------------

    def get_nav_positions(self) -> Dict[str, Dict[str, Any]]:
        return self._nav_positions.copy()

    def reload_from_disk(self) -> None:
        """Reload nav positions and routes from disk (picks up external changes)."""
        try:
            from ..camera_navigation import get_camera_navigation
            nav = get_camera_navigation()
            # Reload the underlying camera navigation JSON files
            nav._load_custom_positions()
            nav._load_routes()
            nav._rebuild_positions()
        except Exception as e:
            carb.log_warn(f"[RobotController] Failed to reload camera navigation: {e}")
        self._load_nav_positions()
        self._load_routes()
        carb.log_info("[RobotController] Reloaded positions and routes from disk")

    def get_grid_data(self) -> Optional[Dict[str, Any]]:
        """Return grid data for frontend visualization."""
        if self._nav_mesh and self._nav_mesh.is_built:
            return self._nav_mesh.get_grid_data()
        return None

    def _load_nav_positions(self) -> None:
        """Load robot_* entries from the shared nav_presets.json."""
        try:
            from ..camera_navigation import get_camera_navigation
            nav = get_camera_navigation()
            all_positions = nav.get_positions()
            self._nav_positions = {}
            for key, data in all_positions.items():
                if key.startswith(ROBOT_PREFIX):
                    loc = data.get("location", [0, 0, 0])
                    rot = data.get("rotation", [0, 0, 0])
                    # Nav presets store camera convention (+Y forward at rz=0).
                    # Robot model faces +X at rz=0.  Add 90° to convert.
                    yaw = (rot[2] + 90.0) if len(rot) > 2 else 0.0
                    self._nav_positions[key] = {
                        "location": list(loc),
                        "yaw": yaw,
                        "description": data.get("description", key),
                    }
            carb.log_info(
                f"[RobotController] Loaded {len(self._nav_positions)} robot nav positions "
                f"(robot_* prefix from nav_presets.json)"
            )
        except Exception as e:
            carb.log_error(f"[RobotController] Failed to load robot nav positions: {e}")

    # ------------------------------------------------------------------
    # Route presets (robot_* from shared nav_routes.json)
    # ------------------------------------------------------------------

    def get_routes(self) -> Dict[str, Dict[str, Any]]:
        return self._routes.copy()

    def _load_routes(self) -> None:
        """Load robot_* entries from the shared nav_routes.json."""
        try:
            from ..camera_navigation import get_camera_navigation
            nav = get_camera_navigation()
            all_routes = nav.get_all_routes()
            self._routes = {}
            for key, data in all_routes.items():
                if key.startswith(ROBOT_PREFIX):
                    raw_wps = data.get("waypoints", [])
                    converted_wps = []
                    for wp in raw_wps:
                        loc = wp.get("location", [0, 0, 0])
                        rot = wp.get("rotation", [0, 0, 0])
                        # Camera convention → robot convention: +90°
                        yaw = (rot[2] + 90.0) if len(rot) > 2 else 0.0
                        converted_wps.append({
                            "location": list(loc),
                            "yaw": yaw,
                        })
                    self._routes[key] = {"waypoints": converted_wps}
            carb.log_info(
                f"[RobotController] Loaded {len(self._routes)} robot routes "
                f"(robot_* prefix from nav_routes.json)"
            )
        except Exception as e:
            carb.log_error(f"[RobotController] Failed to load robot routes: {e}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()


# Singleton
_robot_controller: Optional[RobotController] = None


def get_robot_controller() -> RobotController:
    global _robot_controller
    if _robot_controller is None:
        _robot_controller = RobotController()
    return _robot_controller

