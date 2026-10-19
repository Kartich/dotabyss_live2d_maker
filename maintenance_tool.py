# -*- coding: utf-8 -*-
"""Dot 离线资源维护器 1.0 - 索引与公共资源整理核心。

Responsibilities:
- detect the player project root even when this tool is stored in a subfolder;
- rebuild story/thumb/audio indexes from what story.json actually references;
- repair BGM/SE cue entries and multi-path candidates in each story.json;
- consolidate shared BGM/SE into a canonical public pool;
- optionally keep only the public pool and remove verified duplicate legacy files + empty folders.

This tool is intentionally independent from the downloader.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import queue
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from tool_paths import find_project_root as _shared_find_project_root
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
AUDIO_EXTS = {".ogg", ".wav", ".mp3", ".m4a", ".aac"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
BGM_RE = re.compile(r"\bbgm_?(\d+)\b", re.I)
SE_RE = re.compile(r"\bse(\d+)\b", re.I)
BATCH_WORKERS = 30


def find_project_root(start: Path | str) -> Path:
    return _shared_find_project_root(start)


def validate_root(root: Path) -> tuple[bool, str]:
    root = Path(root)
    clues = []
    for name in ("index.html", "data_r18_all", "data", "src"):
        if (root / name).exists():
            clues.append(name)
    ok = (root / "data_r18_all" / "stories").exists() or (
        (root / "index.html").exists() and (root / "data_r18_all").exists()
    )
    return ok, ", ".join(clues) if clues else "未发现播放器特征目录"


def canonical_cue_id(value: str, kind: str | None = None) -> str:
    text = str(value or "").strip().lower()
    if kind in (None, "bgm"):
        m = BGM_RE.search(text)
        if m:
            return "bgm" + m.group(1)
    if kind in (None, "se"):
        m = SE_RE.search(text)
        if m:
            return "se" + m.group(1)
    return Path(text).stem


def is_audio(path: Path) -> bool:
    try:
        return path.is_file() and path.suffix.lower() in AUDIO_EXTS and path.stat().st_size > 0
    except OSError:
        return False


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def web_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def audio_paths(root: Path) -> dict[str, Path]:
    data = root / "data"
    data_r18 = root / "data_r18_all"
    return {
        "data": data,
        "data_r18": data_r18,
        "bgm_root": data / "audio" / "bgm" / "decoded",
        "bgm_index": data / "audio" / "bgm" / "index.json",
        "se_root": data / "audio" / "se" / "decoded" / "misc",
        "se_index": data / "audio" / "se" / "index.json",
        "combined_index": data / "audio" / "index.json",
    }


def story_dirs(root: Path) -> list[Path]:
    base = root / "data_r18_all" / "stories"
    if not base.exists():
        return []
    return sorted((p for p in base.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: p.name)


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_strings(child)


def story_resource_requirements(story: dict) -> dict[str, set[str]]:
    """Read BGM/SE requirements from story content, not from the filesystem."""
    out = {"bgm": set(), "se": set()}
    if not isinstance(story, dict):
        return out

    cues = ((story.get("audio") or {}).get("cues") or {})
    if isinstance(cues, dict):
        for key, entry in cues.items():
            category = str((entry or {}).get("category", "")).lower() if isinstance(entry, dict) else ""
            text = f"{key} {(entry or {}).get('name', '') if isinstance(entry, dict) else ''}"
            if category == "bgm" or BGM_RE.search(text):
                m = BGM_RE.search(text)
                if m:
                    out["bgm"].add("bgm" + m.group(1))
            if category == "se" or SE_RE.search(text):
                m = SE_RE.search(text)
                if m:
                    out["se"].add("se" + m.group(1))

    scripts = story.get("scripts", []) or []
    for text in _walk_strings(scripts):
        for m in BGM_RE.finditer(text):
            out["bgm"].add("bgm" + m.group(1))
        for m in SE_RE.finditer(text):
            out["se"].add("se" + m.group(1))
    return out


def collect_story_requirements(root: Path):
    """Read story requirements concurrently; aggregation stays on the caller thread."""
    per_story: dict[str, dict[str, set[str]]] = {}
    union = {"bgm": set(), "se": set()}
    invalid = 0
    folders = story_dirs(root)

    def inspect(folder: Path):
        story = load_json(folder / "story.json", None)
        if not isinstance(story, dict):
            return folder.name, None
        return folder.name, story_resource_requirements(story)

    with ThreadPoolExecutor(max_workers=BATCH_WORKERS, thread_name_prefix="story-scan") as ex:
        for sid, req in ex.map(inspect, folders):
            if req is None:
                invalid += 1
                continue
            per_story[sid] = req
            union["bgm"].update(req["bgm"])
            union["se"].update(req["se"])
    return per_story, union, invalid


def iter_bgm_files(root: Path) -> Iterable[Path]:
    p = audio_paths(root)
    bases = [
        p["bgm_root"],
        p["data_r18"] / "audio" / "BGM",
        p["data_r18"] / "audio" / "bgm",
        root / "audio" / "bgm",
    ]
    base_story = p["data_r18"] / "stories"
    if base_story.exists():
        bases.extend(base_story.glob("*/audio/decoded/bgm"))
    yield from _iter_audio_bases(bases, "bgm")


def iter_se_files(root: Path) -> Iterable[Path]:
    p = audio_paths(root)
    bases = [
        p["se_root"],
        p["data_r18"] / "audio" / "SE",
        p["data_r18"] / "audio" / "se",
        root / "audio" / "se",
    ]
    base_story = p["data_r18"] / "stories"
    if base_story.exists():
        bases.extend(base_story.glob("*/audio/decoded/se"))
        bases.extend(base_story.glob("*/audio/decoded/misc"))
    yield from _iter_audio_bases(bases, "se")


def _iter_audio_bases(bases: Iterable[Path], kind: str) -> Iterable[Path]:
    seen = set()
    pattern = BGM_RE if kind == "bgm" else SE_RE
    for base in bases:
        base = Path(base)
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if not is_audio(f) or not pattern.search(f.stem):
                continue
            try:
                key = f.resolve()
            except Exception:
                key = f
            if key in seen:
                continue
            seen.add(key)
            yield f


def all_kind_files(root: Path, kind: str) -> list[Path]:
    return list(iter_bgm_files(root) if kind == "bgm" else iter_se_files(root))


def canonical_dir(root: Path, kind: str) -> Path:
    p = audio_paths(root)
    return p["bgm_root"] if kind == "bgm" else p["se_root"]


def canonical_expected_path(root: Path, kind: str, cue: str, suffix: str = ".ogg") -> Path:
    return canonical_dir(root, kind) / f"{cue}{suffix}"


def group_files_by_cue(root: Path, kind: str) -> dict[str, list[Path]]:
    grouped: dict[str, list[Path]] = defaultdict(list)
    for f in all_kind_files(root, kind):
        cue = canonical_cue_id(f.stem, kind)
        grouped[cue].append(f)
    return grouped


def _file_rank(root: Path, kind: str, path: Path):
    canon = canonical_dir(root, kind)
    try:
        in_canonical = 0 if path.resolve().is_relative_to(canon.resolve()) else 1
    except Exception:
        try:
            in_canonical = 0 if str(path.resolve()).lower().startswith(str(canon.resolve()).lower()) else 1
        except Exception:
            in_canonical = 1
    ext_rank = {".ogg": 0, ".m4a": 1, ".wav": 2, ".mp3": 3, ".aac": 4}.get(path.suffix.lower(), 9)
    return (in_canonical, ext_rank, -path.stat().st_size, path.as_posix().lower())


def cue_files(root: Path, kind: str, cue: str) -> list[Path]:
    cue = canonical_cue_id(cue, kind)
    return sorted(
        [f for f in all_kind_files(root, kind) if canonical_cue_id(f.stem, kind) == cue],
        key=lambda f: _file_rank(root, kind, f),
    )


def cue_index_entry(root: Path, kind: str, cue: str, files: Iterable[Path]) -> dict:
    cue = canonical_cue_id(cue, kind)
    files = sorted([Path(f) for f in files if is_audio(Path(f))], key=lambda f: _file_rank(root, kind, f))
    paths = []
    for f in files:
        wp = web_path(root, f)
        if wp not in paths:
            paths.append(wp)
    expected = web_path(root, canonical_expected_path(root, kind, cue))
    primary_web = paths[0] if paths else expected
    # Global audio indexes are located under data/, so keep their primary path data-relative.
    primary_index_path = primary_web.removeprefix("data/")
    return {
        "name": cue,
        "category": kind,
        "path": primary_index_path,
        "paths": paths or [expected],
        "source": "",
        "subsong": 1,
        "duration": 0.0,
        "sampleRate": 48000,
        "channels": 2 if kind == "bgm" else 1,
        "encoding": "decoded",
        "bytes": files[0].stat().st_size if files else 0,
    }


def build_common_index(root: Path, kind: str, required_cues: set[str]) -> dict:
    grouped = group_files_by_cue(root, kind)
    cues = {}
    for cue in sorted(required_cues):
        cues[cue] = cue_index_entry(root, kind, cue, grouped.get(cue, []))
    return {"cues": cues, "source": "story.json", "updatedTime": datetime.now().astimezone().isoformat(timespec="seconds")}


def write_common_indexes(root: Path, requirements: dict[str, set[str]]):
    p = audio_paths(root)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="common-index") as ex:
        bgm_future = ex.submit(build_common_index, root, "bgm", requirements["bgm"])
        se_future = ex.submit(build_common_index, root, "se", requirements["se"])
        bgm = bgm_future.result()
        se = se_future.result()
    atomic_write_json(p["bgm_index"], bgm)
    atomic_write_json(p["se_index"], se)
    combined = {"cues": {}, "sources": ["audio/se/index.json", "audio/bgm/index.json"], "source": "story.json"}
    combined["cues"].update(se["cues"])
    combined["cues"].update(bgm["cues"])
    atomic_write_json(p["combined_index"], combined)
    return bgm, se, combined


def _story_local_audio_dirs(story_dir: Path, kind: str) -> list[Path]:
    if kind == "bgm":
        return [story_dir / "audio" / "decoded" / "bgm"]
    return [story_dir / "audio" / "decoded" / "se", story_dir / "audio" / "decoded" / "misc"]


def _story_candidate_files(story_dir: Path, kind: str, cue: str) -> list[Path]:
    out = []
    pattern = BGM_RE if kind == "bgm" else SE_RE
    for base in _story_local_audio_dirs(story_dir, kind):
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if is_audio(f) and pattern.search(f.stem) and canonical_cue_id(f.stem, kind) == cue:
                out.append(f)
    return out


def _story_local_catalog(story_dir: Path) -> dict[str, dict[str, list[Path]]]:
    catalog: dict[str, dict[str, list[Path]]] = {"bgm": defaultdict(list), "se": defaultdict(list)}
    for kind in ("bgm", "se"):
        pattern = BGM_RE if kind == "bgm" else SE_RE
        for base in _story_local_audio_dirs(story_dir, kind):
            if not base.exists():
                continue
            for f in base.rglob("*"):
                if is_audio(f) and pattern.search(f.stem):
                    catalog[kind][canonical_cue_id(f.stem, kind)].append(f)
    return catalog


def _candidate_paths_for_story(
    root: Path, story_dir: Path, kind: str, cue: str, prefer_pool: bool,
    file_catalog: dict[str, dict[str, list[Path]]] | None = None,
    local_catalog: dict[str, dict[str, list[Path]]] | None = None,
) -> tuple[list[str], list[Path]]:
    cue = canonical_cue_id(cue, kind)
    if file_catalog is not None:
        all_files = sorted(file_catalog.get(kind, {}).get(cue, []), key=lambda f: _file_rank(root, kind, f))
    else:
        all_files = cue_files(root, kind, cue)
    local = list(local_catalog.get(kind, {}).get(cue, [])) if local_catalog is not None else _story_candidate_files(story_dir, kind, cue)
    local_resolved = set()
    for f in local:
        try: local_resolved.add(f.resolve())
        except Exception: local_resolved.add(f)
    pool = []
    others = []
    cdir = canonical_dir(root, kind)
    for f in all_files:
        try:
            is_pool = f.resolve().is_relative_to(cdir.resolve())
        except Exception:
            is_pool = str(f.resolve()).lower().startswith(str(cdir.resolve()).lower())
        if is_pool:
            pool.append(f)
        elif f.resolve() not in local_resolved:
            others.append(f)
    ordered = (pool + local + others) if prefer_pool else (local + pool + others)
    paths = []
    for f in ordered:
        wp = web_path(root, f)
        if wp not in paths:
            paths.append(wp)
    expected = web_path(root, canonical_expected_path(root, kind, cue))
    if not paths:
        paths.append(expected)
    return paths, ordered


def repair_story_audio(
    root: Path, story_dir: Path, story: dict, requirements: dict[str, set[str]],
    prefer_pool: bool = False, file_catalog: dict[str, dict[str, list[Path]]] | None = None,
    local_catalog: dict[str, dict[str, list[Path]]] | None = None,
):
    if not isinstance(story, dict):
        return 0, False, []
    audio = story.setdefault("audio", {})
    cues = audio.setdefault("cues", {})
    if not isinstance(cues, dict):
        cues = {}
        audio["cues"] = cues
    added = 0
    changed = False
    missing = []

    for kind in ("bgm", "se"):
        for cue in sorted(requirements[kind]):
            existing = cues.get(cue)
            if not isinstance(existing, dict):
                existing = {}
                added += 1
            paths, files = _candidate_paths_for_story(
                root, story_dir, kind, cue, prefer_pool, file_catalog=file_catalog, local_catalog=local_catalog
            )
            if not files:
                missing.append(f"{story_dir.name}:{cue}")
            primary = paths[0]
            new_entry = {
                **existing,
                "name": cue,
                "category": kind,
                "path": primary,
                "paths": paths,
                "source": existing.get("source", ""),
                "subsong": int(existing.get("subsong") or 1),
                "duration": float(existing.get("duration") or 0.0),
                "sampleRate": int(existing.get("sampleRate") or 48000),
                "channels": int(existing.get("channels") or (2 if kind == "bgm" else 1)),
                "encoding": existing.get("encoding") or "decoded",
                "bytes": int(existing.get("bytes") or (files[0].stat().st_size if files else 0)),
            }
            if cues.get(cue) != new_entry:
                cues[cue] = new_entry
                changed = True
    return added, changed, missing


def story_id_to_thumb_id(story_id: str):
    sid = str(story_id)
    return sid[:-1] if re.fullmatch(r"\d{11}", sid) else None


def find_story_thumb(root: Path, story_id: str):
    thumb_dir = root / "data_r18_all" / "thumb"
    if not thumb_dir.exists():
        return None
    wanted = story_id_to_thumb_id(story_id)
    candidates = []
    for p in thumb_dir.iterdir():
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            continue
        m = re.search(r"(?:hmr|thumb)_(\d+)", p.stem, re.I)
        if m and (m.group(1) == wanted or m.group(1) == str(story_id)):
            candidates.append(p)
    if candidates:
        return sorted(candidates, key=lambda x: x.name.lower())[0]
    fallbacks=[]
    if wanted:
        fallbacks.append(thumb_dir / f"thumb_{wanted}.png")
    fallbacks.append(thumb_dir / f"thumb_{story_id}.png")
    return next((x for x in fallbacks if x.is_file()), None)


def update_thumb_index(root: Path, story_ids: Iterable[str]) -> int:
    data_r18 = root / "data_r18_all"
    thumb_dir = data_r18 / "thumb"
    result = {}

    # v5.1 rescanned the whole thumb directory once per story. Build the lookup once.
    by_number: dict[str, list[Path]] = defaultdict(list)
    if thumb_dir.exists():
        for thumb in thumb_dir.iterdir():
            if not thumb.is_file() or thumb.suffix.lower() not in IMAGE_EXTS:
                continue
            # Pipeline-generated files are named thumb_<10-digit>.png, while
            # original extracted files may still carry hmr_<10-digit>. Support both.
            m = re.search(r"(?:hmr|thumb)_(\d+)", thumb.stem, re.I)
            if m:
                by_number[m.group(1)].append(thumb)

    for sid in sorted(set(map(str, story_ids))):
        if not re.fullmatch(r"\d{11}", sid):
            continue
        wanted = story_id_to_thumb_id(sid)
        candidates = list(by_number.get(wanted or "", [])) + list(by_number.get(sid, []))
        thumb = sorted(set(candidates), key=lambda x: x.name.lower())[0] if candidates else None
        if thumb is None:
            # Older updater checked only the 11-digit story id, but Stage 1
            # exports story icons using the 10-digit icon id.
            fallbacks=[thumb_dir/f"thumb_{wanted}.png" if wanted else None, thumb_dir/f"thumb_{sid}.png"]
            thumb=next((x for x in fallbacks if x is not None and x.is_file()), None)
        if thumb:
            result[sid] = web_path(root, thumb)

    atomic_write_json(data_r18 / "thumb.json", result)
    for legacy in (thumb_dir / "index.json", thumb_dir / "thumb.json"):
        if legacy.is_file() and legacy != data_r18 / "thumb.json":
            try:
                legacy.unlink()
            except OSError:
                pass
    return len(result)


def count_story_files(folder: Path, story: dict) -> dict:
    stats = {"scriptCount": 0, "messageCount": 0, "textureCount": 0, "animationCount": 0, "motionCount": 0, "audioCueCount": 0, "bundleErrorCount": 0}
    scripts = story.get("scripts", []) if isinstance(story, dict) else []
    stats["scriptCount"] = len(scripts)
    stats["messageCount"] = sum(len(s.get("messages", []) or []) for s in scripts if isinstance(s, dict))
    stats["audioCueCount"] = len(((story.get("audio", {}) or {}).get("cues", {}) or {})) if isinstance(story, dict) else 0
    for p in folder.rglob("*"):
        if not p.is_file() or p.name.lower() == "story.json":
            continue
        name, ext = p.name.lower(), p.suffix.lower()
        if name.endswith(".motion3.json"): stats["motionCount"] += 1
        elif ext == ".anim": stats["animationCount"] += 1
        elif ext in IMAGE_EXTS: stats["textureCount"] += 1
        elif "error" in name or "failed" in name or "failure" in name: stats["bundleErrorCount"] += 1
    return stats


def get_story_title(story: dict, fallback: str):
    return story.get("title") or story.get("name") or story.get("sourceId") or fallback


def update_story_index(root: Path, per_story_requirements: dict[str, dict[str, set[str]]], prefer_pool: bool = False):
    index_path = root / "data_r18_all" / "index.json"
    existing = load_json(index_path, {})
    if not isinstance(existing, dict):
        existing = {}
    entries = existing.get("stories", [])
    by_id = {str(x.get("id")): dict(x) for x in entries if isinstance(x, dict) and x.get("id")} if isinstance(entries, list) else {}

    # Build the audio catalog once. v5.1 repeatedly rescanned the whole tree for every cue/story.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="audio-catalog") as ex:
        fut_bgm = ex.submit(group_files_by_cue, root, "bgm")
        fut_se = ex.submit(group_files_by_cue, root, "se")
        file_catalog = {"bgm": fut_bgm.result(), "se": fut_se.result()}

    folders = story_dirs(root)

    def process_story(folder: Path):
        story_json = folder / "story.json"
        story = load_json(story_json, None)
        if not isinstance(story, dict):
            return None
        req = per_story_requirements.get(folder.name) or story_resource_requirements(story)
        local_catalog = _story_local_catalog(folder)
        repaired, changed, missing_one = repair_story_audio(
            root, folder, story, req, prefer_pool=prefer_pool,
            file_catalog=file_catalog, local_catalog=local_catalog
        )
        if changed:
            atomic_write_json(story_json, story)
        sid = folder.name
        item = by_id.get(sid, {})
        item.update({
            "id": sid,
            "title": get_story_title(story, item.get("title", sid)),
            "path": f"stories/{sid}/story.json",
            "stats": count_story_files(folder, story),
            "resources": {"bgm": sorted(req["bgm"]), "se": sorted(req["se"])},
        })
        if not item.get("root"):
            item["root"] = str(folder.resolve())
        return sid, item, repaired, missing_one

    results = []
    with ThreadPoolExecutor(max_workers=BATCH_WORKERS, thread_name_prefix="story-update") as ex:
        futures = [ex.submit(process_story, folder) for folder in folders]
        for fut in as_completed(futures):
            result = fut.result()
            if result is not None:
                results.append(result)

    results.sort(key=lambda row: row[0])
    stories = [row[1] for row in results]
    repaired_total = sum(row[2] for row in results)
    missing = [item for row in results for item in row[3]]

    existing["stories"] = stories
    existing["updatedTime"] = datetime.now().astimezone().isoformat(timespec="seconds")
    existing["maintenanceWorkers"] = BATCH_WORKERS
    atomic_write_json(index_path, existing)
    return stories, repaired_total, missing


def run_index_update(root: Path, prefer_pool: bool = False) -> dict:
    root = Path(root).resolve()
    per_story, requirements, invalid = collect_story_requirements(root)
    bgm_index, se_index, combined = write_common_indexes(root, requirements)
    stories, repaired, missing = update_story_index(root, per_story, prefer_pool=prefer_pool)
    thumbs = update_thumb_index(root, [s.get("id") for s in stories])
    result = {
        "stories": len(stories), "invalid": invalid, "repaired": repaired, "missing": missing,
        "thumbs": thumbs, "bgm": len(bgm_index["cues"]), "se": len(se_index["cues"]),
        "combined": len(combined["cues"]),
    }
    print("\n[索引更新完成]")
    print("项目根目录:", root)
    print(f"剧情 {result['stories']} | 无效 story {invalid} | 新补 cue {repaired} | 缩略图 {thumbs}")
    print(f"按 story 实际需求建立索引: BGM {result['bgm']} | SE {result['se']} | 合计 {result['combined']}")
    if missing:
        print(f"[WARN] {len(missing)} 个剧情 cue 暂时找不到实际音频文件。前 20 个:")
        for item in missing[:20]:
            print("  -", item)
    return result


def _format_bytes(n: int) -> str:
    value = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024


def analyze_kind(root: Path, kind: str):
    grouped = group_files_by_cue(root, kind)
    all_files = [f for files in grouped.values() for f in files]
    hash_map: dict[Path, str | None] = {}

    def hash_one(f: Path):
        try:
            return f, sha256_file(f)
        except Exception:
            return f, None

    # Hashing is the slowest step on large libraries. Run up to 30 independent files in parallel.
    with ThreadPoolExecutor(max_workers=BATCH_WORKERS, thread_name_prefix=f"{kind}-hash") as ex:
        for f, digest in ex.map(hash_one, all_files):
            hash_map[f] = digest

    rows = []
    duplicate_bytes = 0
    conflicts = 0
    for cue, files in sorted(grouped.items()):
        hashes = defaultdict(list)
        for f in files:
            digest = hash_map.get(f)
            if digest:
                hashes[digest].append(f)
        if len(hashes) > 1:
            conflicts += 1
        dup = sum(max(0, len(v) - 1) * v[0].stat().st_size for v in hashes.values() if v)
        duplicate_bytes += dup
        rows.append((cue, files, hashes, dup))
    return rows, duplicate_bytes, conflicts


def _ensure_canonical(root: Path, kind: str, cue: str, files: list[Path], dry_run: bool):
    cdir = canonical_dir(root, kind)
    cdir.mkdir(parents=True, exist_ok=True)
    existing = sorted([p for p in cdir.glob(f"{cue}.*") if is_audio(p)], key=lambda p: _file_rank(root, kind, p))
    if existing:
        return existing[0], False
    ordered = sorted(files, key=lambda p: _file_rank(root, kind, p))
    if not ordered:
        return None, False
    source = ordered[0]
    suffix = source.suffix.lower() if source.suffix.lower() in AUDIO_EXTS else ".ogg"
    target = cdir / f"{cue}{suffix}"
    if dry_run:
        print(f"[DRY] COPY {source} -> {target}")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return target, True


def consolidate_kind(root: Path, kind: str, mode: str, dry_run: bool = False) -> dict:
    rows, duplicate_bytes, conflicts = analyze_kind(root, kind)

    def process_row(row):
        cue, files, hash_groups, _dup = row
        copied = linked = removed = kept_conflict = 0
        reclaimed = added = 0
        if mode == "report":
            return copied, linked, removed, kept_conflict, reclaimed, added
        canonical, was_copied = _ensure_canonical(root, kind, cue, files, dry_run)
        if canonical is None:
            return copied, linked, removed, kept_conflict, reclaimed, added
        if was_copied:
            copied += 1
            if not dry_run and canonical.exists():
                added += canonical.stat().st_size

        # Reuse hashes calculated by analyze_kind. If the canonical was just copied,
        # its source hash is the hash of the first ranked source.
        canonical_hash = None
        if canonical.exists() and not was_copied:
            for digest, members in hash_groups.items():
                for member in members:
                    try:
                        if member.resolve() == canonical.resolve():
                            canonical_hash = digest
                            break
                    except Exception:
                        pass
                if canonical_hash:
                    break
        if canonical_hash is None:
            ranked = sorted(files, key=lambda p: _file_rank(root, kind, p))
            source = ranked[0] if ranked else None
            if source is not None:
                for digest, members in hash_groups.items():
                    if source in members:
                        canonical_hash = digest
                        break
        if canonical_hash is None and canonical.exists():
            try:
                canonical_hash = sha256_file(canonical)
            except Exception:
                return copied, linked, removed, kept_conflict, reclaimed, added

        digest_by_file = {member: digest for digest, members in hash_groups.items() for member in members}
        for f in files:
            try:
                if canonical.exists() and f.resolve() == canonical.resolve():
                    continue
            except Exception:
                pass
            digest = digest_by_file.get(f)
            if digest is None:
                try:
                    digest = sha256_file(f)
                except Exception:
                    digest = None
            if digest != canonical_hash:
                kept_conflict += 1
                continue
            if mode == "pool":
                continue
            if mode == "link":
                try:
                    if canonical.exists() and os.path.samefile(f, canonical):
                        continue
                except Exception:
                    pass
                size = f.stat().st_size
                if dry_run:
                    print(f"[DRY] LINK {f} => {canonical}")
                    linked += 1; reclaimed += size
                    continue
                tmp = f.with_suffix(f.suffix + ".dedupe_tmp")
                try:
                    if tmp.exists(): tmp.unlink()
                    f.replace(tmp)
                    os.link(canonical, f)
                    tmp.unlink()
                    linked += 1; reclaimed += size
                except Exception as exc:
                    try:
                        if not f.exists() and tmp.exists(): tmp.replace(f)
                    except Exception:
                        pass
                    print(f"[WARN] 硬链接失败，保留原文件: {f} ({exc})")
            elif mode in ("prune", "pool_only"):
                size = f.stat().st_size
                if dry_run:
                    print(f"[DRY] DELETE verified duplicate {f}")
                else:
                    try:
                        f.unlink()
                    except Exception as exc:
                        print(f"[WARN] 删除失败: {f} ({exc})")
                        continue
                removed += 1; reclaimed += size
        return copied, linked, removed, kept_conflict, reclaimed, added

    totals = [0, 0, 0, 0, 0, 0]
    if mode == "report":
        results = [process_row(row) for row in rows]
    else:
        with ThreadPoolExecutor(max_workers=BATCH_WORKERS, thread_name_prefix=f"{kind}-{mode}") as ex:
            results = list(ex.map(process_row, rows))
    for result in results:
        for i, value in enumerate(result):
            totals[i] += value
    copied, linked, removed, kept_conflict, reclaimed, added = totals
    return {
        "kind": kind, "files": sum(len(files) for _cue, files, _hash, _dup in rows),
        "cues": len(rows), "duplicate_bytes": duplicate_bytes, "conflicts": conflicts,
        "copied": copied, "linked": linked, "removed": removed,
        "kept_conflict": kept_conflict, "reclaimed": max(0, reclaimed - added),
        "workers": BATCH_WORKERS,
    }


def _dir_has_audio_files(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    try:
        return any(is_audio(p) for p in path.rglob("*"))
    except OSError:
        return True


def cleanup_empty_audio_tree(root: Path, dry_run: bool = False) -> dict:
    """Remove now-empty legacy BGM/SE folders, never voice/L2D/texture folders."""
    candidates: set[Path] = set()
    data_r18 = root / "data_r18_all"
    # Per-story shared-audio branches only.
    for story in story_dirs(root):
        for leaf in (
            story / "audio" / "decoded" / "bgm",
            story / "audio" / "decoded" / "se",
            story / "audio" / "decoded" / "misc",
        ):
            candidates.add(leaf)
            candidates.add(leaf.parent)      # decoded
            candidates.add(leaf.parent.parent)  # audio
    # Old global branches.
    for leaf in (
        data_r18 / "audio" / "BGM", data_r18 / "audio" / "bgm",
        data_r18 / "audio" / "SE", data_r18 / "audio" / "se",
        root / "audio" / "bgm", root / "audio" / "se",
    ):
        candidates.add(leaf)
        candidates.add(leaf.parent)

    removed_dirs = 0
    removed_legacy_indexes = 0
    # Remove redundant index files only inside legacy audio branches that contain no audio files.
    for base in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        if not base.exists() or not base.is_dir():
            continue
        if _dir_has_audio_files(base):
            continue
        for name in ("index.json", "thumb.json"):
            f = base / name
            if f.is_file():
                if dry_run: print(f"[DRY] DELETE legacy index {f}")
                else:
                    try: f.unlink()
                    except OSError: continue
                removed_legacy_indexes += 1
        try:
            # Only remove truly empty directory; unknown files keep the directory alive.
            if any(base.iterdir()):
                continue
        except OSError:
            continue
        if dry_run:
            print(f"[DRY] RMDIR {base}")
            removed_dirs += 1
        else:
            try:
                base.rmdir(); removed_dirs += 1
            except OSError:
                pass
    return {"dirs": removed_dirs, "legacy_indexes": removed_legacy_indexes}


def run_organize(root: Path, mode: str, kind: str = "all", dry_run: bool = False, rebuild_index: bool = True) -> dict:
    root = Path(root).resolve()
    kinds = ("bgm", "se") if kind == "all" else (kind,)
    print("\n" + "=" * 72)
    print("Dot 离线资源维护器 1.0 - 公共资源整理")
    print("项目根目录:", root)
    print("模式:", mode, "(预演，不写文件)" if dry_run else "")
    print(f"批量线程: {BATCH_WORKERS}")
    print("=" * 72)
    rows = []
    for one_kind in kinds:
        r = consolidate_kind(root, one_kind, mode, dry_run)
        rows.append(r)
        print(
            f"[{one_kind.upper()}] files={r['files']} cues={r['cues']} "
            f"重复空间={_format_bytes(r['duplicate_bytes'])} 冲突={r['conflicts']} "
            f"复制={r['copied']} 硬链接={r['linked']} 删除={r['removed']} 保留冲突={r['kept_conflict']}"
        )

    cleanup = None
    if mode == "pool_only":
        print("\n[INFO] 正在清理已空的旧 BGM/SE 目录……")
        cleanup = cleanup_empty_audio_tree(root, dry_run=dry_run)
        print(f"空目录处理={cleanup['dirs']} | 旧索引文件处理={cleanup['legacy_indexes']}")

    # pool_only is explicitly a synchronized operation: index rebuild cannot be disabled.
    should_rebuild = (mode != "report" and not dry_run and (rebuild_index or mode == "pool_only"))
    if should_rebuild:
        print("\n[INFO] 正在按每个 story.json 的实际资源引用同步重建索引……")
        index_result = run_index_update(root, prefer_pool=(mode == "pool_only"))
    else:
        index_result = None
    reclaimed = sum(r["reclaimed"] for r in rows)
    print("预计/实际节省:", _format_bytes(reclaimed))
    return {"rows": rows, "cleanup": cleanup, "reclaimed": reclaimed, "index": index_result}


MODE_INFO = {
    "index": (
        "仅更新/修复索引",
        "不移动、不删除资源。先读取每个 story.json 的实际 BGM/SE 引用，再重建公共音频索引、剧情索引和缩略图索引；"
        "同时修复 story.audio.cues 与 paths。目录里存在但任何 story 都没有引用的历史音频不会再写入全局索引。",
    ),
    "report": (
        "只扫描重复资源",
        "只计算 SHA-256 并统计重复 BGM/SE、可节省空间和同 cue 内容冲突。不会写文件、不会改索引。第一次使用建议先执行。",
    ),
    "pool": (
        "建立公共池（保留旧副本）",
        "把每个 BGM/SE cue 建立到 data/audio 公共池，旧剧情/旧全局目录中的文件仍保留。兼容性最高，但目录不会明显变简洁。完成后可自动重建索引。",
    ),
    "link": (
        "硬链接去重",
        "公共池保留一份数据，SHA-256 完全相同的旧路径改成硬链接。旧目录仍存在但节省磁盘空间；同 cue 内容不同的文件不会处理。",
    ),
    "prune": (
        "删除重复副本",
        "先建立公共池，再删除与公共池 SHA-256 完全相同的旧文件。只删文件，不主动清理所有空目录；完成后自动更新多路径索引。",
    ),
    "pool_only": (
        "仅保留公共池（精简目录）",
        "推荐用于最终整理：先确保 BGM/SE 已进入 data/audio 公共池，再删除所有已验证相同的剧情本地/旧全局副本，"
        "随后删除已经空掉的 bgm/se/misc/decoded/audio 旧目录，并强制按 story.json 实际引用同步重建索引。"
        "若同 cue 内容不同，会保留冲突文件并提示，不会强删。",
    ),
}


def cli_main(argv=None):
    ap = argparse.ArgumentParser(description="Dot 离线资源维护器 1.0：索引更新与公共 BGM/SE 整理核心")
    ap.add_argument("--root", default=None, help="项目根目录；省略时从脚本位置向上自动识别")
    ap.add_argument("--action", choices=tuple(MODE_INFO), default="index")
    ap.add_argument("--kind", choices=("all", "bgm", "se"), default="all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-index", action="store_true", help="整理后不自动更新索引；pool_only 模式会忽略此项并强制同步")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve() if args.root else find_project_root(Path(__file__))
    ok, detail = validate_root(root)
    if not ok:
        print(f"[ERROR] 未识别到有效项目根目录: {root} ({detail})")
        print("可用 --root 指定包含 data_r18_all/stories 的项目目录。")
        return 2
    if args.action == "index":
        run_index_update(root)
    elif args.action == "report":
        run_organize(root, "report", args.kind, dry_run=True, rebuild_index=False)
    else:
        run_organize(root, args.action, args.kind, args.dry_run, rebuild_index=not args.no_index)
    return 0



if __name__ == "__main__":
    raise SystemExit(cli_main())
