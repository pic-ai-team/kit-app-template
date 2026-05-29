# Automated Shelf Analysis Architecture

This document describes the design and implementation of the automated shelf analysis feature using the service robot.

## Naming Convention
- **Rack**: The shelving unit (e.g., "Snacks Rack 6B")
- **Shelf**: One level/row within a rack

## Architecture Overview

The system uses a three-layer architecture for robot-based shelf analysis:

```
NemoClaw Agent ──► Agent Backend (FastAPI) ──► Kit API Server (aiohttp, port 8100)
                                                     │
                                                     ▼
                                               Robot Controller
                                                 ├─ Navigate to waypoint (A* nav mesh)
                                                 ├─ Capture high-res frame (robot camera)
                                                 └─ Return to start position
```

### Stages

#### V1: Shelf stock analysis with hidden camera (Deprecated)
Used a hidden CCTV camera teleported to waypoints. Route prefix: `planogram_analysis_`.

#### V2: Robot-based shelf analysis (Current)
The service robot physically navigates to each waypoint, captures frames from its on-board camera, and returns to its initial position after analysis.

### Route Convention
Routes are stored in `nav_routes.json` with the prefix `robot_shelf_analysis_` (e.g., `robot_shelf_analysis_snacks`). The robot controller loads routes with the `robot_` prefix and converts camera-convention yaw (+Y forward) to robot-convention yaw (+X forward) by adding 90°.

### Robot Camera
The robot camera is mounted on the robot prim at `/World/Robots/service_robotV1/Camera`. For shelf analysis it uses a tall vertical resolution (400×1000 pixels, JPEG Q90) to capture full rack height. This is separate from the default thumbnail resolution (250×175) used for UI previews.

Key methods in `robot_camera.py`:
- `capture_frame(width, quality)` — Standard thumbnail capture with downscaling
- `capture_frame_full_res(width, height, quality)` — High-res capture without compression, recreates HydraTexture at requested resolution and restores default after capture

### Rack Name Logic
Each product in the store has a `rack_id` attribute. During vision identification, the backend uses majority vote across identified `asset_keys` to determine the rack. The rack name is looked up from the `racks` table (seeded from `store_layout.json`).

### Shelf Stock Analysis Pipeline

For each waypoint in the route:
1. **Navigate**: Robot navigates to waypoint via A* pathfinding on the nav mesh
2. **Wait**: `navigate_to_and_wait()` blocks until the robot arrives (up to 120s timeout)
3. **Capture**: High-res frame (400×1000, Q90) from robot's on-board camera
4. **Identify**: POST frame to `/api/identify-shelf-products` → `asset_keys`, `product_info`, `rack_id`
5. **Detect rows**: `usd_spawner.detect_rows_for_key()` per product (floor_z clustering)
6. **Return**: Robot returns to initial position via `reset()`

After all waypoints:
- Merge results by `rack_id` (deduplicate products across waypoints)
- Cluster rows into shelf levels by `floor_z` proximity (2× tolerance gap)
- Compute stock ratios: `shelf_init_stock = product_init_stock / num_shelves_product_appears_on`

### Response Format

`_run_automatic_shelf_analysis` returns:
```json
{
    "success": true,
    "route": "robot_shelf_analysis_snacks",
    "waypoint_count": 3,
    "results": [
        {
            "rack_id": "rack_6B",
            "rack_name": "Snacks Rack 6B",
            "waypoint_count": 2,
            "stock_level": 0.66,
            "stock": 40,
            "initial_stock": 60,
            "asset_keys": ["pringles_bbq", "pringles_cheese", "cheetos_double_cheese"],
            "shelf_levels": [
                {
                    "level": 1,
                    "floor_z": 114.08,
                    "shelf_stock_level": 0.5,
                    "products": {
                        "pringles_cheese": {"stock": 3, "initial_stock": 8},
                        "pringles_bbq": {"stock": 3, "initial_stock": 8},
                        "cheetos_double_cheese": {"stock": 5, "initial_stock": 8}
                    }
                },
                {
                    "level": 2,
                    "floor_z": 85.0,
                    "shelf_stock_level": 0.8,
                    "products": { "..." : "..." }
                }
            ]
        }
    ]
}
```

### Accessing Automated Shelf Analysis

#### Via the Agent Backend API (primary)
Two endpoints proxy to the Kit API server:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/robot/shelf-analysis/routes` | GET | List available `robot_shelf_analysis_*` routes |
| `/api/robot/shelf-analysis?route=<name>` | GET | Run analysis on a route (long-running, up to 10 min timeout) |

#### Via the Kit API Server (internal)
The Kit `APIServer` (port 8100) exposes:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/robot/shelf-analysis/routes` | GET | List available routes |
| `/robot/shelf-analysis?route=<name>` | GET | Run analysis (robot navigates, captures, analyzes) |

#### Via the Webviewer
The Planogram Analysis Tab includes an "Automatic Shelf Analysis" section with a dropdown for `robot_shelf_analysis_*` routes and a run button.

#### Via NemoClaw
The `perform-automatic-shelf-analysis` skill (v2.0) automates:
1. Fetch available routes from `/api/robot/shelf-analysis/routes`
2. Run analysis via `/api/robot/shelf-analysis?route=<name>`
3. Format results and send to Store Manager via Telegram

Policy blueprint: `omniverse-agent-backend-robot-shelf-analysis.yaml`
