import json
import re
import soundfile as sf
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image

COMMAND_INFO = {
    ":label": {
        "category": "flow",
        "title": "标签",
        "args": ["label"],
        "description": "NovelScriptCommands uses ':' lines as jump targets.",
    },
    "adultui": {
        "category": "ui",
        "title": "成人 UI 开关",
        "args": ["on/off"],
        "description": "NovelCmdAdultUI toggles the adult-scene overlay state.",
    },
    "uivisible": {
        "category": "ui",
        "title": "UI 可见性",
        "args": ["on/off"],
        "description": "NovelCmdUIVisible toggles the common UI layer.",
    },
    "window": {
        "category": "ui",
        "title": "文本窗口",
        "args": ["on/off", "fade_seconds"],
        "description": "NovelCmdWindow changes the message window visibility/fade.",
    },
    "fade": {
        "category": "screen",
        "title": "画面淡入淡出",
        "args": ["In/Out", "color", "seconds"],
        "description": "NovelCmdFade drives the screen fade model.",
    },
    "message": {
        "category": "text",
        "title": "旁白/普通文本",
        "args": ["speaker", "message", "voice?"],
        "description": "NovelCmdMessage writes to NovelModelMessage and may play voice.",
    },
    "l2dmessage": {
        "category": "text",
        "title": "Live2D 台词",
        "args": ["speaker", "message", "face_or_empty", "voice_id"],
        "description": "NovelCmdL2dMessage derives from NovelCmdMessage and ties speech to Live2D/lip sync.",
    },
    "l2dshow": {
        "category": "live2d",
        "title": "显示 Live2D",
        "args": ["model_object"],
        "description": "NovelCmdL2dShow loads and draws a NovelModelLive2D object.",
    },
    "l2dhide": {
        "category": "live2d",
        "title": "隐藏 Live2D",
        "args": [],
        "description": "NovelCmdL2dHide releases the active Live2D model.",
    },
    "l2dmotion": {
        "category": "live2d",
        "title": "Live2D 动作",
        "args": ["motion_trigger"],
        "description": "NovelCmdL2dMotion calls NovelModelLive2D.PlayMotion.",
    },
    "asyncl2dmotion": {
        "category": "live2d",
        "title": "延迟 Live2D 动作",
        "args": ["motion_trigger", "bool", "async_code", "delay_seconds"],
        "description": "Async wrapper schedules PlayMotion and finishes according to STOP/async code.",
    },
    "bgmplay": {
        "category": "audio",
        "title": "播放 BGM",
        "args": ["tag", "cue", "fade_seconds"],
        "description": "NovelCmdBGMPlay starts a CRI cue by tag.",
    },
    "bgmstop": {
        "category": "audio",
        "title": "停止 BGM",
        "args": ["tag", "fade_seconds"],
        "description": "NovelCmdBGMStop fades/stops a CRI cue by tag.",
    },
    "bgvplay": {
        "category": "audio",
        "title": "播放背景语音/环境声",
        "args": ["tag", "cue", "volume", "loop"],
        "description": "NovelCmdBGVPlay starts a CRI background-voice cue.",
    },
    "bgvstop": {
        "category": "audio",
        "title": "停止背景语音/环境声",
        "args": ["tag", "fade_seconds", "volume?"],
        "description": "NovelCmdBGVStop fades/stops a CRI background-voice cue.",
    },
    "charaload": {
        "category": "character",
        "title": "加载角色",
        "args": ["tag", "character_id", "display_name"],
        "description": "NovelCmdCharaLoad registers a character resource/name.",
    },
    "wait": {
        "category": "flow",
        "title": "等待",
        "args": ["seconds"],
        "description": "NovelCmdWait blocks script playback for the given seconds.",
    },
    "cleanall": {
        "category": "screen",
        "title": "清空场景",
        "args": ["target"],
        "description": "NovelCmdCleanAll clears visible scene objects/layers.",
    },
}


def parse_line(line: str):
    line = line.strip().lstrip("\ufeff")

    if not line:
        return None

    # label
    if line.startswith(":"):
        return {
            "command": ":label",
            "rawCommand": line,
            "args": [],
            "label": line[1:]
        }

    parts = line.split(",")
    cmd = parts[0]
    args = parts[1:]

    return {
        "command": cmd,
        "rawCommand": cmd,
        "args": args
    }


# =========================
# enrich（完全依赖 COMMAND_INFO）
# =========================
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

    # ===== label =====
    if cmd == ":label":
        entry["label"] = cmd_obj.get("label", "")

    # ===== fade =====
    if cmd == "fade" and len(args) >= 3:
        entry["direction"] = args[0]
        entry["color"] = args[1]
        try:
            entry["seconds"] = float(args[2])
        except:
            pass

    # ===== l2dshow =====
    if cmd == "l2dshow" and args:
        entry["model"] = args[0]

    # ===== motion =====
    if cmd in ("l2dmotion", "asyncl2dmotion") and args:
        entry["motion"] = args[0]
        if cmd == "asyncl2dmotion" and len(args) >= 4:
            try:
                entry["delay"] = float(args[3])
            except:
                pass

    # ===== audio =====
    if cmd in ("bgmplay", "bgvplay") and len(args) >= 2:
        entry["cue"] = args[1]

    # ===== message =====
    if cmd in ("message", "l2dmessage"):
        speaker = args[0] if len(args) > 0 else ""
        message = args[1] if len(args) > 1 else ""
        voice = ""
        for a in args:
            if isinstance(a, str) and a.startswith("vc_"):
                voice = a
                break

        entry["speaker"] = speaker
        entry["message"] = message
        entry["voice"] = voice

    return entry


def command_category(command: str):
    low = (command or "").lower()
    if low.startswith(":") or "wait" in low or "jump" in low or "label" in low or "section" in low or low in ("initend", "title"):
        return "flow"
    if "message" in low or low in ("talk", "telop"):
        return "text"
    if "bgm" in low or "bgv" in low or "seplay" in low or "sestop" in low or "sefade" in low or low.startswith("se") or "voice" in low:
        return "audio"
    if low.startswith("l2d") or "live2d" in low:
        return "live2d"
    if "fade" in low or "blur" in low or "shake" in low or "clean" in low or "linework" in low:
        return "screen"
    if "chara" in low or "silhouette" in low or "emodelete" in low or low == "priority":
        return "character"
    if "bg" in low or "camera" in low or "move" in low or "scale" in low or "rotate" in low:
        return "stage"
    if "still" in low or "image" in low or "prefab" in low or "object" in low or "asset" in low:
        return "asset"
    if "ui" in low or "window" in low or "popup" in low:
        return "ui"
    return "unknown"


# =========================
# script parser
# =========================
def parse_script(txt_path: Path):
    with open(txt_path, "r", encoding="utf-8-sig") as f:
        lines = f.readlines()

    commands = []
    labels = set()
    audio_ids = set()
    motions = set()
    counter = Counter()

    idx = 0

    for line_no, line in enumerate(lines, start=1):
        obj = parse_line(line)
        if not obj:
            continue

        cmd = enrich(obj)
        cmd = dict(cmd)
        cmd["index"] = idx
        cmd["line"] = line_no
        cmd = {
            "index": cmd["index"],
            "line": cmd["line"],
            **{k: v for k, v in cmd.items() if k not in ("index", "line")}
        }

        commands.append(cmd)

        c = cmd["command"]
        counter[c] += 1
        cmd["category"] = command_category(c)
        if c == ":label":
            labels.add(cmd["label"])

        if "motion" in cmd:
            motions.add(cmd["motion"])

        if "cue" in cmd:
            audio_ids.add(cmd["cue"])

        if cmd["command"] == "wait":
            args = cmd.get("args", [])
            if args:
                cmd["seconds"] = float(args[0])


        if cmd.get("voice"):
            audio_ids.add(cmd["voice"])

        if cmd["command"] in ("bgvstop", "bgmstop"):
            args = cmd.get("args") or []
            if len(args) > 0:
                cmd["tag"] = args[0]

        idx += 1
    messages = []

    for cmd in commands:
        if cmd["command"] not in ("message", "l2dmessage"):
            continue

        msg = {
            "index": cmd["index"],
            "line": cmd["line"],
            "command": cmd["command"],
            "rawCommand": cmd["rawCommand"],
            "args": cmd.get("args", []),
            "category": cmd.get("category", ""),
            "title": cmd.get("title", ""),
        }

        # speaker/message/voice
        if cmd["command"] == "l2dmessage":
            msg["speaker"] = cmd.get("speaker", "")
            msg["message"] = cmd.get("message", "")
            msg["voice"] = cmd.get("voice", "")
        else:
            msg["speaker"] = cmd.get("speaker", "")
            msg["message"] = cmd.get("message", "")
            msg["voice"] = cmd.get("voice", "")

        messages.append(msg)
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
            if img.suffix.lower() not in [".png", ".jpg", ".jpeg", ".webp"]:
                continue

            w, h = 0, 0
            try:
                with Image.open(img) as im:
                    w, h = im.size
            except:
                pass

            textures.append({
                "name": img.stem,
                "path": f"textures/{img.name}",
                "width": w,
                "height": h,
                "bundle": ""   # 你现在阶段可以为空
            })

    return textures
def build_animations_from_folder(story_dir: Path, story_id: str):
    motion_dir = story_dir / "motions"
    if not motion_dir.exists():
        return []

    bundle_fallback = f"l2d/{story_id}/l2d/{story_id}.prefab.bundle"

    animations = []

    for m in sorted(motion_dir.glob("*")):
        if not m.is_file():
            continue

        name = m.stem.split(".")[0]

        animations.append({
            "name": name,
            "bundle": bundle_fallback,
            "pathId": 0   # 你现在阶段无法还原
        })

    return animations
def load_motion_meta(file_path: Path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except:
        return None

    # 不同版本字段可能不同，这里做兼容
    meta = data.get("Meta", {}) if isinstance(data, dict) else {}

    return {
        "duration": meta.get("Duration", 0.5),
        "loop": meta.get("Loop", False),
        "fadeInTime": meta.get("FadeInTime", 1.0),
        "fadeOutTime": meta.get("FadeOutTime", 1.0),
    }

def build_motions(story_dir: Path):
    motions_dir = story_dir / "motions"

    if not motions_dir.exists():
        return []

    files = list(motions_dir.glob("*.motion3.json"))

    # 自然排序
    def natural_key(p: Path):
        return [int(x) if x.isdigit() else x.lower()
                for x in re.split(r"(\d+)", p.name)]

    files = sorted(files, key=natural_key)

    groups = defaultdict(list)

    # ------------------------
    # 1. 先分组
    # ------------------------
    for f in files:
        name = f.stem.split(".motion3")[0]

        if name.startswith("scene"):
            group = "Scene"
        else:
            group = re.sub(r"\d+$", "", name)

        groups[group].append((name, f))

    motions = []

    # ------------------------
    # 2. 每组单独 index
    # ------------------------
    for group_name, items in groups.items():

        for idx, (name, f) in enumerate(items):

            meta = load_motion_meta(f) or {}

            name_lower = name.lower()
            loop = meta.get("loop", False) or ("loop" in name_lower)

            motions.append({
                "name": name,
                "group": group_name,
                "path": f"motions/{f.name}",
                "duration": meta.get("duration", 0.5),
                "loop": loop,
                "fadeInTime": meta.get("fadeInTime", 1.0),
                "fadeOutTime": meta.get("fadeOutTime", 1.0),
                "bundle": "",
                "pathId": 0,

                # ⭐ 核心：group 内 index
                "index": idx
            })

    return motions
def build_fade_motions(story_dir: Path, story_id: str):
    bundle_dir = Path("workspace/bundles")
    # 直接猜路径（大佬就是这么干的）
    bundle_name = f"{story_id}.fademotionlist.asset"
    # 模糊匹配
    bundle = None
    for f in bundle_dir.glob("*.bundle"):
        if bundle_name in f.name:
            bundle = f.name
            break
    return [
        {
            "name": f"l2d_{story_id}.fadeMotionList",
            "bundle": f"{bundle}" if bundle else "",
            "pathId": 0,
            "motionInstanceCount": 0,
            "fadeMotionObjectCount": 0
        }
    ]
def build_monobehaviours(story_dir: Path, story_id: str):
    res = []
    bundle_dir = Path("workspace/bundles")
    # 1️⃣ 找所有 prefab / effectobject bundle
    prefab_bundles = []
    for b in bundle_dir.glob("*.bundle"):
        name = b.name
        # 必须属于当前 story
        if story_id not in name:
            continue
        # monoBehaviour来源：prefab / effectobject
        if "prefab" in name or "effectobject" in name:
            prefab_bundles.append(b)
    if not prefab_bundles:
        return []
    # 2️⃣ 每个 prefab bundle 生成一组 MonoBehaviour
    for bundle in prefab_bundles:
        name = bundle.stem
        # 尝试从 bundle 名提取 pathId（如果没有就0）
        # MonoBehaviour_-9180067xxxxxx 这种通常在 name里
        ids = re.findall(r"-?\d{6,}", name)
        if ids:
            path_ids = ids
        else:
            path_ids = ["0"]
        # 3️⃣ 生成结构（关键点：一一对应 bundle）
        for pid in path_ids:
            res.append({
                "name": f"MonoBehaviour_{pid}",
                "bundle": str(bundle),
                "pathId": 0
            })

    return res
def get_duration(file_path):
    try:
        data, sr = sf.read(file_path)
        return len(data) / sr
    except Exception as e:
        print("[duration fail]", file_path, e)
        return 0.0


def build_audio(story_dir: Path):
    audio_root = story_dir / "audio"
    raw_dir = audio_root / "raw"
    decoded_dir = audio_root / "decoded"
    cues = {}
    if not decoded_dir.exists():
        return {"cues": {}}

    # -------------------------
    # 1️⃣ raw映射（acb + awb）
    # -------------------------
    raw_map = {}
    if raw_dir.exists():
        for f in raw_dir.glob("*"):
            name = f.stem
            if "bgv" in name:
                raw_map["bgv"] = f
            elif "vc" in name:
                raw_map["vc"] = f
            elif "bgm" in name:
                raw_map["bgm"] = f

    # -------------------------
    # 2️⃣ decoded扫描
    # -------------------------
    for category_dir in decoded_dir.iterdir():
        if not category_dir.is_dir():
            continue

        subsong_counter = defaultdict(int)

        category = category_dir.name

        for f in category_dir.rglob("*.ogg"):
            cue_id = f.stem

            parts = cue_id.split("_")
            subsong = None

            # -------------------------
            # ① 文件名编号优先（可选保留）
            # -------------------------
            if len(parts) >= 3 and parts[-2].isdigit():
                subsong = int(parts[-2])

            # -------------------------
            # ② fallback：按类别顺序编号（推荐核心）
            # -------------------------
            if subsong is None:
                subsong_counter[category] += 1
                subsong = subsong_counter[category]
            # -------------------------
            # duration 真值（关键）
            # -------------------------
            duration = get_duration(f)

            # -------------------------
            # raw source（bgm特殊awb）
            # -------------------------
            if category == "bgm":
                source = f"audio/raw/{cue_id}.awb"
            elif category in raw_map:
                source = str(raw_map[category].relative_to(story_dir).as_posix())
            else:
                source = ""
            if category == "bgm":
                channel = 2
                subsong = 1
                encoding = "AAC (Advanced Audio Coding)"
            else:
                channel = 1
                encoding = "CRI HCA"
            cues[cue_id] = {
                "name": cue_id,
                "category": category,
                "path": str(f.relative_to(story_dir).as_posix()),
                "source": source,
                "subsong": subsong,
                "duration": duration,
                "sampleRate": 48000,
                "channels": channel,
                "encoding": encoding,
                "bytes": f.stat().st_size
            }

    return {"cues": cues,
            "errors": [],
            "decoder": r"vgmstream-cli\2\vgmstream-cli.exe"
            }
# =========================
# story builder（对齐大佬结构）
# =========================
def build_story_json(story_dir: str):
    story_dir = Path(story_dir)
    text_dir = story_dir / "textassets"
    txt_files = list(text_dir.glob("*.txt"))
    if not txt_files:
        return

    txt = txt_files[0]

    story_id = story_dir.name
    script_id = txt.stem

    parsed = parse_script(txt)
    moc_dir = story_dir / "moc"
    
    # ─── 仅对数据检索进行改正 ───
    moc_file = next(moc_dir.glob("*.moc3"), None) if moc_dir.exists() else None
    # ───────────────────────────
    
    live2d = {
        "moc": {
            "name": f"l2d_{story_id}",
            "path": f"moc/l2d_{story_id}.moc3",
            "bytes": moc_file.stat().st_size if moc_file else 0,
            "bundle":""
        },
        "textures": build_textures(story_dir),
        "animations": build_animations_from_folder(story_dir, story_id),
        "motions": build_motions(story_dir),
        "fadeMotions": build_fade_motions(story_dir, story_id),
        "monobehaviour":build_monobehaviours(story_dir,story_id),
        "model3":f"{story_id}.model3.json"

    }
    suffix = str(story_id)[-1]
    if suffix != "2":
        live2d = {
            "moc": None,
            "textures": build_textures(story_dir),
            "animations": build_animations_from_folder(story_dir, story_id),
            "motions": build_motions(story_dir),
            "fadeMotions": [],
            "monobehaviour": build_monobehaviours(story_dir, story_id),
            "model3": None
        }

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

    audio = build_audio(story_dir)
    story = {
        "id": story_id,
        "sourceId": story_id,
        "root": str(story_dir),
        "scripts": [script],
        "primaryScript": script_id,
        "live2d": live2d,
        "audio": audio
    }
    out_path = story_dir / "story.json"
    out_path.write_text(
        json.dumps(story, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print("[OK]", story_id)


# =========================
# batch
# =========================
def build_all():
    root = Path(__file__).parent / "data_r18_all" / "stories"

    if not root.exists():
        print(f"[FAIL] 找不到指定的剧情根目录: {root.resolve()}")
        return

    for d in root.iterdir():
        if d.is_dir():
            try:
                build_story_json(d)
            except Exception as e:
                print("[FAIL]", d.name, e)


# ─── 改正：将执行守护块移至文件最底部 ───
if __name__ == "__main__":
    build_all()