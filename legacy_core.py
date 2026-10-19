import os
import sys
import re
import json
import shutil
import binascii
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from l2d_motion_resolver import is_runtime_reset, resolve_trigger
from tool_paths import project_root, raw_bundles_dir
from parallel_utils import CPU_WORKERS, workers_for
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
TOOL_ROOT = project_root()
RAW_BUNDLES_DIR = raw_bundles_dir()
OUTPUT_BASE_DIR = TOOL_ROOT / "data_r18_all"
OUTPUT_STORY_DIR = OUTPUT_BASE_DIR / "stories"
OUTPUT_THUMB_DIR = OUTPUT_BASE_DIR / "thumb"
OUTPUT_CHARA_DIR = TOOL_ROOT / "data" / "chara"
TEMP_ANIM_DIR = TOOL_ROOT / "Exported_Live2D_Anims"

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
loaded_global_bgms = {}   # 缓存全局加载的 BGM 音频二进制
pending_audio_tasks = []  # 暂存待处理的故事音频 Bundle 任务


# ==========================================
# 2. Voice / BGV 原始音频高保导转码与 @UTF 提取模块
# ==========================================
def transcode_to_ogg(payload: bytearray, output_path: Path) -> bool:
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
            '-i', str(temp_input),
            '-acodec', 'libvorbis',
            '-q:a', '4',
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
        print(f"  [-] 核心解密失败: 未能在该 MonoBehaviour 中检索到有效 @UTF 容器。")
        return

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
            print(f"    [➔] 已成功存入 {sub_folder}: {base_name}.ogg")
        else:
            fallback_path = target_dir / f"{base_name}{original_ext}"
            fallback_path.write_bytes(payload)
            print(f"    [!] 转换异常，已直接写出原始音轨: {base_name}{original_ext}")


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
            try:
                out_path.unlink()
            except Exception:
                pass

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
    try:
        raw_bytes = bundle_path.read_bytes()
        clean_key = clean_prefix.replace("_", "")
        loaded_global_bgms[clean_key] = raw_bytes
    except Exception:
        pass


def dispatch_bgms_to_stories():
    if not script_bgm_maps: return
    # Public BGM is shared. Collapse references across all Stories first so the
    # same cue is never transcoded once per Story. Independent cues are safe to
    # convert concurrently because each writes a different target file.
    refs=sorted({bgm for values in script_bgm_maps.values() for bgm in values if bgm})
    target_bgm_dir=TOOL_ROOT/'data'/'audio'/'bgm'/'decoded'
    target_bgm_dir.mkdir(parents=True,exist_ok=True)

    def one(bgm_ref):
        matched_key=None
        for key in loaded_global_bgms.keys():
            if key==bgm_ref or key.endswith(bgm_ref) or bgm_ref in key:
                matched_key=key; break
        if not matched_key: return bgm_ref,'missing'
        out_name=bgm_ref.lower().replace('_',''); out_ogg=target_bgm_dir/f'{out_name}.ogg'
        try:
            if out_ogg.is_file() and out_ogg.stat().st_size>4:
                with out_ogg.open('rb') as fh:
                    if fh.read(4)==b'OggS': return bgm_ref,'exists'
        except Exception: pass
        raw_payload=loaded_global_bgms[matched_key]
        if transcode_bgm_to_ogg(raw_payload,out_ogg): return bgm_ref,'decoded'
        clean_m4a=extract_m4a_from_raw_awb_bytes(raw_payload)
        if clean_m4a:
            (target_bgm_dir/f'{out_name}.m4a').write_bytes(clean_m4a); return bgm_ref,'m4a'
        return bgm_ref,'failed'

    if refs:
        w=workers_for(len(refs),cpu_bound=True)
        print(f'[*] [BGM公共池] unique={len(refs)} workers={w}')
        with ThreadPoolExecutor(max_workers=w,thread_name_prefix='bgm-decode') as ex:
            for rid,status in ex.map(one,refs):
                if status in ('failed','missing'): print(f'  [WARN] BGM {rid}: {status}')

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
                    break
            except Exception:
                pass


# ==========================================
# 4. Live2D 单模型隔离哈希对撞引擎（严格保留原保底映射）
# ==========================================

# 完整保留原始硬编码保底映射
EXPLICIT_HARDCODED_HASH_MAP = {
    "path_743000257": {"id": "ParamPenisSwitch1", "type": "Parameter"},
    "path_2250421531": {"id": "ParamSemenCut1", "type": "Parameter"},
    "path_3040876923": {"id": "ParamSemen1", "type": "Parameter"},
    "path_3747488952": {"id": "PartSemen1", "type": "PartOpacity"},
}

STANDARD_LIVE2D_FALLBACK_IDS = [
    # 基础旋转与位置
    "ParamAngleX", "ParamAngleY", "ParamAngleZ",
    "ParamBodyAngleX", "ParamBodyAngleY", "ParamBodyAngleZ",
    "ParamBodyAngle", "ParamBodyAngle2", "ParamBodyAngleM", "ParamBodyX", "ParamBodyY", "ParamBodyZ",
    "ParamBreath", "ParamBreathM", "ParamAllPosition", "ParamAllPosition2", "ParamAllPositionY", "ParamAllPositionX",
    
    # 眼部与眉毛
    "ParamEyeLOpen", "ParamEyeROpen", "ParamEyeLSmile", "ParamEyeRSmile",
    "ParamEyeBallX", "ParamEyeBallY", "ParamEyeBallSize", "ParamEyeForm", "ParamEyesHighlight", "ParamEyesChange",
    "ParamEyeHiSwing", "ParamEyeHiSwing2", "ParamEyeHiSwing3", "ParamEyeHiSwing4", 
    "ParamEyeHiSwing5", "ParamEyeHiSwing6", "ParamEyeHiSwing7", "ParamEyeHiSwing8",
    "ParamBrowLY", "ParamBrowRY", "ParamBrowLX", "ParamBrowRX", 
    "ParamBrowLAngle", "ParamBrowRAngle", "ParamBrowLForm", "ParamBrowRForm", "ParamBrowForm", "ParamBrowForm2", "ParamBrowForm3",
    
    # 嘴部与表情
    "ParamMouthForm", "ParamMouthOpenY", "ParMouthEyeForm3", "ParMouthEyeForm4",
    "ParMouthEyeForm5", "ParMouthEyeForm6", "ParMouthEyeForm7", "ParMouthEyeForm8",
    "ParMouthEyeForm9", "ParMouthEyeForm10", "ParamCheek", "whiteout",
    "ParamMouth00", "ParamMouth01", "ParamMouth02", "ParamMouth03", "ParamMouth04", "ParamMouth05", "ParamMouth06",
    
    # 头发与胸部
    "ParamHairFront", "ParamHairFront2", "ParamHairSide", "ParamHairBack", "ParamHairBackR", "ParamHairBackR2", "ParamHairBackL", "ParamHairBackL2",
    "ParamHairR", "ParamHairR2", "ParamHairL", "ParamHairL2", "ParamHairFuwa", "ParamHairFuwa2", "ParamRibbonR", "ParamRibbonR2", "ParamRibbonL", "ParamRibbonL2",
    "ParamBustX", "ParamBustY", "ParamBUST1", "ParamBUST2", "ParamNippleX", "ParamNipple",
    
    # 肢体与男主位移
    "ParamShoulderY", "ParamArmA", "ParamArmB", "ParamHandA", "ParamHandB", "ParamHandR", "ParamHandL", "ParamMHandLA",
    "ParamArmR1", "ParamArmR2", "ParamArmR3", "ParamArmL1", "ParamArmL2", "ParamArmL3",
    "ParamManWaist", "ParamManWaist2", "ParamManWaist3", "ParamManWaist4", "ParamManWaistX", "ParamManWaistY",

    # R18 特殊参数与全套 H-Scene 深度位移部件
    "ParamPenisSwitch", "ParamPenisSwitch1", "ParamPenisSwitch2",
    "ParamPenisSwing", "ParamPenisSwing1", "ParamPenisSwing2", "ParamPemisSwing",
    "ParamPenisY", "ParamPenisX", "ParamPenisZ", "ParamPenisPos", "ParamPenisPosY", "ParamPenisIn", "ParamPenisDepth",
    "ParamWet", "ParamWetA", "ParamWetB", "ParamLoveJuicea", "ParamSemen", "ParamSemen1", "ParamSemenA", "ParamSemenB", "ParamSemenC", "ParamSemenCut",
    "PartSemen", "PartSemen1", "PartSemenA", "PartSemenB", "PartSemenC", "PartWet",
    "Param6Sigh", "ParamLeg", "ParamLegBound", "ParamTear", "ParamFlush", "ParamHeart", "ParamFemaleGenitals", "ParamWimenSwitch"
]


def determine_target_type(name: str, default_type: str = "Parameter") -> str:
    clean = name.split("/")[-1]
    
    if any(clean.startswith(prefix) for prefix in ["Part", "ArtMesh", "Warp", "Rotation", "Deformer", "Drawable"]):
        return "PartOpacity"
    if any(clean.startswith(prefix) for prefix in ["Param", "Par", "whiteout"]):
        return "Parameter"
        
    return default_type


def get_crc32_unsigned(string_path: str) -> str:
    return str(binascii.crc32(string_path.encode('utf-8')) & 0xFFFFFFFF)


def is_valid_live2d_token(token: str) -> bool:
    if len(token) < 3:
        return False
    if not token[0].isalpha():
        return False
    if re.match(r'^[a-zA-Z]_\d+$', token) or re.match(r'^[a-zA-Z]{1,2}\d*$', token):
        return False
    
    system_junk = {
        "MOC3", "AnimationClip", "Animation", "Curves", "Transform", "GameObject",
        "MonoBehaviour", "Texture2D", "meta", "CubismModel", "CubismParameter",
        "CubismPart", "CubismDrawable", "CubismPhysics", "CubismPose", "CubismUserData",
        "CubismDisplayInfo", "Canvas", "Root", "Model", "Vector", "Vector3", "Quaternion",
        "Color", "Boolean", "Float", "Int32", "String", "System", "UnityEngine"
    }
    return token not in system_junk


def extract_real_ids_from_bytes_or_files(moc3_bytes=None, moc3_path=None) -> List[str]:
    detected_tokens = set()
    content = b""
    
    if moc3_bytes:
        content = moc3_bytes
    elif moc3_path and os.path.exists(moc3_path):
        try:
            with open(moc3_path, 'rb') as f:
                content = f.read()
        except Exception:
            pass

    if content:
        raw_tokens = re.findall(rb'[a-zA-Z][a-zA-Z0-9_-]{2,64}', content)
        for raw in raw_tokens:
            try:
                s = raw.decode('ascii', errors='ignore').strip()
                if is_valid_live2d_token(s):
                    detected_tokens.add(s)
            except Exception:
                pass

    expanded_tokens = set(detected_tokens)
    for token in detected_tokens:
        match = re.match(r'^(.*[a-zA-Z_])(\d+)$', token)
        if match:
            prefix = match.group(1)
            for i in range(1, 101):
                expanded_tokens.add(f"{prefix}{i}")
                expanded_tokens.add(f"{prefix}{i:02d}")

    return list(expanded_tokens)


def generate_all_path_crc_mappings(name_list: List[str], story_id: str = "") -> Dict[str, Dict[str, str]]:
    hash_map = {}
    prefix_rules = [
        ("", "Parameter"),
        ("Parameters/", "Parameter"),
        ("Parts/", "PartOpacity"),
        ("Model/Parameters/", "Parameter"),
        ("Model/Parts/", "PartOpacity"),
        ("Drawables/", "PartOpacity"),
        ("Deformers/", "PartOpacity"),
        ("Model/Drawables/", "PartOpacity"),
        ("Model/Deformers/", "PartOpacity"),
        ("Root/Parameters/", "Parameter"),
        ("Root/Parts/", "PartOpacity"),
        ("Root/Model/Parameters/", "Parameter"),
        ("Root/Model/Parts/", "PartOpacity"),
        ("CubismModel/Parameters/", "Parameter"),
        ("CubismModel/Parts/", "PartOpacity"),
        ("Live2D/Parameters/", "Parameter"),
        ("Live2D/Parts/", "PartOpacity"),
        ("Live2D/Model/Parameters/", "Parameter"),
        ("Live2D/Model/Parts/", "PartOpacity"),
    ]

    if story_id:
        prefix_rules.extend([
            (f"{story_id}/Parameters/", "Parameter"),
            (f"{story_id}/Parts/", "PartOpacity"),
            (f"l2d_{story_id}/Parameters/", "Parameter"),
            (f"l2d_{story_id}/Parts/", "PartOpacity"),
            (f"l2d_{story_id}/Model/Parameters/", "Parameter"),
            (f"l2d_{story_id}/Model/Parts/", "PartOpacity"),
        ])

    suffixes = ["", ".Value", ".Opacity", ".m_LocalPosition.x", ".m_LocalPosition.y", ".m_LocalPosition.z"]

    for name in name_list:
        inferred_type = determine_target_type(name)

        for prefix, ptype in prefix_rules:
            final_target = inferred_type if inferred_type else ptype
            full_path_base = f"{prefix}{name}"

            for sfx in suffixes:
                h_key = f"path_{get_crc32_unsigned(f'{full_path_base}{sfx}')}"
                if h_key not in hash_map:
                    hash_map[h_key] = {"id": name, "type": final_target}

    return hash_map


def build_global_fallback_hash_library() -> Dict[str, Dict[str, str]]:
    expanded_fallback = set(STANDARD_LIVE2D_FALLBACK_IDS)
    for name in STANDARD_LIVE2D_FALLBACK_IDS:
        for i in range(1, 101):
            expanded_fallback.add(f"{name}{i}")
            expanded_fallback.add(f"{name}{i:02d}")

    return generate_all_path_crc_mappings(list(expanded_fallback))

GLOBAL_FALLBACK_HASH_LIBRARY = build_global_fallback_hash_library()


def generate_dynamic_hash_library_for_moc(moc3_bytes=None, moc3_path=None, story_id: str = "") -> Dict[str, Dict[str, str]]:
    extracted_names = extract_real_ids_from_bytes_or_files(moc3_bytes=moc3_bytes, moc3_path=moc3_path)
    return generate_all_path_crc_mappings(extracted_names, story_id=story_id)


def normalize_to_unsigned_path_key(raw_key_str: str) -> str:
    clean_str = raw_key_str.strip()
    if clean_str.startswith("path_"):
        clean_str = clean_str[5:]
    
    try:
        val_int = int(clean_str)
        return f"path_{val_int & 0xFFFFFFFF}"
    except ValueError:
        pass

    try:
        val_int = int(clean_str, 16)
        return f"path_{val_int & 0xFFFFFFFF}"
    except ValueError:
        pass

    return raw_key_str


def resolve_id_from_hash_library(raw_key: str, hash_library: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    standard_key = normalize_to_unsigned_path_key(raw_key)

    if standard_key in hash_library:
        res = hash_library[standard_key]
        return {"id": res["id"], "type": determine_target_type(res["id"], res["type"])}

    if standard_key in EXPLICIT_HARDCODED_HASH_MAP:
        res = EXPLICIT_HARDCODED_HASH_MAP[standard_key]
        return {"id": res["id"], "type": determine_target_type(res["id"], res["type"])}

    if standard_key in GLOBAL_FALLBACK_HASH_LIBRARY:
        res = GLOBAL_FALLBACK_HASH_LIBRARY[standard_key]
        return {"id": res["id"], "type": determine_target_type(res["id"], res["type"])}

    raw_hash_key = f"path_{get_crc32_unsigned(raw_key)}"
    if raw_hash_key in hash_library:
        res = hash_library[raw_hash_key]
        return {"id": res["id"], "type": determine_target_type(res["id"], res["type"])}
    if raw_hash_key in EXPLICIT_HARDCODED_HASH_MAP:
        res = EXPLICIT_HARDCODED_HASH_MAP[raw_hash_key]
        return {"id": res["id"], "type": determine_target_type(res["id"], res["type"])}
    if raw_hash_key in GLOBAL_FALLBACK_HASH_LIBRARY:
        res = GLOBAL_FALLBACK_HASH_LIBRARY[raw_hash_key]
        return {"id": res["id"], "type": determine_target_type(res["id"], res["type"])}

    clean_name = raw_key.split("/")[-1] if "/" in raw_key else raw_key
    clean_hash_key = f"path_{get_crc32_unsigned(clean_name)}"
    if clean_hash_key in hash_library:
        res = hash_library[clean_hash_key]
        return {"id": res["id"], "type": determine_target_type(res["id"], res["type"])}
    if clean_hash_key in EXPLICIT_HARDCODED_HASH_MAP:
        res = EXPLICIT_HARDCODED_HASH_MAP[clean_hash_key]
        return {"id": res["id"], "type": determine_target_type(res["id"], res["type"])}
    if clean_hash_key in GLOBAL_FALLBACK_HASH_LIBRARY:
        res = GLOBAL_FALLBACK_HASH_LIBRARY[clean_hash_key]
        return {"id": res["id"], "type": determine_target_type(res["id"], res["type"])}

    target_type = determine_target_type(clean_name, "Parameter")
    return {"id": clean_name, "type": target_type}


class Live2DMaxPrecisionLinearEncoder(json.JSONEncoder):
    """ 超高精度 JSON 序列化编码器 """
    def encode(self, obj):
        if isinstance(obj, float):
            formatted = f"{obj:.10f}".rstrip('0').rstrip('.')
            return formatted if formatted else "0"
        elif isinstance(obj, list):
            return '[' + ', '.join(self.encode(el) for el in obj) + ']'
        elif isinstance(obj, dict):
            pairs = []
            for k, v in obj.items():
                pairs.append(f'"{k}": ' + self.encode(v))
            return '{' + ', '.join(pairs) + '}'
        return super(Live2DMaxPrecisionLinearEncoder, self).encode(obj)


def get_canonical_group_name(motion_name: str) -> str:
    name_lower = motion_name.lower()
    if name_lower.startswith("scene"):
        return "Scene"
    elif name_lower.startswith("eyeemotion") or (name_lower.startswith("eye") and "open" not in name_lower):
        return "EyeEmotion"
    elif name_lower.startswith("eyeopen"):
        return "EyeOpen"
    elif name_lower.startswith("eyebrow"):
        return "EyebrowEmotion"
    elif name_lower.startswith("faceangle"):
        return "FaceAngle"
    elif name_lower.startswith("faceemotion") or name_lower.startswith("face"):
        return "FaceEmotion"
    elif name_lower.startswith("mouthemotion") or name_lower.startswith("mouth"):
        return "MouthEmotion"
    elif name_lower.startswith("body"):
        return "Body"
    elif "idle" in name_lower:
        return "Idle"
    return "Motion"


def parse_float_safe(val_str: Optional[str], default: float = 0.0) -> float:
    if not val_str:
        return default
    v_low = str(val_str).strip().lower()
    if 'inf' in v_low or 'infinity' in v_low:
        return float('inf')
    try:
        return float(val_str)
    except ValueError:
        return default


def parse_anim_file(file_path: Path, hash_library: Dict[str, Dict[str, str]]):
    """
    【动作解析引擎 - 引入 classID: 114 严格校验，隔离 Unity GameObject 杂质曲线】
    清理尾部死区与缓冲帧，建立跨周期 C1 斜率自然闭合
    """
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()

    anim_name_match = re.search(r'm_Name:\s*(.+)', content)
    anim_name_stem = anim_name_match.group(1).strip() if anim_name_match else file_path.stem

    sample_rate_match = re.search(r'm_SampleRate:\s*([\d\.-]+)', content)
    anim_fps = parse_float_safe(sample_rate_match.group(1) if sample_rate_match else None, 30.0)

    loop_match = re.search(r'm_LoopTime:\s*([01])', content)
    raw_loop = False if not loop_match else (loop_match.group(1) == "1")

    # 只要文件名包含 _loop，绝对判定为循环动作
    is_loop_motion = raw_loop or ("_loop" in anim_name_stem.lower()) or ("loop" in anim_name_stem.lower())

    stop_time_match = re.search(r'm_StopTime:\s*([\d\.-]+)', content)
    anim_stop_time = parse_float_safe(stop_time_match.group(1) if stop_time_match else None, 0.0)

    curve_blocks = re.findall(r'-\s*curve:(.*?)(?=\n\s*-\s*curve:|\Z)', content, re.DOTALL)
    
    curves_data = []
    max_keyframe_time = 0.0

    for block in curve_blocks:
        time_points = re.findall(r'time:\s*([\d\.-]+)', block)
        for t_str in time_points:
            tf = parse_float_safe(t_str, 0.0)
            if tf > max_keyframe_time:
                max_keyframe_time = tf

    final_duration = anim_stop_time if (anim_stop_time is not None and anim_stop_time > max_keyframe_time) else max_keyframe_time
    if final_duration <= 0:
        final_duration = 0.333

    for block in curve_blocks:
        # 1. 过滤 classID 及 Unity 内部无用属性
        class_id_match = re.search(r'classID:\s*(\d+)', block)
        if class_id_match and class_id_match.group(1) != "114":
            continue

        attr_match = re.search(r'attribute:\s*([a-zA-Z0-9_]+)', block)
        if attr_match and attr_match.group(1) in ["m_IsActive", "classID"]:
            continue

        path_match = re.search(r'path:\s*(path_[\d\-]+|-?\d+|[a-zA-Z_0-9\-/]+)', block)
        if not path_match:
            continue

        raw_key = path_match.group(1).strip()
        if raw_key.lower() == "classid":
            continue

        mapped_info = resolve_id_from_hash_library(raw_key, hash_library)

        kf_blocks = re.split(r'-\s*serializedVersion:', block)
        parsed_kfs = []

        for kf in kf_blocks:
            t_m = re.search(r'time:\s*([\d\.-]+)', kf)
            v_m = re.search(r'value:\s*([\d\.-]+)', kf)

            if not (t_m and v_m):
                continue

            tf = parse_float_safe(t_m.group(1))
            vf = parse_float_safe(v_m.group(1))

            in_s_m = re.search(r'inSlope:\s*([\d\.-]+|[-+]?Infinity|[-+]?inf)', kf)
            out_s_m = re.search(r'outSlope:\s*([\d\.-]+|[-+]?Infinity|[-+]?inf)', kf)

            in_slope = parse_float_safe(in_s_m.group(1) if in_s_m else None, 0.0)
            out_slope = parse_float_safe(out_s_m.group(1) if out_s_m else None, 0.0)

            parsed_kfs.append({
                "time": tf,
                "value": vf,
                "inSlope": in_slope,
                "outSlope": out_slope
            })

        if not parsed_kfs:
            continue

        parsed_kfs.sort(key=lambda x: x["time"])

        # 确保 t = 0s 存在首帧
        if parsed_kfs[0]["time"] > 0.00001:
            parsed_kfs.insert(0, {
                "time": 0.0,
                "value": parsed_kfs[0]["value"],
                "inSlope": 0.0,
                "outSlope": parsed_kfs[0]["inSlope"]
            })

        # 2. 剔除死区 & 重新计算衔接切线斜率 (C1 连续)
        if is_loop_motion:
            # 清理超出 final_duration 的冗余缓冲帧
            while len(parsed_kfs) > 1 and parsed_kfs[-1]["time"] >= final_duration - 1e-5:
                parsed_kfs.pop()

            k0_val = parsed_kfs[0]["value"]
            
            if len(parsed_kfs) >= 2:
                prev_k = parsed_kfs[-1]
                next_k = parsed_kfs[1] if len(parsed_kfs) > 2 else parsed_kfs[0]

                dt_p = final_duration - prev_k["time"]
                dt_n = next_k["time"] if next_k["time"] > 0 else final_duration

                if dt_p + dt_n > 1e-5:
                    # 跨周期中心差分斜率
                    cyclic_slope = (next_k["value"] - prev_k["value"]) / (dt_p + dt_n)
                else:
                    cyclic_slope = 0.0

                # 对齐首尾斜率，保证加速度和速度顺畅过渡
                parsed_kfs[0]["outSlope"] = cyclic_slope
                parsed_kfs[0]["inSlope"] = cyclic_slope

                parsed_kfs.append({
                    "time": final_duration,
                    "value": k0_val,
                    "inSlope": cyclic_slope,
                    "outSlope": cyclic_slope
                })
            else:
                parsed_kfs.append({
                    "time": final_duration,
                    "value": k0_val,
                    "inSlope": 0.0,
                    "outSlope": 0.0
                })
        else:
            if parsed_kfs[-1]["time"] < final_duration - 0.00001:
                parsed_kfs.append({
                    "time": final_duration,
                    "value": parsed_kfs[-1]["value"],
                    "inSlope": 0.0,
                    "outSlope": 0.0
                })

        # 3. 三次 Hermite 样条插值
        def eval_hermite(k_start, k_end, t_curr):
            t0, v0, m0 = k_start["time"], k_start["value"], k_start["outSlope"]
            t1, v1, m1 = k_end["time"], k_end["value"], k_end["inSlope"]
            dt = t1 - t0
            if dt <= 1e-6:
                return v0
            s = (t_curr - t0) / dt
            h00 = 2 * s**3 - 3 * s**2 + 1
            h10 = s**3 - 2 * s**2 + s
            h01 = -2 * s**3 + 3 * s**2
            h11 = s**3 - s**2
            return h00 * v0 + h10 * dt * m0 + h01 * v1 + h11 * dt * m1

        # 4. 原生 30 FPS 均匀采样
        native_fps = anim_fps if (anim_fps is not None and anim_fps > 0) else 30.0
        frame_step = 1.0 / native_fps
        total_frames = int(round(final_duration * native_fps))

        sampled_points = []
        kf_idx = 0
        num_kfs = len(parsed_kfs)

        for f_i in range(total_frames + 1):
            t_sample = f_i * frame_step
            if t_sample > final_duration:
                t_sample = final_duration

            while kf_idx < num_kfs - 2 and parsed_kfs[kf_idx + 1]["time"] <= t_sample:
                kf_idx += 1

            v_sample = eval_hermite(parsed_kfs[kf_idx], parsed_kfs[kf_idx + 1], t_sample)
            sampled_points.append((t_sample, v_sample))

        sampled_points[-1] = (final_duration, parsed_kfs[0]["value"] if is_loop_motion else parsed_kfs[-1]["value"])

        # 5. RDP 曲线精简 (带环形边界保护)
        def rdp_simplify(pts, epsilon):
            if len(pts) <= 2:
                return pts
            dmax = 0.0
            index = 0
            end = len(pts) - 1
            t0, v0 = pts[0]
            tn, vn = pts[end]
            dt = tn - t0

            for i in range(1, end):
                t_i, v_i = pts[i]
                if dt > 1e-7:
                    v_line = v0 + (vn - v0) * ((t_i - t0) / dt)
                    dist = abs(v_i - v_line)
                else:
                    dist = abs(v_i - v0)

                if dist > dmax:
                    index = i
                    dmax = dist

            if dmax > epsilon:
                rec1 = rdp_simplify(pts[:index+1], epsilon)
                rec2 = rdp_simplify(pts[index:], epsilon)
                return rec1[:-1] + rec2
            else:
                return [pts[0], pts[end]]

        v_vals = [p[1] for p in sampled_points]
        v_range = max(v_vals) - min(v_vals)

        if v_range < 1e-5:
            simplified_points = [sampled_points[0], sampled_points[-1]]
        else:
            dynamic_eps = max(1e-4, v_range * 0.0012)

            if is_loop_motion and len(sampled_points) > 6:
                pad_count = min(4, len(sampled_points) // 3)
                head_pad = [(p[0] - final_duration, p[1]) for p in sampled_points[-pad_count-1:-1]]
                tail_pad = [(p[0] + final_duration, p[1]) for p in sampled_points[1:pad_count+1]]

                padded_pts = head_pad + sampled_points + tail_pad
                padded_simplified = rdp_simplify(padded_pts, dynamic_eps)

                simplified_points = [p for p in padded_simplified if 0.0 <= p[0] <= final_duration + 1e-5]

                if not simplified_points or simplified_points[0][0] > 1e-5:
                    simplified_points.insert(0, (0.0, parsed_kfs[0]["value"]))
                else:
                    simplified_points[0] = (0.0, parsed_kfs[0]["value"])

                if abs(simplified_points[-1][0] - final_duration) > 1e-5:
                    simplified_points.append((final_duration, parsed_kfs[0]["value"]))
                else:
                    simplified_points[-1] = (final_duration, parsed_kfs[0]["value"])
            else:
                simplified_points = rdp_simplify(sampled_points, dynamic_eps)

        # 6. 构建 Type 0 线性分段 Segments
        segments = [simplified_points[0][0], simplified_points[0][1]]
        for pt in simplified_points[1:]:
            segments.extend([0, pt[0], pt[1]])

        curves_data.append({
            "Target": mapped_info["type"],
            "Id": mapped_info["id"],
            "Segments": segments
        })

    # 7. 元数据配置
    if is_loop_motion:
        fade_in_time = 0.0
        fade_out_time = 0.0
        meta_loop_flag = False
    else:
        fade_in_time = 0.15
        fade_out_time = 0.15
        meta_loop_flag = False

    return curves_data, meta_loop_flag, final_duration, anim_fps, fade_in_time, fade_out_time, anim_name_stem

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
                raw_bytes = None
                if hasattr(obj, "get_raw_data"):
                    try:
                        raw_bytes = obj.get_raw_data()
                    except Exception:
                        pass
                
                if not raw_bytes or b"MOC3" not in raw_bytes:
                    try:
                        data = obj.read()
                        if hasattr(data, "bytes") and data.bytes:
                            raw_bytes = bytes(data.bytes)
                        elif hasattr(data, "m_Script") and data.m_Script:
                            if isinstance(data.m_Script, (bytes, bytearray)):
                                raw_bytes = bytes(data.m_Script)
                            elif isinstance(data.m_Script, str):
                                raw_bytes = data.m_Script.encode('utf-8', 'ignore')
                            elif isinstance(data.m_Script, list):
                                raw_bytes = bytes(data.m_Script)
                    except Exception:
                        pass

                if raw_bytes and isinstance(raw_bytes, (bytes, bytearray)):
                    idx = raw_bytes.find(b"MOC3")
                    if idx != -1:
                        clean_moc = raw_bytes[idx:]
                        if len(clean_moc) > 10000:
                            moc3_bytes_data = bytes(clean_moc)
            except Exception:
                pass

    if not textures and not moc3_bytes_data:
        return None

    story_path = OUTPUT_STORY_DIR / story_id
    moc_dir = story_path / "moc"
    motion_dir = story_path / "motions"
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

    local_hash_library = generate_dynamic_hash_library_for_moc(moc3_bytes=moc3_bytes_data, moc3_path=moc3_path, story_id=story_id)

    motions_config = {}
    bundle_anim_temp = TEMP_ANIM_DIR / story_id

    if L2D_BUNDLE_PATTERN.match(bundle_path.name):
        has_extracted = extract_anim_by_cli(bundle_path, bundle_anim_temp)
        if has_extracted:
            for root, _, files in os.walk(bundle_anim_temp):
                for file in files:
                    if file.endswith('.anim'):
                        anim_file_path = Path(root) / file
                        try:
                            curves, is_loop, duration, fps, fade_in_time, fade_out_time, anim_name_stem = parse_anim_file(anim_file_path, local_hash_library)
                            if not curves: continue
                            
                            canonical_group = get_canonical_group_name(anim_name_stem)

                            total_segments = sum((len(c["Segments"]) - 2) // 3 for c in curves)
                            total_points = sum((len(c["Segments"]) - 2) // 3 + 1 for c in curves)

                            motion3_json = {
                                "Version": 3,
                                "Meta": {
                                    "Duration": duration,
                                    "Fps": fps,  
                                    "Loop": is_loop,
                                    "AreBeziersRestricted": True,
                                    "FadeInTime": fade_in_time,
                                    "FadeOutTime": fade_out_time,
                                    "CurveCount": len(curves),
                                    "TotalSegmentCount": total_segments,
                                    "TotalPointCount": total_points,
                                    "UserDataCount": 0,
                                    "TotalUserDataSize": 0
                                },
                                "Curves": curves
                            }
                            
                            motion_filename = f"{anim_name_stem}.motion3.json"
                            out_motion_path = motion_dir / motion_filename
                            
                            if FORCE_OVERWRITE and out_motion_path.exists():
                                try: out_motion_path.unlink()
                                except: pass

                            with open(out_motion_path, 'w', encoding='utf-8') as out_f:
                                json_str = Live2DMaxPrecisionLinearEncoder().encode(motion3_json)
                                parsed_json = json.loads(json_str)
                                json.dump(parsed_json, out_f, indent=2, ensure_ascii=False)
                            
                            if canonical_group not in motions_config:
                                motions_config[canonical_group] = []
                            
                            motions_config[canonical_group].append({
                                "File": f"motions/{motion_filename}"
                            })
                            print(f"    [➔] 成功导出 Motion ({len(curves)} 曲线, FPS: {fps}): {motion_filename}")
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
                "Physics": "",
                "DisplayInfo": "",
                "Expressions": [],
                "Motions": motions_config
            },
            "Groups": [],
            "HitAreas": []
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
        # l2dmessage keeps the original 3rd argument as the face/expression
        # selector. Preserve it both in args and as an explicit field so the
        # player can switch expressions without having to reinterpret args.
        if cmd == "l2dmessage" and len(args) > 2:
            entry["face"] = args[2]
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
    if low.startswith("l2d") or "l2d" in low or "live2d" in low: return "live2d"
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

def build_motions_list(story_dir: Path):
    motions_dir = story_dir / "motions"
    if not motions_dir.exists(): return []
    files = sorted(list(motions_dir.glob("*.motion3.json")), key=lambda p: [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", p.name)])
    groups = defaultdict(list)
    for f in files:
        name = f.name.replace(".motion3.json", "")
        group = get_canonical_group_name(name)
        groups[group].append((name, f))
    motions = []
    for group_name, items in groups.items():
        for idx, (name, f) in enumerate(items):
            duration = 0.5
            is_loop = ("_loop" in name.lower()) or ("loop" in name.lower())
            try:
                with open(f, "r", encoding="utf-8") as mf:
                    m_data = json.load(mf)
                    meta = m_data.get("Meta", {})
                    duration = meta.get("Duration", 0.5)
                    if "Loop" in meta and meta["Loop"]:
                        is_loop = True
            except Exception:
                pass

            motions.append({
                "name": name,
                "group": group_name,
                "path": f"motions/{f.name}",
                "duration": duration,
                "loop": is_loop,
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

def _motion_files(story_dir: Path):
    motion_dir = story_dir / "motions"
    if not motion_dir.exists():
        return []
    return sorted(
        motion_dir.glob("*.motion3.json"),
        key=lambda p: [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", p.stem)],
    )


def _is_runtime_only_motion_reference(name):
    """Return True for Story motion names that are runtime-only reset triggers.

    The original Unity Story can call these names even when no standalone
    AnimationClip/motion3.json exists in the exported L2D resources. They are
    intentionally excluded from asset-existence validation only.
    """
    return bool(str(name or '').strip()) and is_runtime_reset(str(name))


def _motion_ref_enabled(cmd):
    args=cmd.get('args') or []
    if len(args) < 2:
        return True
    value=args[1]
    if isinstance(value,bool):
        return value
    if isinstance(value,(int,float)):
        return bool(value)
    text=str(value).strip().casefold()
    if text in ('false','0','off','no','disable','disabled'):
        return False
    if text in ('true','1','on','yes','enable','enabled'):
        return True
    return True


def _motion_reference_aliases(name):
    text=Path(str(name or '').replace('\\','/')).name.casefold().strip()
    for suffix in ('.motion3.json','.motion3','.json'):
        if text.endswith(suffix):
            text=text[:-len(suffix)]
    compact=re.sub(r'[^a-z0-9]+','',text)
    aliases={text,compact}
    if text.endswith('_loop'):
        base=text[:-5].rstrip('_- '); aliases.add(base); aliases.add(re.sub(r'[^a-z0-9]+','',base))
    if compact.endswith('loop'):
        aliases.add(compact[:-4])
    return {x for x in aliases if x}


def _canonicalize_motion_references(commands, motion_files):
    """Validate Story logical motion triggers against exported motion3 assets.

    The Story script does not always use the exported AnimationClip filename.
    For example EyeOpen03 maps to EyeOpenF03, EyeOpen03_N maps to EyeOpenN03,
    and EyeEmotionLeft/Right/... are logical Animator triggers backed by
    numbered EyeEmotion clips.  Keep the original command text intact so the
    browser player can resolve it with model data; only report triggers that
    truly cannot be backed by the exported model.
    """
    actual_names=[path.name[:-len('.motion3.json')] for path in motion_files]
    missing=[]
    for cmd in commands:
        if cmd.get('command') not in ('l2dmotion','asyncl2dmotion'):
            continue
        raw=str(cmd.get('motion','') or '')
        if not raw or not _motion_ref_enabled(cmd) or _is_runtime_only_motion_reference(raw):
            continue
        resolved=resolve_trigger(raw, actual_names)
        if not resolved:
            missing.append(raw)
    return sorted(set(missing),key=str.casefold)


def build_story_json(story_dir: Path):
    text_dir = story_dir / "textassets"
    txt_files = sorted(text_dir.glob("*.txt"))
    if not txt_files:
        return

    txt = txt_files[0]
    story_id = story_dir.name
    script_id = txt.stem
    parsed = parse_script(txt)

    motion_files = _motion_files(story_dir)
    missing_motions = _canonicalize_motion_references(parsed["commands"], motion_files)
    # Rebuild summary motion list after canonicalization.
    parsed["motions"] = sorted(
        {c["motion"] for c in parsed["commands"] if c.get("motion")},
        key=str.casefold,
    )
    if missing_motions:
        print(f"  [WARN] {story_id}: {len(missing_motions)} story motion references have no exported motion3.json")
        for name in missing_motions[:20]:
            print(f"         missing motion: {name}")

    moc_dir = story_dir / "moc"
    moc_file = next(moc_dir.glob("*.moc3"), None) if moc_dir.exists() else None

    thumb_rel_path = None
    thumb_icon_id = story_id[:-1] if re.fullmatch(r"\d{11}", story_id) else story_id
    thumb_candidates = [
        OUTPUT_THUMB_DIR / f"thumb_{thumb_icon_id}.png",
        OUTPUT_THUMB_DIR / f"thumb_{story_id}.png",
    ]
    target_thumb_file = next((x for x in thumb_candidates if x.exists()), None)
    if target_thumb_file is not None:
        thumb_rel_path = f"../thumb/{target_thumb_file.name}"

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

    if not moc_file:
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
    tmp_path = out_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(story, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)
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
    print(" 启动全流程解包管线 (ClassID 过滤修复版)")
    print("==========================================================")

    # Stage 1: 扫描与预载
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

    # Stage 3: Live2D 解包与原版无损动画提取
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