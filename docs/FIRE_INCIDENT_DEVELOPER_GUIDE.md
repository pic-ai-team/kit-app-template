# Fire Incident Simulation — Developer Guide

This guide explains how the fire incident feature works, how it is wired together, and what a new developer needs to know to extend or debug it.

---

## Overview

The fire feature lets a browser user place a volumetric fire effect anywhere on the virtual store floor by clicking a button in the UI. The fire is simulated using **Omniverse Flow** (`omni.flowusd`) and renders as a real particle-based flame in the RTX viewport. Users can adjust flame parameters live and extinguish fires from a management panel in the browser.

```
Browser (React UI)
    │  WebSocket / Livestream Messaging
    ▼
Kit Extension  (custom_messaging.py)
    │
    └──► FireIncidentManager  (fire_incident_manager.py)
              │
              ├── Creates Flow USD prims on the stage
              ├── Adjusts emitter attributes live
              └── Removes prims on extinguish
```

---

## Files Involved

| File | Location | Purpose |
|------|----------|---------|
| `fire_incident_manager.py` | `source/extensions/markertest.*/` | Core logic: create, adjust, extinguish Flow prims |
| `custom_messaging.py` | same extension | Receives browser messages, calls FireIncidentManager |
| `Window.tsx` | `omniverse-webviewer/src/` | Fire mode overlay, alert panel, adjustment sliders |
| `App.css` | `omniverse-webviewer/src/` | Fire badge glow + slide-in animations |

The same files exist in the **extension template** at:
```
templates/extensions/usd_viewer.messaging/template/{{python_module_path}}/
```
So any new extension generated from that template already includes the fire feature.

---

## How It Works — Step by Step

### 1. User clicks the 🔥 button in the browser

The fire mode overlay activates. The cursor changes to a crosshair. A red banner prompts: _"Click on the floor to place a fire incident"_.

### 2. User clicks on the viewport floor

The browser captures the normalized screen coordinate `(screen_x, screen_y)` in `[0, 1]` range and sends:

```json
{
  "event_type": "fireIncidentRequest",
  "payload": {
    "action": "trigger",
    "incident_id": "fire_1716000000000",
    "screen_x": 0.51,
    "screen_y": 0.72,
    "severity": "high"
  }
}
```

### 3. Kit converts screen coords → 3D world position

`custom_messaging.py → _on_fire_incident_request()` receives the message. Since `screen_x/screen_y` are provided, it calls:

```python
position = self._usd_spawner._compute_world_position(screen_x, screen_y)
```

This casts a camera frustum ray from the screen point and intersects it with the floor plane (Y=0 for Y-up scenes, Z=floor_level for Z-up). This is the same ray-cast used by click-to-spawn assets.

### 4. FireIncidentManager creates Flow USD prims

`trigger_fire(incident_id, position, ...)` is called. It:

1. Calls `_enable_flow_prerequisites()` — enables the `omni.flowusd` extension and sets `/rtx/flow/enabled = True` in RTX settings
2. Creates this prim hierarchy on the USD stage:

```
/World/FireIncidents/
  FireIncident_fire_1716000000000/     ← Xform at world position
    flowSimulate                        ← simulation physics settings
    flowOffscreen/                      ← colormap (temp → RGBA)
      colormap
    flowRender                          ← composites volume into RTX frame
    flowEmitterSphere                   ← particle emission source
```

3. All four prims share the **same integer layer ID** — this is how Flow knows they belong to one simulation. Layer IDs start at 100 and increment per incident to avoid collisions with other Flow presets.
4. Calls `_ensure_timeline_playing()` — starts the Kit timeline if it is paused.

### 5. Kit broadcasts `fireAlert` to the browser

```json
{
  "event_type": "fireAlert",
  "payload": {
    "incident_id": "fire_1716000000000",
    "location_id": "",
    "position": { "x": -2.8, "y": 204.9, "z": 1.8 },
    "severity": "high",
    "timestamp": 1716000000
  }
}
```

The browser adds the incident to `activeFireIncidents` state and opens the fire alert panel.

### 6. Fire alert panel (browser)

A compact panel appears at the **bottom-left** of the viewport (out of the way of the fire). It shows:
- Incident label, timestamp, severity
- **⚙ Adjust** — expands flame parameter sliders
- **✕ Extinguish** — removes the fire
- **▶ Play Timeline** / **⏹ Stop** — controls Kit's timeline
- Simulation prerequisites checklist

When minimised via `−`, a pulsing red badge replaces the panel at the same bottom-left position. Clicking it reopens the panel.

---

## Flame Parameters (Adjustable Live)

| Parameter | USD Attribute | Default | Effect |
|-----------|--------------|---------|--------|
| Radius | `radius` on `flowEmitterSphere` | 15 | Size of the ember source sphere |
| Temperature | `temperature` | 2.0 | Drives combustion intensity |
| Fuel | `fuel` | 1.0 | Combustion input; 0 = no flame |
| Smoke | `smoke` | 0.0 | Smoke density above the flame |
| Lift speed | `velocity` (Y component) | 80 | How fast flame rises |

When the user clicks **Apply Changes**, the browser sends:

```json
{
  "event_type": "fireAdjustRequest",
  "payload": {
    "incident_id": "fire_1716000000000",
    "radius": 29,
    "temperature": 2.9,
    "fuel": 1.05,
    "smoke": 0.35,
    "velocity_up": 80
  }
}
```

Kit's `_on_fire_adjust_request()` calls `FireIncidentManager.adjust_fire()`, which:

1. **Disables** the emitter (`enabled = False`) — stops emission momentarily
2. Writes the new attribute values to the USD prim
3. **Re-enables** the emitter (`enabled = True`) — Flow reinitialises from current attributes
4. Sets `forceClear = True` on `flowSimulate` — kills all existing in-flight particles so the change is **visually immediate** rather than gradual
5. Schedules `forceClear = False` on the **next Kit frame** via `asyncio.ensure_future(_reset_force_clear(...))` so normal emission resumes

> **Why the enable/forceClear dance?**
> Flow reads emitter attributes when particles are emitted, not every frame. Without disabling the emitter first, existing cached particles continue with the old parameters until they dissipate. The `forceClear` wipes the particle pool instantly.

---

## Message Reference

### Browser → Kit

| `event_type` | Key payload fields | Description |
|---|---|---|
| `fireIncidentRequest` | `action: "trigger"`, `incident_id`, `screen_x`, `screen_y` | Place a fire at screen click position |
| `fireIncidentRequest` | `action: "trigger"`, `incident_id`, `location_id` | Place fire at a named waypoint marker |
| `fireIncidentRequest` | `action: "trigger"`, `incident_id`, `position: {x,y,z}` | Place fire at explicit world coordinates |
| `fireIncidentRequest` | `action: "extinguish"`, `incident_id` | Remove a fire |
| `fireIncidentRequest` | `action: "list"` | Get all active incidents |
| `fireAdjustRequest` | `incident_id`, `radius`, `temperature`, `fuel`, `smoke`, `velocity_up` | Live-adjust flame parameters |

### Kit → Browser

| `event_type` | Key payload fields | Description |
|---|---|---|
| `fireAlert` | `incident_id`, `position`, `location_id`, `severity`, `timestamp` | Fire placed successfully — show alert panel |
| `fireCleared` | `incident_id` | Fire extinguished — dismiss UI |
| `fireIncidentResponse` | `result`, `incident_id`, `message`, `position` | Direct reply to trigger/extinguish/list |
| `fireAdjustResponse` | `result`, `incident_id`, `message` | Confirm param adjustment succeeded or show error |

---

## Position Resolution Priority

When `action: "trigger"` is received, Kit resolves the world position in this order:

```
1. location_id matches a saved waypoint marker  →  use marker's 3D position
2. raw position { x, y, z } provided            →  use directly
3. screen_x + screen_y provided                 →  camera ray-cast to floor plane
```

This means you can also trigger fires programmatically from the agent backend by passing a `location_id` that matches a registered nav position (e.g. `"aisle_3"`).

---

## Simulation Prerequisites

For the volumetric flame to actually render (not just the browser emoji overlay):

| Requirement | How it's handled |
|---|---|
| `omni.flowusd` extension enabled | Auto-enabled by `_enable_flow_prerequisites()` on first fire trigger |
| Flow enabled in RTX renderer (`/rtx/flow/enabled`) | Auto-set by same method |
| Timeline playing | Auto-started by `_ensure_timeline_playing()` |
| Flow prim types recognised by Kit | Check Kit console for `known=True` on `[FireIncidentManager]` lines |

### Diagnosing prim type issues

After placing a fire, look in the **Kit console** for lines like:

```
[FireIncidentManager] flowEmitterSphere: type='FlowEmitterSphere' known=True attrs=[...]
```

- `known=True` → the type is registered by `omni.flowusd` — simulation will run
- `known=False` → the type name is not registered in your installed version; Flow will ignore the prim

If you see `known=False`, open the Presets window (`Window → Simulation → Presets`), drag a Fire preset onto the stage, then inspect the prim type names in the Stage panel. Update `_create_flow_prims()` in `fire_incident_manager.py` to match those exact type strings.

---

## Max Concurrent Fires

`_MAX_INCIDENTS = 5` in `fire_incident_manager.py`. Attempting to place a 6th fire returns an error in `fireIncidentResponse`. Layer IDs start at 100 and increment per session so they never collide with manually-created Flow presets (which use low integers).

---

## Adding a New Extension from the Template

When a colleague runs:

```bash
./repo.sh new_extension
# or
repo.bat new_extension
```

and selects the `usd_viewer.messaging` template, they get `fire_incident_manager.py`, the updated `custom_messaging.py`, and an empty `markers.json` automatically. They only need to:

1. Edit `usd_config.json` to point at their USD asset directory
2. Register waypoints for their store via the 🧭 nav panel — these populate `markers.json` and can then be used as named fire locations
3. Click 🔥 in the browser, enable Flow when prompted, and place fires

No additional code changes are required.

---

## Key Source Locations

```
kit-app-template-james/
├── source/extensions/markertest.my_usd_viewer_messaging_extension/
│   └── markertest/my_usd_viewer_messaging_extension/
│       ├── fire_incident_manager.py    ← FireIncidentManager class
│       └── custom_messaging.py         ← _on_fire_incident_request, _on_fire_adjust_request
├── templates/extensions/usd_viewer.messaging/
│   └── template/{{python_module_path}}/
│       ├── fire_incident_manager.py    ← synced copy (template for new extensions)
│       ├── custom_messaging.py         ← synced copy
│       └── markers.json                ← empty {} (store-specific, not committed)
└── docs/
    ├── FIRE_INCIDENT_DEVELOPER_GUIDE.md   ← this file
    └── FIRE_INCIDENT_SIMULATION_STRATEGY.md  ← original design strategy

omniverse-webviewer/
└── src/
    ├── Window.tsx   ← fireMode, activeFireIncidents, fireAdjustParams state + all fire UI
    └── App.css      ← fire-glow, fire-slide-in keyframe animations
```
