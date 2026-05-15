# Incident Spawner — Debris / Hazard Simulation

Spawn incident objects (trash, spills) at random ground-level positions inside
the 3D store scene.  Useful for training anomaly-detection models, testing
vision agents, and simulating store maintenance scenarios.

---

## Architecture Overview

```
Browser (Window.tsx)                      Kit (usd_spawner.py)
┌──────────────────┐                     ┌──────────────────────┐
│ Incident Panel   │                     │ UsdSpawner           │
│  ┌─────────────┐ │  incidentSpawn      │                      │
│  │ Spawn btn   │─┼──Request──────────► │ _on_incident_spawn   │
│  │ Delete btn  │─┼──DeleteAllReq─────► │ _on_incident_delete  │
│  │ Auto toggle │ │                     │   _all_request       │
│  └─────────────┘ │  incidentSpawn      │                      │
│                  │◄──Response────────── │ _pick_random_ground  │
│ Timer (setInterval)                    │   _position()        │
│  fires incidentSpawnRequest            │ _spawn_usd()         │
│  every N seconds when enabled          │                      │
└──────────────────┘                     └──────────────────────┘
```

## Message Protocol

### Spawn an incident

**Browser → Kit:**
```json
{
  "event_type": "incidentSpawnRequest",
  "payload": {
    "incident_type": "trash" | "spill" | "random"
  }
}
```

**Kit → Browser:**
```json
{
  "event_type": "incidentSpawnResponse",
  "payload": {
    "result": "success" | "error",
    "prim_path": "/World/trash_2",
    "incident_type": "trash",
    "asset_key": "trash",
    "position": [42.1, 0.0, -87.3],
    "error": ""
  }
}
```

### Delete all incidents

**Browser → Kit:**
```json
{
  "event_type": "incidentDeleteAllRequest",
  "payload": {
    "incident_type": "trash" | "spill" | "all"
  }
}
```

**Kit → Browser:**
```json
{
  "event_type": "incidentDeleteAllResponse",
  "payload": {
    "result": "success" | "none",
    "count": 5,
    "deleted_paths": ["/World/trash", "/World/trash_1", ...]
  }
}
```

---

## Spawn Zones

Incidents spawn at random positions within configured rectangles on the ground
plane.  This prevents items from appearing inside shelf geometry.

**Default zone** (covers the full store floor):
```
(-200, 200, -200, 200)   →   x ∈ [-200, 200], z ∈ [-200, 200]
```

### Configuring spawn zones

Edit `usd_config.json` (next to `usd_spawner.py`):

```json
{
  "incident_spawn_zones": [
    [-150, -20, -80, 80],
    [20, 150, -80, 80],
    [-50, 50, -150, -100]
  ]
}
```

Each entry is `[x_min, x_max, y_min, z_max]`.  The spawner picks a zone at
random, then picks a random (x, z) within it.  The Y coordinate is set to the
stage floor level automatically.

**Tip:** To find good coordinate ranges, move the camera to the store floor in
the Kit viewport, read the camera position from the Navigation panel, and note
the X/Z bounds of open aisle areas.

---

## Incident Asset Types

| Type    | Asset Key        | USD File             |
|---------|------------------|----------------------|
| Trash   | `trash`          | `Trash.usdz`         |
| Spill   | `spilled_coffee` | `Spilled_Coffee.usdz`|

Assets live in the directory specified by `usd_base` in `usd_config.json`
(default: `omniverse-vs-agent-backend/assets/usd/`).

### Adding new incident types

1. Place the `.usdz` file in the USD assets directory.
2. Add a key → filename mapping in `ASSET_LIBRARY` (under the `# Incidents`
   comment) in `usd_spawner.py`.
3. Add the type → key mapping in the `INCIDENT_ASSETS` dict.
4. Add an entry in `ASSET_CATALOG` in `Window.tsx` under the `"Incidents"`
   category.
5. Add rotation/scale corrections if needed (see below).
6. Add Spawn/Delete buttons in the incident panel UI in `Window.tsx`.

---

## File Reference

| File | Purpose |
|------|---------|
| `usd_spawner.py` | Kit-side spawn logic, event handlers, spawn zones |
| `usd_config.json` | Paths, spawn zone overrides |
| `rotation_corrections.json` | Per-asset Euler rotation corrections |
| `scale_corrections.json` | Per-asset scale corrections |
| `Window.tsx` | Browser UI: incident panel, automation timer |
| `omniverse-vs-agent-backend/assets/usd/` | USD asset files |
