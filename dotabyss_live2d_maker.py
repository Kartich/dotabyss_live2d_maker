import os
import json
import zlib
import re
import shutil

def extract_strings_from_moc3(moc3_bytes):
    """
    扫描 .moc3 二进制提取所有组件
    """
    pattern = re.compile(b'[a-zA-Z0-9_]{3,64}')
    potential_strings = pattern.findall(moc3_bytes)
    
    valid_ids = set()
    for b_str in potential_strings:
        try:
            s = b_str.decode('utf-8')
            if s in ["MOC3", "Count", "Canvas", "Parts", "Parameters", "MocVersion"]:
                continue
            if s.isdigit():
                continue
            if s.startswith("Param") or s.startswith("Part") or s.startswith("part"):
                valid_ids.add(s)
        except UnicodeDecodeError:
            pass
            
    # 注入标准面部及眼部参数（特别细化眼、嘴部分，防止 moc3 没有导出默认参数名）
    default_face_params = [
        "ParamAngleX", "ParamAngleY", "ParamAngleZ",
        "ParamEyeBallX", "ParamEyeBallY",
        "ParamEyeLOpen", "ParamEyeLSmile", "ParamEyeROpen", "ParamEyeRSmile",
        "ParamBrowLY", "ParamBrowRY", "ParamBrowLX", "ParamBrowRX", "ParamBrowLAngle", "ParamBrowRAngle",
        "ParamMouthForm", "ParamMouthOpen", "ParamLipSync", "ParamCheek",
        "ParamBodyAngleX", "ParamBodyAngleY", "ParamBodyAngleZ", "ParamBreath"
    ]
    for p in default_face_params:
        valid_ids.add(p)
        
    return list(valid_ids)

def build_hash_dictionary(names):
    """
    计算 Unity CRC32 哈希映射，区分 Parameters、Parts 以及特殊的控制曲线属性
    """
    hash_map = {}
    # 针对 Unity 层次结构的不同路径前缀
    prefixes = ["", "Parameters/", "Parts/", "Drawables/"]
    
    for name in names:
        for prefix in prefixes:
            full_path = f"{prefix}{name}"
            # 无符号 CRC32
            crc = zlib.crc32(full_path.encode('utf-8'))
            hash_map[crc] = {"name": name, "type": "Part" if "Part" in name else "Param"}
            # 有符号 CRC32 (Signed Int32)
            if crc > 0x7FFFFFFF:
                hash_map[crc - 0x100000000] = {"name": name, "type": "Part" if "Part" in name else "Param"}
                
    return hash_map

def parse_anim_curves(anim_path):
    """
    健壮地流式解析 Unity .anim YAML 曲线数据。
    完全基于块结构（block-based），解决对非数字 attribute/path 带来的 IndexError 报错。
    """
    curves = []
    current_curve = None
    current_keyframe = None
    
    with open(anim_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line_strip = line.strip()
            if not line_strip:
                continue
                
            # 1. 发现新曲线段
            if line_strip.startswith("- curve:"):
                if current_curve:
                    curves.append(current_curve)
                current_curve = {
                    "keyframes": [],
                    "path_hash": None,
                    "path_raw": "",
                    "attribute_hash": None,
                    "attribute_raw": "",
                    "class_id": None
                }
                current_keyframe = None
                continue
                
            if current_curve is None:
                continue
                
            # 2. 解析元数据
            if line_strip.startswith("attribute:"):
                attr_val = line_strip.split(":", 1)[1].strip()
                current_curve["attribute_raw"] = attr_val
                digits = re.findall(r'\d+', attr_val)
                if digits:
                    current_curve["attribute_hash"] = int(digits[0])
                continue
                
            elif line_strip.startswith("path:"):
                path_val = line_strip.split(":", 1)[1].strip()
                current_curve["path_raw"] = path_val
                digits = re.findall(r'\d+', path_val)
                if digits:
                    current_curve["path_hash"] = int(digits[0])
                continue
                
            elif line_strip.startswith("classID:"):
                current_curve["class_id"] = int(line_strip.split(":", 1)[1].strip())
                continue
                
            # 3. 发现新关键帧
            if line_strip.startswith("- serializedVersion:") or line_strip.startswith("m_Curve:"):
                current_keyframe = {}
                current_curve["keyframes"].append(current_keyframe)
                continue
                
            # 4. 填充关键帧参数
            if current_keyframe is not None:
                if line_strip.startswith("time:"):
                    current_keyframe["time"] = float(line_strip.split(":", 1)[1].strip())
                elif line_strip.startswith("value:"):
                    current_keyframe["value"] = float(line_strip.split(":", 1)[1].strip())
                elif line_strip.startswith("inSlope:"):
                    current_keyframe["inSlope"] = float(line_strip.split(":", 1)[1].strip())
                elif line_strip.startswith("outSlope:"):
                    current_keyframe["outSlope"] = float(line_strip.split(":", 1)[1].strip())
                elif line_strip.startswith("weightedMode:"):
                    current_keyframe["weightedMode"] = int(line_strip.split(":", 1)[1].strip())

        if current_curve:
            curves.append(current_curve)
            
    # 清洗多余空关键帧
    for c in curves:
        c["keyframes"] = [kf for kf in c["keyframes"] if "time" in kf and "value" in kf]
        
    return curves

def convert_curve_to_segments(keyframes):
    """
    物理无损换算：严格保留 .anim 里的参数，仅通过切线换算解决贝塞尔映射，不改变数据本身。
    """
    if not keyframes:
        return []
    
    # 按照标准 Segments 格式：前两个元素为起点 [Time, Value]
    segments = [keyframes[0]["time"], keyframes[0]["value"]]
    
    for i in range(len(keyframes) - 1):
        k_start = keyframes[i]
        k_end = keyframes[i + 1]
        
        t_start, v_start = k_start["time"], k_start["value"]
        t_end, v_end = k_end["time"], k_end["value"]
        dt = t_end - t_start
        
        # 1. 阶跃段/常数段 (Step)
        is_step = (abs(k_start.get("outSlope", 0)) > 1e10 or dt <= 1e-5)
        
        # 2. 线性段 (Linear)
        is_linear = (k_start.get("outSlope", 0) == 0 and k_end.get("inSlope", 0) == 0)
        
        if is_step:
            segments.extend([2, t_end, v_end])
        elif is_linear:
            segments.extend([0, t_end, v_end])
        else:
            # 3. 三次贝塞尔段 (Bezier)：1:1 数学等效转换切线
            p1_t = t_start + dt / 3.0
            p1_v = v_start + k_start.get("outSlope", 0) * (dt / 3.0)
            
            p2_t = t_end - dt / 3.0
            p2_v = v_end - k_end.get("inSlope", 0) * (dt / 3.0)
            
            segments.extend([
                1, 
                round(p1_t, 6), round(p1_v, 6), 
                round(p2_t, 6), round(p2_v, 6), 
                round(t_end, 6), round(v_end, 6)
            ])
            
    return segments

def parse_and_convert_anim(anim_path, hash_dict):
    """
    替换为无损的流式解析和 1:1 数学换算
    """
    raw_curves = parse_anim_curves(anim_path)
    curves_data = []
    
    def resolve_hash(num):
        if num is None:
            return None
        # 尝试无符号碰撞
        if num in hash_dict:
            return hash_dict[num]
        # 尝试有符号碰撞
        signed_num = num if num <= 0x7FFFFFFF else num - 0x100000000
        if signed_num in hash_dict:
            return hash_dict[signed_num]
        return None

    for rc in raw_curves:
        path_hash = rc["path_hash"]
        attr_hash = rc["attribute_hash"]
        
        path_info = resolve_hash(path_hash)
        attr_info = resolve_hash(attr_hash)
        
        real_id = None
        target = "Parameter"
        
        # 1. 确定 ID 与 Target 类型
        if path_info:
            real_id = path_info["name"]
            if path_info["type"] == "Part":
                target = "PartOpacity"
        elif attr_info:
            real_id = attr_info["name"]
            if attr_info["type"] == "Part":
                target = "PartOpacity"
        else:
            # 备用
            raw_id = path_hash if path_hash is not None else rc["path_raw"]
            real_id = f"ParamUnknown_{raw_id}"
            
        # 2. 核心修复：防止核心面部控制参数被意外置为 PartOpacity
        if real_id and any(keyword in real_id for keyword in ["Eye", "Mouth", "Blink", "Lip", "Brow"]):
            target = "Parameter"
            
        keyframes = rc["keyframes"]
        if not keyframes:
            continue
            
        # 3. 物理无损换算动画段
        segments = convert_curve_to_segments(keyframes)
        if not segments:
            continue
            
        curves_data.append({
            "Target": target,
            "Id": real_id,
            "Segments": segments
        })
        
    return curves_data

def run_pipeline(current_dir="."):
    print("[*] 开始执行修复级自适应 Live2D 转换流程...")
    all_files = os.listdir(current_dir)
    
    textures = [f for f in all_files if f.lower().startswith("texture") and os.path.isfile(f)]
    anims = [f for f in all_files if f.lower().endswith(".anim") and os.path.isfile(f)]
    
    model_jsons = []
    for f in all_files:
        if f.lower().endswith(".json") and os.path.isfile(f):
            try:
                with open(f, 'r', encoding='utf-8') as jf:
                    data = json.load(jf)
                    if "_bytes" in data and "m_Name" in data:
                        model_jsons.append((f, data))
            except:
                continue
                
    if not model_jsons:
        print("[-] 错误：当前目录下没有发现任何有效的 Live2D 导出 Json 文件！")
        return

    for json_file, model_data in model_jsons:
        model_name = model_data.get("m_Name", "model")
        print(f"\n[▶] 正在处理模型: {model_name}")
        
        model_dir = os.path.join(current_dir, model_name)
        textures_dir = os.path.join(model_dir, "textures")
        motions_dir = os.path.join(model_dir, "motions")
        os.makedirs(model_dir, exist_ok=True)
        os.makedirs(textures_dir, exist_ok=True)
        os.makedirs(motions_dir, exist_ok=True)
        
        # 提取二进制 .moc3
        moc3_bytes = bytes(model_data.get("_bytes", []))
        moc3_filename = f"{model_name}.moc3"
        moc3_path = os.path.join(model_dir, moc3_filename)
        with open(moc3_path, 'wb') as f_bin:
            f_bin.write(moc3_bytes)
            
        # 提取 moc3 内置参数并构建哈希对照字典
        extracted_names = extract_strings_from_moc3(moc3_bytes)
        hash_dict = build_hash_dictionary(extracted_names)
        
        # 拷贝贴图
        model_textures_config = []
        for tex in textures:
            dest_tex_path = os.path.join(textures_dir, tex)
            shutil.copy2(tex, dest_tex_path)
            model_textures_config.append(f"textures/{tex}")
            
        # 转换该模型对应的全部动画
        motions_config = {}
        for anim in anims:
            anim_name_clean = os.path.splitext(anim)[0]
            motion3_filename = f"{anim_name_clean}.motion3.json"
            motion3_out_path = os.path.join(motions_dir, motion3_filename)
            
            curves_data = parse_and_convert_anim(anim, hash_dict)
            if curves_data:
                # 准确统计点数
                total_segment_count = 0
                total_point_count = 0
                max_duration = 0.0
                
                for c in curves_data:
                    segs = c["Segments"]
                    if not segs:
                        continue
                    if segs[-2] > max_duration:
                        max_duration = segs[-2]
                    
                    seg_idx = 2
                    point_count = 1
                    segment_count = 0
                    while seg_idx < len(segs):
                        seg_type = segs[seg_idx]
                        segment_count += 1
                        if seg_type == 1:
                            point_count += 3
                            seg_idx += 7
                        elif seg_type in (0, 2):
                            point_count += 1
                            seg_idx += 3
                        else:
                            break
                    total_segment_count += segment_count
                    total_point_count += point_count
                
                motion3 = {
                    "Version": 3,
                    "Meta": {
                        "Duration": round(max_duration, 3),
                        "Fps": 30.0,
                        "Loop": True,
                        "AreBeziersRestricted": True,
                        "CurveCount": len(curves_data),
                        "TotalSegmentCount": total_segment_count,
                        "TotalPointCount": total_point_count
                    },
                    "Curves": curves_data
                }
                
                with open(motion3_out_path, 'w', encoding='utf-8') as f_mot:
                    json.dump(motion3, f_mot, indent=2, ensure_ascii=False)
                
                group_name = "Idle" if "loop" in anim_name_clean.lower() or "idle" in anim_name_clean.lower() else anim_name_clean
                if group_name not in motions_config:
                    motions_config[group_name] = []
                motions_config[group_name].append({"File": f"motions/{motion3_filename}"})
                print(f"  [+] 成功将 '{anim}' 转换至 motions/{motion3_filename}")
                
        # 动态创建 model3.json
        model3_cfg = {
            "Version": 3,
            "FileReferences": {
                "Moc": moc3_filename,
                "Textures": model_textures_config,
                "Motions": motions_config
            },
            "Groups": []
        }
        
        model3_path = os.path.join(model_dir, f"{model_name}.model3.json")
        with open(model3_path, 'w', encoding='utf-8') as f_cfg:
            json.dump(model3_cfg, f_cfg, indent=2, ensure_ascii=False)
        print(f"  [✔] {model_name}.model3.json 生成成功。")
        
    print("\n[✔] 脚本运行完毕，请使用 Viewer 加载生成的 model3.json 配置文件！")

if __name__ == "__main__":
    run_pipeline()