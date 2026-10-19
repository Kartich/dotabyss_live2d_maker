# -*- coding: utf-8 -*-
"""Cubism MotionFade / .fade.asset parsing helpers.

The Unity-side relation is:
    AnimationClip <-> .fade.asset
    AnimationClip instance IDs <-> .fadeMotionList.asset

The Web runtime does not consume the Unity .fade.asset directly.  Its
parameter-level fade values belong in motion3.json Curve.FadeInTime /
Curve.FadeOutTime, while overall fade values belong in motion3 Meta or the
model3 Motion entry.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


def _norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def is_fade_bundle(path: Path | str) -> bool:
    name = Path(path).name.lower()
    # Fade data comes only from Unity Cubism .fade.asset resources and the
    # associated FadeMotionList. addanimations/scene bundles are motion sources,
    # but their AnimationClips are not themselves FadeMotionData.
    return ".fade.asset_" in name or "fademotionlist" in name


def is_motion_extension_bundle(path: Path | str) -> bool:
    name = Path(path).name.lower()
    # addanimations_* and animations_scene* contain AnimationClips that are
    # real motion sources.  A standalone fademotionlist is configuration only.
    return "_addanimations_" in name or re.search(r"_animations_scene\d+", name) is not None


def _bundle_motion_hint(path: Path) -> Optional[str]:
    name = path.name
    m = re.search(r"_(?:addanimations|animations)_([^.]*)\.fade(?:\.asset)?_", name, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"_(?:addanimations|animations)_([^.]*)", name, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _find_exact_field(node: Any, wanted: set[str]) -> Any:
    """Return the first field whose normalized key is in wanted.

    Unity serialized fields frequently appear as m_Foo rather than Foo.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            norm = _norm_key(k)
            if norm in wanted or (norm.startswith("m") and norm[1:] in wanted):
                return v
        for v in node.values():
            hit = _find_exact_field(v, wanted)
            if hit is not None:
                return hit
    elif isinstance(node, (list, tuple)):
        for v in node:
            hit = _find_exact_field(v, wanted)
            if hit is not None:
                return hit
    return None


def _flatten_scalars(value: Any) -> list[Any]:
    if isinstance(value, dict):
        # UnityPy may wrap arrays in {"Array": [...]} or similar.
        for key in ("Array", "array", "data", "Data", "value", "Value"):
            if key in value:
                return _flatten_scalars(value[key])
        out: list[Any] = []
        for v in value.values():
            out.extend(_flatten_scalars(v))
        return out
    if isinstance(value, (list, tuple)):
        out: list[Any] = []
        for v in value:
            out.extend(_flatten_scalars(v))
        return out
    return [value]


def _scalar_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _string_values(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        # Preserve scalar string members inside Unity array/object wrappers.
        for key in ("name", "Name", "id", "Id", "m_Id", "value", "Value"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return [v.strip()]
        for v in value.values():
            out.extend(_string_values(v))
        return out
    if isinstance(value, (list, tuple)):
        for item in value:
            out.extend(_string_values(item))
        return out
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return out


def _float_values(value: Any) -> list[float]:
    out: list[float] = []
    for item in _flatten_scalars(value):
        v = _scalar_float(item)
        if v is not None:
            out.append(v)
    return out


def extract_fade_motion_data(tree: Any, fallback_name: Optional[str] = None) -> Optional[dict]:
    """Extract CubismFadeMotionData-like fields from a Unity typetree.

    The exact serialized wrapper differs between Unity/Cubism versions, so we
    match normalized field names recursively rather than depending on one
    concrete typetree shape.
    """
    if not tree:
        return None

    name_value = _find_exact_field(tree, {"motionname"})
    motion_name = str(name_value).strip() if isinstance(name_value, str) and name_value.strip() else fallback_name

    fade_in_v = _find_exact_field(tree, {"fadeintime"})
    fade_out_v = _find_exact_field(tree, {"fadeouttime"})
    length_v = _find_exact_field(tree, {"motionlength"})
    parameter_ids_v = _find_exact_field(tree, {"parameterids"})
    parameter_in_v = _find_exact_field(tree, {"parameterfadeintimes"})
    parameter_out_v = _find_exact_field(tree, {"parameterfadeouttimes"})

    fade_in = _scalar_float(fade_in_v)
    fade_out = _scalar_float(fade_out_v)
    motion_length = _scalar_float(length_v)
    parameter_ids = _string_values(parameter_ids_v) if parameter_ids_v is not None else []
    parameter_in = _float_values(parameter_in_v) if parameter_in_v is not None else []
    parameter_out = _float_values(parameter_out_v) if parameter_out_v is not None else []

    has_any = any([
        motion_name,
        fade_in is not None,
        fade_out is not None,
        motion_length is not None,
        parameter_ids,
        parameter_in,
        parameter_out,
    ])
    if not has_any:
        return None

    result = {
        "MotionName": motion_name or "",
        "FadeInTime": fade_in,
        "FadeOutTime": fade_out,
        "MotionLength": motion_length,
        "ParameterIds": parameter_ids,
        "ParameterFadeInTimes": parameter_in,
        "ParameterFadeOutTimes": parameter_out,
    }

    # Pair parameter arrays by index exactly as CubismFadeMotionData does.
    pcount = len(parameter_ids)
    per_parameter: dict[str, dict[str, float]] = {}
    for i, pid in enumerate(parameter_ids):
        d: dict[str, float] = {}
        if i < len(parameter_in):
            d["FadeInTime"] = parameter_in[i]
        if i < len(parameter_out):
            d["FadeOutTime"] = parameter_out[i]
        if d:
            per_parameter[pid.casefold()] = d
    result["ParameterFade"] = per_parameter
    return result


def _merge_fade_data(existing: dict, incoming: dict) -> dict:
    """Prefer the more complete source while retaining all discovered data."""
    result = dict(existing)
    for key in ("MotionName", "FadeInTime", "FadeOutTime", "MotionLength"):
        if not result.get(key) and incoming.get(key) is not None:
            result[key] = incoming[key]
    if incoming.get("ParameterIds") and len(incoming["ParameterIds"]) > len(result.get("ParameterIds") or []):
        result["ParameterIds"] = list(incoming["ParameterIds"])
    merged_param = dict(result.get("ParameterFade") or {})
    merged_param.update(incoming.get("ParameterFade") or {})
    result["ParameterFade"] = merged_param
    return result


def collect_fade_configs(bundle_paths: Iterable[Path]) -> Dict[str, Dict[str, Any]]:
    """Collect CubismFadeMotionData from all relevant bundles, keyed by motion name.

    This reads .fade.asset-like objects as configuration only. It never creates
    motion3 files and never treats fademotionlist itself as an AnimationClip
    source.
    """
    try:
        import UnityPy
    except ImportError:
        return {}

    result: Dict[str, Dict[str, Any]] = {}
    for bundle_path in bundle_paths:
        path = Path(bundle_path)
        if not is_fade_bundle(path):
            continue
        try:
            env = UnityPy.load(str(path))
        except Exception:
            continue

        bundle_hint = _bundle_motion_hint(path)
        for obj in list(env.objects):
            try:
                typ = getattr(obj.type, "name", str(obj.type))
                if typ == "AnimationClip":
                    continue
                tree = obj.read_typetree() if hasattr(obj, "read_typetree") else None
                if not tree:
                    continue
                data = extract_fade_motion_data(tree, bundle_hint)
                if not data:
                    continue
                name = str(data.get("MotionName") or bundle_hint or "").strip()
                if not name:
                    continue

                # Unity FadeMotionData stores MotionName as the original asset
                # path (e.g. Assets/.../EyebrowEmotion02.motion3.json), while
                # our exported Motion3 is addressed by basename.  Index both
                # forms so Fade data is matched by resource identity, not by a
                # lossy string guess.
                aliases = {name.casefold()}
                motion_path = Path(name.replace("\\", "/"))
                aliases.add(motion_path.name.casefold())
                if motion_path.suffix.casefold() == ".json":
                    aliases.add(motion_path.stem.casefold())
                if bundle_hint:
                    aliases.add(str(bundle_hint).casefold().strip())

                merged = None
                for key in aliases:
                    merged = _merge_fade_data(merged or {}, result.get(key, {}))
                merged = _merge_fade_data(merged or {}, data)
                for key in aliases:
                    result[key] = dict(merged)
            except Exception:
                continue

    return result


def fade_data_for_motion(
    fade_map: Dict[str, Dict[str, Any]], motion_name: str
) -> Optional[Dict[str, Any]]:
    """Return the complete FadeMotionData for a motion using the same
    normalization/alias rules as fade_for_motion().

    This is the authoritative lookup for Stage 3. The caller must not use
    fade_map.get(motion_name.casefold()) directly because Unity Fade assets
    store MotionName as a full Assets/.../*.motion3.json path.
    """
    if not isinstance(fade_map, dict):
        return None
    key = str(motion_name).casefold().strip().replace('\\', '/')
    path_name = Path(key).name
    candidates = [key, path_name, key + '.motion3', path_name + '.motion3', key + '.motion3.json', path_name + '.motion3.json']
    if path_name.endswith('.motion3.json'):
        candidates.append(path_name[:-len('.motion3.json')])
    if path_name.endswith('.motion3'):
        candidates.append(path_name[:-len('.motion3')])
    if path_name.endswith('.json'):
        candidates.append(Path(path_name).stem.casefold())
    if key.endswith('_loop'):
        candidates.append(key[:-5])
    if path_name.endswith('_loop'):
        candidates.append(path_name[:-5])
    if path_name.endswith('_loop.motion3.json'):
        candidates.append(path_name[:-len('_loop.motion3.json')])
    seen = set()
    for candidate in candidates:
        candidate = str(candidate).strip().casefold()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        data = fade_map.get(candidate)
        if data:
            return data
    return None


def fade_for_motion(
    fade_map: Dict[str, Dict[str, Any]],
    motion_name: str,
) -> tuple[Optional[float], Optional[float]]:
    """Return only Fade values actually found in Unity FadeMotionData.

    No synthetic 0.15/1.0 defaults are introduced here.  Omitting a value lets
    Cubism use the native Model3/Motion3 default resolution path.
    """
    key = str(motion_name).casefold().strip().replace("\\", "/")
    path_name = Path(key).name
    path_stem = Path(path_name).stem if path_name.endswith(".json") else path_name
    candidates = [key, path_name, path_stem]
    if path_stem.endswith(".motion3"):
        candidates.append(path_stem[:-8])
    if key.endswith("_loop"):
        candidates.append(key[:-5])
    if path_stem.endswith("_loop"):
        candidates.append(path_stem[:-5])
    seen = set()
    for candidate in candidates:
        candidate = str(candidate).strip().casefold()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        data = fade_map.get(candidate)
        if data:
            fi = data.get("FadeInTime")
            fo = data.get("FadeOutTime")
            return (float(fi) if fi is not None else None, float(fo) if fo is not None else None)
    return None, None

def parameter_fade_for_motion(
    fade_map: Dict[str, Dict[str, Any]], motion_name: str
) -> Dict[str, Dict[str, float]]:
    key = str(motion_name).casefold().strip().replace("\\", "/")
    path_name = Path(key).name
    candidates = [key, path_name]
    if path_name.endswith(".json"):
        candidates.append(Path(path_name).stem.casefold())
    if path_name.endswith(".motion3.json"):
        candidates.append(path_name[:-len(".motion3.json")])
    if key.endswith("_loop"):
        candidates.append(key[:-5])
    if path_name.endswith("_loop"):
        candidates.append(path_name[:-5])
    if path_name.endswith("_loop.motion3.json"):
        candidates.append(path_name[:-len("_loop.motion3.json")])
    seen = set()
    for candidate in candidates:
        candidate = str(candidate).strip().casefold()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        data = fade_map.get(candidate)
        if data and data.get("ParameterFade"):
            return dict(data["ParameterFade"])
    return {}
