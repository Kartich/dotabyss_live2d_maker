# -*- coding: utf-8 -*-
"""Thin Story Bundle -> story.json command parser.

The Story TextAsset is authoritative. This module only parses the command text
and extracts fields that are explicitly encoded in the command itself.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

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
    line = line.rstrip("\r\n").lstrip("\ufeff")
    if not line.strip():
        return None

    stripped = line.lstrip()
    if stripped.startswith(":"):
        return {
            "command": ":label",
            "rawCommand": ":label",
            "args": [],
            "label": stripped[1:]
        }

    parts = line.split(",")

    return {
        "command": parts[0].strip(),
        "rawCommand": parts[0].strip(),
        "args": parts[1:]
    }


def enrich(cmd_obj):
    cmd = cmd_obj["command"]
    args = list(cmd_obj.get("args", []))
    info = COMMAND_INFO.get(cmd)

    entry = {
        "command": cmd,
        "rawCommand": cmd_obj.get("rawCommand", cmd),
        "args": args,
        "category": info["category"] if info else "unknown",
        "title": info["title"] if info else cmd,
    }

    if cmd == ":label":
        entry["label"] = cmd_obj.get("label", "")

    elif cmd == "fade" and len(args) >= 3:
        entry["direction"] = args[0]
        entry["color"] = args[1]
        try:
            entry["seconds"] = float(args[2])
        except:
            pass

    elif cmd in ("l2dmotion", "asyncl2dmotion") and args:
        entry["motion"] = args[0]

    elif cmd in ("bgmplay", "bgvplay") and len(args) >= 2:
        entry["cue"] = args[1]

    elif cmd in ("message", "l2dmessage"):
        entry["speaker"] = args[0] if args else ""
        entry["message"] = args[1] if len(args) > 1 else ""
        entry["voice"] = next(
            (x for x in args if isinstance(x, str) and x.startswith("vc_")),
            ""
        )

    return entry
    def parse_script(txt_path: Path):
    with open(txt_path, "r", encoding="utf-8-sig", newline="") as f:
        lines = f.readlines()

    commands = []
    labels = set()
    audio_ids = set()
    motions = set()
    counter = Counter()

    for line_no, line in enumerate(lines, start=1):

        obj = parse_line(line)

        if obj is None:
            continue

        cmd = enrich(obj)

        cmd["index"] = len(commands)
        cmd["line"] = line_no

        cmd = {
            "index": cmd["index"],
            "line": cmd["line"],
            **{
                k: v
                for k, v in cmd.items()
                if k not in ("index", "line")
            },
        }

        commands.append(cmd)

        name = cmd["command"]
        counter[name] += 1

        if name == ":label":
            labels.add(cmd.get("label", ""))

        if cmd.get("motion"):
            motions.add(cmd["motion"])

        if cmd.get("cue"):
            audio_ids.add(cmd["cue"])

        if cmd.get("voice"):
            audio_ids.add(cmd["voice"])

        if name == "wait" and cmd.get("args"):

            try:
                cmd["seconds"] = float(cmd["args"][0])

            except:
                pass

        if name in ("bgvstop", "bgmstop") and cmd.get("args"):

            cmd["tag"] = cmd["args"][0]


    messages = []

    for cmd in commands:

        if cmd["command"] not in ("message", "l2dmessage"):
            continue

        messages.append({
            "index": cmd["index"],
            "line": cmd["line"],
            "command": cmd["command"],
            "rawCommand": cmd["rawCommand"],
            "args": cmd.get("args", []),
            "category": cmd.get("category", ""),
            "title": cmd.get("title", ""),
            "speaker": cmd.get("speaker", ""),
            "message": cmd.get("message", ""),
            "voice": cmd.get("voice", ""),
        })


    # 新增：生成 story.json 所需 audio.cues 基础结构
    # 具体 duration/source 等信息由 bundle 导出阶段补充
    audio_cues = {}

    for cue in sorted(audio_ids):

        category = "voice"

        if cue.startswith("bgm"):
            category = "bgm"

        elif cue.startswith("se"):
            category = "se"

        elif cue.startswith("vc_"):
            category = "voice"


        audio_cues[cue] = {
            "name": cue,
            "category": category,
            "path": "",
            "source": "",
            "subsong": 1,
            "duration": 0.0,
            "sampleRate": 48000,
            "channels": 2 if category == "bgm" else 1,
            "encoding": "decoded",
            "bytes": 0
        }


    return {
        "commands": commands,
        "messages": messages,
        "labels": sorted(labels),
        "audioIds": sorted(audio_ids),

        "audio": {
            "cues": audio_cues
        },

        "motions": sorted(
            motions,
            key=str.casefold
        ),

        "commandCounts": dict(counter),
    }