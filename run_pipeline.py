# -*- coding: utf-8 -*-
"""Integrated R18 player asset pipeline.

Based on test(4).py. The original story/audio/thumb functions are retained;
Live2D and stand extraction are replaced by the validated modules from the
previous work in this conversation.
"""
from __future__ import annotations
import argparse
import importlib
import os
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tool_paths import project_root, raw_bundles_dir
from parallel_utils import IO_WORKERS, CPU_WORKERS, workers_for


def ensure_unitypy():
    try:
        return importlib.import_module("UnityPy")
    except ImportError:
        pass
    commands = [
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "UnityPy==1.25.3"],
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "git+https://github.com/K0lb3/UnityPy.git"],
    ]
    last_error = None
    for cmd in commands:
        try:
            print("[*] UnityPy 未安装，尝试自动安装:", " ".join(cmd))
            subprocess.check_call(cmd)
            return importlib.import_module("UnityPy")
        except Exception as exc:
            last_error = exc
    raise RuntimeError(
        "无法自动安装 UnityPy。请确认 Python/pip 可访问 PyPI 或 GitHub；"
        f"最后错误: {last_error}"
    )


UnityPy = ensure_unitypy()

import legacy_core as legacy
import player_json_builder as storymake
import story_dependency_assets as storydeps
import catalog_downloader as catalogdl
from l2d_module import process_group as process_l2d_group, get_id_from_name
from stand_module import output_character as process_stand_env

ROOT = project_root()
RAW = raw_bundles_dir()
CATALOG = ROOT / 'catalog_1.bin'
ACTIVE_REQUEST_ID = None  # deprecated compatibility only
ACTIVE_BUNDLES = None
_BUNDLE_CACHE = None
PIPELINE_WORKERS = IO_WORKERS


def infer_request_folder(name: str) -> str:
    low = str(name).lower()
    patterns = [
        r"\b(abg\d+[a-z]?)\b", r"\b(bgm\d+)\b", r"\b(se\d+)\b",
        r"\b(vbg?\w*)\b", r"hmr_(\d{11})", r"charastand(\d{6}\d{3}[gx]?)",
    ]
    for pat in patterns:
        m = re.search(pat, low, re.I)
        if m: return m.group(1)
    m = re.search(r"hmr_(\d{11})", low, re.I)
    return m.group(1) if m else 'unassigned'


def copy_inputs(paths):
    """Copy selected local Bundles into the maintainer-owned flat cache."""
    global _BUNDLE_CACHE
    RAW.mkdir(parents=True, exist_ok=True)
    jobs=[]
    for value in paths:
        src=Path(value)
        if not src.exists() or src.suffix.lower() != '.bundle':
            continue
        dst=RAW/src.name
        jobs.append((src,dst))

    def one(pair):
        src,dst=pair
        dst.parent.mkdir(parents=True,exist_ok=True)
        if src.resolve()!=dst.resolve():
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                return dst
            shutil.copy2(src,dst)
        return dst

    copied=[]
    if jobs:
        with ThreadPoolExecutor(max_workers=workers_for(len(jobs)), thread_name_prefix='bundle-copy') as ex:
            copied=list(ex.map(one,jobs))
    _BUNDLE_CACHE=None
    return copied


def load_manifest(path):
    try:
        import json
        data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except Exception as exc:
        raise RuntimeError(f'无法读取 Bundle 清单 {path}: {exc}') from exc
    out=[]; seen=set()
    for row in data.get('bundles',[]) if isinstance(data,dict) else []:
        value=row.get('path') if isinstance(row,dict) else row
        if not value: continue
        p=Path(value)
        if not p.is_absolute(): p=RAW/p
        p=p.resolve()
        if p.suffix.lower()=='.bundle' and p.is_file() and p not in seen:
            seen.add(p); out.append(p)
    return out


def all_bundles(refresh=False):
    global _BUNDLE_CACHE
    RAW.mkdir(parents=True, exist_ok=True)
    if _BUNDLE_CACHE is not None and not refresh:
        return list(_BUNDLE_CACHE)
    if ACTIVE_BUNDLES is not None:
        bundles=sorted({Path(p).resolve() for p in ACTIVE_BUNDLES if Path(p).is_file() and Path(p).suffix.lower()=='.bundle'}, key=lambda p:p.name.lower())
    else:
        bundles=sorted(RAW.glob('*.bundle'), key=lambda p:p.name.lower())
    _BUNDLE_CACHE=tuple(bundles)
    return list(_BUNDLE_CACHE)

def stage_legacy():
    bundles=all_bundles()
    legacy.script_voice_maps.clear(); legacy.script_bgm_maps.clear(); legacy.loaded_global_bgms.clear(); legacy.pending_audio_tasks.clear()
    print(f'>>> Stage 1: scan story/BGM/thumb/audio resources (workers={CPU_WORKERS})')
    script_jobs=[]; thumb_jobs=[]; audio_tasks=[]
    for bundle in bundles:
        name=bundle.name.lower()
        if re.search(r'mainchara_hmr_\d{11}_hmr_\d{11}\.txt_',name): script_jobs.append(bundle)
        elif 'bgm' in name and 'voice' not in name: pass  # handled once in Stage 5 public dependency resolver
        elif 'icon-story-s_' in name or 'thumb' in name: thumb_jobs.append(bundle)
        elif ('voice_chara' in name or 'backgroundvoice_chara' in name or '_voice' in name or '_bgv' in name):
            m=re.search(r'hmr_(\d+)',bundle.name)
            if m: audio_tasks.append((m.group(1),bundle,'backgroundvoice_chara' in name or '_bgv' in name))

    def safe_call(label, fn, arg):
        try: fn(arg); return True
        except Exception as exc:
            print(f'[WARN] {label} {getattr(arg,"name",arg)}: {exc}'); return False

    jobs=[('script',legacy.parse_script_and_cache,b) for b in script_jobs]
    jobs += [('thumb',legacy.process_story_thumb_bundle,b) for b in thumb_jobs]
    if jobs:
        with ThreadPoolExecutor(max_workers=workers_for(len(jobs),cpu_bound=True), thread_name_prefix='legacy-scan') as ex:
            list(ex.map(lambda j:safe_call(j[0],j[1],j[2]),jobs))
    legacy.pending_audio_tasks.extend(audio_tasks)

    print(f'>>> Stage 2: decode story voice/BGV (workers={CPU_WORKERS})')
    print('    [INFO] BGM is resolved later by the canonical Story dependency stage; legacy early dispatch is disabled.')
    def decode(task):
        story_id,bundle,is_bgv=task
        try:
            legacy.slice_audio_from_bundle(story_id,bundle,is_bgv); return True
        except Exception as exc:
            print(f'[WARN] audio {bundle.name}: {exc}'); return False
    if audio_tasks:
        with ThreadPoolExecutor(max_workers=workers_for(len(audio_tasks),cpu_bound=True), thread_name_prefix='audio-decode') as ex:
            list(ex.map(decode,audio_tasks))

def _is_l2d_core_bundle(name: str) -> bool:
    low = name.lower()
    if '_l2d_' not in low and '_l2d' not in low:
        return False
    # Main L2D conversion source is the model Asset/Prefab.
    # Fade/addanimation bundles are auxiliary and must not be fed into the
    # Core->Motion generator, otherwise unrelated AnimationClips can collide.
    if '_addanimations_' in low or '_animations_' in low:
        return False
    if '.fade.asset_' in low or 'fademotionlist' in low:
        return False
    return ('.asset_' in low or '.prefab_' in low)


def stage_l2d():
    groups={}
    for bundle in all_bundles():
        low=bundle.name.lower(); is_core=_is_l2d_core_bundle(bundle.name)
        is_aux=('_addanimations_' in low or re.search(r'_animations_scene\d+',low) is not None or '.fade.asset_' in low)
        if not (is_core or is_aux): continue
        try: cid=get_id_from_name(bundle.name)
        except Exception: continue
        groups.setdefault(cid,[]).append(bundle)
    items=sorted(groups.items())
    print(f'>>> Stage 3: Live2D groups={len(items)} workers={workers_for(len(items),cpu_bound=True)}')
    def one(item):
        cid,paths=item
        try:
            result=process_l2d_group(cid,sorted(paths,key=lambda x:x.name))
            motion_dir=legacy.OUTPUT_STORY_DIR/cid/'motions'
            exported={p.name[:-len('.motion3.json')] for p in motion_dir.glob('*.motion3.json')} if motion_dir.exists() else set()
            return cid,len(exported),len((result or {}).get('missing_parameter_paths') or []),None
        except Exception as exc: return cid,0,0,exc
    if items:
        with ThreadPoolExecutor(max_workers=workers_for(len(items),cpu_bound=True), thread_name_prefix='l2d') as ex:
            for cid,count,missing,err in ex.map(one,items):
                if err: print(f'[FAILED] L2D {cid}: {err}')
                else:
                    print(f'    [OK] {cid}: exported motion3={count}')
                    if missing: print(f'    [WARN] {cid}: unresolved AnimationClip bindings={missing}')

def _story_ids_from_bundles_and_output():
    ids = set()
    # 1) Discover every story id from every relevant bundle name.
    for bundle in all_bundles():
        m = re.search(r"hmr_(\d{11})", bundle.name, re.IGNORECASE)
        if m:
            ids.add(m.group(1))
    # 2) Only when no explicit bundle scope is active, preserve already materialized stories.
    # Scoped runs must never rebuild unrelated historical stories from the flat cache.
    story_root = legacy.OUTPUT_STORY_DIR
    if ACTIVE_BUNDLES is None and story_root.exists():
        for d in story_root.iterdir():
            if d.is_dir() and d.name.isdigit():
                ids.add(d.name)
    return sorted(ids)


def _reconcile_missing_story_scripts(story_ids):
    bundles=all_bundles()
    by_sid={}
    for b in bundles:
        m=re.search(r'mainchara_hmr_(\d{11})_hmr_\1\.txt_',b.name,re.I)
        if m: by_sid.setdefault(m.group(1),[]).append(b)
    def one(story_id):
        text_file=legacy.OUTPUT_STORY_DIR/story_id/'textassets'/f'hmr_{story_id}.txt'
        if text_file.is_file() and text_file.stat().st_size>0: return
        for bundle in by_sid.get(story_id,[]):
            try:
                print(f'  [*] Stage 4 补解析剧本: {story_id} <- {bundle.name}')
                legacy.parse_script_and_cache(bundle)
                if text_file.is_file() and text_file.stat().st_size>0: break
            except Exception as exc: print(f'  [WARN] Stage 4 剧本补解析失败 {bundle.name}: {exc}')
    ids=list(story_ids)
    if ids:
        with ThreadPoolExecutor(max_workers=workers_for(len(ids),cpu_bound=True), thread_name_prefix='story-script') as ex:
            list(ex.map(one,ids))

def stage_story_json():
    print('>>> Stage 4: build story.json')
    try:
        # Important: do not rely on the discovery order of earlier stages.
        story_ids = _story_ids_from_bundles_and_output()
        print(f'    discovered story IDs: {len(story_ids)}')
        _reconcile_missing_story_scripts(story_ids)

        # Rebuild from the final filesystem state, not from a stale in-memory set.
        for story_id in story_ids:
            story_dir = legacy.OUTPUT_STORY_DIR / story_id
            story_dir.mkdir(parents=True, exist_ok=True)

        def build_one(story_id):
            try: return 1 if storymake.build_story_json(legacy.OUTPUT_STORY_DIR / story_id) else 0
            except Exception as exc:
                print(f'[WARN] story.json {story_id}: {exc}'); return 0
        with ThreadPoolExecutor(max_workers=workers_for(len(story_ids)), thread_name_prefix='story-json') as ex:
            count=sum(ex.map(build_one,story_ids)) if story_ids else 0
        print(f'    generated story.json: {count} workers={workers_for(len(story_ids))}')

        # Hard verification: every discovered story must either have story.json
        # or be explicitly reported as missing its script TextAsset.
        for story_id in story_ids:
            story_json = legacy.OUTPUT_STORY_DIR / story_id / 'story.json'
            text_file = legacy.OUTPUT_STORY_DIR / story_id / 'textassets' / f'hmr_{story_id}.txt'
            if not story_json.exists() and not text_file.exists():
                print(f'  [WARN] {story_id}: no story TextAsset, story.json cannot be generated')
            elif not story_json.exists():
                print(f'  [WARN] {story_id}: story TextAsset exists but story.json generation failed')
    except Exception as e:
        print(f'[WARN] story.json generation: {e}')


def stage_story_dependencies():
    print('>>> Stage 5: recover missing Story dependencies (BG/BGM/SE)')
    try:
        current_story_ids = _story_ids_from_bundles_and_output()
        dep_summary = storydeps.run_story_dependency_scan(
            legacy.OUTPUT_STORY_DIR,
            request_id=ACTIVE_REQUEST_ID,
            story_ids=current_story_ids,
            workers=PIPELINE_WORKERS,
        )
        for sid, details in dep_summary.items():
            total = sum(len(v) for v in details.values())
            if total:
                print(f'    [DEP] {sid}: ' + ', '.join(f'{k}={len(v)}' for k,v in details.items() if v))
    except Exception as e:
        print(f'[WARN] Story dependency recovery: {e}')



def stage_finalize_story_json():
    """Rebuild only the current stories after dependency recovery.

    Global/story indexes are intentionally NOT updated here.  Index maintenance
    is a separate tool so downloading/processing never mutates unrelated indexes.
    """
    print('>>> Stage 6: finalize current story audio metadata')
    story_ids = _story_ids_from_bundles_and_output()
    def rebuild_one(story_id):
        try: return 1 if storymake.build_story_json(legacy.OUTPUT_STORY_DIR / story_id) else 0
        except Exception as exc:
            print(f'[WARN] final story.json rebuild {story_id}: {exc}'); return 0
    with ThreadPoolExecutor(max_workers=workers_for(len(story_ids)), thread_name_prefix='story-final') as ex:
        rebuilt=sum(ex.map(rebuild_one,story_ids)) if story_ids else 0
    print(f'    rebuilt story.json after dependencies: {rebuilt} workers={workers_for(len(story_ids))}')
    print('    [INFO] indexes were not touched; run the standalone index updater when needed')


def stage_stand():
    bundles=[b for b in all_bundles() if 'charastand' in b.name.lower()]
    print(f'>>> Stage 7: extract character stand portraits bundles={len(bundles)} workers={workers_for(len(bundles),cpu_bound=True)}')
    def one(bundle):
        try:
            env=UnityPy.load(str(bundle)); process_stand_env(env,bundle); return bundle.name,None
        except Exception as exc: return bundle.name,exc
    if bundles:
        with ThreadPoolExecutor(max_workers=workers_for(len(bundles),cpu_bound=True), thread_name_prefix='stand') as ex:
            for name,err in ex.map(one,bundles):
                if err: print(f'[FAILED] Stand {name}: {err}')

def main(argv=None):
    global ACTIVE_REQUEST_ID, ACTIVE_BUNDLES, PIPELINE_WORKERS
    ap = argparse.ArgumentParser()
    ap.add_argument('bundles', nargs='*')
    ap.add_argument('--no-copy', action='store_true')
    ap.add_argument('--manifest', default=None, help='只处理清单中列出的 Bundle；避免扫描整个缓存池')
    ap.add_argument('--request-id', default=None, help=argparse.SUPPRESS)
    ap.add_argument('--version', default=None, help='兼容旧配置：CDN 版本字段，例如 8374')
    ap.add_argument('--base-url', default=None, help='从完整 Catalog URL 自动识别的 Bundle 基础地址')
    ap.add_argument('--workers', type=int, default=IO_WORKERS, help=f'文件/依赖并发，默认 {IO_WORKERS}')
    args = ap.parse_args(argv)
    PIPELINE_WORKERS=max(1,min(64,int(args.workers)))
    os.environ['DOT_IO_WORKERS']=str(PIPELINE_WORKERS)
    ACTIVE_REQUEST_ID = str(args.request_id).strip() if args.request_id else None
    RAW.mkdir(parents=True, exist_ok=True)

    selected=[]
    if args.manifest:
        selected.extend(load_manifest(args.manifest))
    if args.bundles:
        if args.no_copy:
            selected.extend(Path(x).resolve() for x in args.bundles if Path(x).is_file())
        else:
            selected.extend(copy_inputs(args.bundles))
    if selected:
        dedup=[]; seen=set()
        for p in selected:
            p=Path(p).resolve()
            if p not in seen:
                seen.add(p); dedup.append(p)
        ACTIVE_BUNDLES=dedup
    else:
        ACTIVE_BUNDLES=None
        print('[WARN] 未提供 Bundle 清单或文件，将处理维护器 raw_bundles 根目录中的全部 Bundle。')

    active_base = (args.base_url or os.environ.get('DOT_CATALOG_BASE_URL', '')).strip()
    if active_base:
        catalogdl.BASE_URL = active_base.rstrip('/') + '/'
    elif args.version:
        catalogdl.BASE_URL = catalogdl.build_base_url(args.version)
    else:
        raise SystemExit('必须提供完整 Bundle 基础地址或版本号。请在设置中填入完整 catalog_1.bin 地址。')

    bundles=all_bundles()
    if not bundles:
        print(f'No .bundle files selected/found in {RAW}')
        return 2
    print(f'[BUNDLE SCOPE] selected={len(bundles)} cache={RAW}')

    stage_legacy()
    stage_l2d()
    stage_story_json()
    stage_story_dependencies()
    stage_finalize_story_json()
    stage_stand()
    print('=== COMPLETE ===')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
