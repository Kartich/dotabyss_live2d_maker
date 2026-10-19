# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import json
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent


def find_project_root(start: Path | str | None = None) -> Path:
    env = os.environ.get('DOT_PROJECT_ROOT', '').strip()
    if env:
        p = Path(env).expanduser().resolve()
        if p.exists():
            return p
    p = Path(start or TOOL_DIR).expanduser().resolve()
    if p.is_file():
        p = p.parent
    candidates = [p, *list(p.parents)[:8]]
    best = p
    best_score = -1
    for c in candidates:
        score = 0
        markers = (
            ('data_r18_all', 8), ('data', 6), ('index.html', 5),
            ('src', 3), ('vendor', 2), ('raw_bundles', 1),
        )
        for name, weight in markers:
            if (c / name).exists():
                score += weight
        if score > best_score:
            best, best_score = c, score
    return best


def project_root() -> Path:
    return find_project_root(TOOL_DIR)


def catalog_path() -> Path:
    env = os.environ.get('DOT_CATALOG', '').strip()
    if env:
        return Path(env).expanduser().resolve()
    local = TOOL_DIR / 'catalog_1.bin'
    if local.exists():
        return local
    root = project_root()
    root_catalog = root / 'catalog_1.bin'
    return root_catalog if root_catalog.exists() else local


def raw_bundles_dir() -> Path:
    """Return the configured shared flat Bundle cache (never inside player assets by default)."""
    configured = ""
    try:
        settings = json.loads((TOOL_DIR / "settings.json").read_text(encoding="utf-8-sig"))
        configured = str(settings.get("raw_bundle_root") or "").strip()
    except Exception:
        configured = ""
    p = Path(configured).expanduser() if configured else TOOL_DIR / "raw_bundles"
    if not p.is_absolute():
        p = TOOL_DIR / p
    p.mkdir(parents=True, exist_ok=True)
    return p.resolve()


def runtime_dir() -> Path:
    p = TOOL_DIR / '.runtime'
    p.mkdir(parents=True, exist_ok=True)
    return p
