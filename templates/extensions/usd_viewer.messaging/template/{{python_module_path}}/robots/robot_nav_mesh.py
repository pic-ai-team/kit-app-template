"""
robot_nav_mesh.py — Navigation plane for the robot.

Builds a 2D occupancy grid from the USD stage geometry at initialization.
Provides:
  • C-Space inflation (expands obstacles by the robot radius so path-planning
    can treat the robot as a point).
  • A* shortest-path search on the inflated grid.
  • World ↔ grid coordinate conversion helpers.

The store ground plane is X,Y (Z is height / up).  The grid maps world X → columns,
world Y → rows.  Resolution is configurable (default 10 cm/cell).

If the prim ``/World/Floor`` exists its bounding box is used to determine
the grid extents; otherwise the full ``/World`` bbox is used with padding.

Usage:
    mesh = RobotNavMesh(cell_size=10.0, robot_radius=30.0)
    mesh.build_from_stage()            # scans /World for obstacle geometry
    path = mesh.find_path((x1, y1), (x2, y2))  # list of world (x,y) waypoints
"""

import heapq
import math
from typing import Dict, List, Optional, Tuple

import carb

# Grid cell states
FREE = 0
OBSTACLE = 1
INFLATED = 2  # C-Space inflation zone — blocked for the robot centre


class RobotNavMesh:
    """2D occupancy grid + A* path-planner for the robot."""

    def __init__(
        self,
        cell_size: float = 10.0,
        robot_radius: float = 30.0,
        floor_z: float = 0.0,
        scan_height: float = 30.0,
    ):
        """
        Args:
            cell_size: Size of each grid cell in scene units (cm).
            robot_radius: Robot bounding-circle radius in scene units.
                          Used for C-Space inflation.
            floor_z: Z coordinate of the floor plane (Z-up store).
            scan_height: Only geometry between floor_z and floor_z + scan_height
                         is considered an obstacle.
        """
        self._cell_size = cell_size
        self._robot_radius = robot_radius
        self._floor_z = floor_z
        self._scan_height = scan_height

        # Grid data — populated by build_from_stage()
        self._grid: List[List[int]] = []
        self._width = 0   # columns (X axis)
        self._height = 0  # rows    (Y axis — store ground plane)
        self._origin_x = 0.0  # world X of grid column 0
        self._origin_y = 0.0  # world Y of grid row 0

        self._built = False

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build_from_stage(
        self,
        obstacle_prims: Optional[List[str]] = None,
        bounds_min: Optional[Tuple[float, float]] = None,
        bounds_max: Optional[Tuple[float, float]] = None,
    ) -> bool:
        """
        Scan the USD stage and build the occupancy grid.

        Args:
            obstacle_prims: Explicit list of prim paths to treat as obstacles.
                            If None, all Mesh / Xform children of /World that
                            intersect the scan slab are included.
            bounds_min: Optional (x_min, y_min) world bounds override.
            bounds_max: Optional (x_max, y_max) world bounds override.

        Returns True on success.
        """
        try:
            import omni.usd
            from pxr import Usd, UsdGeom, Gf

            stage = omni.usd.get_context().get_stage()
            if not stage:
                carb.log_error("[RobotNavMesh] No stage available")
                return False

            # --- Collect obstacle bounding boxes ---
            boxes: List[Tuple[float, float, float, float]] = []  # (x_min, y_min, x_max, y_max)

            if obstacle_prims:
                prims = [stage.GetPrimAtPath(p) for p in obstacle_prims]
                prims = [p for p in prims if p and p.IsValid()]
            else:
                prims = self._collect_obstacle_prims(stage)

            z_lo = self._floor_z
            z_hi = self._floor_z + self._scan_height

            for prim in prims:
                bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"])
                bbox = bbox_cache.ComputeWorldBound(prim)
                rng = bbox.ComputeAlignedRange()
                if rng.IsEmpty():
                    continue

                lo = rng.GetMin()
                hi = rng.GetMax()

                # Filter by height slab (Z-up)
                if hi[2] < z_lo or lo[2] > z_hi:
                    continue

                # Ground plane is X,Y
                boxes.append((lo[0], lo[1], hi[0], hi[1]))

            if not boxes:
                carb.log_warn("[RobotNavMesh] No obstacle geometry found — grid will be fully free")

            # --- Determine grid extents ---
            if bounds_min and bounds_max:
                world_x_min, world_y_min = bounds_min
                world_x_max, world_y_max = bounds_max
            else:
                # Try /World/Floor first — its XY bbox defines the drivable area
                floor_prim = stage.GetPrimAtPath("/World/Floor")
                if floor_prim and floor_prim.IsValid():
                    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"])
                    bbox = bbox_cache.ComputeWorldBound(floor_prim)
                    rng = bbox.ComputeAlignedRange()
                    lo = rng.GetMin()
                    hi = rng.GetMax()
                    pad = self._robot_radius
                    world_x_min = lo[0] - pad
                    world_y_min = lo[1] - pad
                    world_x_max = hi[0] + pad
                    world_y_max = hi[1] + pad
                    carb.log_info(
                        f"[RobotNavMesh] Using /World/Floor bounds: "
                        f"({world_x_min:.0f},{world_y_min:.0f})-({world_x_max:.0f},{world_y_max:.0f})"
                    )
                else:
                    # Fallback: auto-detect from /World root bbox
                    root = stage.GetPrimAtPath("/World")
                    if root and root.IsValid():
                        bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"])
                        bbox = bbox_cache.ComputeWorldBound(root)
                        rng = bbox.ComputeAlignedRange()
                        lo = rng.GetMin()
                        hi = rng.GetMax()
                        pad = self._robot_radius * 2
                        world_x_min = lo[0] - pad
                        world_y_min = lo[1] - pad
                        world_x_max = hi[0] + pad
                        world_y_max = hi[1] + pad
                    else:
                        world_x_min, world_y_min = -1000.0, -1000.0
                        world_x_max, world_y_max = 2000.0, 2000.0

            self._origin_x = world_x_min
            self._origin_y = world_y_min
            self._width = max(1, int(math.ceil((world_x_max - world_x_min) / self._cell_size)))
            self._height = max(1, int(math.ceil((world_y_max - world_y_min) / self._cell_size)))

            carb.log_info(
                f"[RobotNavMesh] Grid: {self._width}x{self._height} cells, "
                f"cell_size={self._cell_size}, world=({world_x_min:.0f},{world_y_min:.0f})-"
                f"({world_x_max:.0f},{world_y_max:.0f})"
            )

            # --- Rasterize obstacles ---
            self._grid = [[FREE] * self._width for _ in range(self._height)]

            for bx_min, by_min, bx_max, by_max in boxes:
                c_min = max(0, int((bx_min - self._origin_x) / self._cell_size))
                c_max = min(self._width - 1, int((bx_max - self._origin_x) / self._cell_size))
                r_min = max(0, int((by_min - self._origin_y) / self._cell_size))
                r_max = min(self._height - 1, int((by_max - self._origin_y) / self._cell_size))
                for r in range(r_min, r_max + 1):
                    for c in range(c_min, c_max + 1):
                        self._grid[r][c] = OBSTACLE

            # --- C-Space inflation ---
            inflate_cells = max(1, int(math.ceil(self._robot_radius / self._cell_size)))
            self._inflate(inflate_cells)

            self._built = True
            obstacle_count = sum(
                1 for r in range(self._height)
                for c in range(self._width)
                if self._grid[r][c] != FREE
            )
            carb.log_info(
                f"[RobotNavMesh] Built successfully. "
                f"Obstacles + inflated: {obstacle_count}/{self._width * self._height} cells"
            )
            return True

        except Exception as e:
            carb.log_error(f"[RobotNavMesh] build_from_stage failed: {e}")
            import traceback
            carb.log_error(traceback.format_exc())
            return False

    def _collect_obstacle_prims(self, stage):
        """Collect prims under /World that are likely physical obstacles."""
        from pxr import UsdGeom
        import carb

        result = []
        world = stage.GetPrimAtPath("/World")
        if not world or not world.IsValid():
            return result

        # Folders we want to completely ignore
        skip_prefixes = ("/World/Camera", "/World/Light", "/World/Robots", "/World/Prototypes", "/World/Floor")
        
        # Broken meshes that are split across the store which leads to gigantic bounding boxes that mess up the free space calculation.
        # TODO: Split these meshes properly in the source USD and remove this hack
        skip_broken_wall_meshes = ("/World/_dstore/wall_02/Mesh_215", "/World/_dstore/Screen/Mesh_203", "/World/_dstore/Shelves_L_01/Mesh_204", "/World/_dstore/wall_01/Mesh_214", "/World/_dstore/wall_02/Mesh_215", "/World/_dstore/wall_03/Mesh_216", "/World/_dstore/ceiling/Mesh_217", "/World/_dstore/Shelves_m_01/Mesh_232", "/World/_dstore/UI_panel02/Mesh_245", "/World/_dstore/Small_Shelves002/Mesh_263", "/World/_dstore/candy_ZB_B_01/Mesh_316")
        skip_prefixes += skip_broken_wall_meshes
        def traverse_and_collect(prim):
            path_str = str(prim.GetPath())

            # 1. PRUNE: If the path starts with a skipped folder, return immediately.
            # This completely stops the script from looking at any children inside.
            if any(path_str.startswith(s) for s in skip_prefixes):
                return

            # 2. COLLECT: If it is a mesh, add it to our obstacle list
            if prim.IsA(UsdGeom.Mesh):
                result.append(prim)

            # 3. DIG DEEPER: Recursively call this function on all children
            for child in prim.GetChildren():
                traverse_and_collect(child)

        # Kick off the scan starting at the /World root
        traverse_and_collect(world)

        carb.log_info(f"[RobotNavMesh] Collected {len(result)} obstacle prims")
        return result

    def _inflate(self, cells: int) -> None:
        """Expand obstacles by `cells` in all directions (C-Space inflation)."""
        if cells <= 0:
            return

        # Collect all obstacle cell positions first
        obstacles: List[Tuple[int, int]] = []
        for r in range(self._height):
            for c in range(self._width):
                if self._grid[r][c] == OBSTACLE:
                    obstacles.append((r, c))

        # Mark neighbours as INFLATED
        for r, c in obstacles:
            for dr in range(-cells, cells + 1):
                for dc in range(-cells, cells + 1):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < self._height and 0 <= nc < self._width:
                        if self._grid[nr][nc] == FREE:
                            # Use circular inflation (Euclidean distance)
                            if dr * dr + dc * dc <= cells * cells:
                                self._grid[nr][nc] = INFLATED

    # ------------------------------------------------------------------
    # Coordinate conversion
    # ------------------------------------------------------------------

    def world_to_grid(self, wx: float, wy: float) -> Tuple[int, int]:
        """Convert world (X, Y) → grid (col, row)."""
        c = int((wx - self._origin_x) / self._cell_size)
        r = int((wy - self._origin_y) / self._cell_size)
        c = max(0, min(self._width - 1, c))
        r = max(0, min(self._height - 1, r))
        return c, r

    def grid_to_world(self, col: int, row: int) -> Tuple[float, float]:
        """Convert grid (col, row) → world (X, Y) at cell centre."""
        wx = self._origin_x + (col + 0.5) * self._cell_size
        wy = self._origin_y + (row + 0.5) * self._cell_size
        return wx, wy

    def is_free(self, col: int, row: int) -> bool:
        """Return True if the cell is traversable."""
        if 0 <= row < self._height and 0 <= col < self._width:
            return self._grid[row][col] == FREE
        return False

    @property
    def is_built(self) -> bool:
        return self._built

    # ------------------------------------------------------------------
    # A* Path-finding
    # ------------------------------------------------------------------

    def find_path(
        self,
        start_world: Tuple[float, float],
        goal_world: Tuple[float, float],
    ) -> Optional[List[Tuple[float, float]]]:
        """
        Find the shortest path between two world (X, Y) coordinates.

        Uses A* on the 4-connected grid (robot moves in straight lines only).
        Returns a list of world (X, Y) waypoints, or None if no path exists.
        """
        if not self._built:
            carb.log_error("[RobotNavMesh] Grid not built — call build_from_stage() first")
            return None

        sc, sr = self.world_to_grid(*start_world)
        gc, gr = self.world_to_grid(*goal_world)

        # Snap start/goal to nearest free cell if they're in an obstacle
        sc, sr = self._nearest_free(sc, sr)
        gc, gr = self._nearest_free(gc, gr)

        if sc is None or gc is None:
            carb.log_warn("[RobotNavMesh] Start or goal in unreachable area")
            return None

        # A* (4-connected: up, down, left, right — straight moves only)
        open_set: List[Tuple[float, int, int]] = []  # (f_score, col, row)
        heapq.heappush(open_set, (0.0, sc, sr))

        came_from: Dict[Tuple[int, int], Tuple[int, int]] = {}
        g_score: Dict[Tuple[int, int], float] = {(sc, sr): 0.0}

        directions = [(0, 1), (0, -1), (1, 0), (-1, 0)]  # 4-connected

        while open_set:
            _, cc, cr = heapq.heappop(open_set)

            if (cc, cr) == (gc, gr):
                # Reconstruct path
                path_grid = [(gc, gr)]
                node = (gc, gr)
                while node in came_from:
                    node = came_from[node]
                    path_grid.append(node)
                path_grid.reverse()

                # Convert to world coords and simplify (remove collinear points)
                path_world = [self.grid_to_world(c, r) for c, r in path_grid]
                path_world = self._simplify_path(path_world)
                return path_world

            for dc, dr in directions:
                nc, nr = cc + dc, cr + dr
                if not self.is_free(nc, nr):
                    continue

                tentative_g = g_score[(cc, cr)] + 1.0
                if tentative_g < g_score.get((nc, nr), float("inf")):
                    came_from[(nc, nr)] = (cc, cr)
                    g_score[(nc, nr)] = tentative_g
                    f = tentative_g + abs(nc - gc) + abs(nr - gr)  # Manhattan heuristic
                    heapq.heappush(open_set, (f, nc, nr))

        carb.log_warn("[RobotNavMesh] No path found")
        return None

    def _nearest_free(self, col: int, row: int, max_search: int = 20) -> Tuple[Optional[int], Optional[int]]:
        """Find nearest free cell using BFS spiral from (col, row)."""
        if self.is_free(col, row):
            return col, row

        from collections import deque
        visited = set()
        queue = deque([(col, row, 0)])
        visited.add((col, row))

        while queue:
            c, r, dist = queue.popleft()
            if dist > max_search:
                break
            if self.is_free(c, r):
                return c, r
            for dc, dr in [(0, 1), (0, -1), (1, 0), (-1, 0)]:
                nc, nr = c + dc, r + dr
                if (nc, nr) not in visited and 0 <= nr < self._height and 0 <= nc < self._width:
                    visited.add((nc, nr))
                    queue.append((nc, nr, dist + 1))

        return None, None

    def _simplify_path(self, path: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """Collapse A* staircase segments into clean L-shaped turns.

        Instead of many tiny axis-aligned zigzags, finds the farthest point
        reachable via an L-shape (drive X then Y, or Y then X) where both
        legs are fully obstacle-free.  Result: the robot makes a small number
        of 90° turns instead of dozens of staircase steps.
        """
        if len(path) <= 2:
            return path

        smoothed = [path[0]]
        anchor = 0

        while anchor < len(path) - 1:
            # Greedily find the farthest point reachable via an L-shape
            best = anchor + 1
            best_corner = None

            for candidate in range(len(path) - 1, anchor + 1, -1):
                corner = self._find_l_shape(path[anchor], path[candidate])
                if corner is not None:
                    best = candidate
                    best_corner = corner
                    break

            if best_corner is not None:
                ax, ay = path[anchor]
                cx, cy = best_corner
                bx, by = path[best]
                # Only insert corner if it's actually a turn (not collinear)
                if not (abs(cx - ax) < 0.01 and abs(cx - bx) < 0.01) and \
                   not (abs(cy - ay) < 0.01 and abs(cy - by) < 0.01):
                    smoothed.append(best_corner)
            smoothed.append(path[best])
            anchor = best

        return smoothed

    def _find_l_shape(
        self, p1: Tuple[float, float], p2: Tuple[float, float]
    ) -> Optional[Tuple[float, float]]:
        """Check if p1→p2 is reachable via an L-shaped axis-aligned path.

        Tries two options:
          A) Go X first: p1 → (p2.x, p1.y) → p2
          B) Go Y first: p1 → (p1.x, p2.y) → p2

        Returns the corner point if either option has both legs free,
        or None if neither works.  Prefers the option with shorter total
        distance (they're equal for L-shapes, so prefer whichever is free).
        """
        x1, y1 = p1
        x2, y2 = p2

        # Option A: X first, then Y — corner at (x2, y1)
        corner_a = (x2, y1)
        if self._axis_line_free(x1, y1, x2, y1) and self._axis_line_free(x2, y1, x2, y2):
            return corner_a

        # Option B: Y first, then X — corner at (x1, y2)
        corner_b = (x1, y2)
        if self._axis_line_free(x1, y1, x1, y2) and self._axis_line_free(x1, y2, x2, y2):
            return corner_b

        return None

    def _axis_line_free(
        self, x1: float, y1: float, x2: float, y2: float
    ) -> bool:
        """Check if an axis-aligned line between two world points is obstacle-free."""
        c1, r1 = self.world_to_grid(x1, y1)
        c2, r2 = self.world_to_grid(x2, y2)

        if c1 == c2:
            # Vertical line (same column, varying row)
            lo, hi = (min(r1, r2), max(r1, r2))
            for r in range(lo, hi + 1):
                if not self.is_free(c1, r):
                    return False
        else:
            # Horizontal line (same row, varying column)
            lo, hi = (min(c1, c2), max(c1, c2))
            for c in range(lo, hi + 1):
                if not self.is_free(c, r1):
                    return False
        return True

    # ------------------------------------------------------------------
    # Debug / introspection
    # ------------------------------------------------------------------

    def get_grid_info(self) -> Dict:
        """Return grid metadata for debugging."""
        if not self._built:
            return {"built": False}

        total = self._width * self._height
        obstacle_count = sum(1 for r in self._grid for c in r if c == OBSTACLE)
        inflated_count = sum(1 for r in self._grid for c in r if c == INFLATED)
        free_count = total - obstacle_count - inflated_count

        return {
            "built": True,
            "width": self._width,
            "height": self._height,
            "cell_size": self._cell_size,
            "robot_radius": self._robot_radius,
            "origin": [self._origin_x, self._origin_y],
            "total_cells": total,
            "obstacle_cells": obstacle_count,
            "inflated_cells": inflated_count,
            "free_cells": free_count,
        }

    def get_grid_data(self) -> Optional[Dict]:
        """Return the grid for visualization as a flat string.

        Each cell is encoded as a single character ('0'=free, '1'=obstacle,
        '2'=inflated).  This keeps the payload well under the WebRTC 64 KB
        message limit even for large grids.
        """
        if not self._built:
            return None

        # Downsample so the grid stays manageable (max ~80 cells per side)
        step = max(1, max(self._width, self._height) // 80)
        chars = []
        out_h = 0
        out_w = 0
        for r in range(0, self._height, step):
            row_w = 0
            for c in range(0, self._width, step):
                chars.append(str(self._grid[r][c]))
                row_w += 1
            out_w = row_w
            out_h += 1

        return {
            "flat": ''.join(chars),
            "width": out_w,
            "height": out_h,
            "cell_size": self._cell_size * step,
            "origin_x": self._origin_x,
            "origin_y": self._origin_y,
            "step": step,
        }


# Singleton
_nav_mesh: Optional[RobotNavMesh] = None


def get_robot_nav_mesh(
    cell_size: float = 10.0,
    robot_radius: float = 50.0,
) -> RobotNavMesh:
    """Get or create the singleton navigation mesh."""
    global _nav_mesh
    if _nav_mesh is None:
        _nav_mesh = RobotNavMesh(cell_size=cell_size, robot_radius=robot_radius)
    return _nav_mesh
