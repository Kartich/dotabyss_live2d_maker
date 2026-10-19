# -*- coding: utf-8 -*-
"""Minimal audio path helpers used by the downloader only."""
from __future__ import annotations

import re
from pathlib import Path
from tool_paths import find_project_root as _find_project_root

BGM_RE = re.compile(r"\bbgm_?(\d+)\b", re.I)
SE_RE = re.compile(r"\bse(\d+)\b", re.I)


def find_project_root(start: Path | str) -> Path:
    return _find_project_root(start)


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


def web_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def audio_paths(root: Path):
    data = root / "data"
    data_r18 = root / "data_r18_all"
    return {
        "data": data,
        "data_r18": data_r18,
        "bgm_root": data / "audio" / "bgm" / "decoded",
        "bgm_index": data / "audio" / "bgm" / "index.json",
        "se_root": data / "audio" / "se" / "decoded" / "misc",
        "se_index": data / "audio" / "se" / "index.json",
    }
