import os
import sys
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from PIL import Image
import UnityPy
from UnityPy.enums import ClassIDType

# ==========================================
# 1. 基础路径配置
# ==========================================
RAW_BUNDLE_DIR = Path("./raw_bundles")   # 原始立绘 .bundle 目录
OUTPUT_CHARA_DIR = Path("./data/chara")  # 播放器标准立绘输出路径

OUTPUT_CHARA_DIR.mkdir(parents=True, exist_ok=True)

class CharaStandExtractor:
    def __init__(self):
        self.stats = {
            "atlas_processed": 0,
            "characters_exported": 0,
        }

    def _get_default_rect(self) -> Dict[str, Any]:
        """ 当 Prefab 中高级嵌套缺失某些可选空间节点时，用于合规填充的空白标准格式模板 """
        return {
            "anchoredPosition": {"x": 0.0, "y": 0.0},
            "sizeDelta": {"x": 0.0, "y": 0.0},
            "anchorMin": {"x": 0.5, "y": 0.5},
            "anchorMax": {"x": 0.5, "y": 0.5},
            "pivot": {"x": 0.5, "y": 0.5},
            "localPosition": {"x": 0.0, "y": 0.0, "z": 0.0},
            "localScale": {"x": 1.0, "y": 1.0, "z": 1.0},
            "father": 0,
            "children": [],
            "rectId": 0,
            "worldPosition": {"x": 0.0, "y": 0.0}
        }

    def _collect_rt_layout(self, env) -> Dict[str, Dict[str, Any]]:
        """ 
        严格参照原生反序列化规范：
        1. 交叉索引 GameObject 与 RectTransform，精准提取各节点 64 位原生 PathID 关系链
        2. 基于父子依赖树，级联递归累加计算出绝对 worldPosition 偏置
        """
        go_names: Dict[int, str] = {}
        for obj in env.objects:
            if obj.type == ClassIDType.GameObject:
                try:
                    go = obj.read()
                    go_names[obj.path_id] = getattr(go, "m_Name", "")
                except:
                    pass

        # 辅助 PPtr 转换函数：精准提取内部跨组件引用的 64 位唯一 PathID
        def get_path_id(pptr) -> int:
            if pptr is None:
                return 0
            if hasattr(pptr, "m_PathID"):
                return pptr.m_PathID
            if isinstance(pptr, dict):
                return pptr.get("m_PathID", 0)
            return 0

        def parse_xy(vec):
            return {"x": float(getattr(vec, "x", 0.0)), "y": float(getattr(vec, "y", 0.0))} if vec else {"x": 0.0, "y": 0.0}
        def parse_xyz(vec):
            return {"x": float(getattr(vec, "x", 0.0)), "y": float(getattr(vec, "y", 0.0)), "z": float(getattr(vec, "z", 0.0))} if vec else {"x": 0.0, "y": 0.0, "z": 0.0}

        raw_rt_dict = {}
        for obj in env.objects:
            type_id = obj.type.value if hasattr(obj.type, "value") else obj.type
            if obj.type != ClassIDType.RectTransform and type_id != 224:
                continue
            try:
                rt = obj.read()
                rt_id = obj.path_id
                
                go_id = get_path_id(getattr(rt, "m_GameObject", None))
                go_name = go_names.get(go_id, "")
                if not go_name:
                    continue

                father_id = get_path_id(getattr(rt, "m_Father", None))
                children_ids = []
                if hasattr(rt, "m_Children") and rt.m_Children:
                    for child in rt.m_Children:
                        c_id = get_path_id(child)
                        if c_id:
                            children_ids.append(c_id)

                parent_name = go_names.get(get_path_id(getattr(rt.m_Father, "m_GameObject", None)), "") if father_id else ""

                anchored_pos = parse_xy(getattr(rt, "m_AnchoredPosition", None))
                size_delta = parse_xy(getattr(rt, "m_SizeDelta", None))

                raw_rt_dict[rt_id] = {
                    "go_name": go_name,
                    "anchoredPosition": anchored_pos,
                    "sizeDelta": size_delta,
                    "anchorMin": parse_xy(getattr(rt, "m_AnchorMin", None)),
                    "anchorMax": parse_xy(getattr(rt, "m_AnchorMax", None)),
                    "pivot": parse_xy(getattr(rt, "m_Pivot", None)),
                    "localPosition": parse_xyz(getattr(rt, "m_LocalPosition", None)),
                    "localScale": parse_xyz(getattr(rt, "m_LocalScale", None)),
                    "father": father_id,
                    "children": children_ids,
                    "rectId": rt_id,
                    # 级联对齐算法强依赖的内部简称键名
                    "ax": anchored_pos["x"],
                    "ay": anchored_pos["y"],
                    "sw": size_delta["x"],
                    "sh": size_delta["y"],
                    "parent": parent_name
                }
            except:
                pass

        # 核心级联拓扑演算法：顺着 father 链向上递归累加，计算出完全真实的绝对世界坐标 (worldPosition)
        def get_absolute_world_pos(path_id: int) -> Dict[str, float]:
            node = raw_rt_dict.get(path_id)
            if not node:
                return {"x": 0.0, "y": 0.0}
            if "worldPosition" in node:
                return node["worldPosition"]
                
            ax = node["anchoredPosition"]["x"]
            ay = node["anchoredPosition"]["y"]
            f_id = node["father"]
            
            if f_id and f_id in raw_rt_dict:
                f_world = get_absolute_world_pos(f_id)
                w_pos = {"x": ax + f_world["x"], "y": ay + f_world["y"]}
            else:
                w_pos = {"x": ax, "y": ay}
                
            node["worldPosition"] = w_pos
            return w_pos

        for pid in raw_rt_dict:
            get_absolute_world_pos(pid)

        final_layout_tree = {}
        for pid, node in raw_rt_dict.items():
            final_layout_tree[node["go_name"]] = node
        return final_layout_tree

    def _extract_face_layout(self, layout_tree: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """ 从底层空间矩阵结构中提取 FaceContent 相对于 Body 的几何参数映射 """
        face = layout_tree.get("FaceContent")
        body = layout_tree.get("Body")

        if face and body and face["parent"] == "Body":
            return {
                "face_anchor_x": face["ax"],
                "face_anchor_y": face["ay"],
                "face_w": face["sw"],
                "face_h": face["sh"],
                "body_w": body["sw"],
                "body_h": body["sh"],
            }
        return None

    def _prepare_sprite_layer(self, expr_data: Dict[str, Any], target_w: int, target_h: int) -> Image.Image:
        """ 完美复刻 Git 脚本对齐核心：还原表情的面部边缘边距画布，并缩放到标准物理尺寸 """
        expr_rect_w = expr_data["rect_w"] if expr_data["rect_w"] > 0 else expr_data["img_w"]
        expr_rect_h = expr_data["rect_h"] if expr_data["rect_h"] > 0 else expr_data["img_h"]

        # 创建原始未裁剪 Rect 大小的透明底图画布
        expr_full = Image.new("RGBA", (expr_rect_w, expr_rect_h), (0, 0, 0, 0))
        tro_x = int(round(expr_data["tro_x"]))
        tro_y_flipped = expr_rect_h - int(round(expr_data["tro_y"])) - expr_data["img_h"]
        tro_y_flipped = max(0, tro_y_flipped)
        tro_x = max(0, tro_x)
        
        # 将裁剪图混合还原回原本的空间画布中
        expr_full.alpha_composite(expr_data["img"], dest=(tro_x, tro_y_flipped))

        # 核心缩放：将其自适应调整为播放器对应的物理像素尺寸
        if target_w > 0 and target_h > 0:
            return expr_full.resize((target_w, target_h), Image.Resampling.LANCZOS)
        return expr_full

    def process_bundle(self, bundle_path: Path):
        """ 100% 直出原始身体，并对表情组件执行原版无损画布恢复与尺寸等比缩放 """
        try:
            env = UnityPy.load(str(bundle_path))
            
            all_sprites = {}
            for obj in env.objects:
                if obj.type == ClassIDType.Sprite:
                    try:
                        all_sprites[obj.path_id] = obj.read()
                    except:
                        pass

            # 抓取完整的空间矩阵树结构与世界级联树
            layout_tree = self._collect_rt_layout(env)
            face_info = self._extract_face_layout(layout_tree)

            for obj in env.objects:
                if obj.type == ClassIDType.SpriteAtlas:
                    atlas = obj.read()
                    atlas_name = getattr(atlas, "m_Name", "Unknown")
                    
                    chara_id_match = re.search(r"(\d{9}[a-zA-Z]?)", atlas_name)
                    chara_id_raw = chara_id_match.group(1) if chara_id_match else atlas_name
                    
                    chara_id_lower = chara_id_raw.lower()
                    chara_id_upper = chara_id_raw.upper()
                    
                    chara_dir = OUTPUT_CHARA_DIR / chara_id_lower
                    faces_dir = chara_dir / "faces"
                    
                    chara_dir.mkdir(parents=True, exist_ok=True)
                    faces_dir.mkdir(parents=True, exist_ok=True)

                    sprites_in_atlas = []
                    if hasattr(atlas, "m_PackedSprites"):
                        for pptr in atlas.m_PackedSprites:
                            if pptr.m_PathID != 0:
                                if pptr.m_PathID in all_sprites:
                                    sprites_in_atlas.append(all_sprites[pptr.m_PathID])
                                else:
                                    resolved = pptr.resolve()
                                    if resolved:
                                        sprites_in_atlas.append(resolved.read())

                    if not sprites_in_atlas:
                        continue

                    sprite_map = {}
                    for sprite in sprites_in_atlas:
                        name = getattr(sprite, "m_Name", "unknown")
                        if name == "_stand1" or name.startswith("unnamed"):
                            continue
                        if not hasattr(sprite, "image") or sprite.image is None:
                            continue

                        m_rect = getattr(sprite, "m_Rect", None)
                        rect_w = int(m_rect.width) if m_rect and hasattr(m_rect, "width") else 0
                        rect_h = int(m_rect.height) if m_rect and hasattr(m_rect, "height") else 0

                        tro_x, tro_y = 0.0, 0.0
                        if hasattr(sprite, "m_RD") and hasattr(sprite.m_RD, "textureRectOffset"):
                            tro_x = sprite.m_RD.textureRectOffset.x
                            tro_y = sprite.m_RD.textureRectOffset.y

                        sprite_map[name] = {
                            "name": name,
                            "img": sprite.image,
                            "img_w": sprite.image.size[0],
                            "img_h": sprite.image.size[1],
                            "rect_w": rect_w,
                            "rect_h": rect_h,
                            "tro_x": tro_x,
                            "tro_y": tro_y,
                        }

                    if not sprite_map:
                        continue

                    # 自动过滤出 Body 基图
                    body_data = None
                    expression_list = []
                    for name, data in sprite_map.items():
                        name_lower = name.lower()
                        if "body" in name_lower or "base" in name_lower:
                            if not body_data or (data["img_w"] * data["img_h"] > body_data["img_w"] * body_data["img_h"]):
                                if body_data:
                                    expression_list.append(body_data)
                                body_data = data
                            else:
                                expression_list.append(data)
                        else:
                            expression_list.append(data)

                    if not body_data:
                        print(f"  [-] 警告: 图集 {atlas_name} 内未发现有效 Body 底图，跳过。")
                        continue

                    # 1. 百分之百纯净直出主身体 Body.png
                    body_img = body_data["img"]
                    canvas_w, canvas_h = body_img.size
                    body_img.save(chara_dir / "Body.png")
                    print(f"[*] 成功无损直出原始身体 -> {chara_id_lower}/Body.png")

                    # 寻找当前的 Root 节点作为配置入口
                    root_key = next((k for k in layout_tree.keys() if k.lower().endswith("stand") or k == chara_id_raw), None)
                    if not root_key and layout_tree:
                        root_key = next((k for k, v in layout_tree.items() if v["father"] == 0), list(layout_tree.keys())[0])

                    def clean_rect(node):
                        if not node:
                            return self._get_default_rect()
                        return {
                            "anchoredPosition": node["anchoredPosition"],
                            "sizeDelta": node["sizeDelta"],
                            "anchorMin": node["anchorMin"],
                            "anchorMax": node["anchorMax"],
                            "pivot": node["pivot"],
                            "localPosition": node["localPosition"],
                            "localScale": node["localScale"],
                            "father": node["father"],
                            "children": node["children"],
                            "rectId": node["rectId"],
                            "worldPosition": node["worldPosition"]
                        }

                    # 2. 组装输出高保真、包含完美级联树关系的 meta.json
                    meta_output = {
                        "id": chara_id_upper,
                        "sourceBundle": f"workspace/bundles/{bundle_path.name}",
                        "rootRect": clean_rect(layout_tree.get(root_key)),
                        "bodyRect": clean_rect(layout_tree.get("Body")),
                        "faceContentRect": clean_rect(layout_tree.get("FaceContent")),
                        "emotionRect": clean_rect(layout_tree.get("Emotion", layout_tree.get("EmotionContent"))),
                        "effectRect": clean_rect(layout_tree.get("Effect", layout_tree.get("EffectContent"))),
                        "zoomRect": clean_rect(layout_tree.get("Zoom")),
                        "poseRect": clean_rect(layout_tree.get("Pose")),
                        "files": {
                            "body": "Body.png"  # 大写匹配磁盘实体文件
                        },
                        "faces": {},
                        "spriteSizes": {}
                    }

                    meta_output["spriteSizes"]["Body"] = {
                        "width": body_data["rect_w"] if body_data["rect_w"] > 0 else body_data["img_w"],
                        "height": body_data["rect_h"] if body_data["rect_h"] > 0 else body_data["img_h"]
                    }

                    # 3. 严格沿用原版缩放比例系数，动态计算面部缩放视口物理大小
                    target_fw, target_fh = 0, 0
                    if face_info and expression_list:
                        face_w = face_info["face_w"] if face_info["face_w"] > 0 else expression_list[0]["rect_w"]
                        face_h = face_info["face_h"] if face_info["face_h"] > 0 else expression_list[0]["rect_h"]
                        body_rt_w = face_info["body_w"]
                        body_rt_h = face_info["body_h"]

                        # 还原图片像素分辨率与 RectTransform 设计单位的比率
                        scale_x = canvas_w / body_rt_w if body_rt_w > 0 else 1.0
                        scale_y = canvas_h / body_rt_h if body_rt_h > 0 else 1.0

                        target_fw = int(round(face_w * scale_x))
                        target_fh = int(round(face_h * scale_y))

                    # 4. 调用原生恢复模块导出各个表情，确保 aspect 比例与身体完美对齐一致
                    for expr_data in expression_list:
                        expr_name_raw = expr_data["name"]
                        expr_name_lower = expr_name_raw.lower()
                        
                        # 恢复并等比重采样至 target 视口大小
                        face_canvas = self._prepare_sprite_layer(expr_data, target_fw, target_fh)
                        face_canvas.save(faces_dir / f"{expr_name_raw}.png")
                        
                        # 在索引树中完成高精准登记
                        meta_output["faces"][expr_name_lower] = f"faces/{expr_name_raw}.png"
                        meta_output["spriteSizes"][expr_name_raw] = {
                            "width": expr_data["rect_w"] if expr_data["rect_w"] > 0 else expr_data["img_w"],
                            "height": expr_data["rect_h"] if expr_data["rect_h"] > 0 else expr_data["img_h"]
                        }

                    # 写出绝对精准的元配置文件
                    with open(chara_dir / "meta.json", "w", encoding="utf-8") as f_meta:
                        json.dump(meta_output, f_meta, indent=2, ensure_ascii=False)

                    print(f"  [➔] 成功 DUMP 级联拓扑对齐矩阵配置文件 meta.json。")
                    self.stats["characters_exported"] += 1
                    self.stats["atlas_processed"] += 1

        except Exception as e:
            print(f"[-] 严重错误: 处理 Bundle {bundle_path.name} 时发生不可预期的崩溃: {e}")

    def execute_pipeline(self):
        if not RAW_BUNDLE_DIR.exists():
            print(f"[-] 核心错误: 找不到存放原始 Bundle 的目录 '{RAW_BUNDLE_DIR}'")
            return

        all_bundles = list(RAW_BUNDLE_DIR.glob("*.bundle"))
        if not all_bundles:
            print(f"[-] 未在 '{RAW_BUNDLE_DIR}' 目录下检索到任何有效的 .bundle 资产。")
            return

        print(f"[*] 启动立绘全原生矩阵拓扑、级联世界树还原与像素比例对齐提取管线...")
        for bundle_path in all_bundles:
            if "chara_" in bundle_path.name or "stand" in bundle_path.name.lower():
                self.process_bundle(bundle_path)

        print(f"\n[★] 立绘组件分离与全级联关系矩阵 DUMP 全部完美合闭！")
        print(f"    - 成功规范化同步角色数: {self.stats['characters_exported']}")
        print(f"    - 成功处理图集总计数: {self.stats['atlas_processed']}")


if __name__ == "__main__":
    extractor = CharaStandExtractor()
    extractor.execute_pipeline()