import os
import sys
import re
import json
import shutil
import binascii
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter, defaultdict
from PIL import Image
import UnityPy
from UnityPy.enums import ClassIDType

try:
    import soundfile as sf
except ImportError:
    sf = None

# ==========================================
# 1. 统一全局路径与参数配置
# ==========================================
RAW_BUNDLES_DIR = Path("./raw_bundles")             # 原始 .bundle 存放目录
OUTPUT_BASE_DIR = Path("./data_r18_all")            # 输出总目录
OUTPUT_STORY_DIR = OUTPUT_BASE_DIR / "stories"      # 故事输出总目录
OUTPUT_THUMB_DIR = OUTPUT_BASE_DIR / "thumb"        # Thumb 缩略图统一输出目录
OUTPUT_CHARA_DIR = Path("./data/chara")            # 播放器标准立绘输出路径
TEMP_ANIM_DIR = Path("./Exported_Live2D_Anims")     # AssetStudio 提取动画临时目录

# 工具与解包开关
ASSET_STUDIO_CLI = "AssetStudio.CLI.exe"
GAME_TYPE = "Normal"
ASSET_TYPES = ["AnimationClip"]
L2D_BUNDLE_PATTERN = re.compile(r".*_l2d_.*\.prefab.*", re.IGNORECASE)
FORCE_OVERWRITE = True

# 创建初始化目录
OUTPUT_STORY_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_THUMB_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_CHARA_DIR.mkdir(parents=True, exist_ok=True)

# 运行时全局状态
script_voice_maps = {}    # 存放故事 ID 解析出的 vc_ 语音物理顺序队列
script_bgm_maps = {}      # 存放故事 ID 解析出的 bgm 引用列表
loaded_global_bgms = {}   # 缓存全局加载的 BGM 音频二进制: { clean_bgm_name: payload_bytes }
pending_audio_tasks = []  # 暂存待处理的故事音频 Bundle 任务


# ==========================================
# 2. Voice / BGV 原始音频高保真转码与 @UTF 提取模块
# ==========================================
def transcode_to_ogg(payload: bytearray, output_path: Path) -> bool:
    """ 将盲切出来的任意音频二进制流转换为标准 OGG 格式 """
    ffmpeg_bin = 'ffmpeg'
    try:
        import imageio_ffmpeg
        ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass

    temp_input = output_path.with_suffix('.tmp_in')
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_input.write_bytes(payload)
        
        cmd = [
            ffmpeg_bin, '-y',
            '-i', str(temp_input),   # 读取落地临时文件
            '-acodec', 'libvorbis',  # 使用标准 vorbis 编码器
            '-q:a', '4',             # 设置音频质量级别 (~128kbps 高音质)
            str(output_path)
        ]
        res = subprocess.run(cmd, capture_output=True)
        return res.returncode == 0
    except Exception as e:
        print(f"      [-] FFmpeg 转换时发生异常: {e}")
        return False
    finally:
        if temp_input.exists():
            temp_input.unlink()


def find_acb_bytes_recursive(data):
    """ 递归遍历整个反序列化树，自适应通杀所有包体结构提取 ACB 二进制数据 """
    if isinstance(data, (bytes, bytearray)):
        if data.startswith(b'@UTF'):
            return bytearray(data)
            
    elif isinstance(data, list):
        if len(data) >= 4 and data[0] == 64 and data[1] == 85 and data[2] == 84 and data[3] == 70: 
            return bytearray(data)
        for item in data:
            res = find_acb_bytes_recursive(item)
            if res is not None:
                return res
                
    elif isinstance(data, dict):
        for k, v in data.items():
            res = find_acb_bytes_recursive(v)
            if res is not None:
                return res
                
    return None


def extract_acb_from_monobehaviour(obj):
    """ 安全调用 Typetree 并通过递归搜索剥离出 ACB 数据 """
    try:
        tree = obj.read_typetree()
    except Exception:
        try:
            tree = obj.parse_as_dict()
        except Exception:
            return None

    if not tree:
        return None
        
    return find_acb_bytes_recursive(tree)


def slice_audio_from_bundle(story_id: str, bundle_path: Path, is_bgv: bool = False):
    """ 
    解析提取原始 ACB，执行特征码扫描后，
    按照播放器补丁规范自动分流到 /voice/ 或 /bgv/ 目录并输出标准 .ogg
    """
    print(f"[*] [音频解包] 正在解包音频资产: {story_id} ({'环境音BGV' if is_bgv else '人物语音Voice'})")
    
    try:
        env = UnityPy.load(str(bundle_path))
    except Exception as e:
        print(f"  [-] 载入音频 Bundle 失败: {e}")
        return

    acb_bytes = None
    for obj in env.objects:
        if obj.type.name == "MonoBehaviour":
            acb_bytes = extract_acb_from_monobehaviour(obj)
            if acb_bytes:
                break

    if not acb_bytes:
        print(f"  [-] 核心解密失败: 未能在该 MonoBehaviour 字典树中检索到有效 @UTF 容器。")
        return

    # --- 三模弹性特征码扫描状态机 ---
    audio_starts = []
    i = 0
    limit = len(acb_bytes) - 8
    while i < limit:
        if acb_bytes[i:i+4] == b'RIFF':
            audio_starts.append((i, ".wav"))
            i += 4
            continue
        elif acb_bytes[i:i+4] == b'OggS':
            if (acb_bytes[i+5] & 0x02) != 0:
                audio_starts.append((i, ".ogg"))
            i += 4
            continue
        elif acb_bytes[i:i+4] == b'ftyp':
            start_offset = max(0, i - 4)
            audio_starts.append((start_offset, ".m4a"))
            i += 4
            continue
        i += 1

    if not audio_starts:
        print(f"  [-] 该 ACB 容器内未检索到任何内嵌的独立音频轨。")
        return

    sub_folder = "bgv" if is_bgv else "voice"
    target_dir = OUTPUT_STORY_DIR / story_id / "audio" / "decoded" / sub_folder
    target_dir.mkdir(parents=True, exist_ok=True)
    
    voice_sequence = script_voice_maps.get(story_id, [])
    print(f"  [+] 在内存中精准检索到 {len(audio_starts)} 个音频轨道起始点。")

    for idx, (start_pos, original_ext) in enumerate(audio_starts):
        end_pos = audio_starts[idx + 1][0] if idx + 1 < len(audio_starts) else len(acb_bytes)
        payload = acb_bytes[start_pos:end_pos]

        if is_bgv:
            base_name = f"bgv_{story_id}_{idx + 1:03d}_01"
        else:
            if idx < len(voice_sequence):
                base_name = f"{voice_sequence[idx]}"
            else:
                base_name = f"voice_extra_{idx + 1}"

        target_ogg_path = target_dir / f"{base_name}.ogg"

        success = transcode_to_ogg(payload, target_ogg_path)
        if success:
            print(f"    [➔] 已成功存入 {sub_folder}: {base_name}.ogg ({len(payload)} bytes)")
        else:
            fallback_path = target_dir / f"{base_name}{original_ext}"
            fallback_path.write_bytes(payload)
            print(f"    [!] 转换异常，已直接写出原始音轨: {base_name}{original_ext}")

    print(f"  [+] 成功分类导出该音频序列。")


# ==========================================
# 3. BGM 独立脱壳与贴图抠图模块
# ==========================================
def extract_m4a_from_raw_awb_bytes(raw_bytes: bytes) -> Optional[bytes]:
    if not raw_bytes or len(raw_bytes) < 100:
        return None

    ftyp_idx = raw_bytes.find(b'ftyp')
    if ftyp_idx != -1:
        start_pos = max(0, ftyp_idx - 4)
        return raw_bytes[start_pos:]

    ogg_idx = raw_bytes.find(b'OggS')
    if ogg_idx != -1:
        return raw_bytes[ogg_idx:]

    riff_idx = raw_bytes.find(b'RIFF')
    if riff_idx != -1:
        return raw_bytes[riff_idx:]

    if raw_bytes.startswith(b'AFS2'):
        return raw_bytes[0x20:]

    return raw_bytes


def transcode_bgm_to_ogg(payload: bytes, output_path: Path) -> bool:
    clean_payload = extract_m4a_from_raw_awb_bytes(payload)
    if not clean_payload:
        clean_payload = payload

    return transcode_to_ogg(bytearray(clean_payload), output_path)


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


def get_clean_bundle_prefix(bundle_name: str) -> str:
    return bundle_name.split('.')[0].split('_')[0]


def parse_script_and_cache(bundle_path: Path):
    id_match = re.search(r"hmr_(\d+)", bundle_path.name)
    if not id_match:
        return

    story_id = id_match.group(1)
    print(f"[*] [剧本解包] 正在解析剧本包: {story_id}")
    
    try:
        env = UnityPy.load(str(bundle_path))
    except Exception as e:
        print(f"  [-] 载入剧本 Bundle 失败: {e}")
        return

    script_text = ""
    for obj in env.objects:
        if obj.type.name == "TextAsset":
            data = obj.read()
            if hasattr(data, "text") and data.text:
                script_text = data.text
            elif hasattr(data, "m_Script") and data.m_Script:
                script_text = data.m_Script.decode('utf-8', errors='ignore') if isinstance(data.m_Script, bytes) else str(data.m_Script)
            elif hasattr(data, "bytes") and data.bytes:
                script_text = data.bytes.decode('utf-8', errors='ignore')
            break
            
    if not script_text:
        return

    textasset_dir = OUTPUT_STORY_DIR / story_id / "textassets"
    textasset_dir.mkdir(parents=True, exist_ok=True)
    
    script_name = f"hmr_{story_id}.txt"
    with open(textasset_dir / script_name, "w", encoding="utf-8") as f_txt:
        f_txt.write(script_text)
    print(f"  [+] 剧本导出成功 -> stories/{story_id}/textassets/{script_name}")

    voice_pattern = re.compile(r"vc_[a-zA-Z0-9_]+")
    ordered_voices = []
    
    strict_bgm_pattern = re.compile(r"\b(bgm_?\d+)\b", re.IGNORECASE)
    referenced_bgms = []

    for line in script_text.splitlines():
        v_matches = voice_pattern.findall(line)
        for voice_id in v_matches:
            if voice_id not in ordered_voices:
                ordered_voices.append(voice_id)
        
        b_matches = strict_bgm_pattern.findall(line)
        for bgm_id in b_matches:
            clean_bgm = bgm_id.lower().replace("_", "")
            if clean_bgm not in referenced_bgms:
                referenced_bgms.append(clean_bgm)
                
    script_voice_maps[story_id] = ordered_voices
    script_bgm_maps[story_id] = referenced_bgms


def cache_global_bgm_bundle(bundle_path: Path):
    clean_prefix = get_clean_bundle_prefix(bundle_path.name)
    print(f"[*] [BGM预载] 正在读取 BGM 包: {clean_prefix}")

    try:
        raw_bytes = bundle_path.read_bytes()
        clean_key = clean_prefix.replace("_", "")
        loaded_global_bgms[clean_key] = raw_bytes
        print(f"  [+] 成功缓存 BGM 数据流: {clean_prefix} ({len(raw_bytes)} bytes)")
    except Exception as e:
        print(f"  [-] 读取 BGM 文件失败: {e}")


def dispatch_bgms_to_stories():
    print("\n[*] [BGM分发] 正在根据剧本引用导出属于各寝室故事的 BGM...")
    if not script_bgm_maps:
        return

    for story_id, referenced_bgms in script_bgm_maps.items():
        if not referenced_bgms:
            continue
        
        target_bgm_dir = OUTPUT_STORY_DIR / story_id / "audio" / "decoded" / "bgm"
        
        for bgm_ref in referenced_bgms:
            matched_key = None
            for key in loaded_global_bgms.keys():
                if key == bgm_ref or key.endswith(bgm_ref) or bgm_ref in key:
                    matched_key = key
                    break

            if matched_key:
                target_bgm_dir.mkdir(parents=True, exist_ok=True)
                raw_payload = loaded_global_bgms[matched_key]
                
                out_name = matched_key if matched_key.startswith("bgm") else f"bgm_{matched_key}"
                out_ogg_path = target_bgm_dir / f"{out_name}.ogg"
                
                success = transcode_bgm_to_ogg(raw_payload, out_ogg_path)
                if success:
                    print(f"  [+] 故事 {story_id} -> 成功导出 BGM: {out_name}.ogg")
                else:
                    clean_m4a = extract_m4a_from_raw_awb_bytes(raw_payload)
                    if clean_m4a:
                        fallback_path = target_bgm_dir / f"{out_name}.m4a"
                        fallback_path.write_bytes(clean_m4a)


def process_story_thumb_bundle(bundle_path: Path):
    id_match = re.search(r"hmr_(\d+)", bundle_path.name)
    if not id_match:
        return

    story_id = id_match.group(1)
    
    try:
        env = UnityPy.load(str(bundle_path))
    except Exception:
        return

    for obj in env.objects:
        type_name = getattr(obj.type, "name", str(obj.type))
        if type_name in ["Texture2D", "Sprite"]:
            try:
                data = obj.read()
                img = getattr(data, "image", None)
                if img:
                    thumb_path = OUTPUT_THUMB_DIR / f"thumb_{story_id}.png"
                    remove_pure_red_background_pillow(img, thumb_path, force_overwrite=FORCE_OVERWRITE)
                    print(f"  [+] 成功导出故事 {story_id} 封面缩略图 -> data_r18_all/thumb/thumb_{story_id}.png")
                    break
            except Exception as e:
                print(f"  [-] 提取封面缩略图失败 ({story_id}): {e}")


# ==========================================
# 4. Live2D 模型与动画解析模块 (符合 Cubism 规范)
# ==========================================
def calculate_fade_times(anim_name: str, duration: float, is_loop: bool) -> Tuple[float, float]:
    """
    根据 Live2D 官方设计规范，依据动作名称、类型与总时长，
    计算差异化的渐入 (FadeInTime) 与渐出 (FadeOutTime) 时间，防止播放冲突与画面直断。
    """
    name_lower = anim_name.lower()

    # 1. 无缝循环动作或极短的动效：不宜设置长淡入淡出，防止淡入淡出比动作还长导致的视觉卡顿
    if is_loop and duration > 0 and duration <= 1.0:
        return 0.0, 0.0

    # 2. 点击响应 / 交互 / 突发动作 (Tap, Touch, Shock, Hit 等)：需要高响应度，采用快速过渡 (0.15s ~ 0.25s)
    if any(k in name_lower for k in ["tap", "touch", "react", "shock", "hit", "surprise", "shake"]):
        return 0.2, 0.2

    # 3. 待机动作 (Idle, Wait, Standby) / 呼吸基础动作：平滑过渡 (0.8s ~ 1.0s)，避免切换动作时骨骼位置突变
    if any(k in name_lower for k in ["idle", "wait", "standby", "breath"]):
        return 0.8, 0.8

    # 4. 如果动作时长很短 (< 1.2s)，渐入渐出不能大于时长的一半，防止过渡区间重叠
    if duration > 0 and duration < 1.2:
        half_dur = round(duration / 2.0, 2)
        return half_dur, half_dur

    # 5. 标准场景 / 剧情对话动作 (Scene, Motion, Talk)：使用标准规范 0.5s 淡入淡出
    return 0.5, 0.5


def extract_real_ids_from_bytes_or_files(moc3_bytes=None, moc3_path=None):
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


def process_story_live2d_bundle(bundle_path: Path):
    id_match = re.search(r"(\d{11})", bundle_path.name)
    if not id_match:
        return None
        
    story_id = id_match.group(1)
    
    try:
        env = UnityPy.load(str(bundle_path))
    except Exception as e:
        print(f"[-] 无法载入 Bundle: {e}")
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

    story_path = OUTPUT_STORY_DIR / story_id
    moc_dir = story_path / "moc"
    motion_dir = story_path / "motion"
    textures_dir = story_path / "textures"
    
    moc_dir.mkdir(parents=True, exist_ok=True)
    motion_dir.mkdir(parents=True, exist_ok=True)
    textures_dir.mkdir(parents=True, exist_ok=True)

    print(f"[*] [Live2D解包] 正在导出故事 Live2D: {story_id}")

    moc3_path = moc_dir / f"l2d_{story_id}.moc3"
    if moc3_bytes_data:
        if FORCE_OVERWRITE and moc3_path.exists():
            try: moc3_path.unlink()
            except Exception: pass

        with open(moc3_path, "wb") as f_moc:
            f_moc.write(moc3_bytes_data)

    hash_library = generate_dynamic_hash_library_for_moc(moc3_bytes=moc3_bytes_data, moc3_path=moc3_path)

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
                            
                            anim_name_stem = file.replace('.anim', '')
                            # 根据动作类型计算渐入渐出时间
                            fade_in_time, fade_out_time = calculate_fade_times(anim_name_stem, duration, is_loop)

                            motion3_json = {
                                "Version": 3,
                                "Meta": {
                                    "Duration": duration,
                                    "Fps": 30.0,
                                    "Loop": is_loop,
                                    "AreBeziersRestricted": True,
                                    "FadeInTime": fade_in_time,   # 差异化渐入时间
                                    "FadeOutTime": fade_out_time, # 差异化渐出时间
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
                            
                            # 依据名称规则自动分流 Group
                            stem_lower = anim_name_stem.lower()
                            if "idle" in stem_lower:
                                group_key = "Idle"
                            elif stem_lower.startswith("scene"):
                                group_key = "Scene"
                            elif "tap" in stem_lower or "touch" in stem_lower:
                                group_key = "Tap"
                            else:
                                group_key = "Motion"

                            if group_key not in motions_config:
                                motions_config[group_key] = []
                            
                            # 在 model3.json 中同步写入相应的 FadeIn / FadeOut
                            motions_config[group_key].append({
                                "File": f"motion/{motion_filename}",
                                "FadeInTime": fade_in_time,
                                "FadeOutTime": fade_out_time
                            })
                        except Exception as e:
                            print(f"  [-] 动画 {file} 转换失败: {e}")

    model_textures_config = []
    for name, img_obj in textures.items():
        if "grabmask" in name.lower():
            mask_path = textures_dir / "GrabMask.png"
            if FORCE_OVERWRITE and mask_path.exists():
                try: mask_path.unlink()
                except: pass
            img_obj.save(mask_path)
        else:
            clean_img_name = f"{name}.png"
            out_png_path = textures_dir / clean_img_name
            if remove_pure_red_background_pillow(img_obj, out_png_path, force_overwrite=FORCE_OVERWRITE):
                model_textures_config.append(f"textures/{clean_img_name}")

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
# 5. Build_Story.py 结构对齐与 JSON 编译模块
# ==========================================
COMMAND_INFO = {
    ":label": {"category": "flow", "title": "标签", "args": ["label"]},
    "adultui": {"category": "ui", "title": "成人 UI 开关", "args": ["on/off"]},
    "uivisible": {"category": "ui", "title": "UI 可见性", "args": ["on/off"]},
    "window": {"category": "ui", "title": "文本窗口", "args": ["on/off", "fade_seconds"]},
    "fade": {"category": "screen", "title": "画面淡入淡出", "args": ["In/Out", "color", "seconds"]},
    "message": {"category": "text", "title": "旁白/普通文本", "args": ["speaker", "message", "voice?"]},
    "l2dmessage": {"category": "text", "title": "Live2D 台词", "args": ["speaker", "message", "face_or_empty", "voice_id"]},
    "l2dshow": {"category": "live2d", "title": "显示 Live2D", "args": ["model_object"]},
    "l2dhide": {"category": "live2d", "title": "隐藏 Live2D", "args": []},
    "l2dmotion": {"category": "live2d", "title": "Live2D 动作", "args": ["motion_trigger"]},
    "asyncl2dmotion": {"category": "live2d", "title": "延迟 Live2D 动作", "args": ["motion_trigger", "bool", "async_code", "delay_seconds"]},
    "bgmplay": {"category": "audio", "title": "播放 BGM", "args": ["tag", "cue", "fade_seconds"]},
    "bgmstop": {"category": "audio", "title": "停止 BGM", "args": ["tag", "fade_seconds"]},
    "bgvplay": {"category": "audio", "title": "播放背景语音/环境声", "args": ["tag", "cue", "volume", "loop"]},
    "bgvstop": {"category": "audio", "title": "停止背景语音/环境声", "args": ["tag", "fade_seconds", "volume?"]},
    "charaload": {"category": "character", "title": "加载角色", "args": ["tag", "character_id", "display_name"]},
    "wait": {"category": "flow", "title": "等待", "args": ["seconds"]},
    "cleanall": {"category": "screen", "title": "清空场景", "args": ["target"]},
}

def parse_line(line: str):
    line = line.strip().lstrip("\ufeff")
    if not line: return None
    if line.startswith(":"):
        return {"command": ":label", "rawCommand": line, "args": [], "label": line[1:]}
    parts = line.split(",")
    return {"command": parts[0], "rawCommand": parts[0], "args": parts[1:]}

def enrich(cmd_obj):
    cmd = cmd_obj["command"]
    args = cmd_obj.get("args", [])
    info = COMMAND_INFO.get(cmd, {})
    entry = {
        "command": cmd,
        "rawCommand": cmd_obj.get("rawCommand", cmd),
        "args": args,
        "category": info.get("category", "unknown"),
        "title": info.get("title", cmd),
    }
    if cmd == ":label": entry["label"] = cmd_obj.get("label", "")
    if cmd == "fade" and len(args) >= 3:
        entry["direction"], entry["color"] = args[0], args[1]
        try: entry["seconds"] = float(args[2])
        except: pass
    if cmd == "l2dshow" and args: entry["model"] = args[0]
    if cmd in ("l2dmotion", "asyncl2dmotion") and args:
        entry["motion"] = args[0]
        if cmd == "asyncl2dmotion" and len(args) >= 4:
            try: entry["delay"] = float(args[3])
            except: pass
    if cmd in ("bgmplay", "bgvplay") and len(args) >= 2: entry["cue"] = args[1]
    if cmd in ("message", "l2dmessage"):
        entry["speaker"] = args[0] if len(args) > 0 else ""
        entry["message"] = args[1] if len(args) > 1 else ""
        voice = ""
        for a in args:
            if isinstance(a, str) and a.startswith("vc_"):
                voice = a
                break
        entry["voice"] = voice
    return entry

def command_category(command: str):
    low = (command or "").lower()
    if low.startswith(":") or "wait" in low or "jump" in low or "label" in low or "section" in low or low in ("initend", "title"): return "flow"
    if "message" in low or low in ("talk", "telop"): return "text"
    if "bgm" in low or "bgv" in low or "seplay" in low or "sestop" in low or "sefade" in low or low.startswith("se") or "voice" in low: return "audio"
    if low.startswith("l2d") or "live2d" in low: return "live2d"
    if "fade" in low or "blur" in low or "shake" in low or "clean" in low or "linework" in low: return "screen"
    if "chara" in low or "silhouette" in low or "emodelete" in low or low == "priority": return "character"
    if "bg" in low or "camera" in low or "move" in low or "scale" in low or "rotate" in low: return "stage"
    if "still" in low or "image" in low or "prefab" in low or "object" in low or "asset" in low: return "asset"
    if "ui" in low or "window" in low or "popup" in low: return "ui"
    return "unknown"

def parse_script(txt_path: Path):
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        lines = f.readlines()
    commands, labels, audio_ids, motions = [], set(), set(), set()
    counter = Counter()
    for idx, line in enumerate(lines, start=1):
        obj = parse_line(line)
        if not obj: continue
        cmd = enrich(obj)
        cmd["index"] = len(commands)
        cmd["line"] = idx
        commands.append(cmd)
        c = cmd["command"]
        counter[c] += 1
        cmd["category"] = command_category(c)
        if c == ":label": labels.add(cmd["label"])
        if "motion" in cmd: motions.add(cmd["motion"])
        if "cue" in cmd: audio_ids.add(cmd["cue"])
        if cmd.get("voice"): audio_ids.add(cmd["voice"])
    messages = [cmd for cmd in commands if cmd["command"] in ("message", "l2dmessage")]
    return {
        "commands": commands,
        "messages": messages,
        "labels": sorted(labels),
        "audioIds": sorted(audio_ids),
        "motions": sorted(motions),
        "commandCounts": dict(counter)
    }

def build_textures(story_dir: Path):
    tex_dir = story_dir / "textures"
    textures = []
    if tex_dir.exists():
        for img in sorted(tex_dir.glob("*")):
            if img.suffix.lower() not in [".png", ".jpg", ".jpeg", ".webp"]: continue
            w, h = 0, 0
            try:
                with Image.open(img) as im: w, h = im.size
            except: pass
            textures.append({"name": img.stem, "path": f"textures/{img.name}", "width": w, "height": h, "bundle": ""})
    return textures

# 读取各个 motion3.json 真实的 Meta 参数，准确读取对应淡入淡出时间
def build_motions_list(story_dir: Path):
    motions_dir = story_dir / "motion"
    if not motions_dir.exists(): return []
    files = sorted(list(motions_dir.glob("*.motion3.json")), key=lambda p: [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", p.name)])
    groups = defaultdict(list)
    for f in files:
        name = f.stem.split(".motion3")[0]
        group = "Scene" if name.startswith("scene") else re.sub(r"\d+$", "", name)
        groups[group].append((name, f))
    motions = []
    for group_name, items in groups.items():
        for idx, (name, f) in enumerate(items):
            duration = 0.5
            fade_in = 0.5
            fade_out = 0.5
            is_loop = "loop" in name.lower()
            try:
                with open(f, "r", encoding="utf-8") as mf:
                    m_data = json.load(mf)
                    meta = m_data.get("Meta", {})
                    duration = meta.get("Duration", 0.5)
                    if "Loop" in meta:
                        is_loop = meta["Loop"]
                    fade_in = meta.get("FadeInTime", 0.5)
                    fade_out = meta.get("FadeOutTime", 0.5)
            except Exception:
                pass

            motions.append({
                "name": name,
                "group": group_name,
                "path": f"motion/{f.name}",
                "duration": duration,
                "loop": is_loop,
                "fadeInTime": fade_in,   # 准确写入动态计算的渐入时间
                "fadeOutTime": fade_out, # 准确写入动态计算的渐出时间
                "bundle": "",
                "pathId": 0,
                "index": idx
            })
    return motions

def get_duration(file_path):
    if not sf: return 0.0
    try:
        data, sr = sf.read(file_path)
        return len(data) / sr
    except: return 0.0

def build_audio(story_dir: Path):
    audio_root = story_dir / "audio"
    decoded_dir = audio_root / "decoded"
    cues = {}
    if not decoded_dir.exists(): return {"cues": {}}
    for category_dir in decoded_dir.iterdir():
        if not category_dir.is_dir(): continue
        category = category_dir.name
        for f in category_dir.rglob("*.ogg"):
            cue_id = f.stem
            duration = get_duration(f)
            cues[cue_id] = {
                "name": cue_id,
                "category": category,
                "path": str(f.relative_to(story_dir).as_posix()),
                "source": f"audio/raw/{cue_id}.awb" if category == "bgm" else "",
                "subsong": 1,
                "duration": duration,
                "sampleRate": 48000,
                "channels": 2 if category == "bgm" else 1,
                "encoding": "AAC (Advanced Audio Coding)" if category == "bgm" else "CRI HCA",
                "bytes": f.stat().st_size
            }
    return {"cues": cues, "errors": [], "decoder": r"vgmstream-cli\vgmstream-cli.exe"}

def build_story_json(story_dir: Path):
    text_dir = story_dir / "textassets"
    txt_files = list(text_dir.glob("*.txt"))
    if not txt_files: return

    txt = txt_files[0]
    story_id = story_dir.name
    script_id = txt.stem
    parsed = parse_script(txt)

    moc_dir = story_dir / "moc"
    moc_file = next(moc_dir.glob("*.moc3"), None) if moc_dir.exists() else None

    # 映射在 data_r18_all/thumb/thumb_story_id.png 的缩略图
    thumb_rel_path = None
    target_thumb_file = OUTPUT_THUMB_DIR / f"thumb_{story_id}.png"
    if target_thumb_file.exists():
        thumb_rel_path = f"../thumb/thumb_{story_id}.png"

    live2d = {
        "moc": {
            "name": f"l2d_{story_id}",
            "path": f"moc/l2d_{story_id}.moc3",
            "bytes": moc_file.stat().st_size if moc_file else 0,
            "bundle": ""
        },
        "textures": build_textures(story_dir),
        "animations": [],
        "motions": build_motions_list(story_dir),
        "fadeMotions": [],
        "monobehaviour": [],
        "model3": f"{story_id}.model3.json"
    }

    if str(story_id)[-1] != "2":
        live2d["moc"] = None
        live2d["model3"] = None

    script = {
        "id": script_id,
        "name": script_id,
        "text": f"textassets/{txt.name}",
        "lineCount": len(open(txt, encoding="utf-8-sig").readlines()),
        "commands": parsed["commands"],
        "messages": parsed["messages"],
        "labels": parsed["labels"],
        "audioIds": parsed["audioIds"],
        "motions": parsed["motions"],
        "commandCounts": parsed["commandCounts"],
    }

    story = {
        "id": story_id,
        "sourceId": story_id,
        "thumb": thumb_rel_path,
        "root": str(story_dir),
        "scripts": [script],
        "primaryScript": script_id,
        "live2d": live2d,
        "audio": build_audio(story_dir)
    }

    out_path = story_dir / "story.json"
    out_path.write_text(json.dumps(story, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [✓] 成功生成对齐格式配置文件 -> stories/{story_id}/story.json")


def generate_all_story_jsons():
    print("\n[*] 正在批量编译组装各故事的 story.json...")
    for d in OUTPUT_STORY_DIR.iterdir():
        if d.is_dir():
            try:
                build_story_json(d)
            except Exception as e:
                print(f"  [-] 编译 {d.name} story.json 失败: {e}")


# ==========================================
# 6. 立绘组件剥离与 RectTransform 级联还原模块
# ==========================================
class CharaStandExtractor:
    def __init__(self):
        self.stats = {
            "atlas_processed": 0,
            "characters_exported": 0,
        }

    def _get_default_rect(self) -> Dict[str, Any]:
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
        go_names: Dict[int, str] = {}
        for obj in env.objects:
            if obj.type == ClassIDType.GameObject:
                try:
                    go = obj.read()
                    go_names[obj.path_id] = getattr(go, "m_Name", "")
                except:
                    pass

        def get_path_id(pptr) -> int:
            if pptr is None: return 0
            if hasattr(pptr, "m_PathID"): return pptr.m_PathID
            if isinstance(pptr, dict): return pptr.get("m_PathID", 0)
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
                        if c_id: children_ids.append(c_id)

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
                    "ax": anchored_pos["x"],
                    "ay": anchored_pos["y"],
                    "sw": size_delta["x"],
                    "sh": size_delta["y"],
                    "parent": parent_name
                }
            except:
                pass

        def get_absolute_world_pos(path_id: int) -> Dict[str, float]:
            node = raw_rt_dict.get(path_id)
            if not node: return {"x": 0.0, "y": 0.0}
            if "worldPosition" in node: return node["worldPosition"]
                
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
        expr_rect_w = expr_data["rect_w"] if expr_data["rect_w"] > 0 else expr_data["img_w"]
        expr_rect_h = expr_data["rect_h"] if expr_data["rect_h"] > 0 else expr_data["img_h"]

        expr_full = Image.new("RGBA", (expr_rect_w, expr_rect_h), (0, 0, 0, 0))
        tro_x = int(round(expr_data["tro_x"]))
        tro_y_flipped = expr_rect_h - int(round(expr_data["tro_y"])) - expr_data["img_h"]
        tro_y_flipped = max(0, tro_y_flipped)
        tro_x = max(0, tro_x)
        
        expr_full.alpha_composite(expr_data["img"], dest=(tro_x, tro_y_flipped))

        if target_w > 0 and target_h > 0:
            return expr_full.resize((target_w, target_h), Image.Resampling.LANCZOS)
        return expr_full

    def process_bundle(self, bundle_path: Path):
        try:
            env = UnityPy.load(str(bundle_path))
            
            all_sprites = {}
            for obj in env.objects:
                if obj.type == ClassIDType.Sprite:
                    try: all_sprites[obj.path_id] = obj.read()
                    except: pass

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

                    body_img = body_data["img"]
                    canvas_w, canvas_h = body_img.size
                    body_img.save(chara_dir / "Body.png")
                    print(f"[*] [立绘解包] 成功导出主底图 -> {chara_id_lower}/Body.png")

                    root_key = next((k for k in layout_tree.keys() if k.lower().endswith("stand") or k == chara_id_raw), None)
                    if not root_key and layout_tree:
                        root_key = next((k for k, v in layout_tree.items() if v["father"] == 0), list(layout_tree.keys())[0])

                    def clean_rect(node):
                        if not node: return self._get_default_rect()
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
                            "body": "Body.png"
                        },
                        "faces": {},
                        "spriteSizes": {}
                    }

                    meta_output["spriteSizes"]["Body"] = {
                        "width": body_data["rect_w"] if body_data["rect_w"] > 0 else body_data["img_w"],
                        "height": body_data["rect_h"] if body_data["rect_h"] > 0 else body_data["img_h"]
                    }

                    target_fw, target_fh = 0, 0
                    if face_info and expression_list:
                        face_w = face_info["face_w"] if face_info["face_w"] > 0 else expression_list[0]["rect_w"]
                        face_h = face_info["face_h"] if face_info["face_h"] > 0 else expression_list[0]["rect_h"]
                        body_rt_w = face_info["body_w"]
                        body_rt_h = face_info["body_h"]

                        scale_x = canvas_w / body_rt_w if body_rt_w > 0 else 1.0
                        scale_y = canvas_h / body_rt_h if body_rt_h > 0 else 1.0

                        target_fw = int(round(face_w * scale_x))
                        target_fh = int(round(face_h * scale_y))

                    for expr_data in expression_list:
                        expr_name_raw = expr_data["name"]
                        expr_name_lower = expr_name_raw.lower()
                        
                        face_canvas = self._prepare_sprite_layer(expr_data, target_fw, target_fh)
                        face_canvas.save(faces_dir / f"{expr_name_raw}.png")
                        
                        meta_output["faces"][expr_name_lower] = f"faces/{expr_name_raw}.png"
                        meta_output["spriteSizes"][expr_name_raw] = {
                            "width": expr_data["rect_w"] if expr_data["rect_w"] > 0 else expr_data["img_w"],
                            "height": expr_data["rect_h"] if expr_data["rect_h"] > 0 else expr_data["img_h"]
                        }

                    with open(chara_dir / "meta.json", "w", encoding="utf-8") as f_meta:
                        json.dump(meta_output, f_meta, indent=2, ensure_ascii=False)

                    print(f"  [➔] 已输出元数据 -> {chara_id_lower}/meta.json")
                    self.stats["characters_exported"] += 1
                    self.stats["atlas_processed"] += 1

        except Exception as e:
            print(f"[-] 错误: 处理立绘 Bundle {bundle_path.name} 时发生异常: {e}")


# ==========================================
# 7. 全流程统一总控入口
# ==========================================
def main():
    if not RAW_BUNDLES_DIR.exists():
        print(f"[-] 错误: 未找到存放原始 Bundle 的目录 '{RAW_BUNDLES_DIR}'")
        return

    all_bundles = list(RAW_BUNDLES_DIR.glob("*.bundle"))
    if not all_bundles:
        print(f"[-] 未在 '{RAW_BUNDLES_DIR}' 找到任何 .bundle 资产。")
        return

    print("==========================================================")
    print(" 启动全流程解包管线 (剧本/Voice&BGV原生@UTF/BGM脱壳/Thumb储存/Live2D/story.json/立绘矩阵)")
    print("==========================================================")

    # Stage 1: 扫描与预载（优化过滤，仅将真正的 Voice/BGV 音频包加入待切片队列）
    print("\n>>> Stage 1: 正在扫描剧本包、BGM 与 封面(thumb) 资源...")
    for bundle in all_bundles:
        bundle_name = bundle.name.lower()

        if "mainchara_hmr_" in bundle_name:
            parse_script_and_cache(bundle)
        elif "bgm" in bundle_name and "voice" not in bundle_name:
            cache_global_bgm_bundle(bundle)
        elif "icon-story-s_" in bundle_name or "thumb" in bundle_name:
            process_story_thumb_bundle(bundle)
        elif "voice_chara" in bundle_name or "backgroundvoice_chara" in bundle_name or "_voice" in bundle_name or "_bgv" in bundle_name:
            id_match = re.search(r"hmr_(\d+)", bundle.name)
            if id_match:
                story_id = id_match.group(1)
                is_bgv = "backgroundvoice_chara" in bundle_name or "_bgv" in bundle_name
                pending_audio_tasks.append((story_id, bundle, is_bgv))

    # Stage 2: 音频切片与转码
    print(f"\n>>> Stage 2: 正在处理故事音频切片与 BGM 路由转码...")
    for story_id, bundle, is_bgv in pending_audio_tasks:
        slice_audio_from_bundle(story_id, bundle, is_bgv)

    dispatch_bgms_to_stories()

    # Stage 3: Live2D 解包与动画提取
    print("\n>>> Stage 3: 正在解包故事 Live2D 模型、动画与贴图...")
    for bundle_path in all_bundles:
        process_story_live2d_bundle(bundle_path)

    if TEMP_ANIM_DIR.exists():
        try:
            shutil.rmtree(TEMP_ANIM_DIR)
            print("  [✓] 已清理动画解包临时目录。")
        except Exception as e:
            print(f"  [!] 清理临时目录失败: {e}")

    # Stage 4: 组装生成全量 story.json
    generate_all_story_jsons()

    # Stage 5: 立绘及其 RectTransform 级联树还原
    print("\n>>> Stage 5: 正在提取立绘底图、表情与 RectTransform 级联树...")
    chara_extractor = CharaStandExtractor()
    for bundle_path in all_bundles:
        if "chara_" in bundle_path.name or "stand" in bundle_path.name.lower():
            chara_extractor.process_bundle(bundle_path)

    print("\n" + "="*60)
    print("[★] 剧情包生成完毕")
    print("="*60)


if __name__ == "__main__":
    main()