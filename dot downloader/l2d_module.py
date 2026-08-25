# -*- coding: utf-8 -*-
"""Drag-and-drop Unity L2D AssetBundle -> Cubism 3 package converter.

Install once:
    python -m pip install Pillow lz4 UnityPy

Then drag one or more .bundle files onto run_l2d.bat.
Bundles with the same character ID are merged into one output folder.
"""

import json, math, os, re, struct, zlib, shutil, zipfile, hashlib, sys
from pathlib import Path
from PIL import Image
from parse_anim import parse_sf, make_tree, read_node
from read_anim_exact import read_anim
from fade_module import (collect_fade_configs, fade_data_for_motion, fade_for_motion, parameter_fade_for_motion,
                        is_fade_bundle, is_motion_extension_bundle)



def get_canonical_group_name(motion_name: str) -> str:
    low = str(motion_name or "").casefold()
    if low.startswith("scene"):
        return "Scene"
    if low.startswith("eyeemotion") or (low.startswith("eye") and "open" not in low):
        return "EyeEmotion"
    if low.startswith("eyeopen"):
        return "EyeOpen"
    if low.startswith("eyebrow"):
        return "EyebrowEmotion"
    if low.startswith("faceangle"):
        return "FaceAngle"
    if low.startswith("faceemotion") or low.startswith("face"):
        return "FaceEmotion"
    if low.startswith("mouthemotion") or low.startswith("mouth"):
        return "MouthEmotion"
    if low.startswith("body"):
        return "Body"
    if "idle" in low:
        return "Idle"
    return "Motion"


def cstring(b, p):
    q = b.find(b"\0", p)
    if q < 0:
        raise ValueError("Unterminated UnityFS string")
    return b[p:q].decode("utf-8", "replace"), q + 1


def align4(p):
    return (p + 3) & ~3


def read_i32(b, p): return struct.unpack_from('<i', b, p)[0], p + 4

def read_i16(b, p): return struct.unpack_from('<h', b, p)[0], p + 2

def read_i64(b, p): return struct.unpack_from('<q', b, p)[0], p + 8

def read_str4(b, p):
    n, p = read_i32(b, p)
    s = b[p:p+n].decode('utf-8', 'replace')
    p += max(0, n)
    return s, align4(p)

def read_pptr(b, p):
    fid, p = read_i32(b, p)
    pid, p = read_i64(b, p)
    return (fid, pid), p


def parse_gameobjects_transforms(raw, objs):
    gos = {}
    transforms = {}
    for o in [x for x in objs if x['cid'] == 1]:
        b = raw[o['off']:o['off'] + o['size']]
        p = 0
        n, p = read_i32(b, p)
        comps = []
        for _ in range(n):
            cp, p = read_pptr(b, p)
            comps.append(cp)
        _layer, p = read_i32(b, p)
        name, p = read_str4(b, p)
        _tag, p = read_i16(b, p)
        _active = b[p]
        gos[o['path']] = {'name': name, 'components': comps}
    for o in [x for x in objs if x['cid'] == 4]:
        b = raw[o['off']:o['off'] + o['size']]
        p = 0
        go, p = read_pptr(b, p)
        p += 16 + 12 + 12  # localRotation/localPosition/localScale
        n, p = read_i32(b, p)
        children = []
        for _ in range(n):
            cp, p = read_pptr(b, p)
            children.append(cp)
        father, p = read_pptr(b, p)
        transforms[o['path']] = {'go': go, 'children': children, 'father': father}
    return gos, transforms


def build_root_relative_paths(gos, transforms):
    name_of_transform = {}
    for t, d in transforms.items():
        if d['go'][0] == 0 and d['go'][1] in gos:
            name_of_transform[t] = gos[d['go'][1]]['name']
    roots = [t for t, d in transforms.items() if d['father'] == (0, 0)]
    paths = {}
    for root in roots:
        paths[root] = ''
        def rec(t, prefix):
            for child in transforms[t]['children']:
                if child[0] == 0 and child[1] in transforms:
                    n = name_of_transform.get(child[1], '')
                    path = (prefix + '/' + n).strip('/')
                    paths[child[1]] = path
                    rec(child[1], path)
        rec(root, '')
    return paths, roots, name_of_transform


def _normalize_typetree_animation_clip(raw, o):
    root = make_tree(o['nodes'])
    val = read_node(__import__('parse_anim').R(raw[o['off']:o['off'] + o['size']]), root['children'][0])
    if not isinstance(val, dict):
        raise ValueError('TypeTree AnimationClip root is not a struct')

    def first(d, *keys, default=None):
        for k in keys:
            if k in d:
                return d[k]
        return default

    settings = first(val, "m_AnimationClipSettings", default={}) or {}
    clip_binding = first(val, "m_ClipBindingConstant", default={}) or {}
    generic = first(clip_binding, "genericBindings", default=[]) or []

    bindings=[]
    for g in generic:
        if not isinstance(g, dict):
            continue
        bindings.append({
            "path": int(g.get("path", 0) or 0),
            "attribute": int(g.get("attribute", 0) or 0),
            "typeID": int(g.get("classID", g.get("typeID", 0)) or 0),
        })

    float_curves=[]
    for fc in first(val, "m_FloatCurves", default=[]) or []:
        if not isinstance(fc, dict):
            continue
        cv = fc.get("curve") or {}
        float_curves.append({
            "attribute": int(fc.get("attribute", 0) or 0),
            "path": int(fc.get("path", 0) or 0),
            "typeID": int(fc.get("classID", fc.get("typeID", 0)) or 0),
            "curve": {
                "keys": cv.get("m_Curve", []) or [],
                "preInfinity": int(cv.get("m_PreInfinity", 2) or 2),
                "postInfinity": int(cv.get("m_PostInfinity", 2) or 2),
                "rotationOrder": int(cv.get("m_RotationOrder", 4) or 4),
            },
        })

    events=[]
    for e in first(val, "m_Events", default=[]) or []:
        if isinstance(e, dict):
            events.append({
                "Time": float(e.get("time", 0.0) or 0.0),
                "Value": e.get("data") or e.get("functionName") or "",
            })

    streamed = first(val, "m_StreamingAssets", default=[]) or []
    if not streamed:
        streamed = first(val, "m_Streaming", default=[]) or []

    # The AnimationClip must preserve the complete FloatCurve array.  Unity
    # model tracks (Opacity/EyeBlink/LipSync) legitimately have path=0, so a
    # path-zero curve is NOT an invalid/empty curve.
    if float_curves and len(float_curves) < len(generic):
        # Keep the data we did decode, but make the mismatch visible.  The
        # caller will not silently treat this as a complete AnimationClip.
        raise ValueError(
            f"AnimationClip m_FloatCurves/genericBindings mismatch: "
            f"floatCurves={len(float_curves)} bindings={len(generic)}"
        )

    # TypeTree representation is authoritative for the fields we actually use.
    return {
        "name": str(val.get("m_Name", "") or ""),
        "legacy": bool(val.get("m_Legacy", 0)),
        "sampleRate": float(val.get("m_SampleRate", 30.0) or 30.0),
        "streamed": [int(x) & 0xffffffff for x in streamed],
        "streamCurveCount": len(bindings),
        "denseFrameCount": 0,
        "denseCurveCount": 0,
        "denseSampleRate": 0.0,
        "denseBeginTime": 0.0,
        "dense": [],
        "constant": [],
        "stopTime": float(settings.get("m_StopTime", val.get("m_AnimationClipSettings.m_StopTime", 0.0)) or 0.0),
        "startTime": float(settings.get("m_StartTime", 0.0) or 0.0),
        "loopTime": bool(settings.get("m_LoopTime", 0)),
        "bindings": bindings,
        "events": events,
        "floatCurves": float_curves,
    }


def read_animation_clip(raw, o):
    # TypeTree is useful for explicit m_FloatCurves, but Unity 6 clips in
    # these bundles can store the actual animation in m_MuscleClip/m_Clip
    # (StreamedClip/DenseClip/ConstantClip) while TypeTree still exposes the
    # genericBindings. In that case TypeTree must NOT win just because it has
    # bindings: doing so discards the real curve payload.
    typetree_v = None
    try:
        typetree_v = _normalize_typetree_animation_clip(raw, o)
    except Exception:
        typetree_v = None

    # Exact binary reader for the complete AnimationClip payload.
    v = read_anim(raw[o['off']:o['off'] + o['size']])
    binary = {
        'name': v['name'],
        'legacy': v['legacy'],
        'sampleRate': v['sr'],
        'streamed': v['streamed'],
        'streamCurveCount': v['curveCount'],
        'denseFrameCount': v['frameCount'],
        'denseCurveCount': v['dcurve'],
        'denseSampleRate': v['dsr'],
        'denseBeginTime': v['begin'],
        'dense': v['dense'],
        'constant': v['const'],
        'stopTime': v['stop'],
        'startTime': v['start'],
        'loopTime': bool(v['bools'][1]) if len(v['bools']) > 1 else False,
        'bindings': [{'path': x[0], 'attribute': x[1], 'typeID': x[2]} for x in v['bindings']],
        'events': [{'Time': x[0], 'Value': x[2] or x[1]} for x in v['events']],
        'floatCurves': v.get('floats') or [],
    }

    # Prefer the exact binary payload whenever it contains actual curve data.
    # Preserve TypeTree's explicit FloatCurves only when the binary representation
    # does not expose them (e.g. a tooling-only reconstruction).
    has_binary_curves = bool(binary['floatCurves'] or binary['streamed'] or binary['dense'] or binary['constant'])
    if has_binary_curves:
        return binary
    if typetree_v is not None:
        return typetree_v
    return binary


def decode_streamed_frames(words):
    """Decode Unity StreamedClip frames without inventing loop frames."""
    frames = []
    p = 0
    while p < len(words):
        if p + 2 > len(words):
            raise ValueError('Truncated StreamedClip frame header')
        time = struct.unpack('<f', struct.pack('<I', words[p]))[0]
        count = words[p + 1]
        p += 2
        if count > 10000 or p + count * 5 > len(words):
            raise ValueError(f'Invalid StreamedClip key count {count} at word {p-2}')
        keys = []
        for _ in range(count):
            idx = int(words[p])
            coeff = struct.unpack('<4f', struct.pack('<4I', *words[p + 1:p + 5]))
            keys.append((idx, coeff))
            p += 5
        frames.append((time, keys))
    return frames


def crc32_path(s):
    return zlib.crc32(str(s).encode('utf-8')) & 0xffffffff


def extract_parameter_paths(raw, objs):
    """Build Unity Transform-path CRC -> Cubism parameter ID mapping.

    AnimationClip bindings store the CRC32 of the GameObject transform path.
    The MOC3/Cubism parameter namespace is represented by the GameObject names
    in the L2D prefab, so preserve the original test(4).py mapping strategy.
    """
    gos, transforms = parse_gameobjects_transforms(raw, objs)
    paths, roots, name_of_transform = build_root_relative_paths(gos, transforms)
    hash_to_path = {crc32_path(path): path for path in paths.values()}
    hash_to_id = {}
    path_to_id = {}
    for transform_id, path in paths.items():
        if not path or transform_id not in name_of_transform:
            continue
        pid = name_of_transform[transform_id]
        hash_to_id[crc32_path(path)] = pid
        path_to_id[str(path).replace('\\', '/').strip('/').casefold()] = pid
        # Some Unity bindings include a redundant leading slash.
        path_to_id[('/' + str(path).replace('\\', '/').strip('/')).casefold()] = pid
    return hash_to_path, hash_to_id, paths, path_to_id


def _strip_stream_sentinels(frames):
    if not frames:
        return []
    lo = 1 if abs(frames[0][0]) > 1e30 else 0
    hi = len(frames)
    if hi and abs(frames[-1][0]) > 1e30:
        hi -= 1
    return frames[lo:hi]


def _finite_float(value, fallback=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else fallback
    except Exception:
        return fallback


def _power_to_cubism_bezier(t0, t1, coeff):
    """Convert exact cubic power polynomial segment to an exact cubic Bezier."""
    a, b, c, d = map(float, coeff)
    dt = float(t1 - t0)
    if dt <= 1e-9:
        return None
    v0 = d
    v1 = ((a * dt + b) * dt + c) * dt + d
    m0 = c
    m1 = (3.0 * a * dt + 2.0 * b) * dt + c
    c1x = float(t0 + dt / 3.0)
    c1y = float(v0 + m0 * dt / 3.0)
    c2x = float(t1 - dt / 3.0)
    c2y = float(v1 - m1 * dt / 3.0)
    return [1.0, c1x, c1y, c2x, c2y, float(t1), float(v1)]


def stream_curve_to_cubism_segment(keys):
    """Convert Unity StreamedClip power-polynomial segments to exact Cubism Bezier."""
    if not keys:
        return [], 0, 0
    keys = sorted((float(t), tuple(map(float, coeff))) for t, coeff in keys)
    first_t, first_coeff = keys[0]
    seg = [max(0.0, first_t), float(first_coeff[3])]
    seg_count = 0
    point_count = 1
    for i in range(len(keys) - 1):
        t0, coeff = keys[i]
        t1 = keys[i + 1][0]
        if t1 <= t0 + 1e-9:
            continue
        bez = _power_to_cubism_bezier(t0, t1, coeff)
        if bez is None:
            continue
        # Preserve exact linear pieces as type-0 segments.
        if abs(coeff[0]) < 1e-8 and abs(coeff[1]) < 1e-8:
            expected_slope = (bez[6] - float(coeff[3])) / (t1 - t0)
            if abs(coeff[2] - expected_slope) < 1e-6:
                seg.extend([0.0, float(t1), float(bez[6])])
                point_count += 1
                seg_count += 1
                continue
        seg.extend(bez)
        point_count += 3
        seg_count += 1
    return seg, seg_count, point_count


def _finite_or_none(value):
    try:
        x = float(value)
    except Exception:
        return None
    return x if math.isfinite(x) else None


def animation_curve_to_cubism_segment(curve):
    """Convert Unity AnimationCurve keys exactly to Cubism type-0/type-1 segments.

    Unity's inSlope/outSlope are dy/dt. For each key interval [t0,t1], the
    equivalent cubic Bezier control values are obtained directly from the
    Hermite-to-Bezier transform, with no resampling, RDP simplification, or
    endpoint modification.
    """
    keys = list((curve or {}).get('keys') or [])
    if not keys:
        return [], 0, 0
    keys.sort(key=lambda k: float(k.get('time', 0.0)))
    first_t = float(keys[0].get('time', 0.0))
    first_v = float(keys[0].get('value', 0.0))
    seg = [max(0.0, first_t), first_v]
    segment_count = 0
    point_count = 1

    for a, b in zip(keys, keys[1:]):
        t0 = float(a.get('time', 0.0)); t1 = float(b.get('time', 0.0))
        v0 = float(a.get('value', 0.0)); v1 = float(b.get('value', 0.0))
        dt = t1 - t0
        if dt <= 1e-9:
            continue
        m0 = _finite_or_none(a.get('outSlope'))
        m1 = _finite_or_none(b.get('inSlope'))

        # Unity can encode stepped curves with non-finite tangents. Cubism's
        # type-2 segment represents a forward stepped transition.
        if m0 is None or m1 is None:
            seg.extend([2.0, t1, v1])
            segment_count += 1
            point_count += 1
            continue

        # Exact linear case.
        linear_slope = (v1 - v0) / dt
        if abs(m0 - linear_slope) < 1e-7 and abs(m1 - linear_slope) < 1e-7:
            seg.extend([0.0, t1, v1])
            segment_count += 1
            point_count += 1
            continue

        c1x = t0 + dt / 3.0
        c1y = v0 + m0 * dt / 3.0
        c2x = t1 - dt / 3.0
        c2y = v1 - m1 * dt / 3.0
        seg.extend([1.0, c1x, c1y, c2x, c2y, t1, v1])
        segment_count += 1
        point_count += 3

    return seg, segment_count, point_count


def _normalize_anim_path(path):
    return str(path or '').replace('\\', '/').strip('/').casefold()


def convert_motion(clip, hash_to_id, path_to_id=None, fade_data=None):
    """Convert Unity AnimationClip to Cubism Motion3.

    Primary source: serialized m_FloatCurves (exact Unity key times/slopes).
    Safety fallback: StreamedClip/DenseClip/constant curves from the same clip.

    The fallback is per binding: one unresolvable FloatCurve never invalidates
    the whole AnimationClip. No resampling, RDP reduction, loop surgery,
    synthetic endpoints, or synthetic Fade values are performed.
    """
    path_to_id = path_to_id or {}
    curves = []
    missing = []
    total_segment_count = 0
    total_point_count = 0

    def resolve_binding(path):
        if isinstance(path, int):
            return hash_to_id.get(int(path) & 0xFFFFFFFF)

        raw = str(path or '').strip()
        norm = _normalize_anim_path(raw)
        if norm in path_to_id:
            return path_to_id[norm]
        if norm.startswith('/') and norm[1:] in path_to_id:
            return path_to_id[norm[1:]]

        candidate = norm[5:] if norm.startswith('path_') else norm
        if candidate.lstrip('-').isdigit():
            try:
                return hash_to_id.get(int(candidate) & 0xFFFFFFFF)
            except Exception:
                return None
        return None

    explicit = [
        x for x in (clip.get('floatCurves') or [])
        if int(x.get('typeID', 114)) == 114 and x.get('curve')
    ]

    # Decode streamed data regardless of explicit curves so it is always
    # available as a safe fallback.
    frames = _strip_stream_sentinels(decode_streamed_frames(clip['streamed']))
    per_curve = {i: [] for i in range(clip['streamCurveCount'])}
    for t, keys in frames:
        for idx, coeff in keys:
            if 0 <= idx < clip['streamCurveCount']:
                per_curve[idx].append((float(t), coeff))

    emitted_ids = set()

    # Cubism Unity AnimationClips can contain three special Model tracks
    # (Opacity / EyeBlink / LipSync).  Their Unity bindings use script
    # attribute hashes and an empty transform path, so they cannot be
    # recovered from the normal Transform-path CRC map.  The paired
    # CubismFadeMotionData preserves the authoritative track ordering and
    # IDs; use that resource to recover these bindings rather than dropping
    # them.
    fade_parameter_ids = []
    if isinstance(fade_data, dict):
        fade_parameter_ids = [str(x).strip() for x in (fade_data.get('ParameterIds') or [])]
    model_track_names = {'opacity', 'eyeblink', 'lipsync'}
    # Standard Cubism Unity AnimationClip Model-track binding attribute hashes.
    # These are not inferred from Story or from a guessed player policy: they
    # are the serialized binding identities for the three standard Cubism
    # Model tracks used by motion3 (Opacity, EyeBlink, LipSync).  Keeping this
    # mapping in the Motion converter prevents a missing/failed Fade asset from
    # causing legitimate Model curves to disappear.
    CUBISM_MODEL_TRACK_BY_ATTRIBUTE = {
        2353026298: 'Opacity',
        66473442: 'EyeBlink',
        4109387685: 'LipSync',
    }

    # Build a binding-index -> Cubism track ID map when the FadeMotionData
    # provides the same ordered track set as the AnimationClip.  This is the
    # native Unity/Cubism relation seen in the exported .fade.asset data.
    fade_binding_ids = {}
    if fade_parameter_ids:
        n = min(len(fade_parameter_ids), len(clip.get('bindings') or []), len(clip.get('floatCurves') or []))
        for i in range(n):
            pid = fade_parameter_ids[i]
            if pid:
                fade_binding_ids[i] = pid

    def resolve_track_for_item(item, item_index=None):
        raw_path = item.get('path', '') if isinstance(item, dict) else ''
        attribute = item.get('attribute') if isinstance(item, dict) else None

        # 1. Standard Cubism Model tracks are identified by their Unity script
        # attribute hash and do not have a Transform path.  Resolve these first.
        try:
            # Unity AnimationClip binding attributes are uint32.
            # Some TypeTree revisions expose them as signed int32, so always
            # normalize to the original 32-bit unsigned identity before
            # matching Cubism Model-track hashes.
            attr_int = int(attribute) & 0xFFFFFFFF
        except (TypeError, ValueError):
            attr_int = None
        if not str(raw_path or '').strip() and attr_int in CUBISM_MODEL_TRACK_BY_ATTRIBUTE:
            return ('Model', CUBISM_MODEL_TRACK_BY_ATTRIBUTE[attr_int])

        # 2. Normal Cubism Parameter bindings use the Transform-path CRC.
        pid = resolve_binding(raw_path)
        if pid:
            return ('Parameter', pid)

        # 3. If available, FadeMotionData provides the authoritative ordered
        # track identity as a fallback for Unity/Cubism versions whose binding
        # metadata is wrapped differently.
        if item_index is not None and item_index in fade_binding_ids:
            hinted = fade_binding_ids[item_index]
            return ('Model' if hinted.casefold() in model_track_names else 'Parameter', hinted)
        return (None, None)

    def _is_constant_curve(curve):
        keys = (curve or {}).get('keys') or []
        if len(keys) <= 1:
            return True
        try:
            values = [float(k.get('value', 0.0)) for k in keys]
        except Exception:
            return False
        first = values[0]
        return all(abs(v - first) <= 1e-9 for v in values[1:])

    def emit_exact(target, pid, curve):
        nonlocal total_segment_count, total_point_count

        seg, sc, pc = animation_curve_to_cubism_segment(curve)
        if len(seg) < 2:
            return False
        curves.append({'Target': target, 'Id': pid, 'Segments': seg})
        emitted_ids.add(pid)
        total_segment_count += sc
        total_point_count += pc
        return True

    # 1. Exact serialized AnimationCurve path / authoritative Fade track map.
    # Explicit FloatCurves are in the same order as Unity genericBindings;
    # when path is empty, the paired FadeMotionData identifies Model tracks.
    for item_idx, item in enumerate(explicit):
        target, pid = resolve_track_for_item(item, item_idx)
        if not pid:
            # Do not turn this into a fatal clip error; StreamedClip remains
            # authoritative fallback for the corresponding generic binding.
            continue
        emit_exact(target, pid, item.get('curve') or {})

    # 2. Streamed curves for any parameter that was not recovered from the
    # explicit FloatCurve representation.
    for idx in range(clip['streamCurveCount']):
        if idx >= len(clip['bindings']):
            continue
        binding = clip['bindings'][idx]
        target, pid = resolve_track_for_item(binding, idx)
        if not pid:
            missing.append(binding['path'])
            continue
        if pid in emitted_ids:
            continue
        seg, sc, pc = stream_curve_to_cubism_segment(per_curve.get(idx, []))
        if len(seg) < 2:
            continue
        curves.append({'Target': target, 'Id': pid, 'Segments': seg})
        emitted_ids.add(pid)
        total_segment_count += sc
        total_point_count += pc

    # 3. Dense curves. Their binding indices follow Streamed curves.
    if clip['denseCurveCount'] and clip['denseFrameCount']:
        base = clip['streamCurveCount']
        rate = _finite_float(clip['denseSampleRate'], 30.0) or 30.0
        begin = _finite_float(clip['denseBeginTime'], 0.0)
        for ci in range(clip['denseCurveCount']):
            idx = base + ci
            if idx >= len(clip['bindings']):
                continue
            binding = clip['bindings'][idx]
            target, pid = resolve_track_for_item(binding, idx)
            if not pid:
                missing.append(binding['path'])
                continue
            if pid in emitted_ids:
                continue
            keys = []
            for fi in range(clip['denseFrameCount']):
                pos = fi * clip['denseCurveCount'] + ci
                if pos >= len(clip['dense']):
                    break
                keys.append((begin + fi / rate, (0.0, 0.0, 0.0, float(clip['dense'][pos]))))
            seg, sc, pc = stream_curve_to_cubism_segment(keys)
            if len(seg) < 2:
                continue
            curves.append({'Target': target, 'Id': pid, 'Segments': seg})
            emitted_ids.add(pid)
            total_segment_count += sc
            total_point_count += pc

    # 4. Constant curves. Their binding indices follow Streamed + Dense.
    if clip['constant']:
        base = clip['streamCurveCount'] + clip['denseCurveCount']
        stop = max(0.0, _finite_float(clip['stopTime']))
        for ci, value in enumerate(clip['constant']):
            idx = base + ci
            if idx >= len(clip['bindings']):
                continue
            binding = clip['bindings'][idx]
            target, pid = resolve_track_for_item(binding, idx)
            if not pid:
                missing.append(binding['path'])
                continue
            if pid in emitted_ids:
                continue
            seg = [0.0, float(value), 0.0, stop, float(value)]
            curves.append({'Target': target, 'Id': pid, 'Segments': seg})
            emitted_ids.add(pid)
            total_segment_count += 1
            total_point_count += 2

    duration = max(0.0, _finite_float(clip['stopTime']))
    if duration <= 0.0:
        all_times = []
        for item in explicit:
            all_times.extend(float(k.get('time', 0.0)) for k in (item.get('curve') or {}).get('keys', []))
        duration = max(all_times, default=0.0)
    fps = _finite_float(clip['sampleRate'], 30.0) or 30.0

    motion = {
        'Version': 3,
        'Meta': {
            'Duration': duration,
            'Fps': fps,
            'Loop': bool(clip['loopTime']),
            'AreBeziersRestricted': True,
            'CurveCount': len(curves),
            'TotalSegmentCount': total_segment_count,
            'TotalPointCount': total_point_count,
            'UserDataCount': len(clip['events']),
            'TotalUserDataSize': sum(len(str(x['Value'])) for x in clip['events']),
        },
        'Curves': curves,
        'UserData': clip['events'],
    }
    return motion, sorted(set(str(x) for x in missing if x)), bool(curves)

def remove_pure_red_background_exact(pil_image, out_png_path):
    # This is the user's supplied script's predicate verbatim:
    # R=255, G=0, B<=5 becomes transparent; all other pixels retain RGBA.
    img = pil_image.convert('RGBA')
    datas = list(img.getdata())
    new_data = [
        (0, 0, 0, 0) if (r == 255 and g == 0 and b <= 5) else (r, g, b, a)
        for r, g, b, a in datas
    ]
    removed = sum(1 for old, new in zip(datas, new_data) if old != new)
    img.putdata(new_data)
    Path(out_png_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png_path, 'PNG')
    return removed



# ============================================================
# Drag-drop entry: process one or more UnityFS AssetBundles.
# Bundles are grouped by the 11-digit ID in their filenames.
# ============================================================
import re
import lz4.block

TOOL_ROOT = Path(__file__).resolve().parent
OUTPUT_BASE = TOOL_ROOT / 'data_r18_all' / 'stories'


def unpack_unityfs(path: Path):
    b = path.read_bytes()
    p = 0
    sig, p = cstring(b, p)
    if sig != 'UnityFS':
        raise ValueError(f'Not a UnityFS bundle: {path.name}')
    fmt = struct.unpack_from('>I', b, p)[0]; p += 4
    _ver, p = cstring(b, p)
    _eng, p = cstring(b, p)
    _total, comp_info_size, uncomp_info_size, flags = struct.unpack_from('>QIII', b, p); p += 20
    info_off = 64 if not (flags & 0x80) else len(b) - comp_info_size
    info_comp = b[info_off:info_off + comp_info_size]
    info_type = flags & 3
    if info_type in (2, 3):
        info = lz4.block.decompress(info_comp, uncompressed_size=uncomp_info_size)
    elif info_type == 0:
        info = info_comp
    else:
        raise ValueError(f'Unsupported UnityFS block-info compression {info_type}')
    q = 16
    block_count = struct.unpack_from('>I', info, q)[0]; q += 4
    blocks = []
    for _ in range(block_count):
        usize, csize, bflags = struct.unpack_from('>IIH', info, q); q += 10
        blocks.append((usize, csize, bflags))
    dir_count = struct.unpack_from('>I', info, q)[0]; q += 4
    dirs = []
    for _ in range(dir_count):
        off, size, dflags = struct.unpack_from('>QQI', info, q); q += 20
        name, q = cstring(info, q)
        dirs.append((off, size, dflags, name))
    data_off = align4(info_off + comp_info_size)
    if flags & 0x200:
        data_off = (data_off + 15) & ~15
    raw = bytearray()
    for usize, csize, bflags in blocks:
        blk = b[data_off:data_off + csize]
        data_off += csize
        ct = bflags & 3
        if ct == 0:
            dec = blk
        elif ct in (2, 3):
            dec = lz4.block.decompress(blk, uncompressed_size=usize)
        else:
            raise ValueError(f'Unsupported UnityFS block compression {ct}')
        if len(dec) != usize:
            raise ValueError('UnityFS block size mismatch')
        raw.extend(dec)
    return bytes(raw), dirs


def extract_serialized_files(bundle: Path):
    raw, dirs = unpack_unityfs(bundle)
    result = []
    for off, size, _flags, name in dirs:
        if size <= 0 or off + size > len(raw):
            continue
        sf = raw[off:off + size]
        try:
            unity, types, objs = parse_sf(sf)
        except Exception:
            continue
        result.append((name, sf, unity, types, objs))
    if not result:
        raise ValueError(f'No SerializedFile found in {bundle.name}')
    return result


def get_id_from_name(name: str):
    patterns = [
        r'l2d[_-](\d{8,12})',
        r'hmr[_-](\d{8,12})',
        r'(\d{11})',
    ]
    for pat in patterns:
        m = re.search(pat, name, re.IGNORECASE)
        if m:
            return m.group(1)
    raise ValueError(f'Cannot determine character ID from filename: {name}')


def extract_runtime_assets(bundle_paths):
    """Use UnityPy only for native runtime assets: MOC3 bytes + Texture2D images."""
    import UnityPy
    moc_candidates = []
    textures = {}
    for path in bundle_paths:
        env = UnityPy.load(str(path))
        for obj in env.objects:
            typ = getattr(getattr(obj, 'type', None), 'name', '')
            if typ in ('MonoBehaviour', 'TextAsset'):
                try:
                    raw = obj.get_raw_data()
                except Exception:
                    raw = b''
                if not raw:
                    try:
                        d = obj.read()
                        if hasattr(d, 'bytes') and d.bytes:
                            raw = bytes(d.bytes)
                        elif hasattr(d, 'm_Script') and d.m_Script:
                            raw = bytes(d.m_Script) if isinstance(d.m_Script, (bytes, bytearray)) else str(d.m_Script).encode('utf-8', 'ignore')
                    except Exception:
                        raw = b''
                if raw and b'MOC3' in raw:
                    idx = raw.find(b'MOC3')
                    moc = raw[idx:]
                    if len(moc) > 1000:
                        moc_candidates.append(moc)
            elif typ == 'Texture2D':
                try:
                    d = obj.read()
                    img = getattr(d, 'image', None)
                    name = getattr(d, 'name', '') or getattr(d, 'm_Name', '')
                    if img and name:
                        textures[name] = img.convert('RGBA').copy()
                except Exception:
                    pass
    if not moc_candidates:
        raise ValueError('No MOC3 found in dragged bundles')
    moc = max(moc_candidates, key=len)
    # Prefer known model texture names; exclude GrabMask.
    filtered = {k:v for k,v in textures.items() if 'grabmask' not in k.lower()}
    preferred = {k:v for k,v in filtered.items() if 'texture_' in k.lower()}
    if preferred:
        filtered = preferred
    if not filtered:
        raise ValueError('No model Texture2D found in dragged bundles')
    return moc, filtered


def exact_red_remove(img: Image.Image):
    data = list(img.convert('RGBA').getdata())
    out = [(0,0,0,0) if (r == 255 and g == 0 and b <= 5) else (r,g,b,a) for r,g,b,a in data]
    x = Image.new('RGBA', img.size)
    x.putdata(out)
    return x


def recount_motion(motion):
    total_segments = total_points = 0
    for curve in motion.get('Curves', []):
        seg = curve['Segments']
        i = 2
        points = 1
        segments = 0
        while i < len(seg):
            st = int(seg[i]); i += 1; segments += 1
            if st in (0, 2, 3):
                if i + 2 > len(seg): raise ValueError('Truncated motion segment')
                i += 2; points += 1
            elif st == 1:
                if i + 6 > len(seg): raise ValueError('Truncated Bezier segment')
                i += 6; points += 3
            else:
                raise ValueError(f'Unknown motion segment type {st}')
        total_segments += segments
        total_points += points
    motion['Meta']['CurveCount'] = len(motion.get('Curves', []))
    motion['Meta']['TotalSegmentCount'] = total_segments
    motion['Meta']['TotalPointCount'] = total_points
    return motion


def _is_aux_motion_bundle(path: Path) -> bool:
    return is_fade_bundle(path)


def _is_motion_source_bundle(path: Path) -> bool:
    # Only explicit addanimations/animations bundles are motion sources.
    # The standalone fademotionlist/.fade.asset resource remains configuration
    # only and must never contribute AnimationClips to motion3 generation.
    return is_motion_extension_bundle(path)


def process_group(char_id, bundle_paths):
    """Build one Story-scoped Cubism package from Core + addanimations bundles."""
    out_root = OUTPUT_BASE / char_id
    # Never delete the whole Story directory here. Stage 1/2 already wrote
    # textassets and audio, and a failed L2D pass must never erase them.
    # Each successfully converted motion is written immediately to motions/.
    (out_root / 'moc').mkdir(parents=True, exist_ok=True)
    (out_root / 'textures').mkdir(parents=True, exist_ok=True)
    (out_root / 'motions').mkdir(parents=True, exist_ok=True)

    core_bundles = [p for p in bundle_paths if not _is_aux_motion_bundle(p)]
    aux_bundles = [p for p in bundle_paths if _is_motion_source_bundle(p)]
    if not core_bundles:
        raise ValueError(f'No Core/Prefab L2D bundles for {char_id}')

    moc, textures = extract_runtime_assets(core_bundles)
    (out_root / 'moc' / f'l2d_{char_id}.moc3').write_bytes(moc)

    texture_paths = []
    for name in sorted(textures):
        safe = Path(name).name
        if not safe.lower().endswith('.png'):
            safe += '.png'
        exact_red_remove(textures[name]).save(out_root / 'textures' / safe, 'PNG')
        texture_paths.append(f'textures/{safe}')

    # Core bindings define the authoritative MOC3 parameter namespace.
    core_hash_to_id = {}
    core_path_to_id = {}
    for bundle in core_bundles:
        for _sf_name, raw, _unity, _types, objs in extract_serialized_files(bundle):
            _hash_to_path, hash_to_id, _paths, path_to_id = extract_parameter_paths(raw, objs)
            core_hash_to_id.update(hash_to_id)
            core_path_to_id.update(path_to_id)
    if not core_hash_to_id:
        raise ValueError(f'No Cubism parameter bindings could be mapped for {char_id}')

    fade_map = collect_fade_configs(bundle_paths)
    motion_meta = []
    seen_motion_names = set()
    missing_paths = set()

    def emit_bundle_motions(bundle, source_kind):
        for _sf_name, raw, _unity, _types, objs in extract_serialized_files(bundle):
            for o in (x for x in objs if x['cid'] == 74):
                clip = read_animation_clip(raw, o)
                name = str(clip['name'] or '').strip()
                if not name or name in seen_motion_names:
                    continue
                # ===== Stage A: raw conversion =====
                # Convert the AnimationClip using ONLY the data in the L2D
                # AnimationClip.  No Fade asset, no Story information, no
                # post-processing is allowed to influence this first write.
                # This file is the immutable baseline for completeness.
                raw_motion_json, miss, has_curves = convert_motion(
                    clip, core_hash_to_id, core_path_to_id, fade_data=None
                )
                missing_paths.update(miss)

                raw_motion_json['Meta']['Source'] = source_kind
                raw_motion_json['Meta']['SourceBundle'] = bundle.name
                raw_motion_json = recount_motion(raw_motion_json)
                if len(raw_motion_json.get('Curves', [])) < len(clip.get('floatCurves', []) or []):
                    print(f'[WARN] {name}: raw motion curves={len(raw_motion_json.get("Curves", []))} source floatCurves={len(clip.get("floatCurves", []) or [])}')

                raw_dir = out_root / '.motion_stage_tmp'
                raw_dir.mkdir(parents=True, exist_ok=True)
                raw_motion_path = raw_dir / f'{name}.motion3.json'
                raw_motion_path.write_text(
                    json.dumps(raw_motion_json, ensure_ascii=False, indent=2),
                    encoding='utf-8'
                )

                # Preserve the source binding/curve payload independently of
                # the Cubism projection.  If a future supplement step fails,
                # these original curves remain available for inspection and
                # re-processing instead of disappearing into a cache.
                raw_source_path = raw_dir / f'{name}.source.json'
                raw_source = {
                    'name': name,
                    'sampleRate': clip.get('sampleRate', 30.0),
                    'loopTime': bool(clip.get('loopTime')),
                    'startTime': clip.get('startTime', 0.0),
                    'stopTime': clip.get('stopTime', 0.0),
                    'bindings': clip.get('bindings', []),
                    'floatCurves': clip.get('floatCurves', []),
                    'events': clip.get('events', []),
                    'unresolvedBindings': sorted(set(str(x) for x in miss if x)),
                }
                raw_source_path.write_text(
                    json.dumps(raw_source, ensure_ascii=False, indent=2),
                    encoding='utf-8'
                )

                if not has_curves:
                    # A raw artifact still exists, but there is nothing valid
                    # for the final Cubism motion index to reference.
                    continue

                # ===== Stage B: supplementation =====
                # Start from the already-persisted raw Motion3. Fade/parameter
                # metadata is the only thing allowed to change here. If this
                # phase fails, promote the raw artifact unchanged instead of
                # losing a working motion.
                out_motion = out_root / 'motions' / f'{name}.motion3.json'
                try:
                    motion_json = json.loads(raw_motion_path.read_text(encoding='utf-8'))
                    fade_in, fade_out = fade_for_motion(fade_map, name)
                    if fade_in is not None:
                        motion_json['Meta']['FadeInTime'] = fade_in
                    if fade_out is not None:
                        motion_json['Meta']['FadeOutTime'] = fade_out

                    per_param_fade = parameter_fade_for_motion(fade_map, name)
                    for curve in motion_json.get('Curves', []):
                        pid = str(curve.get('Id', ''))
                        fd = per_param_fade.get(pid.casefold())
                        if not fd:
                            continue
                        if 'FadeInTime' in fd and fd['FadeInTime'] >= 0:
                            curve['FadeInTime'] = float(fd['FadeInTime'])
                        if 'FadeOutTime' in fd and fd['FadeOutTime'] >= 0:
                            curve['FadeOutTime'] = float(fd['FadeOutTime'])

                    motion_json = recount_motion(motion_json)
                    out_motion.write_text(
                        json.dumps(motion_json, ensure_ascii=False, indent=2),
                        encoding='utf-8'
                    )
                except Exception as supplement_error:
                    # Stage A is authoritative. Never let Fade/model3
                    # supplementation destroy or suppress a successfully
                    # converted raw Motion.
                    shutil.copy2(raw_motion_path, out_motion)
                    motion_json = json.loads(raw_motion_path.read_text(encoding='utf-8'))
                    print(f'    [WARN] {char_id}/{name}: supplement skipped, raw motion retained: {supplement_error}')

                seen_motion_names.add(name)
                motion_meta.append((name, motion_json['Meta']))

    # Core first: if the same motion name exists in an auxiliary bundle, the
    # authoritative Core version wins. Auxiliary bundles then fill missing names.
    for bundle in core_bundles:
        emit_bundle_motions(bundle, 'l2d_core')
    for bundle in aux_bundles:
        emit_bundle_motions(bundle, 'l2d_addanimation')

    motions = {}
    for name, meta in sorted(motion_meta, key=lambda x: x[0].casefold()):
        group = get_canonical_group_name(name)
        item = {
            'File': f'motions/{name}.motion3.json',
        }
        # Keep Model3-level Fade in sync with exported Motion3. Cubism gives
        # Model3-level Fade higher priority than Motion3-level Fade.
        if 'FadeInTime' in meta:
            item['FadeInTime'] = float(meta.get('FadeInTime', 0.0))
        if 'FadeOutTime' in meta:
            item['FadeOutTime'] = float(meta.get('FadeOutTime', 0.0))
        motions.setdefault(group, []).append(item)

    model3 = {
        'Version': 3,
        'FileReferences': {
            'Moc': f'moc/l2d_{char_id}.moc3',
            'Textures': texture_paths,
            'Motions': motions,
        },
        'Groups': [
            {'Target': 'Parameter', 'Name': 'EyeBlink', 'Ids': ['ParamEyeLOpen', 'ParamEyeROpen']},
            {'Target': 'Parameter', 'Name': 'LipSync', 'Ids': ['ParamMouthOpenY']},
        ],
        'HitAreas': [],
    }
    model3_path = out_root / f'{char_id}.model3.json'
    model3_tmp = out_root / f'{char_id}.model3.json.tmp'
    model3_tmp.write_text(json.dumps(model3, ensure_ascii=False, indent=2), encoding='utf-8')
    model3_tmp.replace(model3_path)

    # Raw Motion files are an internal safety stage only. They are never part
    # of the final Story package. Remove them after all motions/model3 have
    # been committed; if cleanup itself fails, report it without deleting the
    # final motion files.
    try:
        raw_tmp = out_root / '.motion_stage_tmp'
        if raw_tmp.exists():
            shutil.rmtree(raw_tmp)
    except Exception as _cleanup_exc:
        print(f'    [WARN] {char_id}: temporary motion stage cleanup failed: {_cleanup_exc}')

    return {
        'id': char_id,
        'motion_names': sorted(seen_motion_names, key=str.casefold),
        'motion_count': len(motion_meta),
        'aux_motion_count': sum(1 for _, m in motion_meta if m.get('Source') == 'l2d_addanimation'),
        'fade_count': len(fade_map),
        'missing_parameter_paths': sorted(missing_paths),
    }

def main():
    import traceback
    if len(sys.argv) < 2:
        print('Drag one or more .bundle files onto run_l2d.bat')
        return 2
    groups = {}
    for arg in sys.argv[1:]:
        p = Path(arg.strip('"'))
        try:
            cid = get_id_from_name(p.name)
        except Exception as e:
            print(f'[SKIP] {p.name}: {e}')
            continue
        groups.setdefault(cid, []).append(p)
    if not groups:
        return 1
    failed = 0
    for cid, paths in sorted(groups.items()):
        # Automatically include sibling bundles with the same ID, so dragging
        # either the prefab bundle or its companion asset bundle is sufficient.
        merged = {p.resolve() for p in paths}
        for base in list(paths):
            for sibling in base.parent.glob('*.bundle'):
                try:
                    if get_id_from_name(sibling.name) == cid:
                        merged.add(sibling.resolve())
                except Exception:
                    pass
        paths = sorted((Path(x) for x in merged), key=lambda x: x.name)
        try:
            process_group(cid, paths)
        except Exception as e:
            failed += 1
            print(f'[FAILED] {cid}: {e}')
            traceback.print_exc()
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
