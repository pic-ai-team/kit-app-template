"""
FireIncidentManager — programmatically creates Omniverse Flow fire effects on the USD stage.

Flow prim structure per incident (all four share the same layer integer):
  /World/FireIncidents/FireIncident_<id>/    ← Xform root, move this to reposition
    flowSimulate                              ← physics / simulation settings
    flowOffscreen                             ← colormap + off-screen volume render
    flowRender                                ← composites volume into RTX frame
    flowEmitterSphere                         ← particle emission source

NOTE: USD type names (FlowSimulate, FlowEmitterSphere, etc.) are registered by the
omni.flowusd extension. If the stage rejects them, verify the exact type strings
against your installed omni.flowusd version — they may use an "Omni" prefix.

Message flow:
  Browser → Kit:  "fireIncidentRequest"  { action: "trigger"|"extinguish"|"list",
                                           incident_id, location_id?, position? }
  Kit → Browser:  "fireIncidentResponse" { result: "ok"|"error", incident_id, position?, message }
  Kit → Browser:  "fireAlert"            { incident_id, location_id, position, severity, timestamp }
  Kit → Browser:  "fireCleared"          { incident_id }
"""

import asyncio
import time
from typing import Any, Dict, Optional, Tuple

import carb
import omni.usd
from carb.eventdispatcher import get_eventdispatcher
from pxr import Gf, Sdf, Usd, UsdGeom

# ---------------------------------------------------------------------------
# Stage constants
# ---------------------------------------------------------------------------
_FLOW_ROOT = "/World/FireIncidents"
_MAX_INCIDENTS = 5

# ---------------------------------------------------------------------------
# Default fire appearance / physics parameters — tune these to taste
# ---------------------------------------------------------------------------
_FIRE_CFG = {
    # Emitter sphere
    "emitter_radius":           15.0,   # world-space radius of the ember source
    "emitter_temperature":       2.0,   # ignition temperature (drives combustion)
    "emitter_fuel":              1.0,   # fuel level (combustion input)
    "emitter_smoke":             0.0,   # initial smoke (starts clean, builds naturally)
    "emitter_couple_temp":     120.0,   # instant coupling → strong burst on spawn
    "emitter_couple_fuel":     120.0,
    "emitter_couple_smoke":      8.0,   # slower smoke coupling for natural build-up
    "emitter_velocity_up":      80.0,   # upward lift speed (Y-up assumed)
    "emitter_couple_velocity":   8.0,
    "emitter_allocation_scale":  1.2,   # slight padding around emitter sphere
    # Simulate
    "cell_size":                 5.0,   # density cell size in world units (larger = faster, less detail)
    "steps_per_second":         60,
    "max_steps_per_frame":       2,
    # Render
    "render_attenuation":        0.5,
    "render_color_scale":        1.5,   # >1 brightens for bloom interaction
    # Colormap: temperature → RGBA (RGB can exceed 1.0 for HDR/bloom)
    # Five control points: x in [0..1] maps to temperature range
    "colormap_x_points":    [0.0,  0.25, 0.50, 0.75, 1.0],
    "colormap_rgba_points": [
        0.0,  0.0,  0.0,  0.0,   # 0.00 → fully transparent (no heat)
        0.5,  0.05, 0.0,  0.8,   # 0.25 → dark ember red
        1.5,  0.3,  0.0,  1.0,   # 0.50 → orange flame (HDR)
        4.0,  1.0,  0.3,  1.0,   # 0.75 → bright orange-yellow (HDR)
        8.0,  2.0,  1.5,  1.0,   # 1.00 → near-white hot core (HDR)
    ],
    "colormap_color_scale": 1.0,
}


class FireIncidentManager:
    """Manages Flow fire effects on the USD stage."""

    def __init__(self, markers: Dict[str, Dict[str, Any]]):
        # Shared reference to CustomMessageManager._markers (read-only)
        self._markers = markers
        # incident_id → { layer_id, prim_path, position, location_id, severity, timestamp }
        self._active: Dict[str, Dict[str, Any]] = {}
        # Layer IDs start at 100 to avoid collision with other Flow presets (which use low integers)
        self._next_layer = 100

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def trigger_fire(
        self,
        incident_id: str,
        position: Gf.Vec3d,
        location_id: str = "",
        severity: str = "high",
    ) -> Tuple[bool, str]:
        """
        Spawn a Flow fire effect at `position` on the USD stage.
        Returns (success, message_or_prim_path).
        """
        if incident_id in self._active:
            return False, f"Incident '{incident_id}' already active"
        if len(self._active) >= _MAX_INCIDENTS:
            return False, f"Max concurrent fires ({_MAX_INCIDENTS}) reached"

        stage = omni.usd.get_context().get_stage()
        if not stage:
            return False, "No USD stage available"

        layer_id = self._next_layer
        self._next_layer += 1
        root_path = f"{_FLOW_ROOT}/FireIncident_{incident_id}"

        self._enable_flow_prerequisites()

        try:
            self._create_flow_prims(stage, root_path, position, layer_id)
        except Exception as exc:
            carb.log_error(f"[FireIncidentManager] Failed to create Flow prims: {exc}")
            return False, str(exc)

        self._ensure_timeline_playing()

        record = {
            "layer_id": layer_id,
            "prim_path": root_path,
            "position": [position[0], position[1], position[2]],
            "location_id": location_id,
            "severity": severity,
            "timestamp": int(time.time()),
        }
        self._active[incident_id] = record

        carb.log_info(
            f"[FireIncidentManager] triggered: id={incident_id} pos={list(position)} layer={layer_id}"
        )

        get_eventdispatcher().dispatch_event(
            "fireAlert",
            payload={
                "incident_id": incident_id,
                "location_id": location_id,
                "position": {"x": position[0], "y": position[1], "z": position[2]},
                "severity": severity,
                "timestamp": record["timestamp"],
            },
        )

        return True, root_path

    def extinguish_fire(self, incident_id: str) -> Tuple[bool, str]:
        """Remove the Flow prims for `incident_id` from the stage."""
        if incident_id not in self._active:
            return False, f"Incident '{incident_id}' not found"

        record = self._active.pop(incident_id)
        stage = omni.usd.get_context().get_stage()
        if stage:
            prim = stage.GetPrimAtPath(record["prim_path"])
            if prim.IsValid():
                stage.RemovePrim(Sdf.Path(record["prim_path"]))
                carb.log_info(f"[FireIncidentManager] extinguished: id={incident_id}")

        get_eventdispatcher().dispatch_event(
            "fireCleared",
            payload={"incident_id": incident_id},
        )

        return True, f"Incident '{incident_id}' extinguished"

    def adjust_fire(
        self,
        incident_id: str,
        radius: Optional[float] = None,
        temperature: Optional[float] = None,
        fuel: Optional[float] = None,
        smoke: Optional[float] = None,
        velocity_up: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Live-tweak emitter parameters for an active fire incident."""
        if incident_id not in self._active:
            return False, f"Incident '{incident_id}' not found"

        stage = omni.usd.get_context().get_stage()
        if not stage:
            return False, "No USD stage"

        root_path = self._active[incident_id]["prim_path"]
        emit_path = f"{root_path}/flowEmitterSphere"
        sim_path  = f"{root_path}/flowSimulate"

        emit_prim = stage.GetPrimAtPath(emit_path)
        if not emit_prim.IsValid():
            carb.log_error(f"[FireIncidentManager] adjust: emitter not found at {emit_path}")
            return False, f"Emitter prim not found at {emit_path}"

        # Disable emitter so Flow sees a clean boundary on re-enable
        _attr(emit_prim, "enabled", False, Sdf.ValueTypeNames.Bool)

        if radius is not None:
            _attr(emit_prim, "radius", float(radius), Sdf.ValueTypeNames.Float)
        if temperature is not None:
            _attr(emit_prim, "temperature", float(temperature), Sdf.ValueTypeNames.Float)
        if fuel is not None:
            _attr(emit_prim, "fuel", float(fuel), Sdf.ValueTypeNames.Float)
        if smoke is not None:
            _attr(emit_prim, "smoke", float(smoke), Sdf.ValueTypeNames.Float)
        if velocity_up is not None:
            _attr(emit_prim, "velocity",
                  Gf.Vec3f(0.0, float(velocity_up), 0.0),
                  Sdf.ValueTypeNames.Float3)

        # Re-enable with new params
        _attr(emit_prim, "enabled", True, Sdf.ValueTypeNames.Bool)

        # Ensure timeline is playing — forceClear has no effect when paused
        self._ensure_timeline_playing()

        # forceClear kills existing in-flight particles so the visual change
        # is immediate rather than waiting for old particles to dissipate.
        # We set it True for one simulation frame, then reset it async.
        sim_prim = stage.GetPrimAtPath(sim_path)
        if sim_prim.IsValid():
            _attr(sim_prim, "forceClear", True, Sdf.ValueTypeNames.Bool)
            asyncio.ensure_future(self._reset_force_clear(sim_path))

        carb.log_info(
            f"[FireIncidentManager] adjusted {incident_id}: "
            f"radius={radius}, temp={temperature}, fuel={fuel}, "
            f"smoke={smoke}, vel_up={velocity_up}"
        )
        return True, "Parameters applied — fire refreshed"

    @staticmethod
    async def _reset_force_clear(sim_path: str) -> None:
        """Wait one Kit frame, then turn off forceClear so normal emission resumes."""
        try:
            import omni.kit.app
            await omni.kit.app.get_app().next_update_async()
        except Exception:
            pass  # fallback: tiny sleep if next_update_async unavailable
        stage = omni.usd.get_context().get_stage()
        if stage:
            prim = stage.GetPrimAtPath(sim_path)
            if prim.IsValid():
                _attr(prim, "forceClear", False, Sdf.ValueTypeNames.Bool)

    def list_active(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._active)

    def resolve_position(self, location_id: str) -> Optional[Gf.Vec3d]:
        """Resolve a waypoint marker name → 3D world position, or None if unknown."""
        marker = self._markers.get(location_id)
        if not marker:
            return None
        pos = marker.get("position", [0.0, 0.0, 0.0])
        return Gf.Vec3d(float(pos[0]), float(pos[1]), float(pos[2]))

    def on_shutdown(self) -> None:
        """Extinguish all active fires cleanly on extension shutdown."""
        for incident_id in list(self._active.keys()):
            self.extinguish_fire(incident_id)

    # ------------------------------------------------------------------
    # USD / Flow prim creation
    # ------------------------------------------------------------------

    def _create_flow_prims(
        self,
        stage: Usd.Stage,
        root_path: str,
        position: Gf.Vec3d,
        layer_id: int,
    ) -> None:
        cfg = _FIRE_CFG

        # Root Xform — translating this moves the whole fire
        root_prim = UsdGeom.Xform.Define(stage, root_path)
        UsdGeom.XformCommonAPI(root_prim.GetPrim()).SetTranslate(position)

        # ---- flowSimulate -----------------------------------------------
        sim = stage.DefinePrim(f"{root_path}/flowSimulate", "FlowSimulate")
        _attr(sim, "layer",               layer_id,               Sdf.ValueTypeNames.Int)
        _attr(sim, "densityCellSize",     cfg["cell_size"],        Sdf.ValueTypeNames.Float)
        _attr(sim, "stepsPerSecond",      cfg["steps_per_second"], Sdf.ValueTypeNames.Int)
        _attr(sim, "maxStepsPerSimulate", cfg["max_steps_per_frame"], Sdf.ValueTypeNames.Int)

        # ---- flowOffscreen -----------------------------------------------
        off = stage.DefinePrim(f"{root_path}/flowOffscreen", "FlowOffscreen")
        _attr(off, "layer", layer_id, Sdf.ValueTypeNames.Int)

        # Colormap lives as a child prim of flowOffscreen
        cmap = stage.DefinePrim(f"{root_path}/flowOffscreen/colormap", "FlowColormap")
        _attr(cmap, "xPoints",      cfg["colormap_x_points"],    Sdf.ValueTypeNames.FloatArray)
        _attr(cmap, "rgbaPoints",   cfg["colormap_rgba_points"],  Sdf.ValueTypeNames.FloatArray)
        _attr(cmap, "colorScale",   cfg["colormap_color_scale"],  Sdf.ValueTypeNames.Float)

        # ---- flowRender -------------------------------------------------
        ren = stage.DefinePrim(f"{root_path}/flowRender", "FlowRender")
        _attr(ren, "layer",                  layer_id,                   Sdf.ValueTypeNames.Int)
        _attr(ren, "rayMarch:attenuation",   cfg["render_attenuation"],  Sdf.ValueTypeNames.Float)
        _attr(ren, "rayMarch:colorScale",    cfg["render_color_scale"],  Sdf.ValueTypeNames.Float)

        # ---- flowEmitterSphere ------------------------------------------
        emit = stage.DefinePrim(f"{root_path}/flowEmitterSphere", "FlowEmitterSphere")
        _attr(emit, "layer",              layer_id,                          Sdf.ValueTypeNames.Int)
        _attr(emit, "radius",             cfg["emitter_radius"],             Sdf.ValueTypeNames.Float)
        _attr(emit, "radiusIsWorldSpace", True,                              Sdf.ValueTypeNames.Bool)
        # Combustion channels
        _attr(emit, "temperature", cfg["emitter_temperature"], Sdf.ValueTypeNames.Float)
        _attr(emit, "fuel",        cfg["emitter_fuel"],        Sdf.ValueTypeNames.Float)
        _attr(emit, "smoke",       cfg["emitter_smoke"],       Sdf.ValueTypeNames.Float)
        # Couple rates
        _attr(emit, "coupleRateTemperature", cfg["emitter_couple_temp"],     Sdf.ValueTypeNames.Float)
        _attr(emit, "coupleRateFuel",        cfg["emitter_couple_fuel"],     Sdf.ValueTypeNames.Float)
        _attr(emit, "coupleRateSmoke",       cfg["emitter_couple_smoke"],    Sdf.ValueTypeNames.Float)
        _attr(emit, "coupleRateVelocity",    cfg["emitter_couple_velocity"], Sdf.ValueTypeNames.Float)
        # Upward velocity lifts the flame (Y-up coordinate system)
        _attr(emit, "velocity",
              Gf.Vec3f(0.0, cfg["emitter_velocity_up"], 0.0),
              Sdf.ValueTypeNames.Float3)
        _attr(emit, "allocationScale", cfg["emitter_allocation_scale"], Sdf.ValueTypeNames.Float)

        # ---- Diagnostic — log prim type recognition -------------------------
        # If known=False, the type name is NOT registered by omni.flowusd;
        # Flow will ignore the prim. Check the Kit console after triggering fire.
        for label, prim in [("flowSimulate", sim), ("flowEmitterSphere", emit),
                             ("flowOffscreen", off), ("flowRender", ren)]:
            schema_type = prim.GetPrimTypeInfo().GetSchemaType()
            is_known    = bool(schema_type) and schema_type.IsValid()
            carb.log_info(
                f"[FireIncidentManager] {label}: "
                f"type='{prim.GetTypeName()}' known={is_known} "
                f"attrs={[a.GetName() for a in prim.GetAttributes()][:6]}"
            )

    @staticmethod
    def _enable_flow_prerequisites() -> None:
        """Enable omni.flowusd extension and RTX Flow rendering — required for any visible effect."""
        # 1. Enable the omni.flowusd extension
        try:
            import omni.kit.app
            mgr = omni.kit.app.get_app().get_extension_manager()
            if not mgr.is_extension_enabled("omni.flowusd"):
                mgr.set_extension_enabled_immediate("omni.flowusd", True)
                carb.log_info("[FireIncidentManager] Enabled omni.flowusd extension")
            else:
                carb.log_info("[FireIncidentManager] omni.flowusd already enabled")
        except Exception as exc:
            carb.log_warn(f"[FireIncidentManager] Could not enable omni.flowusd: {exc}")

        # 2. Enable Flow in the RTX renderer (equivalent to ticking the checkbox in
        #    Render Settings → Common → Flow)
        try:
            import carb.settings as _cs
            _cs.get_settings().set("/rtx/flow/enabled", True)
            carb.log_info("[FireIncidentManager] RTX Flow rendering enabled")
        except Exception as exc:
            carb.log_warn(f"[FireIncidentManager] Could not enable RTX Flow setting: {exc}")

    @staticmethod
    def _ensure_timeline_playing() -> None:
        try:
            from omni.timeline import get_timeline_interface
            tl = get_timeline_interface()
            if not tl.is_playing():
                tl.play()
                carb.log_info("[FireIncidentManager] Started timeline for Flow simulation")
            else:
                carb.log_info("[FireIncidentManager] Timeline already playing")
        except Exception as exc:
            carb.log_warn(f"[FireIncidentManager] Could not start timeline: {exc}")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _attr(prim: Usd.Prim, name: str, value: Any, type_name: Sdf.ValueTypeName) -> None:
    """Set a USD attribute, creating it first if it does not yet exist."""
    attr = prim.GetAttribute(name)
    if not attr.IsValid():
        attr = prim.CreateAttribute(name, type_name)
    attr.Set(value)
