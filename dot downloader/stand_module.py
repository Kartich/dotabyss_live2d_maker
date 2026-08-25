# -*- coding: utf-8 -*-
"""Character stand extractor.

Output follows the character-package structure from test(4).py:

    data/chara/<character_id_lower>/
        body.png
        faces/
            <SpriteName>.png
        meta.json

Important: Body and face sprites are exported separately; no final composed
portrait is generated. Face sprite placement/restoration uses the validated
RectTransform/FaceContent positioning logic from the earlier test(3) work.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image
import UnityPy
from UnityPy.enums import ClassIDType

ROOT = Path(__file__).resolve().parent
OUTPUT_CHARA_DIR = ROOT / "data" / "chara"

# Same useful expression names seen in the stand assets. The exporter also
# keeps any additional non-body sprites in the atlas, matching test(4).py.
EXPRESSION_ORDER = [
    "Normal", "Happy", "Sad", "Fun", "Anger",
    "Surprise", "Shy", "Closed", "Unique01", "Unique02",
]


def path_id(v):
    if v is None:
        return 0
    if hasattr(v, "m_PathID"):
        try:
            return int(v.m_PathID)
        except Exception:
            pass
    if isinstance(v, dict):
        for k in ("m_PathID", "pathID", "path_id"):
            try:
                return int(v.get(k, 0) or 0)
            except Exception:
                pass
    return 0


def read_dict(obj):
    try:
        d = obj.read_typetree()
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    try:
        d = obj.parse_as_dict()
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return None


def vec_dict(v, default=None):
    if v is None:
        if default is None:
            default = (0.0, 0.0, 0.0)
        return {"x": float(default[0]), "y": float(default[1]), "z": float(default[2])}
    return {
        "x": float(getattr(v, "x", 0.0)),
        "y": float(getattr(v, "y", 0.0)),
        "z": float(getattr(v, "z", 0.0)),
    }


def vec2_dict(v, default=(0.0, 0.0)):
    if v is None:
        return {"x": float(default[0]), "y": float(default[1])}
    return {
        "x": float(getattr(v, "x", 0.0)),
        "y": float(getattr(v, "y", 0.0)),
    }


def clean_rect(node: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not node:
        return {
            "anchoredPosition": {"x": 0.0, "y": 0.0},
            "sizeDelta": {"x": 0.0, "y": 0.0},
            "anchorMin": {"x": 0.0, "y": 0.0},
            "anchorMax": {"x": 1.0, "y": 1.0},
            "pivot": {"x": 0.5, "y": 0.5},
            "localPosition": {"x": 0.0, "y": 0.0, "z": 0.0},
            "localScale": {"x": 1.0, "y": 1.0, "z": 1.0},
            "father": 0,
            "children": [],
            "rectId": 0,
            "worldPosition": {"x": 0.0, "y": 0.0},
        }
    return {
        "anchoredPosition": dict(node.get("anchoredPosition", {"x": 0.0, "y": 0.0})),
        "sizeDelta": dict(node.get("sizeDelta", {"x": 0.0, "y": 0.0})),
        "anchorMin": dict(node.get("anchorMin", {"x": 0.0, "y": 0.0})),
        "anchorMax": dict(node.get("anchorMax", {"x": 1.0, "y": 1.0})),
        "pivot": dict(node.get("pivot", {"x": 0.5, "y": 0.5})),
        "localPosition": dict(node.get("localPosition", {"x": 0.0, "y": 0.0, "z": 0.0})),
        "localScale": dict(node.get("localScale", {"x": 1.0, "y": 1.0, "z": 1.0})),
        "father": int(node.get("father", 0) or 0),
        "children": [int(x) for x in node.get("children", [])],
        "rectId": int(node.get("rectId", 0) or 0),
        "worldPosition": dict(node.get("worldPosition", {"x": 0.0, "y": 0.0})),
    }


def collect_rt_layout(env):
    """Collect RectTransforms, using the same anchoredPosition recursion as test(3)."""
    go_names: Dict[int, str] = {}

    for obj in env.objects:
        if obj.type == ClassIDType.GameObject:
            try:
                go = obj.read()
                go_names[int(obj.path_id)] = str(getattr(go, "m_Name", "") or "")
            except Exception:
                pass

    def get_path_id(v):
        if v is None:
            return 0
        if hasattr(v, "m_PathID"):
            try:
                return int(v.m_PathID)
            except Exception:
                return 0
        if isinstance(v, dict):
            return int(v.get("m_PathID", 0) or 0)
        return 0

    raw = {}
    for obj in env.objects:
        type_id = obj.type.value if hasattr(obj.type, "value") else obj.type
        if obj.type != ClassIDType.RectTransform and type_id != 224:
            continue
        try:
            rt = obj.read()
            rid = int(obj.path_id)
            go_id = get_path_id(getattr(rt, "m_GameObject", None))
            name = go_names.get(go_id, "")
            if not name:
                continue

            father = get_path_id(getattr(rt, "m_Father", None))
            children = []
            for child in getattr(rt, "m_Children", []) or []:
                cid = get_path_id(child)
                if cid:
                    children.append(cid)

            parent_name = ""
            if father:
                try:
                    parent_go = getattr(rt.m_Father, "m_GameObject", None)
                    parent_name = go_names.get(get_path_id(parent_go), "")
                except Exception:
                    pass

            raw[rid] = {
                "go_name": name,
                "anchoredPosition": vec2_dict(getattr(rt, "m_AnchoredPosition", None)),
                "sizeDelta": vec2_dict(getattr(rt, "m_SizeDelta", None)),
                "anchorMin": vec2_dict(getattr(rt, "m_AnchorMin", None), (0.5, 0.5)),
                "anchorMax": vec2_dict(getattr(rt, "m_AnchorMax", None), (0.5, 0.5)),
                "pivot": vec2_dict(getattr(rt, "m_Pivot", None), (0.5, 0.5)),
                "localPosition": vec_dict(getattr(rt, "m_LocalPosition", None)),
                "localScale": vec_dict(getattr(rt, "m_LocalScale", None), (1.0, 1.0, 1.0)),
                "father": father,
                "children": children,
                "rectId": rid,
                "parent": parent_name,
            }
        except Exception:
            pass

    def world(rid):
        node = raw.get(rid)
        if not node:
            return {"x": 0.0, "y": 0.0}
        if "worldPosition" in node:
            return node["worldPosition"]
        ax = node["anchoredPosition"]["x"]
        ay = node["anchoredPosition"]["y"]
        father = node["father"]
        if father and father in raw:
            p = world(father)
            result = {"x": ax + p["x"], "y": ay + p["y"]}
        else:
            result = {"x": ax, "y": ay}
        node["worldPosition"] = result
        return result

    for rid in list(raw):
        world(rid)

    layout = {}
    for node in raw.values():
        layout[node["go_name"]] = node
    return layout


def extract_face_layout(layout_tree):
    face = layout_tree.get("FaceContent")
    body = layout_tree.get("Body")
    if face and body:
        # Keep the earlier validated positioning method. Do NOT require
        # FaceContent to be a direct child of Body; real stand variants may not.
        return {
            "face_anchor_x": face["anchoredPosition"]["x"],
            "face_anchor_y": face["anchoredPosition"]["y"],
            "face_w": face["sizeDelta"]["x"],
            "face_h": face["sizeDelta"]["y"],
            "body_w": body["sizeDelta"]["x"],
            "body_h": body["sizeDelta"]["y"],
        }
    return None


def prepare_sprite_layer(expr_data, target_w, target_h):
    expr_rect_w = expr_data["rect_w"] if expr_data["rect_w"] > 0 else expr_data["img_w"]
    expr_rect_h = expr_data["rect_h"] if expr_data["rect_h"] > 0 else expr_data["img_h"]
    expr_full = Image.new("RGBA", (expr_rect_w, expr_rect_h), (0, 0, 0, 0))
    tro_x = int(round(expr_data["tro_x"]))
    tro_y_flipped = expr_rect_h - int(round(expr_data["tro_y"])) - expr_data["img_h"]
    expr_full.alpha_composite(expr_data["img"], dest=(max(0, tro_x), max(0, tro_y_flipped)))
    if target_w > 0 and target_h > 0:
        return expr_full.resize((target_w, target_h), Image.Resampling.LANCZOS)
    return expr_full


def sprite_info(sprite):
    if not getattr(sprite, "image", None):
        return None
    rect = getattr(sprite, "m_Rect", None)
    rect_w = int(rect.width) if rect is not None and hasattr(rect, "width") else 0
    rect_h = int(rect.height) if rect is not None and hasattr(rect, "height") else 0
    tro_x = tro_y = 0.0
    rd = getattr(sprite, "m_RD", None)
    off = getattr(rd, "textureRectOffset", None) if rd is not None else None
    if off is not None:
        tro_x = float(getattr(off, "x", 0.0))
        tro_y = float(getattr(off, "y", 0.0))
    return {
        "name": str(getattr(sprite, "m_Name", "unknown")),
        "img": sprite.image.convert("RGBA").copy(),
        "img_w": sprite.image.size[0],
        "img_h": sprite.image.size[1],
        "rect_w": rect_w,
        "rect_h": rect_h,
        "tro_x": tro_x,
        "tro_y": tro_y,
    }


def atlas_sprites(env):
    all_sprites = {}
    for obj in env.objects:
        if obj.type == ClassIDType.Sprite:
            try:
                all_sprites[int(obj.path_id)] = obj.read()
            except Exception:
                pass

    for obj in env.objects:
        if obj.type != ClassIDType.SpriteAtlas:
            continue
        try:
            atlas = obj.read()
            atlas_name = str(getattr(atlas, "m_Name", "Unknown"))
            packed = []
            for pptr in getattr(atlas, "m_PackedSprites", []) or []:
                pid = int(getattr(pptr, "m_PathID", 0) or 0)
                if pid and pid in all_sprites:
                    packed.append(all_sprites[pid])
                elif pid:
                    try:
                        resolved = pptr.resolve()
                        if resolved:
                            packed.append(resolved.read())
                    except Exception:
                        pass
            yield atlas_name, packed
        except Exception:
            continue


def atlas_character_id(atlas_name: str, bundle: Path) -> str:
    # test(4).py uses the atlas name first: 9 digits + optional g/x.
    m = re.search(r"(\d{9}[a-zA-Z]?)", atlas_name)
    if m:
        return m.group(1).upper()
    m = re.search(r"charastand(\d{9}[a-zA-Z]?)", bundle.name, re.I)
    if m:
        return m.group(1).upper()
    raise RuntimeError(f"Cannot determine character id from atlas/bundle: {atlas_name} / {bundle.name}")


def output_character(env, bundle: Path):
    layout_tree = collect_rt_layout(env)
    face_info = extract_face_layout(layout_tree)

    written = []
    atlases = list(atlas_sprites(env))
    if not atlases:
        raise RuntimeError("No SpriteAtlas found.")

    for atlas_name, sprites in atlases:
        if not sprites:
            continue

        cid = atlas_character_id(atlas_name, bundle)
        cid_lower = cid.lower()
        out_dir = OUTPUT_CHARA_DIR / cid_lower
        faces_dir = out_dir / "faces"
        out_dir.mkdir(parents=True, exist_ok=True)
        faces_dir.mkdir(parents=True, exist_ok=True)

        sprite_map = {}
        for sprite in sprites:
            name = str(getattr(sprite, "m_Name", "unknown"))
            if name == "_stand1" or name.lower().startswith("unnamed"):
                continue
            info = sprite_info(sprite)
            if info:
                sprite_map[name] = info

        if not sprite_map:
            continue

        body_data = None
        expression_list = []
        for name, data in sprite_map.items():
            low = name.lower()
            if "body" in low or "base" in low:
                if body_data is None or data["img_w"] * data["img_h"] > body_data["img_w"] * body_data["img_h"]:
                    if body_data is not None:
                        expression_list.append(body_data)
                    body_data = data
                else:
                    expression_list.append(data)
            else:
                expression_list.append(data)

        if body_data is None:
            print(f"  [WARN] Atlas {atlas_name}: Body not found, skipped.")
            continue

        body_img = body_data["img"].copy()
        body_path = out_dir / "body.png"
        body_img.save(body_path, "PNG")

        # Determine root exactly as test(4).py does.
        root_key = next((k for k in layout_tree if k.lower().endswith("stand") or k == cid), None)
        if root_key is None and layout_tree:
            root_key = next((k for k, v in layout_tree.items() if v.get("father", 0) == 0), next(iter(layout_tree)))

        # test(4).py exports all non-body atlas sprites under faces/.
        # Preserve deterministic ordering: known expressions first, extras afterwards.
        def order_key(data):
            try:
                return (0, EXPRESSION_ORDER.index(data["name"]))
            except ValueError:
                return (1, data["name"].lower())

        expression_list.sort(key=order_key)

        meta = {
            "id": cid,
            "sourceBundle": f"workspace/bundles/{bundle.name}",
            "rootRect": clean_rect(layout_tree.get(root_key)),
            "bodyRect": clean_rect(layout_tree.get("Body")),
            "faceContentRect": clean_rect(layout_tree.get("FaceContent")),
            "emotionRect": clean_rect(layout_tree.get("Emotion", layout_tree.get("EmotionContent"))),
            "effectRect": clean_rect(layout_tree.get("Effect", layout_tree.get("EffectContent"))),
            "zoomRect": clean_rect(layout_tree.get("Zoom")),
            "poseRect": clean_rect(layout_tree.get("Pose")),
            "files": {"body": "body.png"},
            "faces": {},
            "spriteSizes": {},
        }
        meta["spriteSizes"]["Body"] = {
            "width": body_data["rect_w"] if body_data["rect_w"] > 0 else body_data["img_w"],
            "height": body_data["rect_h"] if body_data["rect_h"] > 0 else body_data["img_h"],
        }

        # Positioning = validated FaceContent-vs-Body calculation from the
        # earlier work, but each face is still saved separately.
        target_fw = target_fh = 0
        if face_info and expression_list:
            fallback_w = next((x["rect_w"] for x in expression_list if x["rect_w"] > 0), expression_list[0]["img_w"])
            fallback_h = next((x["rect_h"] for x in expression_list if x["rect_h"] > 0), expression_list[0]["img_h"])
            face_w = face_info["face_w"] if face_info["face_w"] > 0 else fallback_w
            face_h = face_info["face_h"] if face_info["face_h"] > 0 else fallback_h
            sx = body_img.size[0] / face_info["body_w"] if face_info["body_w"] > 0 else 1.0
            sy = body_img.size[1] / face_info["body_h"] if face_info["body_h"] > 0 else 1.0
            target_fw = int(round(face_w * sx))
            target_fh = int(round(face_h * sy))

        for data in expression_list:
            expr_name = data["name"]
            face_canvas = prepare_sprite_layer(data, target_fw, target_fh)
            face_path = faces_dir / f"{expr_name}.png"
            face_canvas.save(face_path, "PNG")
            meta["faces"][expr_name.lower()] = f"faces/{expr_name}.png"
            meta["spriteSizes"][expr_name] = {
                "width": data["rect_w"] if data["rect_w"] > 0 else data["img_w"],
                "height": data["rect_h"] if data["rect_h"] > 0 else data["img_h"],
            }

        with open(out_dir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)

        written.append((cid, out_dir, len(expression_list)))

    if not written:
        raise RuntimeError("No character atlas was successfully exported.")

    for cid, out_dir, count in written:
        print(f"[OK] {cid}: body + {count} face sprites + meta -> {out_dir}")
    return written


def process(path: Path):
    env = UnityPy.load(str(path))
    return output_character(env, path)


def main(argv=None):
    argv = sys.argv if argv is None else argv
    if len(argv) < 2:
        print("Drag charastand bundle(s) onto the launcher.")
        return 2
    failed = 0
    for raw in argv[1:]:
        p = Path(raw.strip('"'))
        try:
            process(p)
        except Exception as e:
            failed += 1
            print(f"[FAILED] {p.name}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
