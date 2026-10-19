# -*- coding: utf-8 -*-
"""Directory-aware catalog downloader and resource classifier."""
from __future__ import annotations
import argparse, filecmp, json, os, re, shutil, urllib.parse, urllib.request, urllib.error
import functools
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from pathlib import Path
from tool_paths import TOOL_DIR, catalog_path, raw_bundles_dir, project_root
from parallel_utils import IO_WORKERS, workers_for

DEFAULT_CATALOG = catalog_path()
DEFAULT_OUTPUT = raw_bundles_dir()
BASE_URL_PREFIX = 'https://api.abyss-prod-r18.dotabyss.dmmgames.com/resources/webgl/r18/aas/'
BASE_URL_SUFFIX = '/aa/'
DEFAULT_VERSION = ''
BASE_URL = os.environ.get('DOT_CATALOG_BASE_URL', '').strip().rstrip('/') + '/' if os.environ.get('DOT_CATALOG_BASE_URL', '').strip() else BASE_URL_PREFIX + 'VERSION_PLACEHOLDER' + BASE_URL_SUFFIX

def build_base_url(version: str) -> str:
    v = str(version or '').strip().strip('/')
    if not v:
        raise ValueError('版本字段不能为空')
    if not v.isdigit():
        raise ValueError(f'版本字段必须为纯数字，例如 8374；收到: {version}')
    return f'{BASE_URL_PREFIX}{v}{BASE_URL_SUFFIX}'


@functools.lru_cache(maxsize=8)
def _extract_bundle_names_cached(path_string: str, size: int, mtime_ns: int):
    # size/mtime are part of the cache key so replacing catalog_1.bin invalidates
    # the cache automatically without a manual clear operation.
    data = Path(path_string).read_bytes()
    out, seen = [], set()
    pattern = re.compile(rb'[A-Za-z0-9_.@()\[\]-][\x20-\x7e]{8,1200}?\.bundle')
    for m in pattern.finditer(data):
        try:
            name = m.group(0).decode('utf-8')
        except UnicodeDecodeError:
            continue
        if not re.search(r'(?:^|_)assets_assets_project_lazyassets_', name) and not re.match(r'^(?:common|event\.unity_|[A-Za-z0-9_-]+\.unity_|general-|r18-only-|tutorial-|bgm\d+\.awb_)', name):
            continue
        if name not in seen:
            seen.add(name); out.append(name)
    return tuple(out)


def extract_bundle_names(catalog_path: Path):
    catalog_path = Path(catalog_path).resolve()
    st = catalog_path.stat()
    return list(_extract_bundle_names_cached(str(catalog_path), st.st_size, st.st_mtime_ns))


from resource_classifier import classify_bundle, resource_id_info

def classify_resource(name: str):
    info = classify_bundle(name)
    return info.kind, info.token


@functools.lru_cache(maxsize=8)
def _resource_index_cached(path_string: str, size: int, mtime_ns: int):
    index={}
    for name in _extract_bundle_names_cached(path_string,size,mtime_ns):
        info=classify_bundle(name)
        token=str(info.token or '').lower()
        if not token: continue
        index.setdefault((info.kind,token),[]).append(name)
    return {k:tuple(v) for k,v in index.items()}


def resource_catalog_index(catalog_path: Path = DEFAULT_CATALOG):
    path=Path(catalog_path).resolve(); st=path.stat()
    return _resource_index_cached(str(path),st.st_size,st.st_mtime_ns)


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
    """Return the primary CDN path for an Addressables bundle.

    The WebGL CDN stores catalog bundle keys directly under ``.../aa/``.  The
    catalog key is already a flattened filename; strings such as
    ``_assets_assets_project_lazyassets_`` are part of that filename and must
    *not* be expanded back into ``assets/project/lazyassets/...`` directories.

    Keep embedded slashes only when a catalog genuinely contains them.
    """
    return str(resource_name).replace('\\', '/').lstrip('/')


def legacy_catalog_path(resource_name: str) -> str:
    """Old directory-expanded path kept only as a compatibility fallback.

    v1.0 incorrectly used this as the primary path, which turns e.g.
    ``general-sound-..._novel_se_se4300007.acb_HASH.bundle`` into a non-existent
    ``.../assets/project/lazyassets/...`` URL and can return HTTP 403.
    """
    raw = str(resource_name).replace('\\', '/').lstrip('/')
    low = raw.lower()

    if re.fullmatch(r'bgm\d+\.awb_[0-9a-f]+\.bundle', low, re.I):
        return (
            'general-sound-cri_assets_assets/project/'
            'lazyassets/general/sound/cri/webgl/workunit_novel_bgm/' + raw
        )

    families = (
        ('general-sound-cri_assets_assets_project_lazyassets_general_sound_cri_webgl_workunit_novel_bgm_',
         'general-sound-cri_assets_assets/project/lazyassets/general/sound/cri/webgl/workunit_novel_bgm/'),
        ('general-sound-cri_assets_assets_project_lazyassets_general_sound_cri_webgl_workunit_novel_se_',
         'general-sound-cri_assets_assets/project/lazyassets/general/sound/cri/webgl/workunit_novel_se/'),
        ('general-sound-cri_assets_assets_project_lazyassets_general_sound_cri_webgl_workunit_novel_voice_',
         'general-sound-cri_assets_assets/project/lazyassets/general/sound/cri/webgl/workunit_novel_voice/'),
    )
    for prefix, directory in families:
        if low.startswith(prefix):
            return directory + raw[len(prefix):]

    marker = '_assets_assets_project_lazyassets_'
    idx = low.find(marker)
    if idx >= 0:
        return (
            raw[:idx] + '_assets_assets/project/' +
            'lazyassets/' + raw[idx + len(marker):].replace('_', '/')
        )
    return raw


def _encode_cdn_path(path: str) -> str:
    return '/'.join(
        urllib.parse.quote(part, safe='-_.()[]@')
        for part in str(path).split('/')
    )


def make_url(resource_name: str, base_url: str | None = None):
    """Build the primary URL using the catalog key exactly as stored."""
    root = (base_url or BASE_URL).rstrip('/') + '/'
    return root + _encode_cdn_path(canonical_catalog_path(resource_name))


def make_url_candidates(resource_name: str, base_url: str | None = None):
    """Primary flat URL first, legacy directory form second when different."""
    root = (base_url or BASE_URL).rstrip('/') + '/'
    primary = root + _encode_cdn_path(canonical_catalog_path(resource_name))
    legacy = root + _encode_cdn_path(legacy_catalog_path(resource_name))
    return [primary] if legacy == primary else [primary, legacy]


def _fallback_url_candidates(url: str):
    """Derive compatibility URLs from a primary ``.../aa/<catalog-key>`` URL."""
    urls = [url]
    try:
        parsed = urllib.parse.urlsplit(url)
        marker = '/aa/'
        idx = parsed.path.find(marker)
        if idx < 0:
            return urls
        encoded_key = parsed.path[idx + len(marker):]
        key = urllib.parse.unquote(encoded_key)
        legacy = legacy_catalog_path(key)
        if legacy == canonical_catalog_path(key):
            return urls
        legacy_path = parsed.path[:idx + len(marker)] + _encode_cdn_path(legacy)
        fallback = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, legacy_path, parsed.query, parsed.fragment))
        if fallback not in urls:
            urls.append(fallback)
    except Exception:
        pass
    return urls


_CACHE_MIGRATION_GUARD = threading.Lock()
_CACHE_MIGRATED = set()

def migrate_legacy_bundle_cache(output_dir: Path = DEFAULT_OUTPUT):
    """Flatten old request-scoped caches and move player-root caches into the maintainer."""
    out=Path(output_dir).resolve(); out.mkdir(parents=True,exist_ok=True)
    key=str(out).lower()
    with _CACHE_MIGRATION_GUARD:
        if key in _CACHE_MIGRATED:
            return {'moved':0,'reused':0,'conflicts':0}
        _CACHE_MIGRATED.add(key)
    sources=[]
    # Old maintainer versions used raw_bundles/<request>/...
    for p in out.rglob('*.bundle'):
        if p.parent.resolve()!=out:
            sources.append(p)
    # Older integrated versions stored raw_bundles in the player project root.
    legacy=(project_root()/'raw_bundles').resolve()
    if legacy!=out and legacy.exists():
        sources.extend(p for p in legacy.rglob('*.bundle'))
    moved=reused=conflicts=0
    seen=set()
    for src in sources:
        try:
            src=src.resolve()
            if src in seen or not src.is_file(): continue
            seen.add(src); dst=out/src.name
            if dst.exists():
                same = dst.stat().st_size==src.stat().st_size and filecmp.cmp(src,dst,shallow=False)
                if same:
                    try: src.unlink(); reused+=1
                    except OSError: pass
                else:
                    conflicts+=1
                    print(f'[CACHE CONFLICT] 同名 Bundle 内容不同，保留旧文件且不覆盖: {src}', flush=True)
                continue
            try:
                shutil.move(str(src),str(dst)); moved+=1
            except Exception:
                shutil.copy2(src,dst); moved+=1
                try: src.unlink()
                except OSError: pass
        except OSError:
            continue
    for base in (out, legacy if legacy!=out else None):
        if not base or not Path(base).exists(): continue
        dirs=sorted((d for d in Path(base).rglob('*') if d.is_dir()),key=lambda d:len(d.parts),reverse=True)
        for d in dirs:
            try: d.rmdir()
            except OSError: pass
    if moved or reused or conflicts:
        print(f'[CACHE MIGRATE] moved={moved} reused={reused} conflicts={conflicts} -> {out}', flush=True)
    return {'moved':moved,'reused':reused,'conflicts':conflicts}


_DOWNLOAD_LOCK = threading.Lock()

def download_one(url: str, dst: Path, retries: int = 3):
    """Download with retry, resume, and CDN path compatibility fallback.

    The exact flat catalog URL is always attempted first.  HTTP 403/404 on that
    URL immediately tries the legacy directory-expanded form for older resource
    layouts instead of retrying the same broken URL three times.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size > 0:
        return 'exists'
    tmp = dst.with_suffix(dst.suffix + '.part')
    last_error = None
    candidates = _fallback_url_candidates(url)

    for candidate_index, candidate_url in enumerate(candidates):
        for attempt in range(max(1, int(retries))):
            resume_at = tmp.stat().st_size if tmp.exists() else 0
            headers = {'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'}
            if resume_at > 0:
                headers['Range'] = f'bytes={resume_at}-'
            req = urllib.request.Request(candidate_url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    status = getattr(r, 'status', None) or r.getcode()
                    append = resume_at > 0 and int(status) == 206
                    mode = 'ab' if append else 'wb'
                    with open(tmp, mode) as f:
                        while True:
                            chunk = r.read(1024 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                if not tmp.exists() or tmp.stat().st_size <= 0:
                    raise IOError(f'empty download: {candidate_url}')
                tmp.replace(dst)
                if candidate_index:
                    return 'downloaded:fallback'
                return 'resumed' if resume_at and append else 'downloaded'
            except urllib.error.HTTPError as exc:
                last_error = exc
                # A path miss should try the alternate URL immediately.
                if exc.code in (403, 404):
                    break
                if attempt + 1 >= max(1, int(retries)):
                    break
                time.sleep(min(1.5 * (attempt + 1), 4.0))
            except Exception as exc:
                last_error = exc
                if attempt + 1 >= max(1, int(retries)):
                    break
                time.sleep(min(1.5 * (attempt + 1), 4.0))

    raise IOError(f'{last_error}')


def download_many(items, max_workers=IO_WORKERS):
    """Download independent bundles concurrently, skipping already-existing files."""
    results = [None] * len(items)
    workers = workers_for(len(items), requested=max_workers)
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
    """Return the exact CRI BGM companion set from the cached Catalog index."""
    rid=str(resource_id).strip().lower(); names=resource_catalog_index(catalog_path).get(('bgm',rid),())
    acb=[]; awb=[]
    for name in names:
        low=name.lower()
        if '_dummy.' in low or '.dummy.' in low or low.endswith('_dummy.bundle'): continue
        if '.acb_' in low: acb.append(name)
        elif '.awb_' in low: awb.append(name)
    return [(x,'bgm') for x in sorted(dict.fromkeys(acb),key=str.lower)+sorted(dict.fromkeys(awb),key=str.lower)]


def find_resource_bundle(resource_id: str, catalog_path: Path = DEFAULT_CATALOG):
    """Return exact BG/BGM/SE bundles using the preclassified Catalog cache."""
    rid=str(resource_id).strip().lower(); info=resource_id_info(rid)
    kinds={'background':('background',),'se':('se',),'bgm':('bgm',)}.get(info,('background','se','bgm'))
    index=resource_catalog_index(catalog_path); matches=[]
    for kind in kinds:
        for name in index.get((kind,rid),()):
            low=name.lower()
            if '_dummy.' in low or '.dummy.' in low or low.endswith('_dummy.bundle'): continue
            matches.append((name,kind))
    return matches

def ensure_resource_bundle(resource_id: str, catalog_path: Path = DEFAULT_CATALOG, output_dir: Path = DEFAULT_OUTPUT, request_id: str | None = None):
    """Download exact catalog bundles into one shared flat cache.

    ``request_id`` is accepted for backward compatibility but intentionally ignored.
    The catalog bundle filename itself is the cache key, so every feature can reuse
    already-downloaded bytes without searching task subdirectories.
    """
    resource_dir = Path(output_dir)
    migrate_legacy_bundle_cache(resource_dir)
    rid = str(resource_id).strip().lower()
    resource_dir.mkdir(parents=True, exist_ok=True)

    if resource_id_info(rid) == 'bgm':
        matches = find_bgm_pair(rid, catalog_path)
    else:
        matches = find_resource_bundle(rid, catalog_path)
    items = [(make_url(name), resource_dir / name) for name, _kind in matches]
    statuses = download_many(items, max_workers=IO_WORKERS)
    results = []
    for (name, kind), status in zip(matches, statuses):
        dst = resource_dir / name
        if str(status).startswith('failed:'):
            print(f'[WARN] bundle download failed: {name}')
            print(f'       URL: {make_url(name)}')
            print(f'       ERROR: {str(status)[7:]}')
        results.append((name, kind, status, dst))
    return results


def write_manifest(path: Path, rows, *, queries=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'version': 1,
        'cacheDir': str(Path(DEFAULT_OUTPUT).resolve()),
        'queries': list(queries or []),
        'bundles': [
            {'name': name, 'kind': kind, 'path': str(Path(dst).resolve()), 'status': str(status)}
            for (name, kind, status, dst) in rows
            if not str(status).startswith('failed:')
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return path

def _emit_progress(percent, stage, detail='', current=None, total=None):
    import json
    payload={'percent':float(percent),'stage':str(stage),'detail':str(detail)}
    if current is not None: payload['current']=int(current)
    if total is not None: payload['total']=int(total)
    print('@@DOT_PROGRESS\t'+json.dumps(payload,ensure_ascii=False), flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('query', nargs='+', help='一个或多个角色/剧情/资源 ID')
    ap.add_argument('--catalog', default=str(DEFAULT_CATALOG))
    ap.add_argument('--output', default=str(DEFAULT_OUTPUT), help='统一 Bundle 缓存目录；默认维护器目录/raw_bundles')
    ap.add_argument('--request-id', default=None, help=argparse.SUPPRESS)
    ap.add_argument('--manifest', default=None, help='把本次实际选中的 Bundle 清单写入 JSON，供后续流水线精确处理')
    ap.add_argument('--kinds', default='', help='可选资源类型过滤，逗号分隔，如 story,voice,bgv,l2d_core,l2d_motion,l2d_fade')
    ap.add_argument('--workers', type=int, default=IO_WORKERS, help=f'下载并发，默认 {IO_WORKERS}')
    ap.add_argument('--list-only', action='store_true')
    ap.add_argument('--version', default=None, help='CDN 版本字段，例如 8374；示例: https://api.abyss-prod-r18.dotabyss.dmmgames.com/resources/webgl/r18/aas/8374/aa/')
    ap.add_argument('--base-url', default=None, help=argparse.SUPPRESS)
    ap.add_argument('--all-matches', action='store_true')
    args = ap.parse_args(argv)
    global BASE_URL
    if args.base_url:
        BASE_URL = args.base_url.rstrip('/') + '/'
    elif os.environ.get('DOT_CATALOG_BASE_URL', '').strip():
        BASE_URL = os.environ['DOT_CATALOG_BASE_URL'].rstrip('/') + '/'
    elif args.version:
        BASE_URL = build_base_url(args.version)
    else:
        raise SystemExit('必须提供完整 Bundle 基础地址或版本字段。示例: https://api.abyss-prod-r18.dotabyss.dmmgames.com/resources/webgl/r18/aas/12394/aa/')
    catalog = Path(args.catalog)
    if not catalog.exists(): raise SystemExit(f'找不到 Catalog: {catalog}')

    wanted_kinds={x.strip().lower() for x in str(args.kinds).split(',') if x.strip()}
    queries=[]
    seen_q=set()
    for q in args.query:
        q=str(q).strip()
        if q and q.lower() not in seen_q:
            seen_q.add(q.lower()); queries.append(q)
    all_names = extract_bundle_names(catalog)
    print(f'[THREADS] download={workers_for(requested=args.workers)}')
    global_selected=[]
    seen_bundle=set()
    for qi,q in enumerate(queries,1):
        mode, _chara, _target = parse_role_query(q)
        selected, _skipped = filter_relevant(all_names, q)
        if args.all_matches:
            selected = [(x, classify_resource(x)[0]) for x in all_names]
        if wanted_kinds:
            selected=[row for row in selected if str(row[1]).lower() in wanted_kinds]
        print(f'[CATALOG] query={q} mode={mode} selected={len(selected)} total={len(all_names)}')
        for i,(name,kind) in enumerate(selected,1):
            print(f'{i:03d}. [{kind}] {name}\n     {make_url(name)}')
            if name not in seen_bundle:
                seen_bundle.add(name); global_selected.append((name,kind))
        _emit_progress(5 + 15*qi/max(1,len(queries)), 'Catalog 检索', f'{q}: {len(selected)} 个 Bundle', qi, len(queries))

    if args.list_only:
        return 0 if global_selected else 2
    if not global_selected:
        if args.manifest:
            write_manifest(Path(args.manifest), [], queries=queries)
        return 2

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    migrate_legacy_bundle_cache(out)
    items=[(make_url(name), out / name) for name,_kind in global_selected]
    total=len(items); workers=workers_for(total, requested=args.workers)
    results=[None]*total
    if total:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='bundle-dl') as pool:
            futs={pool.submit(download_one,url,dst):i for i,(url,dst) in enumerate(items)}
            done=0
            for fut in as_completed(futs):
                i=futs[fut]
                try: results[i]=fut.result()
                except Exception as exc: results[i]=f'failed:{exc}'
                done+=1
                name,kind=global_selected[i]
                print(f'[GET] [{kind}] [{results[i]}] {name}')
                _emit_progress(20 + 80*done/total, '并行下载 Bundle', name, done, total)
    rows=[(name,kind,status,out/name) for (name,kind),status in zip(global_selected,results)]
    if args.manifest:
        write_manifest(Path(args.manifest), rows, queries=queries)
        print(f'[MANIFEST] {Path(args.manifest).resolve()}')
    failed=sum(1 for x in results if str(x).startswith('failed:'))
    cached=sum(1 for x in results if str(x)=='exists')
    print(f'[DONE] selected={total} cached={cached} failed={failed} workers={workers} cache={out.resolve()}')
    return 1 if failed else 0

if __name__ == '__main__': raise SystemExit(main())
