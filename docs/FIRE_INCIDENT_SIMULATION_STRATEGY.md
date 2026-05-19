# Fire Incident Simulation Strategy

## Overview

This document describes the strategy for adding real-time fire incident simulation and alerting to the virtual shop using **Omniverse Flow** (`omni.flowusd`). The goal is to animate a fire effect at a location in the store, then surface an alert to connected browser users so they can react (evacuate, call services, etc.).

---

## Architecture Summary

```
Browser Client
    │
    │  WebSocket / Livestream Messaging
    ▼
Kit App (CustomMessageManager)
    │
    ├── UsdSpawner ──────► USD Stage (Flow prims: emitter, simulate, offscreen, render)
    ├── WaypointMarkers ──► Existing marker layer (anchor fire to a known location)
    └── FireIncidentManager (new) ──► Trigger / extinguish fire + broadcast alert
```

The fire effect lives entirely on the USD stage. The Kit extension controls its lifecycle and pushes alert events back to the browser via the existing `omni.kit.livestream.messaging` channel.

---

## Phase 1 — USD Stage Setup (Flow Prims)

### Required prims per fire incident

| Prim | USD Type | Purpose |
|------|----------|---------|
| `FireIncident_<id>/Xform` | `UsdGeom.Xform` | Root transform — move this to reposition fire |
| `FireIncident_<id>/flowEmitterSphere` | `Flow.EmitterSphere` | Particle emission source |
| `FireIncident_<id>/flowSimulate` | `Flow.Simulate` | Physics/simulation settings |
| `FireIncident_<id>/flowOffscreen` | `Flow.Offscreen` | Colormap + off-screen volume render |
| `FireIncident_<id>/flowRender` | `Flow.Render` | Composites volume into RTX frame |

All four prims must share the **same layer integer** to form one coherent simulation (see Flow Layers section in the extension docs).

### Preset approach (fastest path)

Use the built-in fire preset from `Window → Simulation → Presets`, then reference it programmatically:

```python
from pxr import Usd, UsdGeom, Sdf

stage = omni.usd.get_context().get_stage()
root_xform = UsdGeom.Xform.Define(stage, f"/World/FireIncident_{incident_id}")
# Add a reference to the Flow fire preset bundled with omni.flowusd
root_xform.GetPrim().GetReferences().AddReference(
    assetPath="omniverse://localhost/NVIDIA/Assets/Omniverse/Flow/Presets/Fire.usd"
)
```

> Alternatively, use `Copy` or `Global Copy` (available from the Presets window right-click menu) so the preset is embedded in the local layer rather than referenced — useful when you want runtime colormap edits without touching the asset library.

### Colormap (fire appearance)

For a realistic fire:
- Select `flowOffscreen/colormap` on the stage prim.
- Temperature values **above 1.0** are expected and required for proper bloom interaction.
- Enable **Bloom** in Post Processing (`Window → Rendering → Post Processing`) to make fire visually bright.

---

## Phase 2 — FireIncidentManager Extension Module

Create `fire_incident_manager.py` alongside the existing extension modules.

### Responsibilities

- **`trigger_fire(location, incident_id)`** — spawns Flow prims at `location` (world XYZ), starts timeline playback, broadcasts `fireAlert` message to all browser clients.
- **`extinguish_fire(incident_id)`** — removes Flow prims from stage, broadcasts `fireCleared` message.
- **`list_active_fires()`** — returns dict of active incidents for the browser HUD.

### Integration with existing UsdSpawner pattern

Follow the same pattern as `UsdSpawner`:

```python
# Message flow
# Browser → Kit:  "fireIncidentRequest"  { action: "trigger"|"extinguish", location_id, incident_id }
# Kit → Browser:  "fireIncidentResponse" { result: "ok"|"error", incident_id, position, message }
# Kit → Browser:  "fireAlert"            { incident_id, position, severity, timestamp }
```

### Anchoring to waypoints

Fire incidents can be anchored to existing waypoint markers (already on the `feature/waypoint-markers` branch). When `location_id` matches a known waypoint name, resolve the 3D position from the marker registry rather than requiring raw XYZ coordinates from the browser:

```python
marker = self._markers.get(location_id)
if marker:
    position = Gf.Vec3d(marker["x"], marker["y"], marker["z"])
```

---

## Phase 3 — Browser Alert UI

### New outbound message: `fireAlert`

```json
{
  "type": "fireAlert",
  "data": {
    "incident_id": "fire_001",
    "location_id": "aisle_3",
    "position": { "x": 120.0, "y": 0.0, "z": -45.0 },
    "severity": "high",
    "timestamp": 1716000000
  }
}
```

### Alert behaviour in the browser

1. **Banner notification** — red overlay banner at top of viewport: "FIRE DETECTED — Aisle 3. Please evacuate."
2. **Camera fly-to** — optionally trigger `navigateTo` (existing camera navigation message) to move the view to the incident location so the user can see the fire.
3. **Waypoint badge** — pulse/highlight the waypoint marker at the fire location in the 3D scene.
4. **Clear alert** — on `fireCleared` message, dismiss banner and remove visual badge.

---

## Phase 4 — Render Settings Checklist

These must be enabled for Flow fire to render correctly:

- [ ] `omni.flowusd` extension enabled (`Window → Extensions`)
- [ ] Flow enabled in **Common Render Settings** (auto-enabled when a preset is added)
- [ ] **Bloom** enabled in Post Processing (makes fire appear bright and realistic)
- [ ] Optionally: **Indirect Diffuse GI** enabled for fire to cast light on surrounding shelves

---

## Phase 5 — Testing Plan

| Test | Expected result |
|------|----------------|
| Trigger fire via browser message | Flow prims appear on stage at correct waypoint position, simulation plays |
| Fire renders in RTX viewport | Volumetric fire visible with bloom, correct colormap |
| `fireAlert` received in browser | Alert banner appears, camera navigates to fire location |
| Extinguish via browser message | Flow prims removed, banner dismissed |
| Trigger multiple fires simultaneously | Each incident uses a unique layer integer, no simulation bleed between incidents |
| Performance under 3 simultaneous fires | Frame rate remains acceptable; use Flow Monitor (`Window → Simulation → Monitor`) to check block usage |

---

## Implementation Order

```
1. fire_incident_manager.py   — core logic (spawn/remove Flow prims, manage layer IDs)
2. custom_messaging.py        — register "fireIncidentRequest" handler, wire up manager
3. Browser JS                 — handle "fireAlert" / "fireCleared" messages, show banner
4. Waypoint integration       — resolve location_id → 3D position via existing marker registry
5. Render settings defaults   — set bloom/GI in the app .kit config so fire looks correct on launch
```

---

## Key Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Flow layer collision between multiple incidents | Assign layer IDs from a monotonic counter per session |
| Timeline not playing (simulation frozen) | Auto-call `omni.timeline.get_timeline_interface().play()` on first fire trigger |
| Preset USD path varies by Omniverse install | Make preset path configurable in `usd_config.json`, same pattern as existing `_USD_BASE` |
| Performance with many fires | Cap concurrent incidents (e.g. max 5); use Flow Monitor to track block usage |
| Flow prims left on stage after app restart | Persist active incident list to a JSON sidecar (same pattern as `stage_prims.json`) |

---

## References

- Omniverse Flow extension docs: `omni.flowusd` — Getting Started, Presets, Layers, Colormap
- Existing asset spawning pattern: `source/extensions/*/usd_spawner.py`
- Existing messaging pattern: `source/extensions/*/custom_messaging.py`
- Waypoint marker system: `feature/waypoint-markers` branch, `custom_messaging.py` markers section
- Flow Monitor: `Window → Simulation → Monitor`
