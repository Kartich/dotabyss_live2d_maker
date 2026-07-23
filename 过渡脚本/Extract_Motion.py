import os
import re
import json
import shutil
import binascii
import subprocess

# ==============================================================================
# 全局配置区域
# ==============================================================================
ASSET_STUDIO_CLI = "AssetStudio.CLI.exe"      # CLI 命令名称
GAME_TYPE = "Normal"                          # AssetStudio 指定游戏类型[cite: 2]
ASSET_TYPES = ["AnimationClip"]              # 只提取动画 Clip[cite: 2]
# 筛选 Live2D bundle 文件的正则[cite: 2]
L2D_BUNDLE_PATTERN = re.compile(r".*_l2d_.*\.prefab.*", re.IGNORECASE)

# 路径设置
INPUT_DIR = os.getcwd()
TEMP_ANIM_DIR = os.path.join(INPUT_DIR, "Exported_Live2D_Anims")
FINAL_MOTION_DIR = os.path.join(INPUT_DIR, "Live2D_Standard_Motions")


# ==============================================================================
# 阶段一：AssetStudio 提取 .anim
# ==============================================================================
def extract_anims_via_cli():
    print("==================================================")
    print(" 阶段 1: 扫描并提取 Live2D AnimationClip")
    print("==================================================")
    
    matched_files = []
    for root, _, files in os.walk(INPUT_DIR):
        # 排除导出与临时文件夹[cite: 2]
        if os.path.commonpath([root, TEMP_ANIM_DIR]) == TEMP_ANIM_DIR or \
           os.path.commonpath([root, FINAL_MOTION_DIR]) == FINAL_MOTION_DIR:
            continue

        for file in files:
            if file.endswith(".py"):
                continue
            if L2D_BUNDLE_PATTERN.match(file):
                matched_files.append(os.path.join(root, file))

    print(f"找到 {len(matched_files)} 个匹配的目标 Bundle 文件，准备提取...\n")

    if not matched_files:
        print("[提示] 未检测到符合条件的 Bundle 文件，直接跳过提取阶段，尝试从已有目录转换。")
        return False

    success_count = 0
    for idx, file_path in enumerate(matched_files, start=1):
        relative_path = os.path.relpath(file_path, INPUT_DIR)
        file_out_dir = os.path.join(TEMP_ANIM_DIR, os.path.dirname(relative_path))

        print(f"[{idx}/{len(matched_files)}] 正在提取: {relative_path}")

        cmd = [
            ASSET_STUDIO_CLI,
            file_path,
            file_out_dir,
            "--game", GAME_TYPE,
            "--types", ",".join(ASSET_TYPES)
        ]

        try:
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if os.path.exists(file_out_dir) and len(os.listdir(file_out_dir)) > 0:
                print(f"  └─> [提取成功]")
                success_count += 1
            else:
                print(f"  └─> [跳过] 该 Bundle 中未找到 AnimationClip 资源")
                if os.path.exists(file_out_dir) and not os.listdir(file_out_dir):
                    os.rmdir(file_out_dir)
        except FileNotFoundError:
            print(f"[错误] 未找到 '{ASSET_STUDIO_CLI}'，请检查环境变量或将其放置于脚本目录下。")
            return False
        except Exception as e:
            print(f"[错误] 提取失败: {e}")

    print(f"\n提取完成，共处理成功 {success_count}/{len(matched_files)} 个 Bundle。\n")
    return True


# ==============================================================================
# 阶段二：计算 Hash 库与参数解析
# ==============================================================================
def extract_real_ids_from_local_files():
    detected_tokens = set()
    # 递归查找当前目录及子目录下的 moc3/txt 文件
    for root, _, files in os.walk(INPUT_DIR):
        for file in files:
            if file.endswith('.moc3') or file.endswith('.txt'):
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                        tokens = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', content)
                        detected_tokens.update(tokens)
                except Exception:
                    pass

    if not detected_tokens:
        print("[警告] 未找到任何 .moc3 或 .txt 模型明文，将使用裸 Hash 参数进行转换。")

    system_excludes = {"MOC3", "Warp", "Rotation", "ArtMesh", "AnimationClip", "Animation", "Curves"}
    return [t for t in detected_tokens if t not in system_excludes]

def get_crc32_unsigned(string_path):
    return str(binascii.crc32(string_path.encode('utf-8')) & 0xFFFFFFFF)

def generate_dynamic_hash_library():
    raw_names = extract_real_ids_from_local_files()
    hash_library = {}
    
    for name in raw_names:
        param_path = f"Parameters/{name}"
        param_hash_key = f"path_{get_crc32_unsigned(param_path)}"
        hash_library[param_hash_key] = {"id": name, "type": "Parameter"}
        
        part_path = f"Parts/{name}"
        part_hash_key = f"path_{get_crc32_unsigned(part_path)}"
        
        is_part_type = name.startswith("Part") or name in [
            "Face", "FaceLine", "Ear_L", "Neck", "Nose", "Skirt", "Shirt", 
            "Shirt2", "M_Shirt", "M_Body", "M_Leg", "Shoulder", "Bust", 
            "Bust2", "Nipple", "Ribbon", "Tie2", "Collar_L", "Collar_R", 
            "UpperArm_L", "Arm_R"
        ]
        if is_part_type:
            hash_library[part_hash_key] = {"id": name, "type": "PartOpacity"}
        else:
            if part_hash_key not in hash_library:
                hash_library[part_hash_key] = {"id": name, "type": "PartOpacity"}
                
    return hash_library


# ==============================================================================
# 阶段三：解析 .anim 转换成 Live2D Motion3 JSON
# ==============================================================================
class Live2DFloatEncoder(json.JSONEncoder):
    def encode(self, obj):
        if isinstance(obj, float):
            return f"{obj:.6f}".rstrip('0').rstrip('.')
        elif isinstance(obj, list):
            return '[' + ', '.join(self.encode(el) for el in obj) + ']'
        elif isinstance(obj, dict):
            pairs = [f'"{k}": ' + self.encode(v) for k, v in obj.items()]
            return '{' + ', '.join(pairs) + '}'
        return super(Live2DFloatEncoder, self).encode(obj)

def parse_anim_file(file_path, hash_library):
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    loop_match = re.search(r'm_LoopTime:\s*([01])', content)
    is_loop = (loop_match.group(1) == "1") if loop_match else True

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
                    segments.extend([0, raw_frames[i][0], raw_frames[i][1]])
                
                curves_data.append({
                    "Target": mapped_info["type"],
                    "Id": mapped_info["id"], 
                    "Segments": segments
                })

    final_duration = anim_stop_time if (anim_stop_time is not None and anim_stop_time > 0) else max_keyframe_time
    return curves_data, is_loop, round(final_duration, 3)

def convert_anims_to_motions():
    print("==================================================")
    print(" 阶段 2: 正在转换动画资产 (.anim -> .motion3.json)")
    print("==================================================")

    if not os.path.exists(FINAL_MOTION_DIR):
        os.makedirs(FINAL_MOTION_DIR)

    hash_library = generate_dynamic_hash_library()
    
    # 递归查找 Extract 出来或者工作路径下的所有 .anim 文件
    anim_files = []
    search_targets = [TEMP_ANIM_DIR, INPUT_DIR]
    for target in search_targets:
        if os.path.exists(target):
            for root, _, files in os.walk(target):
                # 避免重复读取和扫描最终导出文件夹
                if os.path.commonpath([root, FINAL_MOTION_DIR]) == FINAL_MOTION_DIR:
                    continue
                for file in files:
                    if file.endswith('.anim'):
                        anim_files.append(os.path.join(root, file))

    # 去重
    anim_files = list(set(anim_files))

    if not anim_files:
        print("[错误] 未扫描到任何可转换的 .anim 动画文件。")
        return

    print(f"找到 {len(anim_files)} 个动画资产，开始转换...\n")

    for anim_path in anim_files:
        anim_name = os.path.basename(anim_path)
        try:
            curves, is_loop, duration = parse_anim_file(anim_path, hash_library)
            if not curves: 
                continue
            
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
            
            output_path = os.path.join(FINAL_MOTION_DIR, anim_name.replace('.anim', '.motion3.json'))
            
            with open(output_path, 'w', encoding='utf-8') as out_f:
                json_str = Live2DFloatEncoder().encode(motion3_json)
                parsed_json = json.loads(json_str)
                json.dump(parsed_json, out_f, indent=2, ensure_ascii=False)
                
            print(f" -> 【无损导出成功】: {anim_name} -> {os.path.basename(output_path)}")
        except Exception as e:
            print(f" -> 导出失败: {anim_name}，原因: {str(e)}")


# ==============================================================================
# 阶段四：清理中间缓存产物 (.anim)
# ==============================================================================
def clean_temp_files():
    print("\n==================================================")
    print(" 阶段 3: 清理中间临时文件")
    print("==================================================")
    try:
        if os.path.exists(TEMP_ANIM_DIR):
            shutil.rmtree(TEMP_ANIM_DIR)
            print(f" -> 已完全清除临时解包目录: {TEMP_ANIM_DIR}")

        # 如果工作根目录下残留有单独提取的 .anim 文件，也一并剔除
        for root, _, files in os.walk(INPUT_DIR):
            if os.path.commonpath([root, FINAL_MOTION_DIR]) == FINAL_MOTION_DIR:
                continue
            for file in files:
                if file.endswith('.anim'):
                    os.remove(os.path.join(root, file))
                    print(f" -> 已清理根目录残留文件: {file}")

        print("临时文件清理完毕！防止后续批处理污染。")
    except Exception as e:
        print(f"[警告] 清理过程发生异常: {str(e)}")


# ==============================================================================
# 程序入口
# ==============================================================================
def main():
    # 执行阶段一：提取 anim 文件[cite: 2]
    extract_anims_via_cli()
    
    # 执行阶段二：转换 motion3.json 文件[cite: 1]
    convert_anims_to_motions()
    
    # 执行阶段三：彻底清理中间产物
    clean_temp_files()

    print("\n" + "=" * 50)
    print(f"全流程顺利完成！合并后的 Live2D Motion 文件位于:\n👉 {FINAL_MOTION_DIR}")
    print("=" * 50)

if __name__ == "__main__":
    main()