# -*- coding: utf-8 -*-
"""Catalog URL parsing, complete resource table, selective download and path-preserving export.

This module is independent of the older player-specific repair pipeline. It keeps
original Catalog keys intact and exports Unity container assets by their own paths
when UnityPy exposes those paths. Bundle-key path hints are fallback-only.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

from catalog_downloader import (
    DEFAULT_CATALOG, DEFAULT_OUTPUT, extract_bundle_names, classify_resource,
    make_url, download_one, download_many, resource_catalog_index,
)
from resource_classifier import classify_bundle
from parallel_utils import IO_WORKERS, workers_for

DEFAULT_CATALOG_URL = 'https://api.abyss-prod-r18.dotabyss.dmmgames.com/resources/webgl/r18/aas/12394/aa/catalog_1.bin'


def parse_catalog_url(url: str) -> dict[str, str]:
    """Validate a full catalog URL and derive its resource base URL/version."""
    raw = str(url or '').strip()
    p = urllib.parse.urlsplit(raw)
    if p.scheme not in ('http', 'https') or not p.netloc:
        raise ValueError('Catalog 地址必须是完整的 http(s) URL。')
    filename = urllib.parse.unquote(PurePosixPath(p.path).name).lower()
    if filename != 'catalog_1.bin':
        raise ValueError('地址末尾必须是 catalog_1.bin。')
    if p.query or p.fragment:
        raise ValueError('Catalog 地址暂不支持 query string 或 fragment，请使用纯资源地址。')
    path = p.path.rsplit('/', 1)[0].rstrip('/') + '/'
    base_url = urllib.parse.urlunsplit((p.scheme, p.netloc, path, '', ''))
    version = ''
    m = re.search(r'/aas/([^/]+)/aa/catalog_1\.bin$', p.path, re.I)
    if m:
        version = m.group(1)
    return {'catalog_url': raw, 'base_url': base_url, 'version': version}


def download_catalog(url: str, destination: Path) -> dict:
    """Download a catalog to a temporary file; replace destination only if parseable."""
    destination = Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + '.download.part')
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36',
            'Accept-Encoding': 'identity',
        })
        with urllib.request.urlopen(req, timeout=90) as response, tmp.open('wb') as f:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        if not tmp.is_file() or tmp.stat().st_size < 1024:
            raise RuntimeError('下载内容过小，不像有效的 Catalog。')
        names = extract_bundle_names(tmp)
        if len(names) < 20:
            raise RuntimeError(f'Catalog 校验失败：只识别到 {len(names)} 条 Bundle 名称；原文件未覆盖。')
        tmp.replace(destination)
        return {'path': str(destination), 'bytes': destination.stat().st_size, 'bundles': len(names)}
    except Exception:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass
        raise


def _strip_hash(name: str) -> str:
    return re.sub(r'_[0-9a-f]{32}\.bundle$', '', str(name), flags=re.I)


def _source_path_hint(name: str) -> str:
    """Produce a conservative source-path hint from a flattened Addressables key.

    Exact internal paths from Unity's AssetBundle container take precedence during
    export. This hint is for browsing/search and non-container assets only.
    """
    raw = _strip_hash(name).replace('\\', '/')
    rules = [
        (r'^(bgm\d+\.(?:acb|awb))$', 'Assets/Assets/Project/LazyAssets/General/Sound/CRI/WebGL/WorkUnit/Novel/BGM/'),
        (r'^general-ui-bg-novel_assets_assets_project_lazyassets_general_ui_bg_novel_(.+)$', 'Assets/Assets/Project/LazyAssets/General/UI/BG/Novel/'),
        (r'^r18-only-charastand_assets_assets_project_lazyassets_r18-only_charastand_novel_prefabs_([gx])_(charastand\d+[gx]?\.prefab)$', 'Assets/Assets/Project/LazyAssets/R18-Only/CharaStand/Novel/Prefabs/{0}/'),
        (r'^r18-only-icon-story-s_assets_assets_project_lazyassets_r18-only_icon_story_s_story_s_(.+)$', 'Assets/Assets/Project/LazyAssets/R18-Only/Icon/Story/S/'),
        (r'^general-spine_assets_label-projectspine-(.+)$', 'Assets/Label/ProjectSpine/'),
        (r'^general-ingame-chara-unit_assets_assets_project_lazyassets_general_ingame_unit_(.+)$', 'Assets/Assets/Project/LazyAssets/General/Ingame/Unit/'),
        (r'^general-ingame-timelineextract_assets_assets_project_lazyassets_general_ingame_timelineextract_(.+)$', 'Assets/Assets/Project/LazyAssets/General/Ingame/TimelineExtract/'),
        (r'^general-icon-item_assets_assets_project_lazyassets_general_icon_item_(.+)$', 'Assets/Assets/Project/LazyAssets/General/Icon/Item/'),
        (r'^general-icon-enemy_assets_assets_project_lazyassets_general_icon_enemy_(.+)$', 'Assets/Assets/Project/LazyAssets/General/Icon/Enemy/'),
        (r'^general-icon-weapon_assets_assets_project_lazyassets_general_icon_weapon_(.+)$', 'Assets/Assets/Project/LazyAssets/General/Icon/Weapon/'),
        (r'^general-icon-gacha_assets_assets_project_lazyassets_general_icon_gacha_(.+)$', 'Assets/Assets/Project/LazyAssets/General/Icon/Gacha/'),
        (r'^r18-only-icon-charaicon_assets_assets_project_lazyassets_r18-only_icon_charaicon_(.+)$', 'Assets/Assets/Project/LazyAssets/R18-Only/Icon/CharaIcon/'),
        (r'^r18-only-icon-characutin_assets_assets_project_lazyassets_r18-only_icon_characutin_(.+)$', 'Assets/Assets/Project/LazyAssets/R18-Only/Icon/CharaCutin/'),
        (r'^r18-only-icon-shop-([^_]+)_assets_assets_project_lazyassets_r18-only_icon_shop_[^_]+_(.+)$', 'Assets/Assets/Project/LazyAssets/R18-Only/Icon/Shop/'),
        (r'^general-novel_assets_assets_project_lazyassets_general_novel_(.+)$', 'Assets/Assets/Project/LazyAssets/General/Novel/'),
        (r'^general-ui_assets_assets_project_lazyassets_general_ui_(.+)$', 'Assets/Assets/Project/LazyAssets/General/UI/'),
        (r'^general-sound-cri_assets_assets_project_lazyassets_general_sound_cri_webgl_workunit_(.+)$', 'Assets/Assets/Project/LazyAssets/General/Sound/CRI/WebGL/WorkUnit/'),
        (r'^r18-only-sound-cri_assets_assets_project_lazyassets_r18-only_sound_cri_webgl_workunit_(.+)$', 'Assets/Assets/Project/LazyAssets/R18-Only/Sound/CRI/WebGL/WorkUnit/'),
        (r'^r18-only-novel_assets_assets_project_lazyassets_r18-only_novel_(.+)$', 'Assets/Assets/Project/LazyAssets/R18-Only/Novel/'),
    ]
    # The novel script naming rule has repeated IDs and cannot be reduced to the
    # last regex group; preserve the exact logical filename.
    story_match=re.match(r'^r18-only-novel_assets_assets_project_lazyassets_r18-only_novel_mainchara_hmr_(\d{11})_hmr_\1\.txt$', raw, re.I)
    if story_match:
        sid=story_match.group(1)
        return f'Assets/Assets/Project/LazyAssets/R18-Only/Novel/MainChara/hmr_{sid}_hmr_{sid}.txt'
    for pat, prefix in rules:
        m = re.match(pat, raw, re.I)
        if not m:
            continue
        suffix = m.groups()[-1]
        if '{0}' in prefix:
            return prefix.format(m.group(1)) + m.group(2)
        # The bundle key encodes separators and underscores together. Keep the
        # suffix filename readable rather than inventing ambiguous subfolders.
        if pat.startswith(r'^general-spine'):
            return prefix + suffix
        if suffix.lower().endswith(('.png', '.prefab', '.asset', '.fade.asset', '.fademotionlist.asset', '.txt', '.atlas.txt', '.acb', '.awb', '.unity', '.otf', '.usm', '.rendertexture', '.overridecontroller')):
            # Sound and some general prefixes have real folder tokens before the
            # filename; keep them below a family directory, marked as an estimate.
            if 'WorkUnit/' in prefix:
                bits = suffix.split('_')
                folder_map = {'novel':'Novel', 'ingame':'Ingame', 'outgame':'Outgame', 'bgm':'BGM', 'se':'SE', 'voice':'Voice', 'backgroundvoice':'BackgroundVoice'}
                known=[]
                for b in bits[:4]:
                    if b.lower() in folder_map: known.append(folder_map[b.lower()])
                return prefix + ('/'.join(known) + '/' if known else '') + suffix
            return prefix + suffix
        return prefix + suffix

    # Keep unknown family names traceable rather than guessing separators.
    fam = raw.split('_assets_', 1)[0] if '_assets_' in raw else raw.split('_', 1)[0]
    return f'CatalogInferred/{fam}/{raw}'


def _scope(name: str) -> str:
    low = name.lower()
    if low.startswith('r18-only-') or low.startswith('r18-only_'):
        return 'R18'
    if low.startswith(('general-', 'general_', 'common-', 'common_', 'tutorial-', 'home-bgassets-', 'scenes_', 'event.')):
        return '公共'
    return '未判定'


def _fine_kind(name: str) -> str:
    low = name.lower()
    base, _token = classify_resource(name)
    if base not in ('general', 'other', 'sound_other', 'r18_other'):
        return base
    if low.startswith('general-spine_') or re.search(r'general[-_]novel_.*(?:_spine_|_spine[^a-z])', low): return 'Spine / Spine Prefab'
    if 'event_evs_' in low or re.search(r'general-novel_.*\.txt_[0-9a-f]{32}\.bundle$', low): return '事件/普通剧情文本'
    if 'dotnovelcharaicon' in low: return '剧情角色Prefab'
    if low.startswith('general-ingame-chara-unit_'): return '角色单位数据'
    if low.startswith('general-ingame-timelineextract_'): return '战斗Timeline数据'
    if low.startswith('general-icon-item_'): return '物品图标'
    if low.startswith('general-icon-enemy_'): return '敌人图标'
    if low.startswith('general-icon-weapon_'): return '武器图标'
    if low.startswith('general-icon-gacha_'): return '抽卡图像'
    if low.startswith('general-icon-') or low.startswith('r18-only-icon-'): return '图标/图片'
    if low.startswith(('general-sound-cri_', 'r18-only-sound-cri_')):
        m = re.search(r'workunit_(novel|ingame|outgame)_((?:r18_)?)(bgm|se|voice|backgroundvoice)_', low)
        if m:
            area, _, kind = m.groups()
            return f'{area}_{kind}'.upper()
        if 'workunit_' in low: return 'CRI音频'
    if low.startswith('general-ui-') and '_bg_' in low: return '背景图'
    if low.startswith('general-ui_'): return 'UI资源'
    if low.startswith('general-novel_'): return '普通剧情/小说资源'
    if low.startswith('general-attachment_'): return '附件资源'
    if low.startswith('general-effects_'): return '特效资源'
    if low.startswith('general-ingame-'): return '游戏内数据'
    if low.startswith('r18-only-'): return 'R18资源'
    if low.startswith('common-') or low.startswith('common_'): return '公共资源'
    return base


def _story_ids(name: str) -> list[str]:
    low = _strip_hash(name).lower()
    out = set(re.findall(r'hm[ rn]_(\d{11})'.replace(' ', ''), low))
    return sorted(out)


def _character_ids(name: str) -> list[str]:
    low = _strip_hash(name).lower()
    ids = set()
    # Explicit character-ID naming schemas only. Do not infer people from arbitrary
    # nine-digit sequences inside hashes or unrelated battle identifiers.
    patterns = [
        r'charastand(\d{9})[gx]?(?:\.|_|$)',
        r'(?:^|projectspine-)unit(\d{9})[gx]?(?:_|\.|$)',
        r'(?:^|[_-])l2d[_-](?:core|motion|fade)[_-]?(\d{9})(?:\.|_|$)',
        r'(?:^|[_-])(?:model|motion|expression)[_-](\d{9})(?:\.|_|$)',
        r'dotnovelcharaicon(\d{9})[gx]?(?:\.|_|$)',
        r'chara_chara_(\d{9})_',
        r'general_ingame_unit_(\d{9})[gx]?\.',
        r'characutin[^0-9]{0,30}(\d{9})(?:\.|_|$)',
        r'(?:^|_)charaicon_(\d{9})(?:\.|_|$)',
        r'(?:^|_)charaname_(\d{9})(?:\.|_|$)',
        r'(?:^|_)charaicon[^0-9]{0,30}(\d{9})(?:\.|_|$)',
        r'(?:^|_)portrait_(\d{9})(?:\.|_|$)',
    ]
    for pat in patterns:
        ids.update(re.findall(pat, low))
    for sid in _story_ids(low):
        ids.add(sid[:9])
    # Thumbnails carry a ten-digit visual key, not the full 11-digit story ID.
    for thumb_key in re.findall(r'story_s_hmr_(\d{10})', low):
        ids.add(thumb_key[:9])
    return sorted(x for x in ids if re.fullmatch(r'\d{9}', x))


def _family(name: str) -> str:
    low = name.lower()
    m = re.match(r'^([a-z0-9-]+)', low)
    return m.group(1) if m else 'unknown'


def _bundle_hash(name: str) -> str:
    m=re.search(r'_([0-9a-f]{32})\.bundle$', str(name), re.I)
    return m.group(1).lower() if m else ''


def _logical_extension(name: str) -> str:
    raw=_strip_hash(name).lower()
    extensions=('.fademotionlist.asset','.overridecontroller','.rendertexture','.atlas.txt','.fade.asset','.prefab','.asset','.png','.txt','.acb','.awb','.unity','.usm','.otf')
    for ext in extensions:
        if raw.endswith(ext): return ext
    return Path(raw).suffix.lower()


def build_catalog_records(catalog: Path, cache_dir: Path | None = None) -> list[dict]:
    names = extract_bundle_names(Path(catalog))
    cache_dir = Path(cache_dir) if cache_dir else Path(DEFAULT_OUTPUT)
    rows = []
    for name in names:
        kind, token = classify_resource(name)
        stories = _story_ids(name)
        chars = _character_ids(name)
        row = {
            'name': name,
            'kind': _fine_kind(name),
            'base_kind': kind,
            'token': str(token or ''),
            'scope': _scope(name),
            'family': _family(name),
            'character_ids': ','.join(chars),
            'story_ids': ','.join(stories),
            'source_path': _source_path_hint(name),
            'downloaded': '是' if (cache_dir / name).is_file() and (cache_dir / name).stat().st_size > 0 else '否',
        }
        rows.append(row)
    return rows


def _safe_rel(value: str) -> Path:
    raw = str(value or '').replace('\\', '/')
    p = PurePosixPath(raw)
    parts = [x for x in p.parts if x not in ('/', '.', '..', '') and not x.endswith(':')]
    if not parts:
        parts = ['unknown_asset']
    return Path(*parts)


def _story_cues(text: str) -> dict[str, set[str]]:
    """Extract common resource IDs referenced by a downloaded story TextAsset."""
    s = text or ''
    result = {'background': set(), 'bgm': set(), 'se': set()}
    for token in re.findall(r'(?i)(?<![a-z0-9])abg\d+[a-z]?(?![a-z0-9])', s):
        result['background'].add(token.lower())
    for token in re.findall(r'(?i)(?<![a-z0-9])bgm\d+(?![a-z0-9])', s):
        result['bgm'].add(token.lower())
    for token in re.findall(r'(?i)(?<![a-z0-9])se\d+(?![a-z0-9])', s):
        result['se'].add(token.lower())
    return result


def _find_common_bundles(catalog_names: list[str], cues: dict[str, set[str]]) -> list[str]:
    out = set()
    for name in catalog_names:
        low = name.lower()
        kind, token = classify_resource(name)
        if kind in ('background', 'bgm', 'se') and token:
            if str(token).lower() in cues.get(kind, set()):
                out.add(name)
            continue
        # A broader fallback catches ordinary game-side/CRI assets that the legacy
        # player-specific classifier intentionally does not index.
        body = _strip_hash(low)
        for rid in cues.get('background', set()):
            if re.search(rf'_{re.escape(rid)}\.png$', body): out.add(name)
        for rid in cues.get('bgm', set()):
            if re.search(rf'(?:^|_){re.escape(rid)}\.(?:acb|awb)$', body): out.add(name)
        for rid in cues.get('se', set()):
            if re.search(rf'(?:^|_){re.escape(rid)}\.(?:acb|awb)$', body): out.add(name)
    return sorted(out, key=str.lower)


def character_bundle_names(names: list[str], character_id: str) -> tuple[list[str], list[str]]:
    cid = str(character_id).strip()
    if not re.fullmatch(r'\d{9}', cid):
        raise ValueError('人物特征 ID 必须是 9 位数字。')
    private = []
    story_ids = set()
    for name in names:
        chars = _character_ids(name)
        stories = _story_ids(name)
        # Include resources with an explicit matching character schema, plus all
        # bundles of story IDs whose first nine digits map to this person.
        if cid in chars:
            private.append(name)
            story_ids.update(s for s in stories if s.startswith(cid))
        elif any(s.startswith(cid) for s in stories):
            private.append(name)
            story_ids.update(s for s in stories if s.startswith(cid))
    # Second pass brings every story-linked auxiliary file in even when that file
    # does not repeat the character ID directly.
    for name in names:
        if any(s in name.lower() for s in story_ids) and name not in private:
            low = name.lower()
            if any(x in low for x in ('r18-only-novel_', 'sound-cri_', 'icon-story-s_', 'charastand', 'general-novel_')):
                private.append(name)
    return sorted(set(private), key=str.lower), sorted(story_ids)


def _write_asset(data, obj, target_base: Path) -> list[Path]:
    """Export one UnityPy object; return paths actually written."""
    target_base.parent.mkdir(parents=True, exist_ok=True)
    typ = getattr(getattr(obj, 'type', None), 'name', None) or getattr(getattr(data, 'type', None), 'name', None) or 'Asset'
    written: list[Path] = []

    def unique_path(path: Path) -> Path:
        if not path.exists(): return path
        base = path.with_name(f'{path.stem}__pid{getattr(obj, "path_id", "x")}{path.suffix}')
        candidate = base; counter = 2
        while candidate.exists():
            candidate = base.with_name(f'{base.stem}_{counter}{base.suffix}')
            counter += 1
        return candidate

    try:
        if typ in ('Texture2D', 'Sprite'):
            image = getattr(data, 'image', None)
            if image is not None:
                if target_base.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
                    target = target_base.with_suffix('.png')
                else:
                    target = target_base
                target = unique_path(target)
                image.save(target)
                written.append(target)
                return written
        if typ == 'TextAsset':
            payload = getattr(data, 'script', None)
            if payload is not None:
                target = unique_path(target_base if target_base.suffix.lower() in ('.txt', '.json', '.bytes', '.csv', '.xml') else target_base.with_suffix('.txt'))
                if isinstance(payload, str):
                    target.write_text(payload, encoding='utf-8', errors='replace')
                else:
                    payload = bytes(payload)
                    try:
                        text = payload.decode('utf-8-sig')
                        target.write_text(text, encoding='utf-8', errors='replace')
                    except Exception:
                        target.write_bytes(payload)
                written.append(target)
                return written
        if typ == 'AudioClip':
            samples = getattr(data, 'samples', None)
            if isinstance(samples, dict) and samples:
                for sample_name, payload in samples.items():
                    payload = bytes(payload) if not isinstance(payload, bytes) else payload
                    target = unique_path(target_base.parent / (Path(str(sample_name)).name or target_base.stem + '.audio'))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
                    written.append(target)
                if written: return written
        exported = data.export()
        if isinstance(exported, bytes):
            suffix = '.bin'
            target = unique_path(target_base if target_base.suffix else target_base.with_suffix(suffix))
            target.write_bytes(exported)
        elif isinstance(exported, str):
            suffix = '.json' if exported.lstrip().startswith(('{', '[')) else '.txt'
            target = unique_path(target_base if target_base.suffix else target_base.with_suffix(suffix))
            target.write_text(exported, encoding='utf-8', errors='replace')
        else:
            payload = obj.get_raw_data()
            target = unique_path(target_base.with_suffix('.bin'))
            target.write_bytes(payload)
        written.append(target)
    except Exception:
        try:
            payload = obj.get_raw_data()
            target = unique_path(target_base.with_suffix('.bin'))
            target.write_bytes(payload)
            written.append(target)
        except Exception:
            pass
    return written


def extract_bundle(bundle_path: Path, export_root: Path, target_rel: Path, *, character_id: str | None = None,
                   mirror_common_root: Path | None = None, collect_text: bool = False) -> tuple[list[dict], list[str]]:
    """Extract a bundle, preserving Unity container paths whenever available."""
    try:
        import UnityPy
    except ImportError as exc:
        raise RuntimeError('缺少 UnityPy。请在程序目录运行：python -m pip install -r requirements.txt') from exc

    env = UnityPy.load(str(bundle_path))
    container_by_path_id: dict[int, list[str]] = defaultdict(list)
    try:
        for container_path, pointer in (getattr(env, 'container', {}) or {}).items():
            try:
                referenced = pointer.get_obj() if hasattr(pointer, 'get_obj') else pointer
                pid = int(getattr(referenced, 'path_id', 0))
                if pid:
                    container_by_path_id[pid].append(str(container_path))
            except Exception:
                continue
    except Exception:
        pass

    results: list[dict] = []
    texts: list[str] = []
    fallback_hint = _safe_rel(_source_path_hint(bundle_path.name))
    fallback_dir = fallback_hint.parent
    for obj in getattr(env, 'objects', []) or []:
        try:
            data = obj.read()
            typ = getattr(getattr(obj, 'type', None), 'name', None) or 'Asset'
            object_name = str(getattr(data, 'm_Name', '') or getattr(data, 'name', '') or '').strip()
            cp_list = container_by_path_id.get(int(getattr(obj, 'path_id', 0)), [])
            source_paths = cp_list or [str(fallback_hint)]
            if typ == 'TextAsset':
                payload = getattr(data, 'script', None)
                if isinstance(payload, str): texts.append(payload)
                elif payload is not None:
                    try: texts.append(bytes(payload).decode('utf-8-sig', errors='ignore'))
                    except Exception: pass
            for cp in source_paths:
                source_rel = _safe_rel(cp if cp_list else str(fallback_hint))
                if cp_list:
                    target_base = export_root / target_rel / source_rel
                else:
                    # Path hint includes a file-like key; object name differentiates
                    # multiple Unity objects bundled under a single catalog key.
                    name = object_name or f'{typ}_{getattr(obj, "path_id", "x")}'
                    suffix = source_rel.suffix
                    target_base = export_root / target_rel / source_rel.parent / (name + suffix if suffix else name)
                written = _write_asset(data, obj, target_base)
                for out_path in written:
                    results.append({
                        'bundle': bundle_path.name,
                        'container_path': cp if cp_list else '',
                        'object_name': object_name,
                        'type': typ,
                        'path_id': int(getattr(obj, 'path_id', 0) or 0),
                        'output': str(out_path),
                        'character_id': character_id or '',
                    })
                    if mirror_common_root is not None and cp_list:
                        try:
                            rel = out_path.relative_to(export_root / target_rel)
                            mirror_dest = mirror_common_root / rel
                            mirror_dest.parent.mkdir(parents=True, exist_ok=True)
                            if not mirror_dest.exists():
                                try: os.link(out_path, mirror_dest)
                                except OSError: shutil.copy2(out_path, mirror_dest)
                        except Exception:
                            pass
        except Exception as exc:
            results.append({'bundle': bundle_path.name, 'container_path': '', 'object_name': '', 'type': 'ERROR',
                            'path_id': int(getattr(obj, 'path_id', 0) or 0), 'output': '', 'character_id': character_id or '',
                            'error': str(exc)})
    return results, texts


def _common_targets(name: str) -> bool:
    return classify_resource(name)[0] in ('background', 'bgm', 'se') or _fine_kind(name) in ('NOVEL_SE', 'INGAME_SE', 'OUTGAME_SE', 'NOVEL_BGM', 'INGAME_BGM', 'OUTGAME_BGM')


def perform_download(*, catalog: Path, base_url: str, output_dir: Path, export_root: Path,
                     names: list[str], workers: int = IO_WORKERS, extract: bool = True,
                     character_id: str | None = None, story_ids: list[str] | None = None,
                     include_common: bool = True) -> dict:
    names = sorted(dict.fromkeys(str(x) for x in names if str(x)), key=str.lower)
    if not names:
        raise ValueError('没有待下载的 Bundle。')
    catalog_names = extract_bundle_names(catalog)
    output_dir = Path(output_dir); output_dir.mkdir(parents=True, exist_ok=True)
    export_root = Path(export_root)
    work_items = [(make_url(n, base_url=base_url), output_dir / n) for n in names]
    workers_used = workers_for(len(work_items), requested=workers)
    statuses = [None] * len(work_items)
    print(f'[INFO] selected bundles={len(names)} workers={workers_used} base={base_url}', flush=True)
    done = 0
    if work_items:
        with ThreadPoolExecutor(max_workers=workers_used, thread_name_prefix='catalog-download') as pool:
            futures = {pool.submit(download_one, url, dest):(idx, dest) for idx,(url,dest) in enumerate(work_items)}
            for future in as_completed(futures):
                idx, dest = futures[future]
                try: statuses[idx] = future.result()
                except Exception as exc: statuses[idx] = f'failed:{exc}'
                done += 1
                print(f'[GET] [{done}/{len(work_items)}] [{statuses[idx]}] {dest.name}', flush=True)
                print('@@DOT_PROGRESS\t' + json.dumps({'percent':done * 70 / max(1,len(work_items)), 'stage':'下载指定 Bundle', 'detail':dest.name, 'current':done, 'total':len(work_items)}, ensure_ascii=False), flush=True)

    successful = [names[i] for i, status in enumerate(statuses) if status and not str(status).startswith('failed:')]
    failures = [{'name':names[i], 'error':str(status)[7:]} for i,status in enumerate(statuses) if status and str(status).startswith('failed:')]
    exported_rows=[]; script_texts=[]

    # In character mode, keep a canonical group path. Public resources stay in a
    # shared pool as well as being linked into that character's related-resource
    # folder when those resources were resolved from that character's stories.
    def target_for(name: str) -> tuple[Path, Path | None]:
        low = name.lower()
        linked = (character_id in _character_ids(name)) or any(s.startswith(character_id) for s in _story_ids(name))
        # The scope says who can access the bundle, not whether the bundle is
        # globally shared. Only generic background/audio dependencies are copied
        # into the shared pool; character/story-specific common-scope bundles stay
        # inside that character's folder.
        is_common = (_common_targets(name) or _scope(name) == '公共') and not linked
        if character_id:
            if is_common:
                # Public assets are stored once in a categorized shared pool, never
                # duplicated below the character ID.
                kind = _fine_kind(name)
                if kind in ('background', '背景图') or 'BACKGROUND' in kind.upper(): category = '背景'
                elif kind == 'bgm' or 'BGM' in kind.upper(): category = 'BGM'
                elif kind == 'se' or kind.endswith('_SE') or kind.upper().endswith('SE'): category = 'SE'
                elif 'VOICE' in kind.upper() or kind == 'voice' or kind == 'bgv': category = '语音'
                else: category = '其他公共资源'
                return Path('公共资源池') / category, None
            return Path(character_id), None
        chars = _character_ids(name)
        if len(chars) == 1:
            return Path(chars[0]), None
        if is_common:
            kind = _fine_kind(name)
            if kind in ('background', '背景图') or 'BACKGROUND' in kind.upper(): category = '背景'
            elif kind == 'bgm' or 'BGM' in kind.upper(): category = 'BGM'
            elif kind == 'se' or kind.endswith('_SE') or kind.upper().endswith('SE'): category = 'SE'
            elif 'VOICE' in kind.upper() or kind == 'voice' or kind == 'bgv': category = '语音'
            else: category = '其他公共资源'
            return Path('公共资源池') / category, None
        return Path('未归属资源') / _fine_kind(name), None

    if extract:
        try:
            import UnityPy  # noqa: F401
        except ImportError as exc:
            raise RuntimeError('Bundle 已下载，但无法提取：缺少 UnityPy。请运行 python -m pip install -r requirements.txt') from exc
        for i, name in enumerate(successful, 1):
            bundle = output_dir / name
            dest_rel, mirror_rel = target_for(name)
            mirror_root = mirror_rel if mirror_rel is not None else None
            result, texts = extract_bundle(bundle, export_root, dest_rel, character_id=character_id, mirror_common_root=mirror_root)
            exported_rows.extend(result); script_texts.extend(texts)
            print(f'[EXTRACT] [{i}/{len(successful)}] {name}: objects={len(result)}', flush=True)
            print('@@DOT_PROGRESS\t' + json.dumps({'percent':70 + 25*i/max(1,len(successful)), 'stage':'按原目录提取', 'detail':name, 'current':i, 'total':len(successful)}, ensure_ascii=False), flush=True)

    # When a character group is requested, read the downloaded scripts and resolve
    # their public BG/BGM/SE dependencies. These common bundles are added to the
    # same operation and remain reusable in the shared PublicResources pool.
    common_added=[]
    if character_id and include_common and extract and script_texts:
        cues = {'background': set(), 'bgm': set(), 'se': set()}
        for t in script_texts:
            got = _story_cues(t)
            for k,v in got.items(): cues[k].update(v)
        common_added = _find_common_bundles(catalog_names, cues)
        already=set(successful)
        common_added=[x for x in common_added if x not in already]
        if common_added:
            print(f'[INFO] story-referenced common resources={len(common_added)}', flush=True)
            extra = perform_download(catalog=catalog, base_url=base_url, output_dir=output_dir, export_root=export_root,
                                     names=common_added, workers=workers, extract=True,
                                     character_id=character_id, story_ids=story_ids, include_common=False)
            successful.extend(extra.get('downloaded', [])); failures.extend(extra.get('failures', []))
            exported_rows.extend(extra.get('exports', []))

    export_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        'version': 1, 'catalog': str(Path(catalog).resolve()), 'base_url': base_url,
        'character_id': character_id or '', 'story_ids': list(story_ids or []),
        'selected': names, 'downloaded': successful, 'failures': failures,
        'exports': exported_rows, 'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    # Manifests are internal orchestration files in the raw cache, not exported
    # character assets. No CSV/JSON log files are written into the export tree.
    manifest_dir = output_dir / '.dot_runtime'
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / (('manifest_character_' + character_id + '.json') if character_id else 'manifest_selected.json')
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[INTERNAL_MANIFEST] {manifest_path}', flush=True)
    print(f'[SUMMARY] successful={len(successful)} failed={len(failures)} extracted_objects={len(exported_rows)}', flush=True)
    return {'downloaded':successful, 'failures':failures, 'exports':exported_rows, 'manifest':str(manifest_path)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Catalog资源下载、原目录提取与人物资源归档')
    ap.add_argument('--fetch-catalog', action='store_true', help='下载完整 Catalog 地址')
    ap.add_argument('--url', default='')
    ap.add_argument('--destination', default='')
    ap.add_argument('--catalog', default=str(DEFAULT_CATALOG))
    ap.add_argument('--base-url', default='')
    ap.add_argument('--output', default=str(DEFAULT_OUTPUT))
    ap.add_argument('--export-root', default='')
    ap.add_argument('--names-file', default='')
    ap.add_argument('--character-id', default='')
    ap.add_argument('--workers', type=int, default=IO_WORKERS)
    ap.add_argument('--no-extract', action='store_true')
    ap.add_argument('--no-common', action='store_true')
    args = ap.parse_args(argv)

    if args.fetch_catalog:
        if not args.url or not args.destination:
            ap.error('--fetch-catalog 需要 --url 和 --destination')
        config = parse_catalog_url(args.url)
        print(f'[CONFIG] base_url={config["base_url"]} version={config["version"] or "(未识别)"}', flush=True)
        res=download_catalog(args.url, Path(args.destination))
        print(f'[CATALOG] saved={res["path"]} bytes={res["bytes"]} bundles={res["bundles"]}', flush=True)
        return 0

    catalog=Path(args.catalog)
    if not catalog.is_file(): raise SystemExit(f'找不到 Catalog：{catalog}')
    if not args.base_url:
        raise SystemExit('必须提供 --base-url，例如 https://api.abyss-prod-r18.dotabyss.dmmgames.com/resources/webgl/r18/aas/12394/aa/')
    config = parse_catalog_url(args.base_url.rstrip('/') + '/catalog_1.bin')
    names=extract_bundle_names(catalog)
    character_id = args.character_id.strip()
    story_ids=[]
    if character_id:
        selected, story_ids=character_bundle_names(names, character_id)
        print(f'[CHARACTER] id={character_id} private_bundles={len(selected)} story_ids={len(story_ids)}', flush=True)
    elif args.names_file:
        payload=json.loads(Path(args.names_file).read_text(encoding='utf-8-sig'))
        selected = payload.get('names', payload) if isinstance(payload, dict) else payload
        if not isinstance(selected, list): raise SystemExit('names-file 内容必须是字符串数组或 {"names": [...]}。')
        known=set(names); selected=[x for x in selected if x in known]
    else:
        raise SystemExit('必须提供 --names-file 或 --character-id。')
    if not selected: raise SystemExit('Catalog 中没有匹配到可下载的 Bundle。')
    perform_download(catalog=catalog, base_url=config['base_url'], output_dir=Path(args.output),
                     export_root=Path(args.export_root or (Path(args.output).parent / 'catalog_asset_library')),
                     names=selected, workers=args.workers, extract=not args.no_extract,
                     character_id=character_id or None, story_ids=story_ids, include_common=not args.no_common)
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'[ERROR] {exc}', file=sys.stderr, flush=True)
        raise SystemExit(1)
