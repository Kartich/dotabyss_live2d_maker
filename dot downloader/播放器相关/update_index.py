# -*- coding: utf-8 -*-

import json
import re
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent

# ============================================================
# R18 story / thumb
# ============================================================

DATA_R18 = ROOT / "data_r18_all"
STORIES = DATA_R18 / "stories"
THUMB_DIR = DATA_R18 / "thumb"

INDEX = DATA_R18 / "index.json"
THUMB_INDEX = DATA_R18 / "thumb.json"


# ============================================================
# SE
# ============================================================

DATA = ROOT / "data"

SE_DIR = DATA / "audio" / "se" / "decoded" / "misc"
SE_INDEX = DATA / "audio" / "se" / "index.json"


IMAGE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}

AUDIO_EXTS = {
    ".ogg",
    ".wav",
    ".mp3",
}


# ============================================================
# JSON
# ============================================================

def load_json(path: Path, default=None):
    try:
        return json.loads(
            path.read_text(
                encoding="utf-8-sig"
            )
        )
    except Exception:
        return default


def atomic_write_json(path: Path, obj):
    path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    tmp = path.with_suffix(
        path.suffix + ".tmp"
    )

    tmp.write_text(
        json.dumps(
            obj,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    tmp.replace(path)


def relative_web_path(path: Path):
    return (
        path.resolve()
        .relative_to(ROOT.resolve())
        .as_posix()
    )


# ============================================================
# Story statistics
# ============================================================

def count_story_files(folder: Path):
    stats = {
        "scriptCount": 0,
        "messageCount": 0,
        "textureCount": 0,
        "animationCount": 0,
        "motionCount": 0,
        "audioCueCount": 0,
        "bundleErrorCount": 0,
    }

    if not folder.exists():
        return stats

    story_json = folder / "story.json"

    if story_json.is_file():
        stats["scriptCount"] = 1

        story = load_json(
            story_json,
            {}
        )

        if isinstance(story, dict):
            scripts = story.get("scripts", [])

            if isinstance(scripts, list):
                for script in scripts:
                    if isinstance(script, dict):
                        messages = script.get(
                            "messages",
                            []
                        )
                        if isinstance(messages, list):
                            stats["messageCount"] += len(messages)

    for p in folder.rglob("*"):
        if not p.is_file():
            continue

        name = p.name.lower()
        ext = p.suffix.lower()

        if name == "story.json":
            continue

        if name.endswith(".motion3.json"):
            stats["motionCount"] += 1

        elif ext == ".anim":
            stats["animationCount"] += 1

        elif ext in IMAGE_EXTS:
            stats["textureCount"] += 1

        elif ext in AUDIO_EXTS:
            stats["audioCueCount"] += 1

        elif (
            "error" in name
            or "failed" in name
            or "failure" in name
            or name in {"error.log", "errors.log"}
        ):
            stats["bundleErrorCount"] += 1

    return stats
 # ============================================================
# Thumb
# ============================================================

def story_id_to_thumb_id(story_id: str):
    sid = str(story_id)

    if re.fullmatch(
        r"\d{11}",
        sid
    ):
        return sid[:-1]

    return None


def find_story_thumb(story_id: str):
    if not THUMB_DIR.exists():
        return None

    wanted = story_id_to_thumb_id(
        story_id
    )

    candidates = []

    for p in THUMB_DIR.iterdir():
        if (
            not p.is_file()
            or p.suffix.lower() not in IMAGE_EXTS
        ):
            continue

        m = re.search(
            r"hmr_(\d+)",
            p.stem,
            re.IGNORECASE
        )

        if not m:
            continue

        number = m.group(1)

        if wanted and number == wanted:
            candidates.append(p)

        elif number == str(story_id):
            candidates.append(p)

    if candidates:
        return sorted(
            candidates,
            key=lambda p: p.name.lower()
        )[0]

    old = THUMB_DIR / f"thumb_{story_id}.png"

    if old.is_file():
        return old

    return None


def update_thumb_index(story_ids):
    result = {}

    for sid in sorted(
        set(str(x) for x in story_ids)
    ):
        if not re.fullmatch(
            r"\d{11}",
            sid
        ):
            continue

        thumb = find_story_thumb(
            sid
        )

        if thumb is None:
            continue

        # 必须保持播放器当前实际使用的格式：
        # data_r18_all/thumb/xxx.png
        result[sid] = relative_web_path(
            thumb
        )

    # 只删除错误的旧索引位置
    for legacy in (
        THUMB_DIR / "index.json",
        THUMB_DIR / "thumb.json",
    ):
        if legacy.is_file():
            try:
                legacy.unlink()
            except OSError:
                pass

    atomic_write_json(
        THUMB_INDEX,
        result
    )

    return len(result)


# ============================================================
# Story index
# ============================================================

def get_story_title(story, fallback):
    if not isinstance(
        story,
        dict
    ):
        return fallback

    return (
        story.get("title")
        or story.get("name")
        or story.get("sourceId")
        or fallback
    )


def update_story_index():
    existing = load_json(
        INDEX,
        {}
    )

    if not isinstance(
        existing,
        dict
    ):
        existing = {}

    existing_entries = existing.get(
        "stories",
        []
    )

    if not isinstance(
        existing_entries,
        list
    ):
        existing_entries = []

    # 关键：保留原 index 中所有字段
    existing_by_id = {}

    for item in existing_entries:
        if not isinstance(item, dict):
            continue

        sid = str(
            item.get("id", "")
        )

        if sid:
            existing_by_id[sid] = dict(item)

    stories = []

    if STORIES.exists():
        story_dirs = sorted(
            (
                p for p in STORIES.iterdir()
                if p.is_dir() and p.name.isdigit()
            ),
            key=lambda p: p.name
        )

        for story_dir in story_dirs:
            sid = story_dir.name
            story_json = story_dir / "story.json"

            if not story_json.is_file():
                continue

            story = load_json(
                story_json,
                {}
            )

            if not isinstance(
                story,
                dict
            ):
                story = {}

            item = existing_by_id.get(
                sid,
                {}
            )

            # 只更新确认属于 updater 的字段
            item["id"] = sid

            item["title"] = get_story_title(
                story,
                item.get(
                    "title",
                    sid
                )
            )

            item["path"] = (
                f"stories/{sid}/story.json"
            )

            # 对已有条目，保留原作者 root。
            # 新条目才使用当前真实绝对路径，
            # 与原作者生成的 index 格式一致。
            if not item.get("root"):
                item["root"] = str(
                    story_dir.resolve()
                )

            item["stats"] = count_story_files(
                story_dir
            )

            stories.append(item)

    existing["stories"] = stories

    # 保留原有 generatedBy 等顶层字段
    existing["updatedTime"] = (
        datetime.now()
        .astimezone()
        .isoformat(
            timespec="seconds"
        )
    )

    atomic_write_json(
        INDEX,
        existing
    )

    return stories


# ============================================================
# SE index
# ============================================================

def update_se_index():
    cues = {}

    if not SE_DIR.exists():
        atomic_write_json(
            SE_INDEX,
            {"cues": {}}
        )
        return 0

    for p in sorted(
        SE_DIR.iterdir(),
        key=lambda x: x.name.lower()
    ):
        if not p.is_file():
            continue

        if p.suffix.lower() not in AUDIO_EXTS:
            continue

        cue_id = p.stem

        cues[cue_id] = {
            "name": cue_id,
            "category": "se",
            "path": (
                "audio/se/decoded/misc/"
                + p.name
            ),
            "source": "",
            "subsong": 1,
            "duration": 0.0,
            "sampleRate": 48000,
            "channels": 1,
            "encoding": "decoded",
            "bytes": p.stat().st_size,
        }

    atomic_write_json(
        SE_INDEX,
        {
            "cues": cues
        }
    )

    return len(cues)
    # ============================================================
# Main
# ============================================================

def main():
    stories = update_story_index()

    story_ids = [
        item["id"]
        for item in stories
        if isinstance(item, dict)
        and item.get("id")
    ]

    thumb_count = update_thumb_index(
        story_ids
    )

    se_count = update_se_index()

    print("=" * 40)
    print("Index update complete")
    print("=" * 40)
    print("STORY:", len(stories))
    print("THUMB:", thumb_count)
    print("SE:", se_count)


if __name__ == "__main__":
    main()