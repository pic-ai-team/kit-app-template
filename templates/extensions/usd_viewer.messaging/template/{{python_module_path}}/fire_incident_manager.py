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

Message flow (routed via custom_messaging.py):
  Browser → Kit:  "fireIncidentRequest"  { action: "trigger"|"extinguish"|"list",
                                           incident_id, screen_x/y, position?, severity? }
  Kit → Browser:  "fireIncidentResponse" { result: "ok"|"error", incident_id, position?, message }
  Kit → Browser:  "fireAlert"            { incident_id, position, severity, timestamp }
  Kit → Browser:  "fireCleared"          { incident_id }

  Browser → Kit:  "fireAdjustRequest"    { incident_id, radius?, temperature?, fuel?, smoke?, velocity_up? }
  Kit → Browser:  "fireAdjustResponse"   { result, incident_id, message }
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
    # Emitter sphere — these are the primary parameters for fire appearance
    "emitter_radius":            15.0,
    "emitter_temperature":       8.0,    # High temp for bright fire colors
    "emitter_fuel":              3.0,    # Enough fuel for sustained combustion
    "emitter_smoke":             0.0,    # No direct smoke injection (combustion generates it)
    "emitter_couple_temp":       5.0,    # Fast heat injection into grid
    "emitter_couple_fuel":       2.0,    # Fast fuel injection
    "emitter_couple_smoke":      8.0,
    "emitter_velocity_up":       80.0,
    "emitter_couple_velocity":   8.0,
    "emitter_allocation_scale":  1.5,
    # Simulate — minimal overrides; trust schema defaults for advection/combustion
    "cell_size":                 10.0,    # Smaller cells = more detail
}


class FireIncidentManager:
    """Manages Flow fire effects on the USD stage."""

    def __init__(self):
        # incident_id → { layer_id, prim_path, position, severity, timestamp }
        self._active: Dict[str, Dict[str, Any]] = {}
        # Layer IDs start at 100 to avoid collision with other Flow presets
        self._next_layer = 100

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def trigger_fire(
        self,
        incident_id: str,
        position: Gf.Vec3d,
        severity: str = "high",
        fire_params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
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
            self._create_flow_prims(stage, root_path, position, layer_id, overrides=fire_params)
        except Exception as exc:
            carb.log_error(f"[FireIncidentManager] Failed to create Flow prims: {exc}")
            return False, str(exc)

        self._ensure_timeline_playing()

        record = {
            "layer_id": layer_id,
            "prim_path": root_path,
            "position": [position[0], position[1], position[2]],
            "severity": severity,
            "timestamp": int(time.time()),
        }
        self._active[incident_id] = record

        carb.log_info(
            f"[FireIncidentManager] triggered: id={incident_id} pos={list(position)} layer={layer_id}"
        )

        # Broadcast alert
        get_eventdispatcher().dispatch_event(
            "fireAlert",
            payload={
                "incident_id": incident_id,
                "position": {"x": position[0], "y": position[1], "z": position[2]},
                "severity": severity,
                "timestamp": record["timestamp"],
            },
        )
        return True, root_path

    def extinguish_fire(self, incident_id: str) -> Tuple[bool, str]:
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
        if incident_id not in self._active:
            return False, f"Incident '{incident_id}' not found"

        stage = omni.usd.get_context().get_stage()
        if not stage:
            return False, "No USD stage"

        root_path = self._active[incident_id]["prim_path"]
        emit_path = f"{root_path}/flowEmitterSphere"
        sim_path = f"{root_path}/flowSimulate"

        emit_prim = stage.GetPrimAtPath(emit_path)
        if not emit_prim.IsValid():
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

        self._ensure_timeline_playing()

        # forceClear kills existing in-flight particles for immediate visual change
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
        try:
            import omni.kit.app
            await omni.kit.app.get_app().next_update_async()
        except Exception:
            pass
        stage = omni.usd.get_context().get_stage()
        if stage:
            prim = stage.GetPrimAtPath(sim_path)
            if prim.IsValid():
                _attr(prim, "forceClear", False, Sdf.ValueTypeNames.Bool)

    def extinguish_all(self) -> Tuple[bool, str]:
        """Extinguish all active fire incidents."""
        ids = list(self._active.keys())
        for incident_id in ids:
            self.extinguish_fire(incident_id)
        return True, f"Extinguished {len(ids)} fire(s)"

    def list_active(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._active)

    def on_shutdown(self) -> None:
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
        overrides: Optional[Dict[str, Any]] = None,
    ) -> None:
        cfg = dict(_FIRE_CFG)
        # Apply user overrides from the UI sliders
        if overrides:
            _PARAM_MAP = {
                "radius": "emitter_radius",
                "temperature": "emitter_temperature",
                "fuel": "emitter_fuel",
                "smoke": "emitter_smoke",
                "velocity_up": "emitter_velocity_up",
            }
            for ui_key, cfg_key in _PARAM_MAP.items():
                if ui_key in overrides:
                    cfg[cfg_key] = float(overrides[ui_key])

        # Root Xform
        root_prim = UsdGeom.Xform.Define(stage, root_path)
        UsdGeom.XformCommonAPI(root_prim.GetPrim()).SetTranslate(position)

        # ── FlowSimulate ─────────────────────────────────────────────────────
        # Only set layer + cellSize + forceSimulate. Leave stepsPerSecond,
        # maxStepsPerSimulate, and all advection/combustion settings at their
        # SCHEMA DEFAULTS. The Flow plugin ignores custom attributes we might
        # create with wrong types — the schema defaults already enable combustion
        # and produce correct fire behavior.
        sim = stage.DefinePrim(f"{root_path}/flowSimulate", "FlowSimulate")
        _attr(sim, "layer",           layer_id,         Sdf.ValueTypeNames.Int)
        _attr(sim, "densityCellSize", cfg["cell_size"], Sdf.ValueTypeNames.Float)
        _attr(sim, "forceSimulate",   True,             Sdf.ValueTypeNames.Bool)

        # ── FlowOffscreen ────────────────────────────────────────────────────
        # Trust the default colormap. Explicit colormap child prims with wrong
        # attribute types/names can override defaults with garbage.
        off = stage.DefinePrim(f"{root_path}/flowOffscreen", "FlowOffscreen")
        _attr(off, "layer", layer_id, Sdf.ValueTypeNames.Int)

        # ── FlowRender ───────────────────────────────────────────────────────
        ren = stage.DefinePrim(f"{root_path}/flowRender", "FlowRender")
        _attr(ren, "layer", layer_id, Sdf.ValueTypeNames.Int)

        # ── FlowEmitterSphere ────────────────────────────────────────────────
        # The emitter is what we KNOW works — all these attributes are on the
        # emitter schema and our helper handles type coercion.
        emit = stage.DefinePrim(f"{root_path}/flowEmitterSphere", "FlowEmitterSphere")
        _attr(emit, "layer",              layer_id,                          Sdf.ValueTypeNames.Int)
        _attr(emit, "radius",             cfg["emitter_radius"],             Sdf.ValueTypeNames.Float)
        _attr(emit, "radiusIsWorldSpace", True,                              Sdf.ValueTypeNames.Bool)
        _attr(emit, "temperature",        cfg["emitter_temperature"],        Sdf.ValueTypeNames.Float)
        _attr(emit, "fuel",               cfg["emitter_fuel"],               Sdf.ValueTypeNames.Float)
        _attr(emit, "smoke",              cfg["emitter_smoke"],              Sdf.ValueTypeNames.Float)
        _attr(emit, "coupleRateTemperature", cfg["emitter_couple_temp"],     Sdf.ValueTypeNames.Float)
        _attr(emit, "coupleRateFuel",        cfg["emitter_couple_fuel"],     Sdf.ValueTypeNames.Float)
        _attr(emit, "coupleRateSmoke",       cfg["emitter_couple_smoke"],    Sdf.ValueTypeNames.Float)
        _attr(emit, "coupleRateVelocity",    cfg["emitter_couple_velocity"], Sdf.ValueTypeNames.Float)
        _attr(emit, "velocity",
              Gf.Vec3f(0.0, cfg["emitter_velocity_up"], 0.0),
              Sdf.ValueTypeNames.Float3)
        _attr(emit, "allocationScale", cfg["emitter_allocation_scale"], Sdf.ValueTypeNames.Float)

        # Diagnostic — log schema recognition and attribute types
        for label, prim in [("flowSimulate", sim), ("flowEmitterSphere", emit),
                             ("flowOffscreen", off), ("flowRender", ren)]:
            schema_type = prim.GetPrimTypeInfo().GetSchemaType()
            is_known = bool(schema_type) and schema_type.IsValid()
            attrs = prim.GetAttributes()
            attr_info = [(a.GetName(), str(a.GetTypeName())) for a in attrs[:8]]
            carb.log_info(
                f"[FireIncidentManager] {label}: "
                f"type='{prim.GetTypeName()}' schema_known={is_known} "
                f"attrs={attr_info}"
            )

    @staticmethod
    def _enable_flow_prerequisites() -> None:
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
        except Exception as exc:
            carb.log_warn(f"[FireIncidentManager] Could not start timeline: {exc}")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _attr(prim: Usd.Prim, name: str, value: Any, type_name: Sdf.ValueTypeName) -> None:
    attr = prim.GetAttribute(name)
    if not attr.IsValid():
        attr = prim.CreateAttribute(name, type_name)
    attr.Set(value)
