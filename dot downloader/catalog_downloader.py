# -*- coding: utf-8 -*-
"""Directory-aware catalog downloader and resource classifier."""
from __future__ import annotations
import argparse, re, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from pathlib import Path

DEFAULT_CATALOG = Path('catalog_1.bin')
DEFAULT_OUTPUT = Path(__file__).resolve().parent / 'raw_bundles'
BASE_URL_PREFIX = 'https://api.abyss-prod-r18.dotabyss.dmmgames.com/resources/webgl/r18/aas/'
BASE_URL_SUFFIX = '/aa/'
DEFAULT_VERSION = ''
BASE_URL = BASE_URL_PREFIX + 'VERSION_PLACEHOLDER' + BASE_URL_SUFFIX

def build_base_url(version: str) -> str:
    v = str(version or '').strip().strip('/')
    if not v:
        raise ValueError('版本字段不能为空')
    if not v.isdigit():
        raise ValueError(f'版本字段必须为纯数字，例如 8374；收到: {version}')
    return f'{BASE_URL_PREFIX}{v}{BASE_URL_SUFFIX}'


def extract_bundle_names(catalog_path: Path):
    data = catalog_path.read_bytes()
    out, seen = [], set()
    pattern = re.compile(rb'[A-Za-z0-9_.@()\[\]-][\x20-\x7e]{8,1200}?\.bundle')
    for m in pattern.finditer(data):
        try:
            name = m.group(0).decode('utf-8')
        except UnicodeDecodeError:
            continue
        # Most catalog entries carry the full assets/project/lazyassets prefix.
        # CRI BGM companion AWB files are also published as bare names such as
        # `bgm0024.awb_<hash>.bundle`, so those must remain searchable too.
        if not re.search(r'(?:^|_)assets_assets_project_lazyassets_', name) and not re.match(r'^(?:common|event\.unity_|[A-Za-z0-9_-]+\.unity_|general-|r18-only-|tutorial-|bgm\d+\.awb_)', name):
            continue
        if name not in seen:
            seen.add(name); out.append(name)
    return out


from resource_classifier import classify_bundle, resource_id_info

def classify_resource(name: str):
    info = classify_bundle(name)
    return info.kind, info.token


def is_resource_id(value: str):
    s = str(value).strip().lower()
    return resource_id_info(s) is not None


def parse_role_query(value: str):
    s = str(value).strip().lower()
    kind = resource_id_info(s)
    if kind == 'story':
        return 'story', s[:9], s
    if kind == 'character':
        return 'character', re.sub(r'[gx]$','',s), None
    if kind:
        return 'resource', None, s
    raise ValueError(f'无法识别输入编号/资源 ID: {value}')


def role_to_chara(value: str):
    mode, chara, target = parse_role_query(value)
    if mode not in ('story', 'character'): raise ValueError(f'{value} 不是角色/剧情编号')
    return chara


def _stand_matches(name, chara_id):
    info=classify_bundle(name)
    return info.kind == 'stand' and bool(re.search(rf'charastand{re.escape(chara_id)}[gx]?\.prefab_', name, re.I))


def _story_match(name, story_id):
    info=classify_bundle(name)
    return info.kind == 'story' and story_id.lower() in name.lower()


def _story_prefix_match(name, chara_id):
    return bool(re.search(rf'hmr_{re.escape(chara_id)}\d{{2}}', name, re.I))


def _thumb_matches(name, chara_id):
    return bool(re.search(rf'story[_-]s[_-]hmr_{re.escape(chara_id)}\d\.png_', name, re.I))


def _resource_id_match(name, rid):
    info = classify_bundle(name)
    q = rid.lower()
    # Cue-family IDs are strict: vc_ only matches Voice, bgv_ only matches BGV.
    if q.startswith('vc_'):
        if info.kind != 'voice':
            return False
        m = re.search(r'vc_(\d{11})_', q)
        return bool(m and m.group(1) in name.lower())
    if q.startswith('bgv_'):
        if info.kind != 'bgv':
            return False
        m = re.search(r'bgv_(\d{11})_', q)
        return bool(m and m.group(1) in name.lower())
    return info.token == q


def filter_relevant(names, query):
    mode, chara_id, target = parse_role_query(query)
    selected=[]; skipped=[]
    for name in names:
        info=classify_bundle(name); hit=False
        low=name.lower()
        if mode=='resource':
            hit=_resource_id_match(name,target)
        elif mode=='story':
            sid=target.lower()
            if info.kind=='story' and sid in low: hit=True
            elif info.kind in ('l2d_core','l2d_motion','l2d_fade') and sid in low: hit=True
            elif info.kind in ('voice','bgv') and sid in low: hit=True
            elif info.kind=='stand' and _stand_matches(name,chara_id): hit=True
            elif info.kind=='thumb' and _thumb_matches(name,chara_id): hit=True
        else:
            if info.kind=='stand' and _stand_matches(name,chara_id): hit=True
            elif info.kind=='story' and _story_prefix_match(name,chara_id): hit=True
            elif info.kind in ('l2d_core','l2d_motion','l2d_fade') and _story_prefix_match(name,chara_id): hit=True
            elif info.kind in ('voice','bgv') and _story_prefix_match(name,chara_id): hit=True
            elif info.kind=='thumb' and _thumb_matches(name,chara_id): hit=True
        (selected if hit else skipped).append((name, info.kind))
    return selected, skipped


def search_catalog(query: str, catalog_path: Path, filtered=True):
    names = extract_bundle_names(catalog_path)
    if not filtered: return [(x, classify_resource(x)[0]) for x in names], []
    return filter_relevant(names, query)


def canonical_catalog_path(resource_name: str) -> str:
    """Map a catalog bundle name to the concrete CDN resource path.

    Novel BGM records are handled explicitly because the Addressables catalog uses
    a flattened key while the CDN path contains ``assets/project`` as a real
    directory boundary. The companion AWB may be a bare filename in the catalog.
    """
    raw = str(resource_name).replace('\\', '/').lstrip('/')
    low = raw.lower()

    # Bare CRI Novel BGM AWB companion.
    if re.fullmatch(r'bgm\d+\.awb_[0-9a-f]+\.bundle', low, re.I):
        return (
            'general-sound-cri_assets_assets/project/'
            'lazyassets/general/sound/cri/webgl/workunit_novel_bgm/' + raw
        )

    # Explicit flattened General Novel BGM ACB/AWB records.
    m = re.fullmatch(
        r'general-sound-cri_assets_assets_project_lazyassets_general_sound_cri_webgl_workunit_novel_bgm_(.+)',
        raw, re.I,
    )
    if m:
        filename = m.group(1)
        return (
            'general-sound-cri_assets_assets/project/'
            'lazyassets/general/sound/cri/webgl/workunit_novel_bgm/' + filename
        )

    # Explicit flattened General Novel SE records.
    m = re.fullmatch(
        r'general-sound-cri_assets_assets_project_lazyassets_general_sound_cri_webgl_workunit_novel_se_(.+)',
        raw, re.I,
    )
    if m:
        filename = m.group(1)
        return (
            'general-sound-cri_assets_assets/project/'
            'lazyassets/general/sound/cri/webgl/workunit_novel_se/' + filename
        )

    # Explicit flattened General Novel Voice records.
    m = re.fullmatch(
        r'general-sound-cri_assets_assets_project_lazyassets_general_sound_cri_webgl_workunit_novel_voice_(.+)',
        raw, re.I,
    )
    if m:
        filename = m.group(1)
        return (
            'general-sound-cri_assets_assets/project/'
            'lazyassets/general/sound/cri/webgl/workunit_novel_voice/' + filename
        )

    # General fallback for other catalog entries: restore the known project
    # directory boundary, then preserve the remaining flattened path.
    marker = '_assets_assets_project_lazyassets_'
    idx = low.find(marker)
    if idx >= 0:
        return (
            raw[:idx] + '_assets_assets/project/' +
            'lazyassets/' + raw[idx + len(marker):].replace('_', '/')
        )
    return raw

def make_url(resource_name: str, base_url: str | None = None):
    root = (base_url or BASE_URL).rstrip('/') + '/'
    path = canonical_catalog_path(resource_name)
    # Encode each URL path segment, never the separators.
    encoded = '/'.join(urllib.parse.quote(part, safe='-_.()[]@') for part in path.split('/'))
    return root + encoded


_DOWNLOAD_LOCK = threading.Lock()

def download_one(url: str, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Fast local existence check before creating a network request.
    if dst.exists() and dst.stat().st_size > 0:
        return 'exists'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'})
    tmp = dst.with_suffix(dst.suffix + '.part')
    # A stale partial file is never treated as a valid download.
    try:
        if tmp.exists() and tmp.stat().st_size > 0:
            tmp.unlink()
    except OSError:
        pass
    with urllib.request.urlopen(req, timeout=90) as r, open(tmp, 'wb') as f:
        while True:
            chunk = r.read(1024*1024)
            if not chunk: break
            f.write(chunk)
    if tmp.stat().st_size <= 0:
        try: tmp.unlink()
        except OSError: pass
        raise IOError(f'empty download: {url}')
    tmp.replace(dst)
    return 'downloaded'

def download_many(items, max_workers=8):
    """Download independent bundles concurrently, skipping already-existing files."""
    results = [None] * len(items)
    workers = max(1, min(int(max_workers), len(items) or 1))
    if workers == 1:
        for i, (url, dst) in enumerate(items):
            try: results[i] = download_one(url, dst)
            except Exception as exc: results[i] = f'failed:{exc}'
        return results
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='bundle-dl') as pool:
        future_map = {pool.submit(download_one, url, dst): i for i, (url, dst) in enumerate(items)}
        for fut in as_completed(future_map):
            i = future_map[fut]
            try: results[i] = fut.result()
            except Exception as exc: results[i] = f'failed:{exc}'
    return results



def find_bgm_pair(resource_id: str, catalog_path: Path = DEFAULT_CATALOG):
    """Return the exact CRI BGM companion set: matching ACB + AWB only."""
    rid = str(resource_id).strip().lower()
    names = extract_bundle_names(catalog_path)
    acb, awb = [], []
    for name in names:
        low=name.lower()
        if '_dummy.' in low or '.dummy.' in low or low.endswith('_dummy.bundle'):
            continue
        info=classify_bundle(name)
        if info.kind != 'bgm' or info.token != rid:
            continue
        if '.acb_' in low:
            acb.append(name)
        elif '.awb_' in low:
            awb.append(name)
    return [(''.join([x]), 'bgm') for x in sorted(dict.fromkeys(acb), key=str.lower) + sorted(dict.fromkeys(awb), key=str.lower)]

def find_resource_bundle(resource_id: str, catalog_path: Path = DEFAULT_CATALOG):
    """Return exact catalog bundles for one concrete BG/BGM/SE resource.

    The catalog can contain paired `_dummy` bundles for some SE assets. They are
    fallback/test placeholders, not the requested playable asset, so they are
    excluded at the query stage instead of being downloaded and filtered later.
    """
    rid = str(resource_id).strip().lower()
    names = extract_bundle_names(catalog_path)
    matches = []
    for name in names:
        kind, token = classify_resource(name)
        if kind not in ('background', 'se', 'bgm') or token != rid:
            continue
        low = name.lower()
        if '_dummy.' in low or '.dummy.' in low or low.endswith('_dummy.bundle'):
            continue
        matches.append((name, kind))
    return matches


def ensure_resource_bundle(resource_id: str, catalog_path: Path = DEFAULT_CATALOG, output_dir: Path = DEFAULT_OUTPUT, request_id: str | None = None):
    """Download exact catalog bundles. BGM ACB/AWB are handled as an atomic pair.

    Directory ownership follows the caller's input/request ID. Every bundle belongs
    to raw_bundles/<request-id>/; dependency recovery never creates a second-level
    resource-id directory.
    """
    rid = str(resource_id).strip().lower()
    folder_id = str(request_id or resource_id).strip()
    resource_dir = Path(output_dir) / folder_id
    resource_dir.mkdir(parents=True, exist_ok=True)

    if resource_id_info(rid) == 'bgm':
        matches = find_bgm_pair(rid, catalog_path)
        results = []
        for name, kind in matches:
            dst = resource_dir / name
            url = make_url(name)
            try:
                status = download_one(url, dst)
            except Exception as exc:
                status = f'failed:{exc}'
                print(f'[WARN] BGM bundle download failed: {name}')
                print(f'       URL: {url}')
                print(f'       ERROR: {exc}')
            results.append((name, kind, status, dst))
        return results

    matches = find_resource_bundle(rid, catalog_path)
    items = [(make_url(name), resource_dir / name) for name, _kind in matches]
    statuses = download_many(items, max_workers=8)
    return [(name, kind, status, resource_dir / name) for (name, kind), status in zip(matches, statuses)]

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('query')
    ap.add_argument('--catalog', default=str(DEFAULT_CATALOG))
    ap.add_argument('--output', default=str(DEFAULT_OUTPUT))
    ap.add_argument('--list-only', action='store_true')
    ap.add_argument('--version', default=None, help='CDN 版本字段，例如 8374；示例: https://api.abyss-prod-r18.dotabyss.dmmgames.com/resources/webgl/r18/aas/8374/aa/')
    ap.add_argument('--base-url', default=None, help=argparse.SUPPRESS)
    ap.add_argument('--all-matches', action='store_true')
    args = ap.parse_args(argv)
    global BASE_URL
    if args.version:
        BASE_URL = build_base_url(args.version)
    elif args.base_url:
        BASE_URL = args.base_url.rstrip('/') + '/'
    else:
        raise SystemExit('必须提供版本字段。示例: 8374  -> https://api.abyss-prod-r18.dotabyss.dmmgames.com/resources/webgl/r18/aas/8374/aa/')
    catalog = Path(args.catalog)
    if not catalog.exists(): raise SystemExit(f'找不到 Catalog: {catalog}')
    mode, chara, target = parse_role_query(args.query)
    all_names = extract_bundle_names(catalog)
    selected, skipped = filter_relevant(all_names, args.query)
    if args.all_matches: selected = [(x, classify_resource(x)[0]) for x in all_names]
    print(f'[CATALOG] query={args.query} mode={mode} selected={len(selected)} total={len(all_names)}')
    for i,(name,kind) in enumerate(selected,1): print(f'{i:03d}. [{kind}] {name}\n     {make_url(name)}')
    if args.list_only: return 0
    if not selected: return 2
    out=Path(args.output) / str(args.query).strip(); out.mkdir(parents=True, exist_ok=True)
    items=[(make_url(name), out / name) for name, _kind in selected]
    statuses=download_many(items, max_workers=8)
    failed=0
    for (name,kind),status in zip(selected,statuses):
        if str(status).startswith('failed:'): failed += 1
        print(f'[GET] [{kind}] [{status}] {name}')
    print(f'[DONE] attempted={len(selected)} failed={failed} folder={out}')
    return 1 if failed else 0

if __name__ == '__main__': raise SystemExit(main())
