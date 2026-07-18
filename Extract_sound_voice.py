import os
import re
import json
import subprocess
from pathlib import Path
from collections import defaultdict
import UnityPy

# ==========================================
# 1. 基础路径配置
# ==========================================
BUNDLE_DIR = Path("./raw_bundles")       # 原始 .bundle 存放目录
OUTPUT_STORY_DIR = Path("./data_r18_all/stories") # 故事及音频分类输出路径

OUTPUT_STORY_DIR.mkdir(parents=True, exist_ok=True)

# 跨包全局元数据搜集器，用于最终生成各剧情的 story.json
story_metadata = defaultdict(lambda: {
    "storyId": "",
    "textasset": "",
    "voices": [],
    "bgvs": []
})

script_voice_maps = {}    # 存放每个故事ID解析出的 vc_ 严格物理顺序队列
pending_audio_tasks = []  # 暂存音频 Bundle 任务

# ==========================================
# 2. 音频高保真转码函数 (FFmpeg)
# ==========================================
def transcode_to_ogg(payload: bytearray, output_path: Path) -> bool:
    """ 将盲切出来的任意音频二进制流转换为标准 OGG 格式 """
    ffmpeg_bin = 'ffmpeg'
    
    # 优先尝试从 pip install imageio-ffmpeg 中获取二进制路径
    try:
        import imageio_ffmpeg
        ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass

    temp_input = output_path.with_suffix('.tmp_in')
    try:
        temp_input.write_bytes(payload)
        
        cmd = [
            ffmpeg_bin, '-y',
            '-i', str(temp_input),   # 读取落地临时文件
            '-acodec', 'libvorbis',  # 使用标准 vorbis 编码器
            '-q:a', '4',             # 设置音频质量级别 (~128kbps 高音质)
            str(output_path)
        ]
        # 执行转换
        res = subprocess.run(cmd, capture_output=True)
        return res.returncode == 0
    except Exception as e:
        print(f"      [-] FFmpeg 转换时发生异常: {e}")
        return False
    finally:
        if temp_input.exists():
            temp_input.unlink()

# ==========================================
# 3. 核心辅助函数：全自动深度递归检索 @UTF 数组
# ==========================================
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

# ==========================================
# 4. 第一阶段：预扫描与剧本序列抽取
# ==========================================
def parse_script_and_cache(bundle_path: Path):
    """ 从剧本 Bundle 中流式提取文本，并登记该故事的语音先后顺序 """
    id_match = re.search(r"hmr_(\d+)", bundle_path.name)
    if not id_match:
        return
    
    story_id = id_match.group(1)
    print(f"[*] [第一阶段] 正在解析剧本包: {story_id}")
    
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

    # 规范化创建当前故事的 textassets 目录
    textasset_dir = OUTPUT_STORY_DIR / story_id / "textassets"
    textasset_dir.mkdir(parents=True, exist_ok=True)
    
    script_name = f"hmr_{story_id}.txt"
    with open(textasset_dir / script_name, "w", encoding="utf-8") as f_txt:
        f_txt.write(script_text)
    print(f"  [+] 剧本已成功导出 -> stories/{story_id}/textassets/{script_name}")

    # 登记全局 JSON 基础元数据
    story_metadata[story_id]["storyId"] = story_id
    story_metadata[story_id]["textasset"] = script_name

    # 提取当前剧情剧本里所有的 vc_ 语音物理行序
    voice_pattern = re.compile(r"vc_[a-zA-Z0-9_]+")
    ordered_voices = []
    for line in script_text.splitlines():
        matches = voice_pattern.findall(line)
        for voice_id in matches:
            if voice_id not in ordered_voices:
                ordered_voices.append(voice_id)
                
    script_voice_maps[story_id] = ordered_voices
    print(f"  [+] 成功建立语音对齐序列，共计 {len(ordered_voices)} 个语音节点。")

# ==========================================
# 5. 第二阶段：音频包内存流高级特征码盲切 + 目录格式分类对齐
# ==========================================
def slice_audio_from_bundle(story_id: str, bundle_path: Path, is_bgv: bool = False):
    """ 
    解析提取原始 ACB，执行特征码扫描后，
    按照播放器补丁规范自动分流到 /voice/ 或 /bgv/ 目录并输出标准 .ogg
    """
    print(f"[*] [第二阶段] 正在解包音频资产: {story_id} ({'环境音BGV' if is_bgv else '人物语音Voice'})")
    
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

    # 根据载入播放器补充包的标准定义目标子目录
    sub_folder = "bgv" if is_bgv else "voice"
    target_dir = OUTPUT_STORY_DIR / story_id / "audio" / "decoded" / sub_folder
    target_dir.mkdir(parents=True, exist_ok=True)
    
    voice_sequence = script_voice_maps.get(story_id, [])
    print(f"  [+] 在内存中精准检索到 {len(audio_starts)} 个音频轨道起始点。")

    for idx, (start_pos, original_ext) in enumerate(audio_starts):
        end_pos = audio_starts[idx + 1][0] if idx + 1 < len(audio_starts) else len(acb_bytes)
        payload = acb_bytes[start_pos:end_pos]

        # 命名路由切换与规范化适配
        if is_bgv:
            # 环境背景音命名适配：bgv_{story_id}_{序号:03d}_01
            base_name = f"bgv_{story_id}_{idx + 1:03d}_01"
            story_metadata[story_id]["bgvs"].append(f"{base_name}.ogg")
        else:
            # 人物语音命名适配
            if idx < len(voice_sequence):
                base_name = f"{voice_sequence[idx]}"
            else:
                base_name = f"voice_extra_{idx + 1}"
            story_metadata[story_id]["voices"].append(f"{base_name}.ogg")

        target_ogg_path = target_dir / f"{base_name}.ogg"

        # 转码输出
        success = transcode_to_ogg(payload, target_ogg_path)
        if success:
            print(f"    [➔] 已成功存入 {sub_folder}: {base_name}.ogg ({len(payload)} bytes)")
        else:
            fallback_path = target_dir / f"{base_name}{original_ext}"
            fallback_path.write_bytes(payload)
            print(f"    [!] 转换异常，已直接写出原始音轨: {base_name}{original_ext}")

    print(f"  [+] 成功分类导出该音频序列。")

# ==========================================
# 6. 背景音乐 BGM.awb 直接倒出并转码
# ==========================================
def extract_global_bgm(bundle_path: Path):
    """ 处理外部 BGM 包，将其转码为 OGG 并安全倒出到全局目录 """
    print(f"[*] 正在处理公共背景音乐包: {bundle_path.name}")
    try:
        env = UnityPy.load(str(bundle_path))
    except Exception:
        return

    global_audio_dir = Path("./data/audio/se")
    global_audio_dir.mkdir(parents=True, exist_ok=True)

    raw_bytes = None
    for obj in env.objects:
        if obj.type.name == "TextAsset":
            data = obj.read()
            if hasattr(data, "bytes") and data.bytes:
                raw_bytes = data.bytes
        elif obj.type.name == "MonoBehaviour":
            raw_bytes = extract_acb_from_monobehaviour(obj)
            
        if raw_bytes:
            break

    if raw_bytes:
        bgm_base = bundle_path.name.split('.awb')[0]
        target_ogg_path = global_audio_dir / f"{bgm_base}.ogg"
        
        success = transcode_to_ogg(raw_bytes, target_ogg_path)
        if success:
            print(f"  [+] 成功整轨转码 BGM 资产 -> data/audio/se/{bgm_base}.ogg")
        else:
            fallback_ext = ".wav" if raw_bytes[:4] == b'RIFF' else ".ogg"
            with open(global_audio_dir / f"{bgm_base}{fallback_ext}", "wb") as f:
                f.write(raw_bytes)
            print(f"  [!] 转码失败，已直接写出原始 BGM 文件 -> data/audio/se/{bgm_base}{fallback_ext}")

# ==========================================
# 7. 总控制中心
# ==========================================
def main():
    if not BUNDLE_DIR.exists():
        print(f"[-] 核心错误: 找不到存放原始 Bundle 的目录 '{BUNDLE_DIR}'")
        return

    all_bundles = list(BUNDLE_DIR.glob("*.bundle"))
    if not all_bundles:
        print(f"[-] 在 '{BUNDLE_DIR}' 下没有发现任何 .bundle 文件。")
        return

    # ----------------------------------------------------
    # 第一次迭代：精准解析所有剧本、过滤全局公共 BGM
    # ----------------------------------------------------
    for bundle in all_bundles:
        if "mainchara_hmr_" in bundle.name:
            parse_script_and_cache(bundle)
        elif bundle.name.startswith("bgm"):
            extract_global_bgm(bundle)
        else:
            # 登记音频提取任务
            id_match = re.search(r"hmr_(\d+)", bundle.name)
            if id_match:
                story_id = id_match.group(1)
                is_bgv = "_bgv.acb" in bundle.name
                pending_audio_tasks.append((story_id, bundle, is_bgv))

    # ----------------------------------------------------
    # 第二次迭代：在剧本序列解析完毕后，进行音频分类切片
    # ----------------------------------------------------
    print("\n[+] 剧本基设扫描完毕，开始切入音频重组管线...")
    for story_id, bundle, is_bgv in pending_audio_tasks:
        slice_audio_from_bundle(story_id, bundle, is_bgv)

    # ----------------------------------------------------
    # 第三阶段：统一在各剧本根目录生成标准 story.json
    # ----------------------------------------------------
    print("\n[+] 全分类切片结束，开始构建并写出 story.json 索引树...")
    for story_id, meta in story_metadata.items():
        # 兜底确保目标故事文件夹一定存在
        story_folder = OUTPUT_STORY_DIR / story_id
        story_folder.mkdir(parents=True, exist_ok=True)
        
        json_path = story_folder / "story.json"
        with open(json_path, "w", encoding="utf-8") as f_json:
            json.dump(meta, f_json, indent=4, ensure_ascii=False)
        print(f"  [➔] 成功输出元数据索引 -> stories/{story_id}/story.json")

    print("\n[★] 项目解包、分类转码、索引生成全部闭环成功！")

if __name__ == "__main__":
    main()