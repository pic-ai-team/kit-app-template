"""
UsdSpawner — handles click-to-spawn USD assets from the browser chatbot.

Message flow:
  Browser sends  → "spawnUsdRequest"  { screen_x, screen_y, usd_path, prim_name }
  Kit replies    → "spawnUsdResponse" { result, prim_path, position, error }

Screen coordinates are normalized [0..1], origin at top-left of the viewport.
A camera frustum ray is cast and intersected with the Y=0 ground plane to
obtain the 3D world position where the asset will be placed.
"""

import json
import os
import random
import re
import time

import carb
import carb.events
import omni.kit.app
import omni.kit.livestream.messaging as messaging
import omni.usd
from carb.eventdispatcher import get_eventdispatcher
from pxr import Gf, Sdf, Usd, UsdGeom


# ---------------------------------------------------------------------------
# Hardcoded asset library — keys are canonical names the agent can reference.
# Paths should be absolute, relative to the Kit app root, or Omniverse URLs.
# The agent backend resolves natural-language names ("recycle bin") to a key
# in this dict and sends it via metadata.usd_path.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Config — load paths from usd_config.json (sits next to this file).
# Edit usd_config.json to point at the correct USD directory for your machine.
# ---------------------------------------------------------------------------
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "usd_config.json")
try:
    with open(_CONFIG_PATH) as _f:
        _cfg = json.load(_f)
except Exception as _cfg_err:
    import carb as _carb_early
    _carb_early.log_warn(f"[UsdSpawner] Could not load usd_config.json: {_cfg_err} — using built-in defaults")
    _cfg = {}

_USD_BASE         = _cfg.get("usd_base",         "/home/aicenter/adrian/local-omniverse/omniverse-vs-agent-backend/assets/usd")
_USD_ASSETS       = _cfg.get("usd_assets",       "/home/aicenter/adrian/local-omniverse/omniverse-vs-agent-backend/assets/usd")
_INVENTORY_FILE   = _cfg.get("inventory_file",   "/home/aicenter/adrian/local-omniverse/omniverse-vs-agent-backend/assets/asset_list_shop_already.json")
_STAGE_PRIMS_FILE = _cfg.get("stage_prims_file", "/home/aicenter/adrian/local-omniverse/omniverse-vs-agent-backend/assets/stage_prims.json")
_shelf_rowS_FILE  = _cfg.get("shelf_rows_file",  "/home/aicenter/adrian/local-omniverse/omniverse-vs-agent-backend/assets/shelf_rows.json")

# ---------------------------------------------------------------------------
# Incident spawn zones — rectangles on the ground plane (floor level) where
# incident objects (trash, spills, fire) can be randomly placed.
# Each zone is (x_min, x_max, z_min, z_max) for Y-up stages.
# These should be aisle / open-floor areas, NOT inside shelves.
# Edit or extend this list to match your store layout.
# ---------------------------------------------------------------------------
_INCIDENT_SPAWN_ZONES: list[tuple[float, float, float, float]] = _cfg.get("incident_spawn_zones",
[
    [-80, -20, -200, 1000], # Main aisle
    [-620, -510, -180, 1400] # Drinks aisle
])

carb.log_info(f"[UsdSpawner] Using incident spawn zones: {_INCIDENT_SPAWN_ZONES}")

# Incident asset keys — maps incident type name → asset key in ASSET_LIBRARY
INCIDENT_ASSETS: dict[str, str] = {
    "trash":  "trash",
    "spill":  "spilled_coffee",
}

ASSET_LIBRARY: dict[str, str] = {
    **{k: f"{_USD_BASE}/{f}" for k, f in [
        # Incidents
        ("spilled_coffee",           "Spilled_Coffee.usdz"),
        ("trash",                    "Trash.usdz"),
        # Tea & Milk Tea
        ("lipton_milktea",           "Lipton_Milktea.usdz"),
        ("lipton_iced_tea",          "Lipton_Fruit_Iced_Tea.usdz"),
        ("nittotea_milktea",         "NittoTea_Royal_Milktea.usdz"),
        ("freshdel_strawberry_tea",  "FreshDelight_Strawberry_MilkTea.usdz"),
        ("freshdel_coco_tea",        "FreshDelight_Coco_MilkTea.usdz"),
        ("dezheng_oolong_tea",       "De_Zheng_Roasted_Oolong_Milk_Tea.usdz"),
        ("afternoon_cream_milktea",  "AfternoonTeaTime_Heavy_Cream_Milktea.usdz"),
        ("afternoon_jasmine_tea",    "AfternoonTeaTime_Heavy_Cream_Jasmine_Milktea.usdz"),
        ("real_leaf_green_tea",      "Real_Leaf_Green_Tea_Gyokuro.usdz"),
        ("real_leaf_green_tea_pet",  "Real_Leaf_Green_Tea_Gyokuro_PET580.usdz"),
        ("chai_li_won_green_tea",    "Chai_Li_Won_Taiwanese_Green_Tea_PET975.usdz"),
        ("maixiang_black_tea",       "MaiXiang_Black_Tea_TP375.usdz"),
        ("yuancui_black_tea",        "yuancui_black_tea.usdz"),
        ("jasmine_guava_green_tea",  "JasmineTeaGarden_Guava_Lemon_Greentea.usdz"),
        ("jasmine_apple_black_tea",  "JasmineTeaGarden_Apple_Black_tea.usdz"),
        ("honey_apple_orange_tea",   "Honey_Apple_Orange_GreyTea.usdz"),
        ("green_drink",              "green_drink.usdz"),
        # Soy Milk & Oat
        ("uni_soymilk",              "Uni_Sunshine_SoyMilk.usdz"),
        ("uni_brownrice_milk",       "Uni_Sunshine_BrownRiceMilk.usdz"),
        ("kuangchuan_black_soymilk", "KuangChuan_Sugarfree_BlackSoyMilk.usdz"),
        ("kuangchuan_soymilk",       "KuangChuan_Milk_SoyMilk.usdz"),
        ("fuhang_soymilk",           "FuHang_SoyMilk_Unsweetened.usdz"),
        ("kuangchuan_soymilk_357",   "Kuang_Chuan_Unsweetened_SoyMilk_357ml.usdz"),
        ("quaker_soymilk_oat",       "Quaker_SoyMilkOatdrink.usdz"),
        ("agv_milk_oat",             "A_G_V_MilkOatdrink.usdz"),
        ("agv_honey_oat",            "A_G_V_HoneyOatdrink.usdz"),
        ("agv_milk_peanut",          "AGV_Milk_Peanut_Can340.usdz"),
        ("g_nut_milk",               "g_nut_milk.usdz"),
        # Juice & Yogurt
        ("jinjin_asparagus_juice",   "JinJin_Asparagus_Juice.usdz"),
        ("guava_mixed_juice",        "Fresh_Picked_Orchard_Guava_Mixed_Juice.usdz"),
        ("pomi_fruit_veg_juice",     "Pomi_Oneday_Fruit_Vegetable_Juice.usdz"),
        ("ab_strawberry_yogurt",     "ab_strawberry_yogurt.usdz"),
        # Sports & Water
        ("pocari_sweat",             "A_Pocari_Sweat_PET580.usdz"),
        ("supersup_sports_drink",    "A_SuperSup_Sports_Drink_PET590.usdz"),
        ("heysong_fin_drink",        "A_HeySong_FIN_Supply_Drink_PET580.usdz"),
        ("staycool_charcoal_water",  "StayCool_Alkaline_Bamboo_Charcoal_Water_PET700.usdz"),
        ("uni_h2o_water",            "UNI_H2O_Pure_Water_PET600.usdz"),
        ("ufc_coconut_water",        "UFC_Refresh_CoconutWater.usdz"),
        # Snacks (primary)
        ("pringles_cheese",          "Pringles_Cheese_Flavor_Potato_Chips.usdz"),
        ("pringles_bbq",             "Pringles_Charcoal_BBQ_Flavor_Large.usdz"),
        ("pringles_pizza",           "Pringles_Pizza_Flavor_Large.usdz"),
        ("pringles_lobster",         "Pringles_Spicy_Stir-Fried_Garlic_Lobster_Flavor_Potato_Chips.usdz"),
        ("cheetos_double_cheese",    "Cheetos_Double_Cheese_Flavor_Corn_Stick.usdz"),
        ("guai_guai_coconut",        "Guai_Guai_Coconut_Flavor_Large.usdz"),
        # Noodles
        ("uni_minced_meat_noodle",   "Uni_Noodles_Minced_Meat_Flavor_Bowl.usdz"),
        ("uni_braised_beef_noodle",  "Uni_Noodles_Green_Onion_Braised_Beef_Flavor_Bowl.usdz"),
        ("manhan_beef_noodles",      "A_ManHan_Green_Onion_Braised_Beef_Noodles.usdz"),
        ("grab_beef_veg_noodle",     "Grab_Cup_Noodles_Beef_and_Vegetable_Flavor.usdz"),
        ("grab_pork_spinach_noodle", "Grab_Cup_Noodles_Minced_Pork_and_Spinach_Flavor.usdz"),
        ("ramen_do_miso",            "Ramen_Do_Japanese_Miso_Flavor.usdz"),
        ("ramen_do_tonkatsu",        "Ramen_Do_Japanese_Tonkatsu_Flavor.usdz"),
        ("ahq_red_pepper_noodle",    "A_Ah_Q_Cup_Noodles_Red_Pepper_Beef_Flavor.usdz"),
        ("ahq_seafood_noodle",       "Ah_Q_Cup_Noodles_Fresh_Seafood_Flavor.usdz"),
        ("double_bang_satay_noodle", "Double_Bang_Satay_Hotpot_Soup_Noodles.usdz"),
        ("weili_soybean_noodle",     "A_Wei_Li_Fried_Soybean_Paste_Noodles_Bowl.usdz"),
        ("yidu_beef_noodle",         "Yidu_Zan_Aged_Jar_Beef_Noodles.usdz"),
        # Food
        ("royal_deli_braised_egg",   "Royal_Deli_Shian_Farm_Fragrant_Braised_Egg_White_Diced.usdz"),
        ("taiwanese_braised_veg",    "Taiwanese_Braised_Dish_Seasonal_Vegetables.usdz"),
        ("manhan_garlic_sausages",   "ManHan_Mini_Sausages_Garlic_Flavor.usdz"),
        # Props (primary)
        ("recycle_bin",              "recycle-bin.usdz"),
        ("shelf",                    "shelf.usdz"),
    ]},
    **{k: f"{_USD_ASSETS}/{f}" for k, f in [
        # Snacks (USD-Assets)
        ("lays_bags",                "Lays_Bags.usdz"),
        ("lays_cheddar",             "Lays_Cheddar.usdz"),
        ("lays_chile_limon",         "Lays_Chile_Limon.usdz"),
        ("lays_classic",             "Lays_Classic.usdz"),
        ("lays_dill_pickle",         "Lays_Dill_Pickle.usdz"),
        ("lays_flaming_hot",         "Lays_Flaming_Hot.usdz"),
        ("lays_sweet_bbq",           "Lays_Sweet_BBQ.usdz"),
        ("doritos_blue_cool_ranch",  "Doritos_blue_cool_ranch.usdz"),
        ("doritos_nacho",            "Doritos_Nacho.usdz"),
        ("doritos_flaming_hot",      "Doritos_Flamin_Hot_Nacho.usdz"),
        ("doritos_blazin_buffalo_ranch", "Doritos_Blazin_Buffalo_Ranch.usdz"),
        ("doritos_bags",             "Doritos_bags.usdz"),
        ("doritos_bbq",              "Doritos_bbq.usdz"),
        ("doritos_nacho_cheese",     "Doritos_Nacho_Cheese.usdz"),
        ("cheetos_flaming_hot",      "Cheetos_Flaming_Hot.usdz"),
        ("cheetos_puffs",            "Cheetos_Puffs.usdz"),
        ("cheetos_cheddar_jalapeno", "Cheetos_Cheddar_Jalapeno.usdz"),
        ("calbee_chips",             "Calbee_Chips.usdz"),
        ("calbee_hot_spicy_potato_chips", "Calbee_Hot__Spicy_Potato_Chips.usdz"),
        ("calbee_potato_chips_pizza", "Calbee_Potato_Chips_Pizza.usdz"),

        # Props (USD-Assets)
        ("wine_bottles",             "Wine_bottles.usdz"),
        ("liquor_bottles",           "Liquor_bottles.usdz"),
    ]},
}


# ---------------------------------------------------------------------------
# Inventory file — persists spawned prims across extension reloads/restarts.
# Format: { "/World/prim_path": { "asset_key": "...", "asset_name": "...",
#                                 "usd_path": "...", "position": [x,y,z] } }
# ---------------------------------------------------------------------------
INVENTORY_FILE   = _INVENTORY_FILE
STAGE_PRIMS_FILE = _STAGE_PRIMS_FILE

# Brand groups: brand prefix → list of asset keys with that prefix.
# e.g. "pringles" → ["pringles_cheese","pringles_bbq","pringles_pizza","pringles_lobster"]
# Enables "delete all pringles" to match every pringles variant.
_BRAND_GROUPS: dict = {}
for _k in ASSET_LIBRARY:
    _parts = _k.split("_")
    for _n in range(1, len(_parts)):
        _prefix = "_".join(_parts[:_n])
        _BRAND_GROUPS.setdefault(_prefix, []).append(_k)
_BRAND_GROUPS = {k: v for k, v in _BRAND_GROUPS.items() if len(v) > 1}



# ---------------------------------------------------------------------------
# Persistent rotation corrections (saved/loaded from JSON)
# ---------------------------------------------------------------------------
# Per-asset spawn rotation corrections.
# Some USDZ assets are authored with a non-standard "up" axis and appear
# "fallen" when spawned.  These corrections are composed on top of any
# world-rotation inherited from the replaced prim.
#
# Format: prim_name → (axis_vec3, angle_degrees)
# The rotation is applied as: effective_rot = inherited_rot * correction_rot
#
# How to find the right correction:
#   Inspect the spawned item's bbox.  If X-span >> Y-span the asset is
#   authored X-up → rotate +90° around Z to stand it upright (X→Y).
#   If Y-span >> Z-span but item looks tilted, try ±90° around X or Y.
# Per-asset spawn rotation corrections.
# Each entry is a LIST of (axis_vec3, angle_degrees) applied in order.
# Corrections are composed on top of any world-rotation inherited from the replaced prim.
#
# How to find the right values:
#   1. After a replace, select the spawned prim in the Kit viewport.
#   2. Press R to activate the rotate gizmo.  Spin around Y to fix face direction.
#   3. Read the resulting xformOp:orient quaternion in the Properties panel.
#   4. Convert or just try ±90° / 180° around Y until it faces the shelf correctly.
#
# Correction 1 — stand up:  -90° around Z maps X-axis (long axis) → -Y (vertical).
# Correction 2 — face fix:   rotate around Y to point the front face toward the aisle.
#   Try 0, 90, -90, or 180 degrees.  Change only this value while iterating.

# JSON file lives alongside this module so edits survive extension reloads.
# Format: { "prim_name": { "euler_x": 0, "euler_y": 90, "euler_z": -90 } }
# When a key exists here it takes priority over ASSET_SPAWN_ROTATION_CORRECTION.
_ROTATION_CORRECTIONS_PATH = os.path.join(os.path.dirname(__file__), "rotation_corrections.json")


def _load_rotation_corrections() -> dict:
    """Load saved Euler rotation corrections from JSON, return empty dict on failure."""
    try:
        if os.path.exists(_ROTATION_CORRECTIONS_PATH):
            with open(_ROTATION_CORRECTIONS_PATH, "r") as f:
                data = json.load(f)
            carb.log_info(f"[UsdSpawner] Loaded rotation_corrections.json ({len(data)} entries)")
            return data
    except Exception as exc:
        carb.log_warn(f"[UsdSpawner] Could not load rotation_corrections.json: {exc}")
    return {}


def _save_rotation_corrections(corrections: dict) -> None:
    """Persist rotation corrections dict to JSON file."""
    try:
        with open(_ROTATION_CORRECTIONS_PATH, "w") as f:
            json.dump(corrections, f, indent=2)
        carb.log_info(f"[UsdSpawner] Saved rotation_corrections.json ({len(corrections)} entries)")
    except Exception as exc:
        carb.log_warn(f"[UsdSpawner] Could not save rotation_corrections.json: {exc}")


# ---------------------------------------------------------------------------
# Persistent scale corrections (saved/loaded from JSON)
# ---------------------------------------------------------------------------
# Format: { "prim_name": { "scale_x": 1.0, "scale_y": 1.0, "scale_z": 1.0 } }
# When a key exists here it is applied as a scale op on the outer Xform.
_SCALE_CORRECTIONS_PATH = os.path.join(os.path.dirname(__file__), "scale_corrections.json")


def _load_scale_corrections() -> dict:
    """Load saved scale corrections from JSON, return empty dict on failure."""
    try:
        if os.path.exists(_SCALE_CORRECTIONS_PATH):
            with open(_SCALE_CORRECTIONS_PATH, "r") as f:
                data = json.load(f)
            carb.log_info(f"[UsdSpawner] Loaded scale_corrections.json ({len(data)} entries)")
            return data
    except Exception as exc:
        carb.log_warn(f"[UsdSpawner] Could not load scale_corrections.json: {exc}")
    return {}


def _save_scale_corrections(corrections: dict) -> None:
    """Persist scale corrections dict to JSON file."""
    try:
        with open(_SCALE_CORRECTIONS_PATH, "w") as f:
            json.dump(corrections, f, indent=2)
        carb.log_info(f"[UsdSpawner] Saved scale_corrections.json ({len(corrections)} entries)")
    except Exception as exc:
        carb.log_warn(f"[UsdSpawner] Could not save scale_corrections.json: {exc}")


# Reverse map: USD filename stem → asset key.
# e.g. "NittoTea_Royal_Milktea" → "nittotea_milktea"
# Used to scan stage for prims matching a given asset key.
_STEM_TO_KEY: dict = {}
_KEY_TO_STEM: dict = {}
_ASSET_UNIT_SCALE: dict[str, float] = {}
for _k, _fp in ASSET_LIBRARY.items():
    _stem = os.path.splitext(os.path.basename(_fp))[0]
    _STEM_TO_KEY[_stem] = _k
    _STEM_TO_KEY[_stem.replace("-", "_")] = _k
    _KEY_TO_STEM[_k] = _stem
    _ASSET_UNIT_SCALE[_k] = 1.0 if _fp.startswith(_USD_ASSETS) else 100.0
_STEM_TO_KEY_LOWER: dict[str, str] = {k.lower(): v for k, v in _STEM_TO_KEY.items()}


def _resolve_prim_key(prim_name: str) -> str | None:
    """Map a stage prim name (with optional numeric suffix) to an asset_key."""
    base = re.sub(r"_\d+$", "", prim_name)

    # First allow direct asset-key prim names used by spawned items, e.g.
    # /World/pringles_bbq_10 -> pringles_bbq.
    for candidate in (
        prim_name,
        base,
        prim_name.replace("-", "_"),
        base.replace("-", "_"),
    ):
        if candidate in ASSET_LIBRARY:
            return candidate
        lower_candidate = candidate.lower()
        if lower_candidate in ASSET_LIBRARY:
            return lower_candidate

    return (
        _STEM_TO_KEY.get(prim_name)
        or _STEM_TO_KEY.get(base)
        or _STEM_TO_KEY.get(prim_name.replace("-", "_"))
        or _STEM_TO_KEY.get(base.replace("-", "_"))
        or _STEM_TO_KEY_LOWER.get(prim_name.lower())
        or _STEM_TO_KEY_LOWER.get(base.lower())
        or _STEM_TO_KEY_LOWER.get(prim_name.replace("-", "_").lower())
        or _STEM_TO_KEY_LOWER.get(base.replace("-", "_").lower())
    )


def _gap_cluster(
    floors: list[tuple[str, float]], tolerance: float
) -> list[list[tuple[str, float]]]:
    """Gap-based clustering (ascending sort) — splits where consecutive gap > tolerance."""
    floors = sorted(floors, key=lambda x: x[1])
    clusters: list[list[tuple[str, float]]] = []
    cur: list[tuple[str, float]] = [floors[0]]
    for path, z in floors[1:]:
        if z - cur[-1][1] <= tolerance:
            cur.append((path, z))
        else:
            clusters.append(cur)
            cur = [(path, z)]
    clusters.append(cur)
    return clusters


class UsdSpawner:
    """Listens for spawnUsdRequest messages and spawns USD assets on the stage."""

    def __init__(self):
        self._subscriptions = []
        self._spawn_counter: dict[str, int] = {}
        # Tracks prim paths created per asset key so we can delete them later.
        self._spawned_prims: dict[str, list] = {}
        # Persistent per-asset Euler rotation corrections (loaded from JSON).
        # Keys override ASSET_SPAWN_ROTATION_CORRECTION for those assets.
        self._rotation_corrections: dict = _load_rotation_corrections()
        # Persistent per-asset scale corrections (loaded from JSON).
        self._scale_corrections: dict = _load_scale_corrections()

        # Tracking active incidents to be able to delete them incident_id -> prim_path
        self._active_incidents: dict = {}

        # Register outgoing events
        for evt in ("spawnUsdResponse", "deleteUsdResponse", "replaceUsdResponse", "replaceAllUsdResponse",
                    "detectShelfRowsResponse", "replaceRowResponse",
                    "incidentSpawnResponse", "incidentDeleteAllResponse", "incidentDeleteResponse"):
            messaging.register_event_type_to_send(evt)
            omni.kit.app.register_event_alias(
                carb.events.type_from_string(evt), evt
            )

        # Subscribe to spawn request
        omni.kit.app.register_event_alias(
            carb.events.type_from_string("spawnUsdRequest"),
            "spawnUsdRequest",
        )
        self._subscriptions.append(
            get_eventdispatcher().observe_event(
                observer_name="UsdSpawner:spawnUsdRequest",
                event_name="spawnUsdRequest",
                on_event=self._on_spawn_request,
            )
        )

        # Subscribe to delete request
        omni.kit.app.register_event_alias(
            carb.events.type_from_string("deleteUsdRequest"),
            "deleteUsdRequest",
        )
        self._subscriptions.append(
            get_eventdispatcher().observe_event(
                observer_name="UsdSpawner:deleteUsdRequest",
                event_name="deleteUsdRequest",
                on_event=self._on_delete_request,
            )
        )

        # Subscribe to replace request (single)
        omni.kit.app.register_event_alias(
            carb.events.type_from_string("replaceUsdRequest"),
            "replaceUsdRequest",
        )
        self._subscriptions.append(
            get_eventdispatcher().observe_event(
                observer_name="UsdSpawner:replaceUsdRequest",
                event_name="replaceUsdRequest",
                on_event=self._on_replace_request,
            )
        )

        # Subscribe to replace-all request (batch: source_paths + target asset)
        omni.kit.app.register_event_alias(
            carb.events.type_from_string("replaceAllUsdRequest"),
            "replaceAllUsdRequest",
        )
        self._subscriptions.append(
            get_eventdispatcher().observe_event(
                observer_name="UsdSpawner:replaceAllUsdRequest",
                event_name="replaceAllUsdRequest",
                on_event=self._on_replace_all_request,
            )
        )

        # Subscribe to adjust-asset-rotation request (browser rotation panel)
        omni.kit.app.register_event_alias(
            carb.events.type_from_string("adjustAssetRotation"),
            "adjustAssetRotation",
        )
        self._subscriptions.append(
            get_eventdispatcher().observe_event(
                observer_name="UsdSpawner:adjustAssetRotation",
                event_name="adjustAssetRotation",
                on_event=self._on_adjust_asset_rotation,
            )
        )

        # Subscribe to adjust-asset-scale request (browser scale panel)
        omni.kit.app.register_event_alias(
            carb.events.type_from_string("adjustAssetScale"),
            "adjustAssetScale",
        )
        self._subscriptions.append(
            get_eventdispatcher().observe_event(
                observer_name="UsdSpawner:adjustAssetScale",
                event_name="adjustAssetScale",
                on_event=self._on_adjust_asset_scale,
            )
        )

        # Subscribe to shelf-row detection request (browser shelf rows panel)
        omni.kit.app.register_event_alias(
            carb.events.type_from_string("detectShelfRowsRequest"),
            "detectShelfRowsRequest",
        )
        self._subscriptions.append(
            get_eventdispatcher().observe_event(
                observer_name="UsdSpawner:detectShelfRowsRequest",
                event_name="detectShelfRowsRequest",
                on_event=self._on_detect_shelf_rows,
            )
        )

        # Subscribe to replace-row request (chatbot: replace a specific global row)
        omni.kit.app.register_event_alias(
            carb.events.type_from_string("replaceRowRequest"),
            "replaceRowRequest",
        )
        self._subscriptions.append(
            get_eventdispatcher().observe_event(
                observer_name="UsdSpawner:replaceRowRequest",
                event_name="replaceRowRequest",
                on_event=self._on_replace_row_request,
            )
        )

        # Subscribe to incident spawn request (random ground-level placement)
        omni.kit.app.register_event_alias(
            carb.events.type_from_string("incidentSpawnRequest"),
            "incidentSpawnRequest",
        )
        self._subscriptions.append(
            get_eventdispatcher().observe_event(
                observer_name="UsdSpawner:incidentSpawnRequest",
                event_name="incidentSpawnRequest",
                on_event=self._on_incident_spawn_request,
            )
        )

        # Subscribe to incident delete-all request
        omni.kit.app.register_event_alias(
            carb.events.type_from_string("incidentDeleteAllRequest"),
            "incidentDeleteAllRequest",
        )
        self._subscriptions.append(
            get_eventdispatcher().observe_event(
                observer_name="UsdSpawner:incidentDeleteAllRequest",
                event_name="incidentDeleteAllRequest",
                on_event=self._on_incident_delete_all_request,
            )
        )

        # Subscribe to incident delete request
        omni.kit.app.register_event_alias(
            carb.events.type_from_string("incidentDeleteRequest"),
            "incidentDeleteRequest",
        )
        self._subscriptions.append(
            get_eventdispatcher().observe_event(
                observer_name="UsdSpawner:incidentDeleteRequest",
                event_name="incidentDeleteRequest",
                on_event=self._on_incident_delete_request,
            )
        )

        # Deferred stage scan — populate stage_prims.json once the stage loads
        self._scan_pending = True
        self._update_sub = (
            omni.kit.app.get_app()
            .get_update_event_stream()
            .create_subscription_to_pop(
                self._on_update,
                name="UsdSpawner:deferred_stage_scan",
                order=0,
            )
        )

        carb.log_info("[UsdSpawner] Ready. Asset library has "
                      f"{len(ASSET_LIBRARY)} entries.")

    # ------------------------------------------------------------------
    # Deferred stage scan
    # ------------------------------------------------------------------

    def _on_update(self, event) -> None:
        """Runs every frame until the stage is loaded, then requests store rack population"""
        if not self._scan_pending:
            return
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return
        world = stage.GetPrimAtPath("/World")
        if not world:
            return
        if not list(world.GetChildren()):
            return
        # Stage is loaded and has content - Initialize rack item population
        self._send_to_backend("/api/store-layout/populate-racks", "GET")

        self._scan_pending = False
        self._update_sub = None  # release subscription



    def _scan_stage_to_inventory(self) -> None:
        """
        Scan every /World/* prim on the current stage and write stage_prims.json
        with full metadata: asset_key, asset_name, usd_path, position, prim_name.

        This is the authoritative source for "what is on the shelf" — it covers
        items that were pre-placed in the USD file, not just chatbot-spawned ones.
        """
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return
        world = stage.GetPrimAtPath("/World")
        if not world:
            return

        inventory: dict = {}
        xf_cache = UsdGeom.XformCache(Usd.TimeCode.Default())

        def _scan_prim(prim, depth: int) -> None:
            """Recursively scan prims; record known assets, recurse into groups."""
            if depth > 4:
                return
            prim_name = prim.GetName()
            prim_path = str(prim.GetPath())

            # Ignore template/source prims that should not count as live inventory.
            # Prototypes are the main instances for each asset that are referenced by all other copies;
            # they can never be deleted and are invisible in the scene.
            if prim_path == "/World/Prototypes" or prim_path.startswith("/World/Prototypes/"):
                return

            # 1. Check if prim has asset_key (added in _spawn_usd)
            asset_key = prim.GetCustomDataByKey("asset_key")

            if not asset_key:
            # Fallback: Resolve asset_key from prim name (strip trailing _N suffix first)
                base = re.sub(r"_\d+$", "", prim_name)
                asset_key = (
                    _STEM_TO_KEY.get(prim_name)
                    or _STEM_TO_KEY.get(base)
                    or _STEM_TO_KEY.get(prim_name.replace("-", "_"))
                    or _STEM_TO_KEY.get(base.replace("-", "_"))
                    or ""
                )

            if asset_key:
                # Known product — record with position and shelf group
                position = [0.0, 0.0, 0.0]
                rack_id = None
                shelf_row = None

                rack_id = prim.GetCustomDataByKey("rack_id")
                shelf_row = prim.GetCustomDataByKey("shelf_row")

                try:
                    xform_api = UsdGeom.Xformable(prim)
                    translate_op = next(
                        (op for op in xform_api.GetOrderedXformOps()
                         if op.GetOpType() == UsdGeom.XformOp.TypeTranslate),
                        None,
                    )
                    if translate_op:
                        val = translate_op.Get(Usd.TimeCode.Default())
                        position = [round(val[0], 3), round(val[1], 3), round(val[2], 3)]
                    else:
                        world_xf = xf_cache.GetLocalToWorldTransform(prim)
                        trans = world_xf.ExtractTranslation()
                        position = [round(trans[0], 3), round(trans[1], 3), round(trans[2], 3)]
                except Exception as exc:
                    carb.log_warn(f"[UsdSpawner] Stage scan: pos read failed for {prim_path}: {exc}")

                # Count visual units: some models pack multiple cans into one prim
                # where each can is a separate Mesh_N child under a Geometry scope.
                # e.g. Pringles_Lobster has Geometry/Mesh, Mesh_01 … Mesh_07 = 8 cans.
                unit_count = 1
                try:
                    search_root = prim
                    ref_child = prim.GetChild("Ref")
                    if ref_child and ref_child.IsInstance():
                        proto = ref_child.GetPrototype()
                        if proto:
                            search_root = proto

                    def _find_geometry_and_count(node):
                        nonlocal unit_count
                        for ch in node.GetChildren():
                            if ch.GetName().lower() == "geometry":
                                mesh_children = [m for m in ch.GetChildren() if m.GetName().startswith("Mesh")]
                                if len(mesh_children) > 1:
                                    unit_count = len(mesh_children)
                                return True
                            if _find_geometry_and_count(ch):
                                return True
                        return False

                    _find_geometry_and_count(search_root)
                except Exception:
                    pass


                inventory[prim_path] = {
                    "asset_key":   asset_key,
                    "asset_name":  asset_key.replace("_", " ").title(),
                    "usd_path":    ASSET_LIBRARY.get(asset_key, ""),
                    "position":    position,
                    "prim_name":   prim_name,
                    "rack_id":     rack_id,
                    "shelf_row":   shelf_row,
                    "unit_count":  unit_count,
                    "source":      "stage_scan",
                }
            else:
                # Not a known product — recurse into child prims.
                for child in prim.GetChildren():
                    _scan_prim(child, depth + 1)

        for child in world.GetChildren():
            _scan_prim(child, 1)

        try:
            os.makedirs(os.path.dirname(STAGE_PRIMS_FILE), exist_ok=True)
            with open(STAGE_PRIMS_FILE, "w") as f:
                json.dump(inventory, f, indent=2)
            known = sum(1 for v in inventory.values() if v.get("asset_key"))
            total_units = sum(v.get("unit_count", 1) for v in inventory.values() if v.get("asset_key"))
            carb.log_info(
                f"[UsdSpawner] Stage scan complete: {len(inventory)} prims → "
                f"stage_prims.json  ({known} known assets, {total_units} visual units)"
            )
        except Exception as exc:
            carb.log_warn(f"[UsdSpawner] Could not write stage_prims.json: {exc}")

        # Push the full inventory (with unit_count) directly to the agent backend.
        self._send_to_backend("/api/inventory", "PUT", {"inventory": inventory})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_up_axis(self) -> str:
        """Return 'Y' or 'Z' from stage upAxis metadata (defaults to 'Y')."""
        try:
            stage = omni.usd.get_context().get_stage()
            if stage:
                up = stage.GetMetadata("upAxis")
                if up:
                    return str(up).upper()
        except Exception:
            pass
        return "Y"

    def _get_floor_level(self, up_axis: str) -> float:
        """Return the floor coordinate along the up axis."""
        config_key = "floor_z" if up_axis == "Z" else "floor_y"
        if config_key in _cfg:
            return float(_cfg[config_key])
        try:
            stage = omni.usd.get_context().get_stage()
            if stage:
                floor_prim = stage.GetPrimAtPath("/World/Floor")
                if floor_prim:
                    ops = UsdGeom.Xformable(floor_prim).GetOrderedXformOps()
                    translate_op = next(
                        (op for op in ops if op.GetOpType() == UsdGeom.XformOp.TypeTranslate),
                        None,
                    )
                    if translate_op:
                        val = translate_op.Get()
                        return float(val[2]) if up_axis == "Z" else float(val[1])
        except Exception:
            pass
        return 0.0

    def _compute_world_position(
        self, screen_x: float, screen_y: float
    ) -> "Gf.Vec3d | None":
        """
        Convert normalised screen coords → world-space position on the floor plane.

        screen_x, screen_y ∈ [0, 1], (0,0) = top-left corner of the viewport.
        Detects stage up-axis (Y-up or Z-up) and intersects the camera ray with
        the correct horizontal plane.  Returns None when computation fails.
        """
        try:
            from omni.kit.viewport.utility import get_active_viewport

            viewport = get_active_viewport()
            if viewport is None:
                carb.log_error("[UsdSpawner] No active viewport")
                return None

            stage = omni.usd.get_context().get_stage()
            camera_prim = stage.GetPrimAtPath(viewport.camera_path)
            if not camera_prim:
                carb.log_error(f"[UsdSpawner] Camera prim not found: {viewport.camera_path}")
                return None

            frustum   = UsdGeom.Camera(camera_prim).GetCamera(Usd.TimeCode.Default()).frustum
            ndc_x     = 2.0 * screen_x - 1.0
            ndc_y     = 1.0 - 2.0 * screen_y
            ray       = frustum.ComputePickRay(Gf.Vec2d(ndc_x, ndc_y))
            origin    = ray.startPoint
            direction = ray.direction

            up_axis     = self._detect_up_axis()
            floor_level = self._get_floor_level(up_axis)

            if up_axis == "Z":
                # Intersect with horizontal Z = floor_level plane
                if abs(direction[2]) < 1e-6:
                    carb.log_warn("[UsdSpawner] Ray parallel to Z-floor, using fallback distance")
                    t = 1500.0
                    hit = origin + t * direction
                    return Gf.Vec3d(hit[0], hit[1], floor_level)
                t = (floor_level - origin[2]) / direction[2]
                if t < 0:
                    t = abs(t)
                hit = origin + t * direction
                return Gf.Vec3d(hit[0], hit[1], floor_level)
            else:
                # Y-up: intersect with horizontal Y = floor_level plane
                if abs(direction[1]) < 1e-6:
                    carb.log_warn("[UsdSpawner] Ray parallel to Y-floor, using fallback distance")
                    t = 1500.0
                    hit = origin + t * direction
                    return Gf.Vec3d(hit[0], floor_level, hit[2])
                t = (floor_level - origin[1]) / direction[1]
                if t < 0:
                    t = abs(t)
                hit = origin + t * direction
                return Gf.Vec3d(hit[0], floor_level, hit[2])

        except Exception as exc:
            carb.log_error(f"[UsdSpawner] Ray computation error: {exc}")
            return None

    def _make_unique_prim_path(self, prim_name: str) -> str:
        """Return a unique /World/<name> path, appending _N when needed.
        Checks the live stage so paths stay unique across extension reloads."""
        stage = omni.usd.get_context().get_stage()
        safe = re.sub(r"[^A-Za-z0-9_]", "_", prim_name).strip("_") or "Asset"
        while True:
            count = self._spawn_counter.get(safe, 0) + 1
            self._spawn_counter[safe] = count
            suffix = f"_{count}" if count > 1 else ""
            prim_path = f"/World/{safe}{suffix}"
            if not stage.GetPrimAtPath(prim_path):
                return prim_path

    def _spawn_usd(
        self, usd_path: str, asset_key: str, position: "Gf.Vec3d",
        rotation: "Gf.Rotation" = None,
        snap_y_to: float = None,
        rack_id: str = None,
        shelf_row: int = None,
        skip_inventory_update: bool = False
    ) -> str:
        """
        Reference `usd_path` into the current stage at `position`.
        Returns the stage prim path of the spawned asset.

        The reference is placed on a child prim so that our translate op
        on the outer Xform does not conflict with whatever xformOps the
        referenced USD defines on its default prim.

        Optional `rotation` (Gf.Rotation) preserves the world-space orientation
        of the original prim when performing a replace operation.

        Optional `snap_y_to` (float): when provided, the new item's world-space
        bottom (bbox y_min) is shifted to land exactly at this Y level.  Used
        during replace so the replacement sits at the same shelf height as the
        original item regardless of pivot offsets.  When omitted the existing
        floor-snap-to-Y=0 logic runs instead.

        Optional `skip_inventory_update` is only used for the initial store population since whole stage will be
        scanned and sent to inventory anyway
        """
        stage = omni.usd.get_context().get_stage()

        # Ensure /World Xform exists
        if not stage.GetPrimAtPath("/World"):
            UsdGeom.Xform.Define(stage, "/World")

        prim_path = self._make_unique_prim_path(asset_key)

        # Outer Xform — owns the world-space translation (and rotation when replacing).
        xform = UsdGeom.Xform.Define(stage, prim_path)

        # Tag prim with its prim name (asset_key) for later lookup
        xform.GetPrim().SetCustomDataByKey("asset_key", asset_key)

        if rack_id:
            xform.GetPrim().SetCustomDataByKey("rack_id", rack_id)
        if shelf_row and isinstance(shelf_row, int):
            xform.GetPrim().SetCustomDataByKey("shelf_row", shelf_row)

        # Safely get or add the translate op (avoids duplicate-op crash on reload)
        translate_op = next(
            (op for op in xform.GetOrderedXformOps()
             if op.GetOpType() == UsdGeom.XformOp.TypeTranslate),
            None
        )
        if translate_op is None:
            translate_op = xform.AddTranslateOp()
        translate_op.Set(position)

        # Compose any inherited world rotation with a per-asset correction rotation.
        # Some assets are authored with a non-standard up-axis and need a fixed
        # correction so they appear upright when spawned.
        #
        # Priority: JSON file (self._rotation_corrections) > ASSET_SPAWN_ROTATION_CORRECTION
        effective_rotation = rotation  # may be None

        if asset_key in self._rotation_corrections:
            # JSON-saved absolute Euler correction (set via browser rotation panel).
            # Applied as absolute orientation — overrides world rotation for this asset.
            rc = self._rotation_corrections[asset_key]
            ex = float(rc.get("euler_x", 0))
            ey = float(rc.get("euler_y", 0))
            ez = float(rc.get("euler_z", 0))
            rot_z = Gf.Rotation(Gf.Vec3d(0, 0, 1), ez)
            rot_y = Gf.Rotation(Gf.Vec3d(0, 1, 0), ey)
            rot_x = Gf.Rotation(Gf.Vec3d(1, 0, 0), ex)
            effective_rotation = rot_x * rot_y * rot_z
            carb.log_warn(
                f"[UsdSpawner] Spawn: using saved rotation correction"
                f" X={ex}° Y={ey}° Z={ez}° for '{asset_key}'"
            )

        # Apply effective rotation (inherited + correction) if any.
        if effective_rotation is not None:
            try:
                quat_raw = effective_rotation.GetQuaternion()
                q = Gf.Quatd(quat_raw.GetReal(), Gf.Vec3d(quat_raw.GetImaginary()))
                orient_op = xform.AddOrientOp(UsdGeom.XformOp.PrecisionDouble)
                orient_op.Set(q)
                carb.log_warn(f"[UsdSpawner] Spawn: applied rotation quaternion {q} to '{prim_path}'")
            except Exception as rot_err:
                carb.log_warn(f"[UsdSpawner] Spawn: could not apply rotation: {rot_err}")

        # Apply saved scale correction on the outer Xform (scale is innermost, applied first).
        # Multiplies with the /Ref child's 100x unit-conversion scale.
        if asset_key in self._scale_corrections:
            sc = self._scale_corrections[asset_key]
            sx = float(sc.get("scale_x", 1.0))
            sy = float(sc.get("scale_y", 1.0))
            sz = float(sc.get("scale_z", 1.0))
            if sx != 1.0 or sy != 1.0 or sz != 1.0:
                try:
                    scale_op = xform.AddScaleOp(UsdGeom.XformOp.PrecisionFloat)
                    scale_op.Set(Gf.Vec3f(sx, sy, sz))
                    carb.log_warn(
                        f"[UsdSpawner] Spawn: applied scale correction"
                        f" ({sx},{sy},{sz}) for '{asset_key}'"
                    )
                except Exception as sc_err:
                    carb.log_warn(f"[UsdSpawner] Spawn: could not apply scale: {sc_err}")

        # Child prim holds the reference; its internal xformOps are unaffected.
        # Primary assets (metersPerUnit=1.0) need 100x to reach centimeter stage.
        # USD-Assets (metersPerUnit=0.01) are already in centimeters → scale 1.0.
        ref_path = prim_path + "/Ref"
        ref_xform = UsdGeom.Xform.Define(stage, ref_path)
        ref_prim = ref_xform.GetPrim()

        _unit_scale = _ASSET_UNIT_SCALE.get(asset_key, 100.0)
        ref_xform.AddScaleOp().Set(Gf.Vec3f(_unit_scale, _unit_scale, _unit_scale))
        ref_prim.GetReferences().AddReference(usd_path)

        # Tell USD to implicitly instance this.
        # If this is the first Pringles can, USD creates the hidden prototype.
        # If this is the 100th can, USD automatically links it to the existing hidden prototype.
        ref_prim.SetInstanceable(True)

        # Floor-snap: raise prim so its bounding box bottom touches the floor plane.
        # Works for both Y-up (up_idx=1) and Z-up (up_idx=2) stages.
        #
        #   snap_y_to is set  → replace mode: snap bottom to original item's bottom level
        #   snap_y_to is None → fresh spawn:  snap bottom to floor only if below ground
        up_axis     = self._detect_up_axis()
        floor_level = self._get_floor_level(up_axis)
        up_idx      = 2 if up_axis == "Z" else 1
        try:
            bbox_cache = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(),
                includedPurposes=[UsdGeom.Tokens.default_],
                useExtentsHint=False,
            )
            bbox = bbox_cache.ComputeWorldBound(xform.GetPrim())
            rng  = bbox.GetRange()
            if not rng.IsEmpty():
                v_min = rng.GetMin()[up_idx]
                if snap_y_to is not None:
                    cur = list(translate_op.Get(Usd.TimeCode.Default()))
                    old_v = cur[up_idx]
                    cur[up_idx] += snap_y_to - v_min
                    translate_op.Set(Gf.Vec3d(*cur))
                    carb.log_warn(
                        f"[UsdSpawner] Shelf-snap (axis={up_idx}): bbox_min={v_min:.2f}  "
                        f"snap_to={snap_y_to:.2f}  translate {old_v:.2f} → {cur[up_idx]:.2f}"
                    )
                elif v_min < floor_level - 0.5:
                    cur = list(translate_op.Get(Usd.TimeCode.Default()))
                    old_v = cur[up_idx]
                    cur[up_idx] += floor_level - v_min
                    translate_op.Set(Gf.Vec3d(*cur))
                    carb.log_warn(
                        f"[UsdSpawner] Floor-snap (axis={up_idx}): v_min={v_min:.1f}  "
                        f"floor={floor_level:.1f}  translate {old_v:.2f} → {cur[up_idx]:.2f}"
                    )

                # Horizontal centering: compensate for assets whose USDZ origin is at
                # a geometry edge rather than the visual center. No-op for primary assets
                # whose origins are already centered (delta ≈ 0).
                h_axes = [i for i in (0, 1, 2) if i != up_idx]
                cur = list(translate_op.Get(Usd.TimeCode.Default()))
                h_adjusted = False
                for h in h_axes:
                    bbox_center_h = (rng.GetMin()[h] + rng.GetMax()[h]) / 2
                    delta = bbox_center_h - cur[h]
                    if abs(delta) > 0.5:
                        cur[h] -= delta
                        h_adjusted = True
                if h_adjusted:
                    translate_op.Set(Gf.Vec3d(*cur))
                    carb.log_warn(
                        f"[UsdSpawner] Spawn: horizontal bbox-center correction applied for '{asset_key}'"
                    )
        except Exception as snap_err:
            carb.log_warn(f"[UsdSpawner] Floor-snap failed (asset may be partially underground): {snap_err}")

        # Apply per-asset translation offset (saved via browser rotation panel).
        if asset_key in self._rotation_corrections:
            rc = self._rotation_corrections[asset_key]
            ox = float(rc.get("offset_x", 0))
            oy = float(rc.get("offset_y", 0))
            oz = float(rc.get("offset_z", 0))
            if ox or oy or oz:
                cur = translate_op.Get(Usd.TimeCode.Default())
                translate_op.Set(Gf.Vec3d(cur[0] + ox, cur[1] + oy, cur[2] + oz))
                carb.log_warn(
                    f"[UsdSpawner] Spawn: applied translation offset"
                    f" ({ox},{oy},{oz}) cm to '{prim_path}'"
                )

        carb.log_info(
            f"[UsdSpawner] Spawned '{usd_path}' → '{prim_path}'  pos={position}"
        )

        if not skip_inventory_update:
            # Record for later deletion (in-memory + persistent inventory)
            self._spawned_prims.setdefault(asset_key, []).append(prim_path)
            self._inventory_add(prim_path, asset_key, usd_path, position, rack_id=rack_id, shelf_row=shelf_row)

        # ── Debug: report what actually landed on stage ──────────────────
        try:
            outer = stage.GetPrimAtPath(prim_path)
            ref   = ref_xform.GetPrim()

            is_instance = ref.IsInstance()

            # 1. Fetch children based on whether it is an instance or a normal prim
            if is_instance:
                # Instances hide their children to save memory. We must query the Prototype (master blueprint).
                prototype = ref.GetPrototype()
                ref_children = list(prototype.GetChildren()) if prototype else []
                instance_msg = f" (Instance, querying prototype: {prototype.GetPath() if prototype else 'None'})"
            else:
                # Standard prim, we can get children directly.
                ref_children = list(ref.GetChildren())
                instance_msg = ""

            # 2. Log the status of the outer prim
            carb.log_warn(
                f"[UsdSpawner][DEBUG] outer  valid={outer.IsValid()}  "
                f"active={outer.IsActive()}  type='{outer.GetTypeName()}'"
            )

            # 3. Log the status of the reference (now includes instance_msg!)
            carb.log_warn(
                f"[UsdSpawner][DEBUG] ref    valid={ref.IsValid()}  "
                f"active={ref.IsActive()}  type='{ref.GetTypeName()}'  "
                f"children={len(ref_children)}{instance_msg}"
            )

            # 4. Error handling if children are still missing
            if not ref_children:
                carb.log_error(
                    f"[UsdSpawner][DEBUG] Reference has NO children — "
                    f"the file may not be accessible at: {usd_path}"
                )
            else:
                for c in ref_children[:5]:
                    carb.log_warn(f"[UsdSpawner][DEBUG]   child: {c.GetPath()}  type={c.GetTypeName()}")

            # 5. Check visibility
            imageable = UsdGeom.Imageable(outer)
            vis = imageable.ComputeVisibility(Usd.TimeCode.Default())
            carb.log_warn(f"[UsdSpawner][DEBUG] visibility='{vis}'")

            # 6. Check world bounds (may be empty if geometry hasn't cooked yet)
            try:
                from pxr import UsdGeom as _UG
                bbox_cache = _UG.BBoxCache(
                    Usd.TimeCode.Default(),
                    includedPurposes=[_UG.Tokens.default_],
                    useExtentsHint=False,
                )
                bbox = bbox_cache.ComputeWorldBound(outer)
                rng  = bbox.GetRange()
                carb.log_warn(
                    f"[UsdSpawner][DEBUG] bbox min={rng.GetMin()}  max={rng.GetMax()}  "
                    f"empty={rng.IsEmpty()}"
                )
            except Exception as bbox_err:
                carb.log_warn(f"[UsdSpawner][DEBUG] bbox error: {bbox_err}")

        except Exception as dbg_err:
            carb.log_error(f"[UsdSpawner][DEBUG] debug block failed: {dbg_err}")
        # ─────────────────────────────────────────────────────────────────

        return prim_path

    def _reply(self, payload: dict) -> None:
        get_eventdispatcher().dispatch_event("spawnUsdResponse", payload=payload)

    def _reply_delete(self, payload: dict) -> None:
        get_eventdispatcher().dispatch_event("deleteUsdResponse", payload=payload)

    def _reply_replace(self, payload: dict) -> None:
        get_eventdispatcher().dispatch_event("replaceUsdResponse", payload=payload)


    # ------------------------------------------------------------------
    # Simulation Product Restocking
    # ------------------------------------------------------------------
    def _restock_product(self, rack_info: dict, asset_key: str, quantity_ordered: int, skip_inventory_update: bool = False) -> tuple[bool, int]:
        """
        Restocks a specific product across available store shelves using planogram data.
        Calculates theoretical spatial slots using direction vectors to support arbitrary rack rotations,
        faces existing valid stock forward, and spawns new stock in remaining slots.

        Args:
            rack_info (dict): All required information about the rack (dimensions & planogram) for restocking
            asset_key (str): Unique identifier for the product asset.
            quantity_ordered (int): Total quantity of the product to restock across all shelves.
            skip_inventory_update (bool) is only used for the initial store population since whole stage will be
        scanned and sent to inventory anyway

        Returns:
            bool: True if restocking process executed successfully, False otherwise.
        """
        # 1. Retrieve rack and planogram data
        if not rack_info or not asset_key or not quantity_ordered:
            carb.log_error("[UsdSpawner] Required params missing")
            return False, 0

        rack_id = rack_info.get("rack_id")
        shelf_width = rack_info.get("shelf_width")
        shelf_depth = rack_info.get("shelf_depth")
        available_shelf_height = rack_info.get("available_shelf_height")
        shelves = rack_info.get("shelves")
        anchor = rack_info.get("anchor") # Expected format: "(X, Y, Z)"
        if not shelf_width or not shelf_depth or not shelves or not anchor or len(anchor) != 3 or not available_shelf_height:
            carb.log_error(f"[UsdSpawner] Missing required params for rack {rack_id}")
            return False, 0


        # Cast the rack anchor to float
        anchor = [float(anchor[0]), float(anchor[1]), float(anchor[2])]

        carb.log_warn("[UsdSpawner] ====================================================================================")
        carb.log_warn(f"[UsdSpawner] Start Restocking on Rack {rack_id}")
        carb.log_warn("[UsdSpawner] ====================================================================================")
        carb.log_warn("[UsdSpawner] Restock Step 1 Complete")
        # 2. Extract normalized direction vectors for procedural spatial calculations
        width_dir = rack_info.get("shelf_width_direction", [1.0, 0.0, 0.0])
        width_dir = [float(width_dir[0]), float(width_dir[1]), float(width_dir[2])]
        depth_dir = rack_info.get("shelf_depth_direction", [0.0, 1.0, 0.0])
        depth_dir = [float(depth_dir[0]), float(depth_dir[1]), float(depth_dir[2])]
        remaining_to_spawn = quantity_ordered
        total_spawned = 0
        carb.log_warn(f"[UsdSpawner] Width dir: {width_dir}  Depth dir: {depth_dir}")
        relocated_or_spawned_prims = []
        # 3. Process each shelf containing the target product
        for shelf in shelves:
            if remaining_to_spawn <= 0:
                break  # Order fulfilled, halt processing further shelves

            sequence = shelf.get("layout_sequence")
            shelf_height = shelf.get("elevation")
            if not sequence or not shelf_height:
                carb.log_error(f"[UsdSpawner] Planogram missing required params for rack {rack_id} and shelf {shelf}")
                return False, 0

            # Subtract
            shelf_height_offset = shelf_height - anchor[2]
            # Bypassing shelves that do not contain the target asset in the layout
            if not any(item.get("asset_key") == asset_key for item in sequence):
                continue

            shelf_row = shelf.get("shelf_row")
            carb.log_warn("[UsdSpawner] ----------------------------------------------------------------------")
            carb.log_warn(f"[UsdSpawner] Checking Shelf Row {shelf_row}")
            # 4. Calculate Dynamic Margins (Even spacing of separate products across the shelf)
            item_margin = 2.0  # Keep intra-product gap small and fixed (e.g., gap between two Pringles cans)

            # PASS 1: Calculate total physical width of ALL products on this shelf
            total_raw_width = 0.0
            for item in sequence:
                i_key = item.get("asset_key")
                f_count = item.get("facing_count", 1)
                i_w = self._get_usd_item_width(i_key) # Need width of this specific item

                # Raw width = (Width * Facings) + the tiny gaps between the facings
                total_raw_width += (i_w * f_count) + (item_margin * (f_count - 1))
            carb.log_warn(f"[UsdSpawner] Step 4: Restock Step 4 Pass 1 Complete. Total raw width: {total_raw_width}")
            # Distribute remaining shelf space evenly between distinct product groups
            # We divide by (len(sequence) - 1) to have no padding on the left and right side
            available_empty_space = max(0.0, shelf_width - total_raw_width)
            dynamic_margin = available_empty_space / (len(sequence) - 1) if sequence else 0.0
            carb.log_warn(f"[UsdSpawner] Step 4: Available space: {available_empty_space}, Dynamic Margin: {dynamic_margin}")

            if available_empty_space <= 0.0:
                carb.log_error(f"[UsdSpawner] Products require more space than available => Restocking not possible in shelf row {shelf_row}")
                continue

            # PASS 2: Find the specific target zone utilizing the new dynamic margin
            current_width_offset = 0.0 # Start at the very left
            target_zone_start_offset = 0.0
            target_facing_count = 0
            target_zone_width = 0.0

            item_w, item_d, item_h = self._get_usd_item_dimensions(asset_key)

            carb.log_warn("[UsdSpawner] Step 4: Checking Planogram item dimensions")
            for item in sequence:
                item_key = item.get("asset_key")
                facing_count = item.get("facing_count", 1)

                # Width of this specific product group
                group_width = (self._get_usd_item_width(item_key) * facing_count) + (item_margin * (facing_count - 1))
                carb.log_warn(f"[UsdSpawner] Step 4: item: {item_key}, group_width: {group_width}")
                if item_key == asset_key:
                    target_zone_start_offset = current_width_offset
                    target_facing_count = facing_count
                    target_zone_width = group_width
                    break

                # Advance the offset: Add the group width PLUS the dynamic margin between distinct products
                current_width_offset += group_width + dynamic_margin
            carb.log_warn("[UsdSpawner] Restock Step 4 all Complete")
            # 5. Clear Foreign Objects (Oriented Spatial Sweep)
            # Calculate the geometric center of the target zone for the overlap query
            center_x = anchor[0] + (width_dir[0] * (target_zone_start_offset + (target_zone_width / 2.0))) + (depth_dir[0] * (shelf_depth / 2.0))
            center_y = anchor[1] + (width_dir[1] * (target_zone_start_offset + (target_zone_width / 2.0))) + (depth_dir[1] * (shelf_depth / 2.0))
            center_z = min(anchor[2] + shelf_height_offset + (item_h / 2.0), anchor[2] + shelf_height_offset + (available_shelf_height / 2))

            zone_center = [center_x, center_y, center_z]
            half_extents = [target_zone_width / 2.0, shelf_depth / 2.0, item_h / 2.0]
            carb.log_warn("[UsdSpawner] Step 5: Item Bounding Box:")
            carb.log_warn(f"zone_center: {zone_center}")
            carb.log_warn(f"half_extents: {half_extents}")
            # Query PhysX for prims within the oriented bounding box
            prims_in_zone = self._get_prims_in_obb(center=zone_center, half_extents=half_extents, w_dir=width_dir, d_dir=depth_dir)
            carb.log_warn(f"[UsdSpawner] Step 5: Prims in zone for {asset_key}: {str(prims_in_zone)}")
            valid_existing_prims = []
            for prim in prims_in_zone:
                # Bypass structural fixtures (shelves, racks) to prevent deleting store geometry
                if self._is_structural_mesh(prim):
                    continue

                # Bypass in this iteration spawned or relocated prims
                if prim in relocated_or_spawned_prims:
                    continue


                if self._get_prim_asset_key(prim) == asset_key:
                    valid_existing_prims.append(prim)
                else:
                    # Isolate physics-disrupting foreign objects
                    carb.log_warn(f"[UsdSpawner] Step 5: Delete foreign prim: {prim}")
                    prim_path = str(prim.GetPath())
                    self._on_delete_request({"prim_path": prim_path})

            carb.log_warn("[UsdSpawner] Restock Step 5 Complete")
            # 6. Generate the Theoretical Grid Pool
            max_depth_capacity = int(shelf_depth // (item_d + item_margin))
            carb.log_warn(f"[UsdSpawner] Step 6: Max Depth Capacity: {max_depth_capacity}, Facing coung: {target_facing_count}")
            grid_slots = []

            for row in range(max_depth_capacity):
                for col in range(target_facing_count):
                    # Compute localized scalar offsets relative to the anchor
                    w_scalar = target_zone_start_offset + (col * (item_w + item_margin)) + (item_w / 2.0)
                    d_scalar = (row * (item_d + item_margin)) + (item_d / 2.0)

                    # Transform scalars into world-space coordinates using direction vectors
                    slot_x = anchor[0] + (width_dir[0] * w_scalar) + (depth_dir[0] * d_scalar)
                    slot_y = anchor[1] + (width_dir[1] * w_scalar) + (depth_dir[1] * d_scalar)
                    slot_z = anchor[2] + shelf_height_offset

                    grid_slots.append({
                        "pos": (slot_x, slot_y, slot_z),
                        "row_index": row
                    })

            # Sort grid slots (lowest row_index represents the back of the shelf)
            grid_slots.sort(key=lambda slot: slot["row_index"], reverse=True)
            carb.log_warn(f"[UsdSpawner] Step 6: {len(grid_slots)} Grid slots available: {grid_slots}")
            carb.log_warn("[UsdSpawner] Restock Step 6 Complete")

            # 7. Facing: Align valid existing stock to the frontmost available slots
            for prim in valid_existing_prims:
                if grid_slots:
                    front_slot = grid_slots.pop(0)
                    carb.log_warn(f"[UsdSpawner] Step 7: move {prim} to {front_slot['pos']}")
                    self._translate_prim(prim, front_slot["pos"])
                    self._rotate_prim_to_face_shelf_front(prim, depth_dir)
                    relocated_or_spawned_prims.append(prim)
                else:
                    # Zone over-capacity; discard excess manual stock to maintain grid integrity
                    prim_path = str(prim.GetPath())
                    carb.log_error(f"[UsdSpawner] Step 7: delete excessive {prim} ")
                    self._on_delete_request({"prim_path": prim_path})
            carb.log_warn("[UsdSpawner] Restock Step 7 Complete")

            # 8. Restocking: Populate remaining grid slots up to the ordered quantity
            quantity_to_spawn_on_shelf = min(remaining_to_spawn, len(grid_slots))

            for _ in range(quantity_to_spawn_on_shelf):
                empty_slot = grid_slots.pop(0)
                pos = empty_slot["pos"]
                spawn_pos = Gf.Vec3d(pos[0], pos[1], pos[2])

                # Retrieve the USD path from your library
                usd_path = ASSET_LIBRARY.get(asset_key)

                # Use your existing spawn function directly
                carb.log_warn(f"[UsdSpawner] Step 8: spawn {asset_key} to {spawn_pos}")
                new_prim_path = self._spawn_usd(usd_path, asset_key, spawn_pos, rack_id=rack_id, shelf_row=shelf_row, skip_inventory_update=skip_inventory_update)
                stage = omni.usd.get_context().get_stage()
                new_prim = stage.GetPrimAtPath(new_prim_path)
                self._rotate_prim_to_face_shelf_front(new_prim, depth_dir)
                relocated_or_spawned_prims.append(new_prim)

            # Update loop invariants
            remaining_to_spawn -= quantity_to_spawn_on_shelf
            total_spawned += quantity_to_spawn_on_shelf
        carb.log_warn("[UsdSpawner] ----------------------------------------------------------------------")
        carb.log_warn("[UsdSpawner] Restock Step 8 Complete")


        # 9. Final execution report
        return True, total_spawned


    # ------------------------------------------------------------------
    # Simulation Restocking Helpers
    # ------------------------------------------------------------------

    def _get_usd_item_dimensions(self, asset_key: str) -> tuple[float, float, float]: # width, depth, height
        """
        Calculates the physical (Width, Depth, Height) dimensions of an asset
        by reading its source USDZ file directly into memory.

        This prevents bounding box corruption from world-space rotations on the live stage
        and perfectly handles the 'sold out' scenario where no items exist to measure.

        Assumes all assets are natively authored facing the positive X-axis (+X).
        Therefore:
          - Native Y-axis extent = Physical Left/Right Width
          - Native X-axis extent = Physical Front/Back Depth
          - Native Z-axis extent = Physical Height
        """
        usd_path = ASSET_LIBRARY.get(asset_key)

        if usd_path:
            try:
                # Open the asset in memory (USD caches this automatically, making it highly efficient)
                asset_stage = Usd.Stage.Open(usd_path)
                if asset_stage:
                    # Target the default prim, or the root if the default prim isn't specified
                    measure_prim = asset_stage.GetDefaultPrim() or asset_stage.GetPseudoRoot()

                    bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
                    bbox = bbox_cache.ComputeWorldBound(measure_prim)
                    rng = bbox.GetRange()

                    if not rng.IsEmpty():
                        raw_size = rng.GetSize()

                        # 1. Apply base unit scale conversion (e.g., meters to cm) used during spawning
                        unit_scale = _ASSET_UNIT_SCALE.get(asset_key, 1.0)

                        # 2. Apply any custom user scale corrections saved in the UI
                        sc = self._scale_corrections.get(asset_key, {})
                        sx = float(sc.get("scale_x", 1.0))
                        sy = float(sc.get("scale_y", 1.0))
                        sz = float(sc.get("scale_z", 1.0))

                        # Calculate final scaled extents
                        scaled_x = abs(raw_size[0]) * unit_scale * sx
                        scaled_y = abs(raw_size[1]) * unit_scale * sy
                        scaled_z = abs(raw_size[2]) * unit_scale * sz
                        carb.log_warn(f"[UsdSpawner] USD Path: {usd_path}, raw_size: {raw_size}")
                        carb.log_warn(f"[UsdSpawner] Dimensions for {asset_key}: width: {scaled_y}, depth: {scaled_x}, height: {scaled_z}")
                        # Map native axes to contextual dimensions: (Width(Y), Depth(X), Height(Z))
                        return (scaled_y, scaled_x, scaled_z)

            except Exception as e:
                carb.log_warn(f"[UsdSpawner] Failed to measure source file for {asset_key}: {e}")

        # Fallback dimensions in cm if asset is completely unresolvable
        carb.log_warn(f"[UsdSpawner] Using default fallback dimensions for {asset_key}")
        return (10.0, 10.0, 20.0)

    def _get_usd_item_width(self, asset_key: str) -> float:
        """Convenience wrapper to extract only the physical left-to-right width of an asset."""
        return self._get_usd_item_dimensions(asset_key)[0]

    def _get_prims_in_obb(self, center: list[float], half_extents: list[float], w_dir: list[float], d_dir: list[float]) -> list[Usd.Prim]:
        """
        Performs a spatial overlap query using an Oriented Bounding Box (OBB).
        Uses mathematical vector projection against world-space coordinates,
        guaranteeing detection even for decorative assets lacking collision meshes.
        """
        stage = omni.usd.get_context().get_stage()
        world = stage.GetPrimAtPath("/World")
        if not world:
            return []

        u = Gf.Vec3d(*w_dir).GetNormalized()
        v = Gf.Vec3d(*d_dir).GetNormalized()
        w = Gf.Cross(u, v).GetNormalized()

        c = Gf.Vec3d(*center)
        eu, ev, ew = half_extents

        found_prims = []

        # Initialize a BBoxCache to calculate the geometric bounds of the prims
        bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])

        def _scan(prim, depth):
            if depth > 4:
                return

            try:
                # 1. Compute the world-space bounding box of the prim's geometry
                world_bound = bbox_cache.ComputeWorldBound(prim)
                aligned_range = world_bound.ComputeAlignedRange()

                if not aligned_range.IsEmpty():
                    min_pt = aligned_range.GetMin()
                    max_pt = aligned_range.GetMax()

                    # 2. Extract the 8 corners of the bounding box
                    corners = [
                        Gf.Vec3d(x, y, z)
                        for x in (min_pt[0], max_pt[0])
                        for y in (min_pt[1], max_pt[1])
                        for z in (min_pt[2], max_pt[2])
                    ]

                    # Add the center point in case the OBB is entirely inside a massive prim
                    corners.append((min_pt + max_pt) / 2.0)

                    # 3. Check if ANY corner (or the center) falls inside the OBB
                    for pt in corners:
                        d = pt - c
                        if (abs(Gf.Dot(d, u)) <= eu) and (abs(Gf.Dot(d, v)) <= ev) and (abs(Gf.Dot(d, w)) <= ew):
                            found_prims.append(prim)
                            return # Stop recursing; root object overlaps
            except Exception:
                pass

            for child in prim.GetChildren():
                _scan(child, depth + 1)

        for child in world.GetChildren():
            _scan(child, 1)

        return found_prims

    def _is_structural_mesh(self, prim: Usd.Prim) -> bool:
        """
        Safety filter to prevent the restocking logic from accidentally
        deleting store fixtures. Ignores all assets inside the environment folder.
        """
        path = str(prim.GetPath())

        # Explicitly ignore the architecture/store folder
        if path.startswith("/World/_dstore"):
            return True

        # Explicitly ignore all prototype prims
        if path.startswith("/World/Prototypes"):
            return True

        # Fallback keyword safety checks for loose prims
        name = prim.GetName().lower()
        key = self._get_prim_asset_key(prim)
        structural_keywords = ["shelf", "rack", "floor", "wall", "ceiling", "prototype"]

        if any(keyword in name for keyword in structural_keywords):
            return True
        if key and any(keyword in key for keyword in structural_keywords):
            return True

        return False

    def _get_prim_asset_key(self, prim: Usd.Prim) -> str:
        """Wrapper around the global resolve function to extract an asset key."""
        return _resolve_prim_key(prim.GetName()) or ""

    def _remove_foreign_prim(self, prim: Usd.Prim) -> None:
        """
        Permanently removes a specific foreign object from the physical stage.
        We cannot use _delete_usd here, as that deletes the "last spawned"
        item, whereas we need to delete THIS specific physical intrusion.
        """
        stage = omni.usd.get_context().get_stage()
        path = str(prim.GetPath())

        if stage.RemovePrim(path):
            self._inventory_remove([path])
            carb.log_info(f"[UsdSpawner] Cleared foreign object during restock: {path}")

    def _translate_prim(self, prim: Usd.Prim, pos: tuple[float, float, float]) -> None:
        """
        Updates the world translation (position) of a USD prim
        and safely syncs the new coordinates to the persistent JSON inventory.
        """
        xform = UsdGeom.Xformable(prim)
        translate_op = next((op for op in xform.GetOrderedXformOps() if op.GetOpType() == UsdGeom.XformOp.TypeTranslate), None)

        if translate_op is None:
            translate_op = xform.AddTranslateOp()

        translate_op.Set(Gf.Vec3d(*pos))

        # Synchronize location state with the inventory tracking file
        prim_path = str(prim.GetPath())
        inv = self._load_inventory()

        if prim_path in inv:
            inv[prim_path]["position"] = [round(pos[0], 3), round(pos[1], 3), round(pos[2], 3)]
            self._save_inventory(inv)


    def _rotate_prim_to_face_shelf_front(self, prim: Usd.Prim, d_dir: list[float]) -> None:
        """
        Forces a prim to face perfectly outward from the shelf.
        Assumes the asset is natively modeled facing the +X axis. Calculates
        the absolute Z-yaw required to align +X with the shelf's front normal.

        Args:
            prim: The USD prim to rotate.
            d_dir: The depth vector of the shelf (pointing from back to front).
        """
        xform = UsdGeom.Xformable(prim)

        # Find the existing RotateXYZ operation, or add one if it doesn't exist
        rotate_op = next((op for op in xform.GetOrderedXformOps()
                        if op.GetOpType() == UsdGeom.XformOp.TypeRotateXYZ), None)

        if rotate_op is None:
            rotate_op = xform.AddRotateXYZOp()

        # The shelf "front" normal is the depth direction
        target_front_x = d_dir[0]
        target_front_y = d_dir[1]

        # Calculate yaw angle from +X [1, 0, 0] to the target vector using atan2
        import math
        yaw_rad = math.atan2(target_front_y, target_front_x)
        yaw_deg = math.degrees(yaw_rad)

        # Apply absolute mathematical orientation purely around the Z (Up) axis
        rotate_op.Set(Gf.Vec3f(0.0, 0.0, yaw_deg))

    # ------------------------------------------------------------------
    # Backend HTTP helper
    # ------------------------------------------------------------------

    _BACKEND_URL = _cfg.get("backend_url", "http://localhost:8000").rstrip("/")

    def _send_to_backend(self, path: str, method: str, payload: dict = {}) -> tuple[bool, any]:
        import urllib.request as _ur
        url = f"{self._BACKEND_URL}{path}"
        method = method.upper().strip()
        data = json.dumps(payload).encode("utf-8") if method not in ("GET", "HEAD") else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        try:
            req = _ur.Request(url, data=data, headers=headers, method=method)
            with _ur.urlopen(req, timeout=5) as resp:
                body = resp.read().decode()
                carb.log_info(f"[UsdSpawner] {method} {path} → {self._BACKEND_URL}: {body}")
                try:
                    return True, json.loads(body)
                except json.JSONDecodeError:
                    return True, body
        except Exception as exc:
            carb.log_warn(f"[UsdSpawner] {method} {path} to {self._BACKEND_URL} failed: {exc}")
        return False, None


    # ------------------------------------------------------------------
    # Inventory helpers — persistent JSON tracking of spawned prims
    # ------------------------------------------------------------------

    def _load_inventory(self) -> dict:
        """Load the inventory JSON from disk. Returns {} on error."""
        try:
            if os.path.exists(INVENTORY_FILE):
                with open(INVENTORY_FILE, "r") as f:
                    return json.load(f)
        except Exception as exc:
            carb.log_warn(f"[UsdSpawner] Could not load inventory: {exc}")
        return {}

    def _save_inventory(self, inventory: dict) -> None:
        """Write the inventory JSON to disk."""
        try:
            os.makedirs(os.path.dirname(INVENTORY_FILE), exist_ok=True)
            with open(INVENTORY_FILE, "w") as f:
                json.dump(inventory, f, indent=2)
        except Exception as exc:
            carb.log_warn(f"[UsdSpawner] Could not save inventory: {exc}")

    def _inventory_add(self, prim_path: str, asset_key: str, usd_path: str,
                       position: "Gf.Vec3d", rack_id: str = None, shelf_row: int = None) -> None:
        """Record a newly spawned prim in the inventory file and notify backend DB."""
        inv = self._load_inventory()
        inv[prim_path] = {
            "asset_key":  asset_key,
            "asset_name": asset_key.replace("_", " ").title(),
            "usd_path":   usd_path,
            "position":   [round(position[0], 3), round(position[1], 3),
                           round(position[2], 3)],
            "spawned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._save_inventory(inv)
        carb.log_info(f"[UsdSpawner] Inventory: added {prim_path}")

        # Notify backend database
        self._send_to_backend("/api/inventory", "POST", {
            "prim_path": prim_path,
            "asset_key": asset_key,
            "pos_x": round(position[0], 3),
            "pos_y": round(position[1], 3),
            "pos_z": round(position[2], 3),
            "rack_id": rack_id,
            "shelf_row": shelf_row,
            "unit_count": 1,
        })

    def _inventory_remove(self, prim_paths) -> None:
        """Remove one or more prim paths from both inventory files and notify backend DB."""
        if isinstance(prim_paths, str):
            prim_paths = [prim_paths]
        for inv_path in (INVENTORY_FILE, STAGE_PRIMS_FILE):
            try:
                if not os.path.exists(inv_path):
                    continue
                with open(inv_path, "r") as f:
                    inv = json.load(f)
                changed = False
                for pp in prim_paths:
                    if pp in inv:
                        del inv[pp]
                        changed = True
                        carb.log_info(f"[UsdSpawner] Inventory ({os.path.basename(inv_path)}): removed {pp}")
                if changed:
                    with open(inv_path, "w") as f:
                        json.dump(inv, f, indent=2)
            except Exception as exc:
                carb.log_warn(f"[UsdSpawner] Could not update {inv_path}: {exc}")

        # Notify backend database
        self._send_to_backend("/api/inventory/delete", "POST", {
            "prim_paths": list(prim_paths),
        })

    def _inventory_find_by_brand(self, brand_or_key: str) -> list:
        """
        Return all prim paths whose asset_key matches brand_or_key.

        Searches both inventory files:
          - asset_list_shop_already.json  (chatbot-spawned items)
          - stage_prims.json              (full stage scan sent by browser on load)

        Matching rules:
          1. Exact asset_key match  (e.g. "pringles_cheese")
          2. Brand-group prefix     (e.g. "pringles" → pringles_cheese, pringles_bbq ...)
        """
        matched = []
        for inv_path in (INVENTORY_FILE, STAGE_PRIMS_FILE):
            try:
                if not os.path.exists(inv_path):
                    continue
                with open(inv_path, "r") as f:
                    inv = json.load(f)
                for prim_path, info in inv.items():
                    ak = info.get("asset_key", "")
                    if ak and (ak == brand_or_key or ak.startswith(brand_or_key + "_")):
                        if prim_path not in matched:
                            matched.append(prim_path)
            except Exception as exc:
                carb.log_warn(f"[UsdSpawner] Could not read {inv_path}: {exc}")
        return matched

    def _find_prims_by_asset_key(self, asset_key: str) -> list:
        """
        Return ALL /World/* prim paths that belong to the given asset_key.

        Combines results from every available source — no early return — so
        pre-placed stage items, chatbot-spawned items, and inventory entries
        are all included:
          1. In-memory _spawned_prims dict
          2. Both inventory files (asset_list_shop_already.json + stage_prims.json)
          3. Live stage scan using USD filename stem from ASSET_LIBRARY
        """
        stage = omni.usd.get_context().get_stage()
        found: set = set()

        # 1. In-memory tracker
        for p in self._spawned_prims.get(asset_key, []):
            if stage.GetPrimAtPath(p).IsValid():
                found.add(p)

        # 2. Inventory files
        for p in self._inventory_find_by_brand(asset_key):
            if stage.GetPrimAtPath(p).IsValid():
                found.add(p)

        # 3. Live stage scan via USD filename stem (recursive — handles shelf groups)
        stem = _KEY_TO_STEM.get(asset_key, "")
        if stem:
            stem_lower     = stem.lower()
            safe_stem_lower = stem.replace("-", "_").lower()

            def _scan_for_stem(parent_prim, depth: int = 0) -> None:
                if depth > 4:
                    return
                for c in parent_prim.GetChildren():
                    path_str = str(c.GetPath())
                    # Do not include hidden prototype assets in results
                    if "/Prototypes/" in path_str:
                        continue
                    name       = c.GetName()
                    name_lower = name.lower()
                    base_lower = re.sub(r"_\d+$", "", name_lower)
                    if name_lower in (stem_lower, safe_stem_lower) or \
                       base_lower in (stem_lower, safe_stem_lower):
                        found.add(str(c.GetPath()))
                    else:
                        _scan_for_stem(c, depth + 1)

            world = stage.GetPrimAtPath("/World")
            if world:
                _scan_for_stem(world)

        result = sorted(found)
        carb.log_info(
            f"[UsdSpawner] _find_prims_by_asset_key('{asset_key}'): "
            f"{len(result)} prim(s) total"
        )
        return result

    # ------------------------------------------------------------------
    # Delete helpers
    # ------------------------------------------------------------------

    def _delete_usd(self, asset_key: str) -> tuple:
        """
        Remove the most recently spawned prim for the given asset key.
        Returns (success: bool, prim_path_or_error: str).
        """
        stage = omni.usd.get_context().get_stage()

        # 1. Check in-memory spawned prims (Current Session) ---
        tracked_paths = self._spawned_prims.get(asset_key, [])
        valid_tracked = [p for p in tracked_paths if stage.GetPrimAtPath(p).IsValid()]
        self._spawned_prims[asset_key] = valid_tracked  # Clean up dead paths

        if valid_tracked:
            target_path = valid_tracked[-1]
            if stage.RemovePrim(target_path):
                self._spawned_prims[asset_key].remove(target_path)
                carb.log_info(f"[UsdSpawner] Deleted tracked prim: {target_path}")
                return True, target_path
            return False, f"Failed to remove tracked prim at {target_path}"

        # 2. FALLBACK: Include also prims from the stage scan in the candidate set.
        existing_paths = self._find_prims_by_asset_key(asset_key)

        if not existing_paths:
            carb.log_warn(f"[UsdSpawner] Could not find any prims to delete for '{asset_key}'")
            return False, f"No prims found for {asset_key}"

        # Grab the highest-numbered prim on the stage
        target_path = existing_paths[-1]

        if stage.RemovePrim(target_path):
            self._inventory_remove(target_path)
            carb.log_info(f"[UsdSpawner] Successfully deleted fallback prim: {target_path}")
            return True, target_path

        return False, f"Failed to remove prim at {target_path}"

    def _on_delete_request(self, event) -> None:
        payload = getattr(event, "payload", event)
        prim_name  = str(payload.get("prim_name", ""))
        prim_path  = str(payload.get("prim_path", ""))   # single direct path
        prim_paths = payload.get("prim_paths", [])        # batch: list of direct paths
        delete_all = bool(payload.get("delete_all", False))  # delete ALL of named type

        carb.log_info(
            f"[UsdSpawner] deleteUsdRequest  name={prim_name}  path={prim_path}"
            f"  batch={len(prim_paths)}  delete_all={delete_all}"
        )

        stage = omni.usd.get_context().get_stage()

        # ── Batch delete: list of direct paths (multi-selection) ──────────────
        if prim_paths:
            deleted = []
            for pp in prim_paths:
                if stage.GetPrimAtPath(pp).IsValid():
                    stage.RemovePrim(pp)
                    deleted.append(pp)
                    carb.log_info(f"[UsdSpawner] Batch-deleted '{pp}'")
            if deleted:
                self._inventory_remove(deleted)
            self._reply_delete({
                "result":    "success" if deleted else "error",
                "prim_path": deleted[0] if deleted else "",
                "count":     len(deleted),
                "error":     "" if deleted else "None of the specified prims exist on stage",
            })
            return

        # ── Delete all instances of a named brand/type from stage ─────────────
        if prim_name and delete_all:
            # Resolve all asset keys belonging to this brand/key.
            # e.g. "pringles" → ["pringles_cheese","pringles_bbq","pringles_pizza","pringles_lobster"]
            related_keys = _BRAND_GROUPS.get(prim_name, [])
            if not related_keys:
                # prim_name is already a specific key, not a brand prefix
                related_keys = [prim_name]

            # Collect every matching prim across all sources (inventory + stage scan),
            # using _find_prims_by_asset_key which is case-insensitive and exhaustive.
            to_delete_set: set = set()
            for rk in related_keys:
                for p in self._find_prims_by_asset_key(rk):
                    to_delete_set.add(p)

            # Also search by brand prefix directly (catches exact prim_name as asset_key)
            if prim_name not in related_keys:
                for p in self._find_prims_by_asset_key(prim_name):
                    to_delete_set.add(p)

            valid = sorted(to_delete_set)
            carb.log_info(
                f"[UsdSpawner] delete_all '{prim_name}': "
                f"found {len(valid)} prim(s) to delete: {valid}"
            )

            for pp in valid:
                stage.RemovePrim(pp)
            if valid:
                self._inventory_remove(valid)
            # Clear in-memory tracker for all related keys
            for rk in related_keys:
                self._spawned_prims.pop(rk, None)
            self._spawned_prims.pop(prim_name, None)

            self._reply_delete({
                "result":    "success" if valid else "error",
                "prim_path": valid[0] if valid else "",
                "count":     len(valid),
                "error":     "" if valid else f"Could not remove: No '{prim_name}' prims found on stage",
            })
            return

        # ── Single direct path (selected-prim delete) ─────────────────────────
        if prim_path:
            if stage.GetPrimAtPath(prim_path).IsValid():
                stage.RemovePrim(prim_path)
                self._inventory_remove(prim_path)
                carb.log_info(f"[UsdSpawner] Deleted selected prim '{prim_path}'")
                self._reply_delete({"result": "success", "prim_path": prim_path, "count": 1, "error": ""})
            else:
                self._reply_delete({"result": "error", "prim_path": "",
                                    "count": 0, "error": f"Prim not found: {prim_path}"})
            return

        # ── Single named asset (most-recent instance) ─────────────────────────
        if not prim_name:
            self._reply_delete({"result": "error", "count": 0,
                                 "error": "No prim_name, prim_path, or prim_paths provided",
                                 "prim_path": ""})
            return

        success, result = self._delete_usd(prim_name)
        if success:
            self._reply_delete({"result": "success", "prim_path": result, "count": 1, "error": ""})
        else:
            self._reply_delete({"result": "error", "prim_path": "", "count": 0, "error": result})

    # ------------------------------------------------------------------
    # Replace handler
    # ------------------------------------------------------------------

    def _on_replace_request(self, event) -> None:
        """
        replaceUsdRequest payload:
          { target_prim_path: str, prim_name: str }

        Steps:
          1. Get the world-space position of target_prim_path (outer Xform translate).
          2. Remove target_prim_path from the stage.
          3. Spawn the new asset at that same position.
        """
        payload          = event.payload
        target_path      = str(payload.get("target_prim_path", ""))
        asset_key        = str(payload.get("prim_name", ""))

        carb.log_info(
            f"[UsdSpawner] replaceUsdRequest  target={target_path}  new={asset_key}"
        )

        if not target_path or not asset_key:
            self._reply_replace({
                "result": "error",
                "error": "target_prim_path and prim_name are required",
                "prim_path": "", "position": [0, 0, 0],
            })
            return

        usd_path = ASSET_LIBRARY.get(asset_key)
        if not usd_path:
            self._reply_replace({
                "result": "error",
                "error": f"No USD path for asset '{asset_key}'. Add it to ASSET_LIBRARY.",
                "prim_path": "", "position": [0, 0, 0],
            })
            return

        stage = omni.usd.get_context().get_stage()
        target_prim = stage.GetPrimAtPath(target_path)
        if not target_prim or not target_prim.IsValid():
            self._reply_replace({
                "result": "error",
                "error": f"Target prim not found on stage: {target_path}",
                "prim_path": "", "position": [0, 0, 0],
            })
            return

        # Extract the world-space position and rotation of the target prim.
        # Always use XformCache so shelf-nested items (whose local translate is
        # relative to the parent shelf group) are placed correctly in /World.
        # Copy world rotation so replacement inherits the same orientation.
        position = Gf.Vec3d(0.0, 0.0, 0.0)
        rotation = None
        try:
            xf_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
            world_xf = xf_cache.GetLocalToWorldTransform(target_prim)
            position = Gf.Vec3d(world_xf.ExtractTranslation())
            rotation = world_xf.ExtractRotation()
            carb.log_warn(
                f"[UsdSpawner] Replace: world pos={position}  rot={rotation}  "
                f"from '{target_path}'"
            )
        except Exception as pos_err:
            carb.log_warn(f"[UsdSpawner] Replace: could not extract world transform: {pos_err}")

        # Determine whether the target is a pre-placed (shelf-nested) item.
        # Pre-placed items have their local origin AT the shelf surface, so
        # their world Y IS the shelf surface.  Snap the new item's bottom
        # to that Y so it sits on the shelf instead of being half-embedded.
        path_parts = target_path.split("/")   # ["", "World", "Shelf_N", "Item"]
        snap_y_to = position[1] if len(path_parts) > 3 else None

        # Remove the target prim and its inventory entry
        stage.RemovePrim(target_path)
        self._inventory_remove(target_path)
        carb.log_warn(f"[UsdSpawner] Replace: removed '{target_path}'  snap_y_to={snap_y_to}")

        # Spawn the new asset at the original world position with the same rotation.
        try:
            new_prim_path = self._spawn_usd(usd_path, asset_key, position, rotation=rotation, snap_y_to=snap_y_to)
            self._reply_replace({
                "result":    "success",
                "prim_path": new_prim_path,
                "position":  [round(position[0], 3), round(position[1], 3),
                               round(position[2], 3)],
                "error":     "",
            })
        except Exception as exc:
            carb.log_error(f"[UsdSpawner] Replace spawn failed: {exc}")
            self._reply_replace({
                "result": "error", "error": str(exc),
                "prim_path": "", "position": [0, 0, 0],
            })

    # ------------------------------------------------------------------
    # Batch replace handler
    # ------------------------------------------------------------------

    def _on_replace_all_request(self, event) -> None:
        """
        replaceAllUsdRequest payload:
          { source_paths: ["/World/NittoTea_Royal_Milktea", ...], prim_name: "pringles_cheese" }

        For each source path:
          1. Read the world-space position of the existing prim.
          2. Remove it from stage + inventory.
          3. Spawn the new asset at the same position.
        Replies with replaceAllUsdResponse { result, count, prim_paths, error }.
        """
        payload      = event.payload
        source_paths = list(payload.get("source_paths", []))
        source_key   = str(payload.get("source_key", ""))
        asset_key    = str(payload.get("prim_name", ""))

        carb.log_info(
            f"[UsdSpawner] replaceAllUsdRequest  target={asset_key}  "
            f"source_key={source_key}  backend_paths={len(source_paths)}: {source_paths}"
        )
        carb.log_info(f"[UsdSpawner] _spawned_prims: { {k: v for k, v in self._spawned_prims.items()} }")

        # Expand source_key to all brand-group members.
        # e.g. source_key="pringles_cheese" → also scan pringles_bbq, pringles_pizza, pringles_lobster.
        # e.g. source_key="pringles"        → scan all four pringles variants.
        related_source_keys: list = _BRAND_GROUPS.get(source_key, [])
        if not related_source_keys:
            # source_key is a specific key — check if it belongs to a brand group
            for _brand, _keys in _BRAND_GROUPS.items():
                if source_key in _keys:
                    related_source_keys = list(_keys)
                    break
        if not related_source_keys:
            related_source_keys = [source_key]

        # Always do an exhaustive Kit-side scan combining:
        #   - paths supplied by the backend (possibly stale after prior operations)
        #   - live stage scan for every related asset key (catches newly spawned prims)
        all_source: set = set(source_paths)
        for rk in related_source_keys:
            for p in self._find_prims_by_asset_key(rk):
                all_source.add(p)
        # If none of the above matched, also try source_key itself directly
        if not all_source and source_key:
            for p in self._find_prims_by_asset_key(source_key):
                all_source.add(p)
        source_paths = sorted(all_source)

        carb.log_warn(
            f"[UsdSpawner] replaceAllUsdRequest after expansion: "
            f"{len(source_paths)} prim(s) to replace: {source_paths}  →  new='{asset_key}'  usd='{ASSET_LIBRARY.get(asset_key)}'"
        )

        usd_path = ASSET_LIBRARY.get(asset_key)
        if not usd_path:
            get_eventdispatcher().dispatch_event("replaceAllUsdResponse", payload={
                "result": "error", "count": 0, "prim_paths": [],
                "error": f"No USD path for asset '{asset_key}'. Add it to ASSET_LIBRARY.",
            })
            return

        stage    = omni.usd.get_context().get_stage()
        replaced = []
        failed   = []

        for target_path in source_paths:
            target_prim = stage.GetPrimAtPath(target_path)
            if not target_prim or not target_prim.IsValid():
                carb.log_warn(f"[UsdSpawner] ReplaceAll: prim not on stage: {target_path}")
                failed.append(target_path)
                continue

            # Extract world-space position and rotation of the existing prim.
            # Always use XformCache — shelf-nested items have a LOCAL translate op
            # that is relative to their parent shelf group, not to /World.
            # Copy world rotation so the replacement inherits the same orientation.
            position = Gf.Vec3d(0.0, 0.0, 0.0)
            rotation = None
            try:
                xf_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
                world_xf = xf_cache.GetLocalToWorldTransform(target_prim)
                position = Gf.Vec3d(world_xf.ExtractTranslation())
                rotation = world_xf.ExtractRotation()
                carb.log_warn(
                    f"[UsdSpawner] ReplaceAll: world pos={position}  rot={rotation}  "
                    f"for '{target_path}'"
                )
            except Exception as pos_err:
                carb.log_warn(f"[UsdSpawner] ReplaceAll: world transform read failed for {target_path}: {pos_err}")

            # Determine whether the target is a pre-placed (shelf-nested) item.
            # Pre-placed items have their local origin AT the shelf surface, so
            # their world Y IS the shelf surface.  Snap the new item's bottom
            # to that Y so it sits on the shelf instead of being half-embedded.
            # Spawned items (/World/item) have their pivot at their geometric
            # center — use center-snap instead (handled in _spawn_usd).
            path_parts = target_path.split("/")  # ["", "World", "Shelf_N", "Item"]
            snap_y_to = position[1] if len(path_parts) > 3 else None

            # Remove old prim
            stage.RemovePrim(target_path)
            self._inventory_remove(target_path)

            # Spawn new asset at the original world position with the same rotation.
            try:
                new_path = self._spawn_usd(usd_path, asset_key, position, rotation=rotation, snap_y_to=snap_y_to)
                replaced.append(new_path)
                carb.log_warn(f"[UsdSpawner] ReplaceAll: OK  {target_path} → {new_path}  pos={position}")
            except Exception as spawn_err:
                carb.log_warn(f"[UsdSpawner] ReplaceAll: SPAWN FAILED for {target_path}: {spawn_err}")
                import traceback
                carb.log_warn(f"[UsdSpawner] ReplaceAll: traceback: {traceback.format_exc()}")
                failed.append(target_path)

        get_eventdispatcher().dispatch_event("replaceAllUsdResponse", payload={
            "result":    "success" if replaced else "error",
            "count":     len(replaced),
            "prim_paths": replaced,
            "error":     "" if replaced else f"Failed to replace any of {len(source_paths)} prims",
        })

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    def _on_spawn_request(self, event) -> None:
        payload = event.payload
        screen_x  = float(payload.get("screen_x",  0.5))
        screen_y  = float(payload.get("screen_y",  0.5))
        asset_key = str(payload.get("prim_name", "SpawnedAsset"))

        # Resolve asset path: prefer local ASSET_LIBRARY (Kit controls paths),
        # fall back to whatever usd_path the browser forwarded.
        usd_path = ASSET_LIBRARY.get(asset_key) or str(payload.get("usd_path", ""))

        carb.log_info(
            f"[UsdSpawner] spawnUsdRequest  screen=({screen_x:.3f},{screen_y:.3f})"
            f"  name={asset_key}  resolved_path={usd_path}"
        )

        if not usd_path:
            self._reply({"result": "error",
                         "error": f"No USD path for asset '{asset_key}'. "
                                   "Add it to ASSET_LIBRARY in usd_spawner.py.",
                         "prim_path": "", "position": [0, 0, 0]})
            return

        position = self._compute_world_position(screen_x, screen_y)
        if position is None:
            self._reply({"result": "error", "error": "Could not compute world position",
                         "prim_path": "", "position": [0, 0, 0]})
            return

        try:
            prim_path = self._spawn_usd(usd_path, asset_key, position)
            self._reply({
                "result":    "success",
                "prim_path": prim_path,
                "position":  [round(position[0], 3), round(position[1], 3),
                               round(position[2], 3)],
                "error":     "",
            })
        except Exception as exc:
            carb.log_error(f"[UsdSpawner] Spawn failed: {exc}")
            self._reply({"result": "error", "error": str(exc),
                         "prim_path": "", "position": [0, 0, 0]})

    # ------------------------------------------------------------------

    def _on_adjust_asset_rotation(self, event) -> None:
        """
        Browser Rotation Adjustment Panel → adjustAssetRotation event.

        Finds all /World/* prims whose prim name starts with prim_name and
        sets their orient op to the Euler angles (X, Y, Z degrees) received.
        The rotation is applied as an absolute orientation (ZYX order: Z first,
        then Y, then X) so re-running always gives a predictable result.
        """
        payload   = event.payload
        prim_name = str(payload.get("prim_name", ""))
        euler_x   = float(payload.get("euler_x", 0))
        euler_y   = float(payload.get("euler_y", 0))
        euler_z   = float(payload.get("euler_z", 0))
        offset_x  = float(payload.get("offset_x", 0))
        offset_y  = float(payload.get("offset_y", 0))
        offset_z  = float(payload.get("offset_z", 0))

        carb.log_warn(
            f"[UsdSpawner] adjustAssetRotation  prim_name={prim_name}"
            f"  rot=({euler_x}°,{euler_y}°,{euler_z}°)"
            f"  offset=({offset_x},{offset_y},{offset_z}) cm"
        )

        if not prim_name:
            carb.log_warn("[UsdSpawner] adjustAssetRotation: missing prim_name")
            return

        # Compute delta offset vs previously saved values (to update existing prims correctly).
        prev = self._rotation_corrections.get(prim_name, {})
        prev_ox = float(prev.get("offset_x", 0))
        prev_oy = float(prev.get("offset_y", 0))
        prev_oz = float(prev.get("offset_z", 0))
        delta_x = offset_x - prev_ox
        delta_y = offset_y - prev_oy
        delta_z = offset_z - prev_oz

        # Persist to JSON so future spawns use these values.
        self._rotation_corrections[prim_name] = {
            "euler_x": euler_x,
            "euler_y": euler_y,
            "euler_z": euler_z,
            "offset_x": offset_x,
            "offset_y": offset_y,
            "offset_z": offset_z,
        }
        _save_rotation_corrections(self._rotation_corrections)

        stage = omni.usd.get_context().get_stage()
        if not stage:
            carb.log_warn("[UsdSpawner] adjustAssetRotation: no stage")
            return

        world = stage.GetPrimAtPath("/World")
        if not world:
            carb.log_warn("[UsdSpawner] adjustAssetRotation: /World not found")
            return

        # Build absolute quaternion from ZYX Euler angles
        rot_z = Gf.Rotation(Gf.Vec3d(0, 0, 1), euler_z)
        rot_y = Gf.Rotation(Gf.Vec3d(0, 1, 0), euler_y)
        rot_x = Gf.Rotation(Gf.Vec3d(1, 0, 0), euler_x)
        composed = rot_x * rot_y * rot_z
        cq = composed.GetQuaternion()
        new_q = Gf.Quatd(cq.GetReal(), Gf.Vec3d(cq.GetImaginary()))

        updated = 0
        for prim in world.GetAllChildren():
            if not prim.GetName().startswith(prim_name):
                continue
            xform = UsdGeom.Xform(prim)
            ops = xform.GetOrderedXformOps()

            # Update rotation
            orient_op = next(
                (op for op in ops if op.GetOpType() == UsdGeom.XformOp.TypeOrient),
                None,
            )
            if orient_op is not None:
                orient_op.Set(new_q)
            else:
                orient_op = xform.AddOrientOp(UsdGeom.XformOp.PrecisionDouble)
                orient_op.Set(new_q)

            # Apply translation delta (offset change since last save)
            translate_op = next(
                (op for op in ops if op.GetOpType() == UsdGeom.XformOp.TypeTranslate),
                None,
            )
            if translate_op is not None and (delta_x or delta_y or delta_z):
                cur = translate_op.Get(Usd.TimeCode.Default())
                translate_op.Set(Gf.Vec3d(
                    cur[0] + delta_x,
                    cur[1] + delta_y,
                    cur[2] + delta_z,
                ))

            carb.log_warn(
                f"[UsdSpawner] adjustAssetRotation  updated {prim.GetPath()}"
                f"  q={new_q}  translate_delta=({delta_x},{delta_y},{delta_z})"
            )
            updated += 1

        carb.log_warn(
            f"[UsdSpawner] adjustAssetRotation done — {updated} prim(s) updated"
        )

    # ------------------------------------------------------------------

    def _on_adjust_asset_scale(self, event) -> None:
        """
        Browser Scale Adjustment Panel → adjustAssetScale event.

        Finds all /World/* prims whose name starts with prim_name and sets
        their xformOp:scale to (scale_x, scale_y, scale_z).  Values are
        persisted to scale_corrections.json so future spawns apply them too.
        """
        payload   = event.payload
        prim_name = str(payload.get("prim_name", ""))
        scale_x   = float(payload.get("scale_x", 1.0))
        scale_y   = float(payload.get("scale_y", 1.0))
        scale_z   = float(payload.get("scale_z", 1.0))

        carb.log_warn(
            f"[UsdSpawner] adjustAssetScale  prim_name={prim_name}"
            f"  scale=({scale_x},{scale_y},{scale_z})"
        )

        if not prim_name:
            carb.log_warn("[UsdSpawner] adjustAssetScale: missing prim_name")
            return

        # Persist so future spawns of this asset use the correction.
        self._scale_corrections[prim_name] = {
            "scale_x": scale_x,
            "scale_y": scale_y,
            "scale_z": scale_z,
        }
        _save_scale_corrections(self._scale_corrections)

        stage = omni.usd.get_context().get_stage()
        if not stage:
            carb.log_warn("[UsdSpawner] adjustAssetScale: no stage")
            return
        world = stage.GetPrimAtPath("/World")
        if not world:
            carb.log_warn("[UsdSpawner] adjustAssetScale: /World not found")
            return

        new_scale = Gf.Vec3f(scale_x, scale_y, scale_z)
        updated = 0

        for prim in world.GetAllChildren():
            if not prim.GetName().startswith(prim_name):
                continue
            xform = UsdGeom.Xform(prim)
            ops = xform.GetOrderedXformOps()

            scale_op = next(
                (op for op in ops if op.GetOpType() == UsdGeom.XformOp.TypeScale), None,
            )
            if scale_op is not None:
                scale_op.Set(new_scale)
            else:
                scale_op = xform.AddScaleOp(UsdGeom.XformOp.PrecisionFloat)
                scale_op.Set(new_scale)

            carb.log_warn(
                f"[UsdSpawner] adjustAssetScale  updated {prim.GetPath()}"
                f"  scale={new_scale}"
            )
            updated += 1

        carb.log_warn(f"[UsdSpawner] adjustAssetScale done — {updated} prim(s) updated")

    # ------------------------------------------------------------------
    # Shelf-row detection
    # ------------------------------------------------------------------

    def _reply_shelf_rows(self, payload: dict) -> None:
        get_eventdispatcher().dispatch_event("detectShelfRowsResponse", payload=payload)

    def detect_rows_for_key(
        self, filter_key: str | None, tolerance: float = 8.0
    ) -> dict:
        """
        Core shelf-row detection for a single asset_key (or all assets if None).
        Returns the result dict, or None if the stage is unavailable.
        Called directly from CustomMessageManager for multi-product analysis.
        """
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return None

        up_axis  = self._detect_up_axis()
        up_idx   = 2 if up_axis == "Z" else 1
        xf_cache = UsdGeom.XformCache(Usd.TimeCode.Default())

        world = stage.GetPrimAtPath("/World")
        if not world:
            return None

        def _get_height(prim) -> float | None:
            try:
                return round(
                    xf_cache.GetLocalToWorldTransform(prim).ExtractTranslation()[up_idx], 2
                )
            except Exception:
                return None

        filtered_floors: list[tuple[str, float]] = []
        for prim in world.GetChildren():
            key = _resolve_prim_key(prim.GetName())
            if not key:
                continue
            if filter_key and key != filter_key:
                continue
            h = _get_height(prim)
            if h is not None:
                filtered_floors.append((str(prim.GetPath()), h))

        if not filtered_floors:
            return {"up_axis": up_axis, "tolerance_cm": tolerance,
                    "row_count": 0, "asset_key": filter_key or "all", "rows": []}

        ref_rows: list[tuple[float, int]] = []
        if filter_key:
            try:
                with open(_shelf_rowS_FILE) as _rf:
                    _prev = json.load(_rf)
                if _prev.get("asset_key") == filter_key:
                    ref_rows = [(_r["floor_z"], _r["row"]) for _r in _prev.get("rows", [])]
            except Exception:
                pass

        def _row_num_from_ref(h: float) -> int | None:
            if not ref_rows:
                return None
            nearest_z, nearest_row = min(ref_rows, key=lambda t: abs(t[0] - h))
            return nearest_row if abs(nearest_z - h) <= tolerance * 4 else None

        clusters = _gap_cluster(filtered_floors, tolerance)
        clusters.sort(key=lambda g: g[0][1], reverse=True)

        rows = []
        for seq_num, group in enumerate(clusters, start=1):
            floor_z = round(sum(v for _, v in group) / len(group), 2)
            row_num = _row_num_from_ref(floor_z) or seq_num
            rows.append({
                "row":        row_num,
                "floor_z":    floor_z,
                "z_min":      group[0][1],
                "z_max":      group[-1][1],
                "prim_count": len(group),
                "prim_paths": [p for p, _ in group],
            })

        return {
            "up_axis":      up_axis,
            "tolerance_cm": tolerance,
            "row_count":    len(rows),
            "asset_key":    filter_key or "all",
            "rows":         rows,
        }

    def _on_detect_shelf_rows(self, event) -> None:
        """Handle detectShelfRowsRequest — resolve filter_key, detect, save, respond."""
        payload   = dict(event.payload)
        tolerance = float(payload.get("tolerance_cm", 8.0))
        sel_path  = payload.get("selected_prim_path", None)

        filter_key: str | None = None
        if sel_path:
            sel_parts  = sel_path.strip("/").split("/")
            sel_name   = sel_parts[1] if len(sel_parts) >= 2 else sel_parts[0]
            filter_key = _resolve_prim_key(sel_name)
            carb.log_warn(
                f"[UsdSpawner] detectShelfRows: "
                f"{'filtering to ' + repr(filter_key) if filter_key else 'scanning all (no key for ' + repr(sel_name) + ')'}"
            )

        result = self.detect_rows_for_key(filter_key, tolerance)

        if result is None:
            self._reply_shelf_rows({"error": "No stage loaded", "rows": []})
            return

        if not result["rows"]:
            self._reply_shelf_rows({**result, "message": "No matching asset prims found on stage"})
            return

        try:
            os.makedirs(os.path.dirname(_shelf_rowS_FILE), exist_ok=True)
            with open(_shelf_rowS_FILE, "w") as f:
                json.dump(result, f, indent=2)
        except Exception as exc:
            carb.log_warn(f"[UsdSpawner] Could not save shelf_rows.json: {exc}")

        self._reply_shelf_rows(result)

    # ------------------------------------------------------------------
    # Replace a specific shelf row via chatbot
    # ------------------------------------------------------------------

    def _on_replace_row_request(self, event) -> None:
        """
        Replace all items in a specific global shelf row with a new asset.

        Payload:
            asset_key        – asset currently occupying the row (e.g. "cheetos_double_cheese")
            row              – global row number to replace (1 = top shelf)
            target_asset_key – asset to place instead (e.g. "cheetos_cheddar_jalapeno")

        Reads shelf_rows.json (written by detectShelfRowsRequest) to find the
        prim_paths for the requested row, then delegates to _on_replace_all_request.

        Response: replaceRowResponse { result, row, asset_key, target_asset_key,
                                       replaced_count, error }
        """
        payload          = dict(event.payload)
        asset_key        = str(payload.get("asset_key", ""))
        row_num          = int(payload.get("row", 0))
        target_asset_key = str(payload.get("target_asset_key", ""))

        def _reply(ok: bool, msg: str, count: int = 0) -> None:
            get_eventdispatcher().dispatch_event("replaceRowResponse", payload={
                "result":           "success" if ok else "error",
                "row":              row_num,
                "asset_key":        asset_key,
                "target_asset_key": target_asset_key,
                "replaced_count":   count,
                "error":            "" if ok else msg,
                "message":          msg,
            })

        if not asset_key or not target_asset_key or not row_num:
            _reply(False, "asset_key, row, and target_asset_key are required")
            return
        if target_asset_key not in ASSET_LIBRARY:
            _reply(False, f"Unknown target asset: '{target_asset_key}'")
            return

        # Load the latest shelf_rows.json written by detectShelfRowsRequest
        try:
            with open(_shelf_rowS_FILE) as f:
                shelf_data = json.load(f)
        except Exception as exc:
            _reply(False, f"Could not read shelf_rows.json: {exc}. Run Detect Rows first.")
            return

        stored_key = shelf_data.get("asset_key", "")
        if stored_key != asset_key and stored_key != "all":
            _reply(False,
                   f"shelf_rows.json was last built for '{stored_key}', not '{asset_key}'. "
                   f"Run Detect Rows on a {asset_key} item first.")
            return

        source_paths: list[str] = []
        for row in shelf_data.get("rows", []):
            if row.get("row") == row_num:
                source_paths = row.get("prim_paths", [])
                break

        if not source_paths:
            _reply(False, f"Row {row_num} not found for asset '{asset_key}'. "
                          f"Available rows: {[r['row'] for r in shelf_data.get('rows', [])]}")
            return

        carb.log_warn(
            f"[UsdSpawner] replaceRow: row {row_num} of '{asset_key}' → '{target_asset_key}'  "
            f"({len(source_paths)} prims)"
        )

        # Delegate to replaceAll logic by dispatching internally
        get_eventdispatcher().dispatch_event("replaceAllUsdRequest", payload={
            "source_paths": source_paths,
            "prim_name":    target_asset_key,
        })

        _reply(True, f"Replacing row {row_num} ({len(source_paths)} items) with {target_asset_key}",
               count=len(source_paths))

    # ------------------------------------------------------------------
    # Incident spawner — random ground-level placement in safe zones
    # ------------------------------------------------------------------

    def _pick_random_ground_position(self) -> "Gf.Vec3d":
        """Pick a random position within one of the configured spawn zones on the floor plane."""
        zone = random.choice(_INCIDENT_SPAWN_ZONES)
        x_min, x_max, z_min, z_max = zone
        x = random.uniform(x_min, x_max)
        z = random.uniform(z_min, z_max)
        up_axis = self._detect_up_axis()
        floor_level = self._get_floor_level(up_axis)
        if up_axis == "Z":
            return Gf.Vec3d(x, z, floor_level)
        return Gf.Vec3d(x, floor_level, z)

    def _on_incident_spawn_request(self, event) -> dict:
        """
        Accepts either a carb.events.IEvent or a raw dictionary
        Event:
        incidentSpawnRequest payload:
          { incident_type: "trash" | "spill" | "fire" | "random" }

        Dict: { incident_type: "trash" | "spill" | "fire" | "random" }

        Spawns the corresponding incident asset at a random ground-level
        position within the configured spawn zones.
        Fire incidents are delegated to FireIncidentManager via fireIncidentRequest.
        """
        payload = getattr(event, "payload", event)
        incident_type = str(payload.get("incident_type", "random"))

        carb.log_info(f"[UsdSpawner] incidentSpawnRequest  type={incident_type}")

        # For "random", pick from all incident types including fire
        if incident_type == "random":
            all_types = list(INCIDENT_ASSETS.keys()) + ["fire"]
            incident_type = random.choice(all_types)

        # Create an incident_id
        import time as _time
        incident_id = f"{incident_type}_{int(_time.time() * 1000)}"
        # Fire incidents use Flow prims, not USD assets — delegate
        if incident_type == "fire":
            position = self._pick_random_ground_position()
            fire_params = payload.get("fire_params", {})
            get_eventdispatcher().dispatch_event("fireIncidentRequest", payload={
                "action": "trigger",
                "incident_id": incident_id,
                "severity": "high",
                "position": {"x": float(position[0]), "y": float(position[1]), "z": float(position[2])},
                "fire_params": dict(fire_params) if fire_params else {},
            })

            # Call agent backend so it can notify the store manager (no body required)
            self._send_to_backend("/api/fire-alert", "POST", {})
            self._active_incidents[incident_id] = {"incident_type": "fire"}
            return {"success": True, "incident_id": incident_id, "incident_type": incident_type}

        # Resolve incident type → asset key
        asset_key = INCIDENT_ASSETS.get(incident_type)

        if not asset_key:
            get_eventdispatcher().dispatch_event("incidentSpawnResponse", payload={
                "result": "error",
                "error": f"Unknown incident type: '{incident_type}'. "
                         f"Valid types: {list(INCIDENT_ASSETS.keys()) + ['random']}",
                "prim_path": "", "position": [0, 0, 0],
            })
            return {"success": False}

        usd_path = ASSET_LIBRARY.get(asset_key)
        if not usd_path:
            get_eventdispatcher().dispatch_event("incidentSpawnResponse", payload={
                "result": "error",
                "error": f"No USD path for incident asset '{asset_key}'.",
                "prim_path": "", "position": [0, 0, 0],
            })
            return {"success": False}

        position = self._pick_random_ground_position()

        try:
            prim_path = self._spawn_usd(usd_path, asset_key, position)
            get_eventdispatcher().dispatch_event("incidentSpawnResponse", payload={
                "result": "success",
                "prim_path": prim_path,
                "incident_type": incident_type,
                "asset_key": asset_key,
                "position": [round(position[0], 3), round(position[1], 3),
                             round(position[2], 3)],
                "error": "",
            })
            self._active_incidents[incident_id] = {"incident_type": incident_type, "prim_path": prim_path}
            return {"success": True, "incident_id": incident_id, "incident_type": incident_type}
        except Exception as exc:
            carb.log_error(f"[UsdSpawner] Incident spawn failed: {exc}")
            get_eventdispatcher().dispatch_event("incidentSpawnResponse", payload={
                "result": "error", "error": str(exc),
                "prim_path": "", "position": [0, 0, 0],
            })

    def _on_incident_delete_request(self, event) -> None:
        """
        Accepts either a carb.events.IEvent or a raw dictionary
        Event:
        incidentDeleteRequest payload:
          { incident_id: str}

        Dict: { incident_id: str}


        Deletes a spawned prim matching the given incident_id.
        Fire incidents are delegated to FireIncidentManager via fireIncidentRequest.
        """

        payload = getattr(event, "payload", event)

        incident_id = payload.get("incident_id")

        if not incident_id:
            carb.log_warn("[UsdSpawner] incidentDeleteRequest  incident_id missing")
            return {"success": False}
        if not self._active_incidents.get(incident_id):
            carb.log_warn(f"[UsdSpawner] incidentDeleteRequest  incident_id={incident_id} is not registered as active incident")
            return {"success": False}

        incident = self._active_incidents.pop(incident_id)
        incident_type = incident["incident_type"]
        asset_key = INCIDENT_ASSETS.get(incident_type)

        # Handle fire extinguishing
        if incident_type == "fire":
            get_eventdispatcher().dispatch_event("fireIncidentRequest", payload={"action": "extinguish", "incident_id": incident_id})
            get_eventdispatcher().dispatch_event("incidentDeleteResponse", payload={
                "result": "success", "count": 0, "deleted_paths": [],
            })
            return {"success": True}

        # Other incidents require prim path for deletion
        elif "prim_path" in incident:
            stage = omni.usd.get_context().get_stage()
            if stage:
                prim = stage.GetPrimAtPath(incident["prim_path"])
                if prim.IsValid():
                    stage.RemovePrim(Sdf.Path(incident["prim_path"]))
                    self._spawned_prims[asset_key] = [ prim_path for prim_path in self._spawned_prims[asset_key] if prim_path != incident["prim_path"]]
                    self._inventory_remove([incident["prim_path"]])
                    carb.log_info(f"[UsdSpawner] incident resolved: incident_type={incident_type} incident_id={incident_id}")
                    get_eventdispatcher().dispatch_event("incidentDeleteResponse", payload={
                        "result": "success",
                        "count": 1,
                        "deleted_paths": [incident["prim_path"]],
                    })
                    return {"success": True}

        get_eventdispatcher().dispatch_event("incidentDeleteResponse", payload={
            "result": "error",
            "count": 0,
            "deleted_paths": [],
        })
        carb.log_warn("[UsdSpawner] couldn't delete incident")
        return {"success": False}


    def _on_incident_delete_all_request(self, event) -> None:
        """
        incidentDeleteAllRequest payload:
          { incident_type: "trash" | "spill" | "fire" | "all" }

        Deletes all spawned prims matching the given incident type (or all incident types).
        Fire incidents are delegated to FireIncidentManager via fireIncidentRequest.
        """
        payload = event.payload
        incident_type = str(payload.get("incident_type", "all"))

        carb.log_info(f"[UsdSpawner] incidentDeleteAllRequest  type={incident_type}")

        # Handle fire extinguishing
        if incident_type == "fire":
            get_eventdispatcher().dispatch_event("fireIncidentRequest", payload={"action": "extinguish_all"})
            get_eventdispatcher().dispatch_event("incidentDeleteAllResponse", payload={
                "result": "success", "count": 0, "deleted_paths": [],
            })
            return

        # For "all", also extinguish fires
        if incident_type == "all":
            get_eventdispatcher().dispatch_event("fireIncidentRequest", payload={"action": "extinguish_all"})

        # Determine which asset keys to delete
        if incident_type == "all":
            keys_to_delete = list(INCIDENT_ASSETS.values())
        else:
            key = INCIDENT_ASSETS.get(incident_type)
            keys_to_delete = [key] if key else []

        if not keys_to_delete:
            get_eventdispatcher().dispatch_event("incidentDeleteAllResponse", payload={
                "result": "error",
                "error": f"Unknown incident type: '{incident_type}'.",
                "count": 0,
            })
            return

        stage = omni.usd.get_context().get_stage()
        deleted = []

        for asset_key in keys_to_delete:
            paths = self._find_prims_by_asset_key(asset_key)
            for prim_path in paths:
                prim = stage.GetPrimAtPath(prim_path)
                if prim and prim.IsValid():
                    stage.RemovePrim(prim_path)
                    deleted.append(prim_path)
            # Clear in-memory tracker
            self._spawned_prims.pop(asset_key, None)

        if deleted:
            self._inventory_remove(deleted)

        get_eventdispatcher().dispatch_event("incidentDeleteAllResponse", payload={
            "result": "success" if deleted else "none",
            "count": len(deleted),
            "deleted_paths": deleted,
        })
        carb.log_info(f"[UsdSpawner] Incident delete-all: removed {len(deleted)} prims")

    # ------------------------------------------------------------------

    def on_shutdown(self) -> None:
        self._update_sub = None  # release deferred scan subscription
        for sub in self._subscriptions:
            # ObserverGuard (carb.eventdispatcher) cleans up on deletion,
            # older IEventStream subscriptions use .unsubscribe().
            if hasattr(sub, 'unsubscribe'):
                sub.unsubscribe()
        self._subscriptions.clear()
        carb.log_info("[UsdSpawner] Shutdown")
