# -*- coding: utf-8 -*-
"""After story.json generation, recover and classify referenced BG/BGM/SE.

The authoritative classification is the supplied directory tree:
- data/backgrounds/novel/<abg>.png
- data/audio/se/decoded/misc/<se>.ogg + index.json
- data/audio/bgm/decoded/<bgm>.ogg (canonical shared pool)
- legacy data_r18_all/stories/<story>/audio/decoded/bgm/<bgm>.ogg remains readable
"""
from __future__ import annotations
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tool_paths import project_root, raw_bundles_dir
from collections import OrderedDict

from catalog_downloader import (
    DEFAULT_CATALOG, ensure_resource_bundle, find_resource_bundle, find_bgm_pair,
    make_url, download_many, migrate_legacy_bundle_cache,
)
import legacy_core as legacy
from common_audio import audio_paths
from parallel_utils import IO_WORKERS, workers_for

ROOT = project_root()
RAW = raw_bundles_dir()
CATALOG = DEFAULT_CATALOG
DATA = ROOT / 'data'
DATA_R18 = ROOT / 'data_r18_all'
# Common/shared assets are served by the player's ./data/ root.
# Only story-private assets (e.g. per-story BGM/voice) belong under data_r18_all.
BG_ROOT = DATA / 'backgrounds' / 'novel'
SE_ROOT = DATA / 'audio' / 'se' / 'decoded' / 'misc'
SE_INDEX = DATA / 'audio' / 'se' / 'index.json'
BGM_ROOT = DATA / 'audio' / 'bgm' / 'decoded'
BGM_INDEX = DATA / 'audio' / 'bgm' / 'index.json'

BG_RE = re.compile(r'\babg\d+[a-z]?\b', re.I)
BGM_RE = re.compile(r'\bbgm_?\d+\b', re.I)
SE_RE = re.compile(r'\bse\d+\b', re.I)

_INDEX_LOCK = threading.RLock()
_RESOURCE_LOCKS_GUARD = threading.Lock()
_RESOURCE_LOCKS = {}


def _resource_lock(kind: str, rid: str):
    key=(str(kind).lower(),str(rid).lower())
    with _RESOURCE_LOCKS_GUARD:
        lock=_RESOURCE_LOCKS.get(key)
        if lock is None:
            lock=threading.Lock(); _RESOURCE_LOCKS[key]=lock
        return lock



def is_real_ogg(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 4:
            return False
        with path.open('rb') as f:
            return f.read(4) == b'OggS'
    except Exception:
        return False


def ensure_ogg_file(payload: bytes | bytearray, output_path: Path) -> bool:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Never accept an existing file solely because it has .ogg suffix.
    if is_real_ogg(output_path):
        return True
    try:
        if output_path.exists():
            output_path.unlink()
    except Exception:
        pass
    import legacy_core as legacy
    ok = legacy.transcode_to_ogg(bytearray(payload), output_path)
    return bool(ok and is_real_ogg(output_path))


def _unique(items):
    out=[]; seen=set()
    for x in items:
        x=x.lower()
        if x not in seen:
            seen.add(x); out.append(x)
    return out


def scan_story_json(story_json: Path):
    """Scan the completed story.json commands only."""
    data=json.loads(story_json.read_text(encoding='utf-8'))
    bgs=[]; bgms=[]; ses=[]
    for script in data.get('scripts', []):
        for cmd_obj in script.get('commands', []):
            cmd=str(cmd_obj.get('command','')).lower()
            args=cmd_obj.get('args') or []
            if cmd == 'bg' and args:
                bgs.extend(BG_RE.findall(str(args[0])))
            elif cmd == 'bgmplay':
                for a in args: bgms.extend(BGM_RE.findall(str(a)))
            elif cmd in ('seplay','asyncseplay'):
                for a in args: ses.extend(SE_RE.findall(str(a)))
    return _unique(bgs), _unique(bgms), _unique(ses)


def _extract_images_from_bundle(bundle: Path):
    import UnityPy
    env=UnityPy.load(str(bundle))
    found=[]
    for obj in env.objects:
        if obj.type.name not in ('Texture2D','Sprite'):
            continue
        try:
            data=obj.read()
            image=getattr(data,'image',None)
            if image is None:
                continue
            name=str(getattr(data,'name','') or getattr(obj,'name','') or '')
            found.append((name,image))
        except Exception:
            continue
    return found


def _extract_audio_candidates(bundle: Path):
    import UnityPy
    env=UnityPy.load(str(bundle))
    blobs=[]
    def find_acb(data):
        if isinstance(data,(bytes,bytearray)):
            return bytearray(data) if data.startswith(b'@UTF') else None
        if isinstance(data,list):
            if len(data)>=4 and data[:4]==[64,85,84,70]:
                return bytearray(data)
            for v in data:
                r=find_acb(v)
                if r is not None: return r
        if isinstance(data,dict):
            for v in data.values():
                r=find_acb(v)
                if r is not None: return r
        return None
    for obj in env.objects:
        if obj.type.name != 'MonoBehaviour':
            continue
        try:
            tree=obj.read_typetree()
        except Exception:
            try: tree=obj.parse_as_dict()
            except Exception: continue
        acb=find_acb(tree)
        if acb:
            blobs.append(acb)
    return blobs



def _split_embedded_audio_payloads(acb: bytes | bytearray):
    """Return embedded audio payloads exactly as the legacy Voice/BGV slicer does."""
    starts=[]
    i=0
    limit=len(acb)-8
    while i < limit:
        if acb[i:i+4] == b'RIFF':
            starts.append((i,'.wav')); i += 4; continue
        if acb[i:i+4] == b'OggS':
            if i+5 < len(acb) and (acb[i+5] & 0x02):
                starts.append((i,'.ogg'))
            i += 4; continue
        if acb[i:i+4] == b'ftyp':
            starts.append((max(0,i-4),'.m4a')); i += 4; continue
        i += 1
    out=[]
    for idx,(start,ext) in enumerate(starts):
        end=starts[idx+1][0] if idx+1 < len(starts) else len(acb)
        if end > start:
            out.append((acb[start:end],ext))
    return out

def extract_se_bundle(bundle: Path, se_id: str):
    """Extract the concrete SE cue into the global directory.

    A SE bundle is normally a one-cue ACB. We therefore extract the first
    embedded playable payload; if the bundle has several payloads, write them
    deterministically with a suffix and keep the requested id as the first file.
    """
    for acb in _extract_audio_candidates(bundle):
        # Reuse the proven byte splitter from legacy_core, but write to a
        # temporary story directory and then promote the requested cue.
        tmp_story=DATA_R18 / '_se_extract_tmp'
        if tmp_story.exists():
            import shutil; shutil.rmtree(tmp_story, ignore_errors=True)
        tmp_story.mkdir(parents=True, exist_ok=True)
        try:
            # Split embedded streams exactly like the existing audio pipeline.
            starts=[]; i=0
            while i < len(acb)-8:
                if acb[i:i+4] == b'RIFF': starts.append((i,'.wav')); i+=4; continue
                if acb[i:i+4] == b'OggS':
                    if i+5 < len(acb) and (acb[i+5] & 0x02): starts.append((i,'.ogg'))
                    i+=4; continue
                if acb[i:i+4] == b'ftyp': starts.append((max(0,i-4),'.m4a')); i+=4; continue
                i+=1
            if not starts:
                continue
            start,_=starts[0]
            end=starts[1][0] if len(starts)>1 else len(acb)
            payload=acb[start:end]
            out=SE_ROOT / f'{se_id}.ogg'
            if ensure_ogg_file(payload, out):
                _update_se_index(se_id,out)
                return out
            print(f'[WARN] SE {se_id}: OGG 转码失败，不写入非 OGG 回退文件')
            return None
        finally:
            if tmp_story.exists():
                import shutil; shutil.rmtree(tmp_story, ignore_errors=True)
    return None


def _update_se_index(se_id: str, path: Path):
    # Several dependency workers may finish at once. Serialize the tiny JSON
    # read/modify/write section so parallel extraction cannot lose entries.
    with _INDEX_LOCK:
        SE_INDEX.parent.mkdir(parents=True, exist_ok=True)
        data={'cues':{}}
        if SE_INDEX.exists():
            try: data=json.loads(SE_INDEX.read_text(encoding='utf-8'))
            except Exception: pass
        cues=data.setdefault('cues',{})
        cues[se_id]={
            'name':se_id, 'category':'se',
            'path':path.relative_to(DATA).as_posix(), 'source':'', 'subsong':1,
            'duration':0.0, 'sampleRate':48000, 'channels':1,
            'encoding':'decoded', 'bytes':path.stat().st_size,
        }
        SE_INDEX.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')

def extract_background(bundle: Path, bg_id: str):
    BG_ROOT.mkdir(parents=True, exist_ok=True)
    target=BG_ROOT / f'{bg_id}.png'
    if target.exists() and target.stat().st_size>0:
        return target
    for name,image in _extract_images_from_bundle(bundle):
        leaf=Path(name).stem.lower()
        if leaf == bg_id.lower() or bg_id.lower() in bundle.name.lower():
            image.save(target)
            return target
    # fallback: first image in the exact bg bundle
    images=_extract_images_from_bundle(bundle)
    if images:
        images[0][1].save(target)
        return target
    return None


def _update_bg_index(bg_id: str, path: Path):
    idx=BG_ROOT/'index.json'
    with _INDEX_LOCK:
        data={'backgrounds':{}}
        if idx.exists():
            try: data=json.loads(idx.read_text(encoding='utf-8'))
            except Exception: pass
        try:
            from PIL import Image
            with Image.open(path) as im:
                w,h=im.size
        except Exception:
            w=h=0
        data.setdefault('backgrounds',{})[bg_id]={
            'id':bg_id, 'path':path.relative_to(DATA).as_posix(), 'width':w, 'height':h
        }
        idx.parent.mkdir(parents=True,exist_ok=True)
        idx.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')

def _split_embedded_audio_payloads(acb: bytes | bytearray):
    """Same byte-level splitter used by the existing Voice/BGV pipeline."""
    starts=[]; i=0
    limit=len(acb)-8
    while i < limit:
        if acb[i:i+4] == b'RIFF': starts.append((i,'.wav')); i += 4; continue
        if acb[i:i+4] == b'OggS':
            if i+5 < len(acb) and (acb[i+5] & 0x02): starts.append((i,'.ogg'))
            i += 4; continue
        if acb[i:i+4] == b'ftyp': starts.append((max(0,i-4),'.m4a')); i += 4; continue
        i += 1
    out=[]
    for idx,(start,ext) in enumerate(starts):
        end=starts[idx+1][0] if idx+1 < len(starts) else len(acb)
        if end > start: out.append((acb[start:end],ext))
    return out

def _find_acb_from_unity_bundle(bundle: Path):
    """Same UnityPy -> MonoBehaviour -> @UTF discovery used by Voice/BGV."""
    try:
        import UnityPy
        env=UnityPy.load(str(bundle))
    except Exception:
        return []
    def find_acb(data):
        if isinstance(data,(bytes,bytearray)):
            return bytearray(data) if data.startswith(b'@UTF') else None
        if isinstance(data,list):
            if len(data)>=4 and data[:4]==[64,85,84,70]: return bytearray(data)
            for v in data:
                r=find_acb(v)
                if r is not None: return r
        elif isinstance(data,dict):
            for v in data.values():
                r=find_acb(v)
                if r is not None: return r
        return None
    blobs=[]
    for obj in env.objects:
        if getattr(obj.type,'name','')!='MonoBehaviour': continue
        try: tree=obj.read_typetree()
        except Exception:
            try: tree=obj.parse_as_dict()
            except Exception: continue
        acb=find_acb(tree)
        if acb: blobs.append(acb)
    return blobs

def _iter_bytes_recursive(value, _seen=None):
    if _seen is None:
        _seen=set()
    oid=id(value)
    if oid in _seen:
        return
    _seen.add(oid)
    if isinstance(value, (bytes, bytearray)):
        yield bytes(value)
        return
    if isinstance(value, str):
        return
    if isinstance(value, dict):
        for v in value.values():
            yield from _iter_bytes_recursive(v, _seen)
        return
    if isinstance(value, (list, tuple)):
        for v in value:
            yield from _iter_bytes_recursive(v, _seen)
        return
    # UnityPy object readers often expose bytes in typed fields rather than as the
    # top-level object. Inspect a bounded __dict__ recursively without depending on
    # a concrete UnityPy class implementation.
    d=getattr(value, '__dict__', None)
    if isinstance(d, dict):
        for v in d.values():
            yield from _iter_bytes_recursive(v, _seen)


def _extract_audio_payloads_from_unity_objects(bundle: Path):
    """Generic UnityPy object-level extraction for BGM bundles.

    BGM bundles such as workunit_novel_bgm_bgm0043.acb are UnityFS containers;
    their outer bytes contain @UTF/AFS2 markers but not necessarily the actual
    M4A payload. This pass inspects loaded Unity objects and nested byte fields.
    """
    try:
        import UnityPy
        env=UnityPy.load(str(bundle))
    except Exception:
        return []
    payloads=[]
    seen=set()
    for obj in getattr(env, 'objects', []):
        try:
            data=obj.read()
        except Exception:
            data=None
        values=[]
        if data is not None:
            values.append(data)
        try:
            tree=obj.read_typetree()
            if tree is not None: values.append(tree)
        except Exception:
            pass
        for root in values:
            for raw in _iter_bytes_recursive(root):
                for sig in (b'ftyp', b'OggS', b'RIFF', b'HCA', b'AFS2'):
                    idx=raw.find(sig)
                    if idx < 0: continue
                    if sig == b'ftyp': start=max(0,idx-4)
                    elif sig == b'AFS2': start=idx
                    else: start=idx
                    chunk=raw[start:]
                    key=(len(chunk), chunk[:32])
                    if key in seen: continue
                    seen.add(key)
                    payloads.append(chunk)
                    break
    return payloads


def _transcode_bgm_payload(payload: bytes, output_path: Path) -> bool:
    try:
        clean=legacy.extract_m4a_from_raw_awb_bytes(payload)
        if not clean: return False
        return bool(legacy.transcode_to_ogg(bytearray(clean), output_path) and is_real_ogg(output_path))
    except Exception:
        return False

def _recover_bgm_from_bundle(bundle: Path, rid: str, output: Path) -> bool:
    """Recover BGM using all real paths used by the existing pipeline.

    Pass 1: raw Bundle/AWB byte extraction.
    Pass 2: Voice/BGV-style UnityPy -> MonoBehaviour -> @UTF extraction.
    Pass 3: generic UnityPy object-field extraction for UnityFS BGM bundles.
    """
    try:
        raw=bundle.read_bytes()
    except Exception:
        raw=b''
    if raw and _transcode_bgm_payload(raw, output):
        return True
    try:
        if output.exists(): output.unlink()
    except OSError: pass

    # Proven Voice/BGV path.
    for acb in _find_acb_from_unity_bundle(bundle):
        for payload,_ext in _split_embedded_audio_payloads(acb):
            if _transcode_bgm_payload(payload, output):
                return True
            try:
                if output.exists(): output.unlink()
            except OSError: pass

    # BGM-specific UnityFS object path. Do not assume the outer .bundle contains
    # the M4A; the uploaded bgm0043 sample only exposes @UTF/AFS2 at the outer level.
    for payload in _extract_audio_payloads_from_unity_objects(bundle):
        if _transcode_bgm_payload(payload, output):
            return True
        try:
            if output.exists(): output.unlink()
        except OSError: pass
    return False

def _existing_dependency(kind, rid, story_id):
    """Return an already-materialized dependency without touching the network."""
    if kind == 'background':
        p = BG_ROOT / f'{rid}.png'
        return ('exists', p) if p.is_file() and p.stat().st_size > 0 else (None, None)
    if kind == 'bgm':
        p = BGM_ROOT / f'{rid}.ogg'
        if is_real_ogg(p):
            return 'exists', p
        legacy_p = DATA_R18 / 'stories' / story_id / 'audio' / 'decoded' / 'bgm' / f'{rid}.ogg'
        if is_real_ogg(legacy_p):
            # Promote a valid old per-story BGM into the canonical shared pool.
            # This is much cheaper than downloading/transcoding the same cue again.
            try:
                import shutil
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy_p, p)
                if is_real_ogg(p):
                    return 'promoted-legacy', p
            except Exception:
                pass
            return 'exists-legacy', legacy_p
        return None, None
    if kind == 'se':
        p = SE_ROOT / f'{rid}.ogg'
        return ('exists', p) if is_real_ogg(p) else (None, None)
    return None, None


def _catalog_matches(kind, rid):
    if kind == 'bgm':
        return find_bgm_pair(rid, CATALOG)
    return find_resource_bundle(rid, CATALOG)


def _cached_bundle_rows(kind, rid):
    """Resolve exact Catalog bundles from the shared flat cache only."""
    rows=[]
    for name,bkind in _catalog_matches(kind,rid):
        dst=RAW/name
        status='exists' if dst.is_file() and dst.stat().st_size > 0 else 'missing-cache'
        rows.append((name,bkind,status,dst))
    return rows


def _materialize_dependency_unlocked(kind, rid, story_id, request_id=None, allow_download=True):
    status_existing, path_existing = _existing_dependency(kind, rid, story_id)
    if status_existing:
        return status_existing, path_existing

    results = (ensure_resource_bundle(rid, CATALOG, RAW, request_id=request_id)
               if allow_download else _cached_bundle_rows(kind, rid))
    if not results:
        return 'not-found', None
    ordered=sorted(
        results,
        key=lambda row: (
            1 if '_dummy.' in row[0].lower() or '.dummy.' in row[0].lower() else 0,
            0 if (kind == 'bgm' and '.awb_' in row[0].lower()) else 1,
            row[0].lower(),
        )
    )
    output=None
    if kind == 'bgm':
        awb_files = [dst for bundle_name, _bkind, status, dst in ordered
                     if '.awb_' in bundle_name.lower()
                     and not str(status).startswith('failed')
                     and dst.exists() and dst.stat().st_size > 0]
        if not awb_files:
            print(f'[WARN] BGM {rid}: 配对 AWB 未下载到统一 Bundle 缓存 {RAW}，禁止使用 ACB 进行解析')
            return 'failed', None
    for bundle_name,bkind,status,dst in ordered:
        if status.startswith('failed'): continue
        if kind=='background':
            output=extract_background(dst,rid)
            if output:
                _update_bg_index(rid,output); break
        elif kind=='bgm':
            # A BGM is a CRI ACB/AWB pair. The ACB is metadata only; never feed
            # it (or any other Bundle) into the audio converter. Conversion input
            # is strictly the matching .awb Bundle carrying the actual payload.
            if '.awb_' not in bundle_name.lower():
                continue
            target_dir=BGM_ROOT
            target_dir.mkdir(parents=True,exist_ok=True)
            output=target_dir/f'{rid}.ogg'
            if is_real_ogg(output):
                break
            try:
                if output.exists(): output.unlink()
            except Exception: pass
            if _recover_bgm_from_bundle(dst,rid,output): break
            output=None
        elif kind=='se':
            output=extract_se_bundle(dst,rid)
            if output: break
    if kind == 'bgm' and not output:
        # One warning per requested cue, after all matching AWB candidates have been tried.
        print(f'[WARN] BGM {rid}: 无法从匹配的 AWB Bundle 提取并转换为有效 OGG')
    return ('downloaded' if output else 'failed'), output

def materialize_dependency(kind, rid, story_id, request_id=None, allow_download=True):
    # Deduplicate simultaneous requests for the same common asset within one process.
    with _resource_lock(kind, rid):
        return _materialize_dependency_unlocked(
            kind, rid, story_id, request_id=request_id, allow_download=allow_download
        )


def run_story_dependency_scan(story_root: Path, request_id=None, story_ids=None, workers=IO_WORKERS):
    """Recover all BG/BGM/SE dependencies with one global concurrent queue.

    story.json files are scanned first, duplicate cue requests are collapsed, then
    independent resources are materialized concurrently. This avoids downloading
    the same BGM/SE once per Story and is substantially faster for batch pipelines.
    """
    summary=OrderedDict()
    if not story_root.exists(): return summary
    allowed=set(str(x) for x in story_ids) if story_ids is not None else None
    dirs=sorted(p for p in story_root.iterdir() if p.is_dir() and p.name.isdigit() and (allowed is None or p.name in allowed))
    requirements={}
    jobs={}  # (kind,rid) -> representative story + users
    for story_dir in dirs:
        story_json=story_dir/'story.json'
        if not story_json.exists(): continue
        try:
            bgs,bgms,ses=scan_story_json(story_json)
        except Exception as exc:
            print(f'[WARN] dependency scan {story_dir.name}: {exc}')
            continue
        req={'background':bgs,'bgm':bgms,'se':ses}
        requirements[story_dir.name]=req
        summary[story_dir.name]={'background':{},'bgm':{},'se':{}}
        for kind, ids in req.items():
            for rid in ids:
                key=(kind,rid)
                if key not in jobs:
                    jobs[key]={'story':story_dir.name,'users':[]}
                jobs[key]['users'].append(story_dir.name)

    items=list(jobs.items())
    w=workers_for(len(items), requested=workers)
    print(f'    dependency resources={len(items)} workers={w}')

    # Canonical flow: resolve all missing public dependencies first, collapse their
    # Catalog bundles into ONE shared download queue, then decode/extract from the
    # flat cache.  This avoids the old Stage-2 "BGM missing" false warnings and
    # avoids nested per-resource downloader pools.
    migrate_legacy_bundle_cache(RAW)
    bundle_plan={}
    catalog_missing=[]
    already_ready=0
    for (kind,rid),meta in items:
        existing_status,_existing_path=_existing_dependency(kind,rid,meta['story'])
        if existing_status:
            already_ready+=1
            continue
        matches=_catalog_matches(kind,rid)
        if not matches:
            catalog_missing.append((kind,rid))
            continue
        for name,bkind in matches:
            dst=RAW/name
            if not (dst.is_file() and dst.stat().st_size>0):
                bundle_plan.setdefault(name,(name,bkind,dst))

    bundle_rows=list(bundle_plan.values())
    if bundle_rows:
        dl_workers=workers_for(len(bundle_rows), requested=workers)
        print(f'    [PREFETCH] cached-ready={already_ready} bundles={len(bundle_rows)} workers={dl_workers}')
        statuses=download_many([(make_url(name),dst) for name,_kind,dst in bundle_rows], max_workers=dl_workers)
        failed=0
        for (name,_kind,dst),status in zip(bundle_rows,statuses):
            if str(status).startswith('failed:') or not (dst.is_file() and dst.stat().st_size>0):
                failed+=1
                print(f'    [WARN] prefetch failed: {name} -> {status}')
        print(f'    [PREFETCH] complete={len(bundle_rows)-failed} failed={failed}')
    else:
        print(f'    [PREFETCH] no network download required; cached/ready={already_ready}')

    if catalog_missing:
        preview=', '.join(f'{k}:{r}' for k,r in catalog_missing[:12])
        extra='' if len(catalog_missing)<=12 else f' ... +{len(catalog_missing)-12}'
        print(f'    [WARN] Catalog has no exact bundle for {len(catalog_missing)} dependencies: {preview}{extra}')

    def one(item):
        (kind,rid),meta=item
        status,path=materialize_dependency(
            kind,rid,meta['story'],request_id=request_id,allow_download=False
        )
        return kind,rid,status,path,meta['users']

    if items:
        with ThreadPoolExecutor(max_workers=w, thread_name_prefix='story-dep') as ex:
            futs={ex.submit(one,item):item[0] for item in items}
            done=0
            for fut in as_completed(futs):
                kind,rid=futs[fut]
                try:
                    kind,rid,status,_path,users=fut.result()
                except Exception as exc:
                    status='failed'; users=jobs[(kind,rid)]['users']
                    print(f'[WARN] dependency {kind} {rid}: {exc}')
                for sid in users:
                    if sid in summary:
                        summary[sid][kind][rid]=status
                if status in ('failed','not-found'):
                    print(f'    [WARN] dependency unresolved: {kind} {rid} -> {status}')
                done+=1
                if done==len(items) or done % max(1,len(items)//10) == 0:
                    print(f'    dependency progress {done}/{len(items)}', flush=True)
    return summary

