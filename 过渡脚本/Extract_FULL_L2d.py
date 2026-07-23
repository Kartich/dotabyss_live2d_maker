import os
import re
import json
import shutil
import binascii
import subprocess
from pathlib import Path
from PIL import Image
import UnityPy

# ==========================================
# 1. 基础路径与开关配置
# ==========================================
RAW_BUNDLES_DIR = Path("./raw_bundles")       
OUTPUT_STORY_DIR = Path("./data_r18_all/stories") 
TEMP_ANIM_DIR = Path("./Exported_Live2D_Anims")

ASSET_STUDIO_CLI = "AssetStudio.CLI.exe"
GAME_TYPE = "Normal"
ASSET_TYPES = ["AnimationClip"]
L2D_BUNDLE_PATTERN = re.compile(r".*_l2d_.*\.prefab.*", re.IGNORECASE)

FORCE_OVERWRITE = True  

OUTPUT_STORY_DIR.mkdir(parents=True, exist_ok=True)

# ==========================================
# 2. 贴图绝对红底消除模块
# ==========================================
def remove_pure_red_background_pillow(pil_image, out_png_path, force_overwrite=True):
    try:
        img = pil_image.convert("RGBA")
        datas = img.getdata()
        
        new_data = [
            (0, 0, 0, 0) if (r == 255 and g == 0 and b <= 5) else (r, g, b, a) 
            for r, g, b, a in datas
        ]
        
        img.putdata(new_data)
        out_path = Path(out_png_path)
        os.makedirs(out_path.parent, exist_ok=True)
        
        if force_overwrite and out_path.exists():
            try: out_path.unlink()
            except Exception as del_err: print(f"    [!] 清理旧贴图失败: {del_err}")

        img.save(out_path, "PNG")
        return True
    except Exception as e:
        print(f"    [-] 贴图背景消除失败: {e}")
        return False

# ==============================================================================
# 3. 核心 Hash 映射算法 (保留原始模式并增强明文提取范围)
# ==============================================================================
def extract_real_ids_from_bytes_or_files(moc3_bytes=None, moc3_path=None):
    """
    优先直接从内存/导出的 moc3 中提取真正的 Live2D 参数/图层 ID
    """
    detected_tokens = set()
    content = ""

    if moc3_bytes:
        content = moc3_bytes.decode('utf-8', errors='ignore')
    elif moc3_path and os.path.exists(moc3_path):
        try:
            with open(moc3_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except Exception:
            pass

    if content:
        tokens = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', content)
        detected_tokens.update(tokens)

    system_excludes = {"MOC3", "Warp", "Rotation", "ArtMesh", "AnimationClip", "Animation", "Curves"}
    return [t for t in detected_tokens if t not in system_excludes]

def get_crc32_unsigned(string_path):
    return str(binascii.crc32(string_path.encode('utf-8')) & 0xFFFFFFFF)

def generate_dynamic_hash_library_for_moc(moc3_bytes=None, moc3_path=None):
    """
    针对当前 Live2D 模型的真实明文动态生成 Hash Library
    """
    raw_names = extract_real_ids_from_bytes_or_files(moc3_bytes=moc3_bytes, moc3_path=moc3_path)
    hash_library = {}
    
    for name in raw_names:
        param_path = f"Parameters/{name}"
        param_hash_key = f"path_{get_crc32_unsigned(param_path)}"
        hash_library[param_hash_key] = {"id": name, "type": "Parameter"}
        
        part_path = f"Parts/{name}"
        part_hash_key = f"path_{get_crc32_unsigned(part_path)}"
        
        is_part_type = name.startswith("Part") or name in [
            "Face", "FaceLine", "Ear_L", "Neck", "Nose", "Skirt", "Shirt", "Shirt2", 
            "M_Shirt", "M_Body", "M_Leg", "Shoulder", "Bust", "Bust2", "Nipple", 
            "Ribbon", "Tie2", "Collar_L", "Collar_R", "UpperArm_L", "Arm_R"
        ]
        if is_part_type:
            hash_library[part_hash_key] = {"id": name, "type": "PartOpacity"}
        else:
            if part_hash_key not in hash_library:
                hash_library[part_hash_key] = {"id": name, "type": "PartOpacity"}
                
    return hash_library

# ==============================================================================
# 4. 浮点编码器与动画无损解析引擎 (保持 100% 原始逻辑)
# ==============================================================================
class Live2DFloatEncoder(json.JSONEncoder):
    def encode(self, obj):
        if isinstance(obj, float):
            return f"{obj:.6f}".rstrip('0').rstrip('.')
        elif isinstance(obj, list):
            return '[' + ', '.join(self.encode(el) for el in obj) + ']'
        elif isinstance(obj, dict):
            pairs = []
            for k, v in obj.items():
                pairs.append(f'"{k}": ' + self.encode(v))
            return '{' + ', '.join(pairs) + '}'
        return super(Live2DFloatEncoder, self).encode(obj)

def parse_anim_file(file_path, hash_library):
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    loop_match = re.search(r'm_LoopTime:\s*([01])', content)
    is_loop = True
    if loop_match:
        is_loop = (loop_match.group(1) == "1")

    stop_time_match = re.search(r'm_StopTime:\s*([\d\.-]+)', content)
    anim_stop_time = float(stop_time_match.group(1)) if stop_time_match else None

    curve_matches = re.finditer(r'm_Curve:\s*\n(.*?)(?:path:\s*(path_\d+|\d+)|attribute:\s*(path_\d+))', content, re.DOTALL)
    curves_data = []
    max_keyframe_time = 0.0
    
    for match in curve_matches:
        curve_body = match.group(1)
        raw_key = match.group(2) if match.group(2) else match.group(3)
        
        if raw_key and not raw_key.startswith("path_"):
            raw_key = f"path_{raw_key}"

        if raw_key:
            # 此处拿映射到的真实 ID[cite: 3]
            mapped_info = hash_library.get(raw_key, {"id": raw_key, "type": "Parameter"})
            
            time_points = re.findall(r'time:\s*([\d\.-]+)', curve_body)
            value_points = re.findall(r'value:\s*([\d\.-]+)', curve_body)
            
            if time_points and len(time_points) == len(value_points):
                raw_frames = []
                for t, v in zip(time_points, value_points):
                    tf = float(t)
                    vf = float(v)
                    raw_frames.append((tf, vf))
                    if tf > max_keyframe_time:
                        max_keyframe_time = tf
                
                raw_frames.sort(key=lambda x: x[0])
                
                segments = [0, raw_frames[0][1]]
                for i in range(1, len(raw_frames)):
                    segments.append(0)
                    segments.append(raw_frames[i][0])
                    segments.append(raw_frames[i][1])
                
                curves_data.append({
                    "Target": mapped_info["type"],
                    "Id": mapped_info["id"], 
                    "Segments": segments
                })

    final_duration = anim_stop_time if (anim_stop_time is not None and anim_stop_time > 0) else max_keyframe_time

    return curves_data, is_loop, round(final_duration, 3)

# ==========================================
# 5. 单 Bundle 解析与流程一体化导出
# ==========================================
def extract_anim_by_cli(bundle_path: Path, output_dir: Path):
    cmd = [
        ASSET_STUDIO_CLI,
        str(bundle_path),
        str(output_dir),
        "--game", GAME_TYPE,
        "--types", ",".join(ASSET_TYPES)
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return output_dir.exists() and len(os.listdir(output_dir)) > 0
    except Exception as e:
        print(f"    [-] CLI 动画提取失败: {e}")
        return False

def process_story_bundle(bundle_path: Path):
    id_match = re.search(r"(\d{11})", bundle_path.name)
    if not id_match:
        return None
        
    story_id = id_match.group(1)
    
    try:
        env = UnityPy.load(str(bundle_path))
    except Exception as e:
        print(f"[-] 无法载入 Bundle {bundle_path.name}: {e}")
        return None

    textures = {}
    moc3_bytes_data = None

    for obj in env.objects:
        type_name = getattr(obj.type, "name", str(obj.type))
        
        if type_name == "Texture2D":
            data = obj.read()
            tex_name = getattr(data, "name", "") or getattr(data, "m_Name", "")
            if tex_name and data.image:
                textures[tex_name] = data.image
                
        elif type_name in ["TextAsset", "MonoBehaviour"]:
            try:
                try: as_json_dict = obj.read_typetree()
                except: as_json_dict = {}
                
                raw_moc = as_json_dict.get("m_Script", as_json_dict.get("script", b""))
                if isinstance(raw_moc, str): raw_moc = raw_moc.encode('utf-8', 'ignore')
                
                if b"MOC3" not in raw_moc:
                    raw_bytes = obj.get_raw_data()
                    idx = raw_bytes.find(b"MOC3")
                    if idx != -1: raw_moc = raw_bytes[idx:]
                    
                if isinstance(raw_moc, bytes) and raw_moc.startswith(b"MOC3") and len(raw_moc) > 10000:
                    moc3_bytes_data = raw_moc
            except:
                pass

    if not textures and not moc3_bytes_data:
        return None

    # 创建 live2d 标准目录结构
    story_path = OUTPUT_STORY_DIR / story_id
    moc_dir = story_path / "moc"
    motion_dir = story_path / "motion"
    textures_dir = story_path / "textures"
    
    moc_dir.mkdir(parents=True, exist_ok=True)
    motion_dir.mkdir(parents=True, exist_ok=True)
    textures_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[*] 正在处理故事单元: {story_id} (来源包: {bundle_path.name})")

    # --------------------------------------------------------------------------
    # 步骤 1: 导出 MOC3 模型
    # --------------------------------------------------------------------------
    moc3_path = moc_dir / f"l2d_{story_id}.moc3"
    if moc3_bytes_data:
        if FORCE_OVERWRITE and moc3_path.exists():
            try: moc3_path.unlink()
            except Exception as del_err: print(f"    [!] 清理旧 moc3 模型失败: {del_err}")

        with open(moc3_path, "wb") as f_moc:
            f_moc.write(moc3_bytes_data)
        print(f"  [✓] 保存 Live2D 模型 -> moc/{moc3_path.name}")

    # 🔑【核心修复】步骤 1.5: 实时从 MOC3 数据生成当前模型的映射字典[cite: 3]
    hash_library = generate_dynamic_hash_library_for_moc(moc3_bytes=moc3_bytes_data, moc3_path=moc3_path)

    # --------------------------------------------------------------------------
    # 步骤 2: 紧跟 MOC3，提取 .anim 并精准转为 motion3.json
    # --------------------------------------------------------------------------
    motions_config = {}
    bundle_anim_temp = TEMP_ANIM_DIR / story_id
    
    if L2D_BUNDLE_PATTERN.match(bundle_path.name):
        has_extracted = extract_anim_by_cli(bundle_path, bundle_anim_temp)
        if has_extracted:
            for root, _, files in os.walk(bundle_anim_temp):
                for file in files:
                    if file.endswith('.anim'):
                        anim_file_path = os.path.join(root, file)
                        try:
                            curves, is_loop, duration = parse_anim_file(anim_file_path, hash_library)
                            if not curves: continue
                            
                            motion3_json = {
                                "Version": 3,
                                "Meta": {
                                    "Duration": duration,
                                    "Fps": 30.0,
                                    "Loop": is_loop,
                                    "AreBeziersRestricted": True,
                                    "CurveCount": len(curves),
                                    "TotalSegmentCount": sum((len(c["Segments"]) - 2) // 3 for c in curves),
                                    "TotalPointCount": sum((len(c["Segments"]) - 2) // 3 + 1 for c in curves),
                                    "UserDataCount": 0,
                                    "TotalUserDataSize": 0
                                },
                                "Curves": curves
                            }
                            
                            motion_filename = file.replace('.anim', '.motion3.json')
                            out_motion_path = motion_dir / motion_filename
                            
                            if FORCE_OVERWRITE and out_motion_path.exists():
                                try: out_motion_path.unlink()
                                except: pass

                            with open(out_motion_path, 'w', encoding='utf-8') as out_f:
                                json_str = Live2DFloatEncoder().encode(motion3_json)
                                parsed_json = json.loads(json_str)
                                json.dump(parsed_json, out_f, indent=2, ensure_ascii=False)
                            
                            group_key = "Idle" if "idle" in motion_filename.lower() else "Idle"
                            if group_key not in motions_config:
                                motions_config[group_key] = []
                            motions_config[group_key].append({"File": f"motion/{motion_filename}"})
                            
                            print(f"  [✓] 成功匹配明文 ID 转为 Motion3 -> motion/{motion_filename}")
                        except Exception as e:
                            print(f"  [-] 动画 {file} 转换失败: {e}")

    # --------------------------------------------------------------------------
    # 步骤 3: 导出贴图并执行绝对红底消除
    # --------------------------------------------------------------------------
    model_textures_config = []
    for name, img_obj in textures.items():
        if "grabmask" in name.lower():
            mask_path = textures_dir / "GrabMask.png"
            if FORCE_OVERWRITE and mask_path.exists():
                try: mask_path.unlink()
                except: pass
            img_obj.save(mask_path)
            print("  [✓] 保存遮罩 -> textures/GrabMask.png")
        else:
            clean_img_name = f"{name}.png"
            out_png_path = textures_dir / clean_img_name
            
            if remove_pure_red_background_pillow(img_obj, out_png_path, force_overwrite=FORCE_OVERWRITE):
                model_textures_config.append(f"textures/{clean_img_name}")
                print(f"  [✓] 贴图背景消除 (#FF0005) 并导出 -> textures/{clean_img_name}")

    # --------------------------------------------------------------------------
    # 步骤 4: 生成 .model3.json 配置
    # --------------------------------------------------------------------------
    if model_textures_config or moc3_bytes_data:
        cfg_path = story_path / f"{story_id}.model3.json"
        if FORCE_OVERWRITE and cfg_path.exists():
            try: cfg_path.unlink()
            except: pass

        model3_cfg = {
            "Version": 3,
            "FileReferences": {
                "Moc": f"moc/l2d_{story_id}.moc3",
                "Textures": sorted(list(set(model_textures_config))),
                "Motions": motions_config
            },
            "Groups": []
        }
        with open(cfg_path, 'w', encoding='utf-8') as f_cfg:
            json.dump(model3_cfg, f_cfg, indent=2, ensure_ascii=False)
            
        return {"id": story_id, "type": "live2d", "title": f"Story {story_id}", "hasLive2D": True}
        
    return None

# ==========================================
# 6. 主程序入口
# ==========================================
def extract_live2d_clean_pipeline():
    if not RAW_BUNDLES_DIR.exists():
        print(f"[-] 错误: 未找到原始资产包文件夹: '{RAW_BUNDLES_DIR.resolve()}'")
        return

    print(f"[*] 开始扫描目录: {RAW_BUNDLES_DIR.resolve()}")
    story_list = []

    for bundle_path in RAW_BUNDLES_DIR.rglob("*"):
        if not bundle_path.is_file(): continue
        
        # 逐个故事解包（动态实时获取对应的 moc3 明文字典）
        res = process_story_bundle(bundle_path)
        if res and not any(s["id"] == res["id"] for s in story_list):
            story_list.append(res)

    if story_list:
        index_path = Path("./data_r18_all/index.json")
        existing_stories = []
        if index_path.exists():
            try:
                with open(index_path, "r", encoding="utf-8") as f: 
                    existing_stories = json.load(f).get("stories", [])
            except: pass
            
        for s in story_list:
            if not any(ex["id"] == s["id"] for ex in existing_stories): 
                existing_stories.append(s)
                
        with open(index_path, "w", encoding="utf-8") as f: 
            json.dump({"stories": sorted(existing_stories, key=lambda x: x["id"])}, f, indent=2, ensure_ascii=False)
            
        print("\n" + "="*60 + "\n[SUCCESS] 全部 Live2D 贴图、模型与动画解包融合完毕！")
    else:
        print("\n[-] 未能识别导出任何资产，请检查 RAW_BUNDLES_DIR 目录下是否存在 Bundle 文件。")

    if TEMP_ANIM_DIR.exists():
        try:
            shutil.rmtree(TEMP_ANIM_DIR)
            print("  [✓] 已完全清理中间临时解包文件夹。")
        except Exception as e:
            print(f"  [!] 清理临时文件夹失败: {e}")

if __name__ == "__main__":
    extract_live2d_clean_pipeline()