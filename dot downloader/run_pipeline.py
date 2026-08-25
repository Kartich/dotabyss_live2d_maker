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
from pathlib import Path


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

ROOT = Path(__file__).resolve().parent
RAW = ROOT / 'raw_bundles'
CATALOG = ROOT / 'catalog_1.bin'
ACTIVE_REQUEST_ID = None


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
    RAW.mkdir(parents=True, exist_ok=True)
    copied = []
    for p in paths:
        p = Path(p)
        if not p.exists() or p.suffix.lower() != '.bundle':
            continue
        # Preserve the input request's folder when the bundle already lives
        # under raw_bundles/<request-id>/; otherwise place it by a best-effort
        # resource-ID inferred from the filename.
        parent = p.parent.name if p.parent.name and p.parent.name != 'raw_bundles' else ''
        folder = parent or infer_request_folder(p.name)
        dst = RAW / folder / p.name
        if p.resolve() != dst.resolve():
            shutil.copy2(p, dst)
        copied.append(dst)
    return copied


def all_bundles():
    RAW.mkdir(parents=True, exist_ok=True)
    return sorted(RAW.rglob('*.bundle'))


def stage_legacy():
    bundles = all_bundles()
    legacy.script_voice_maps.clear()
    legacy.script_bgm_maps.clear()
    legacy.loaded_global_bgms.clear()
    legacy.pending_audio_tasks.clear()

    print('>>> Stage 1: scan story/BGM/thumb/audio resources')
    for bundle in bundles:
        name = bundle.name.lower()
        try:
            # Only the actual story TextAsset bundle is a script bundle.
            # Do NOT send L2D/fade bundles through UnityPy story parsing;
            # those also contain mainchara_hmr_ in their names.
            if re.search(r'mainchara_hmr_\d{11}_hmr_\d{11}\.txt_', name):
                legacy.parse_script_and_cache(bundle)
            elif 'bgm' in name and 'voice' not in name:
                legacy.cache_global_bgm_bundle(bundle)
            elif 'icon-story-s_' in name or 'thumb' in name:
                legacy.process_story_thumb_bundle(bundle)
            elif ('voice_chara' in name or 'backgroundvoice_chara' in name
                  or '_voice' in name or '_bgv' in name):
                m = re.search(r'hmr_(\d+)', bundle.name)
                if m:
                    story_id = m.group(1)
                    legacy.pending_audio_tasks.append(
                        (story_id, bundle,
                         'backgroundvoice_chara' in name or '_bgv' in name)
                    )
        except Exception as e:
            print(f'[WARN] legacy stage1 {bundle.name}: {e}')

    print('>>> Stage 2: decode story audio/BGM')
    for story_id, bundle, is_bgv in legacy.pending_audio_tasks:
        try:
            legacy.slice_audio_from_bundle(story_id, bundle, is_bgv)
        except Exception as e:
            print(f'[WARN] audio {bundle.name}: {e}')
    try:
        legacy.dispatch_bgms_to_stories()
    except Exception as e:
        print(f'[WARN] BGM dispatch: {e}')


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
    groups = {}
    for bundle in all_bundles():
        low = bundle.name.lower()
        # Main Core/Prefab resources plus auxiliary fade/animation bundles.
        is_core = _is_l2d_core_bundle(bundle.name)
        # Auxiliary motion bundles are Story-scoped by their hmr/11-digit ID;
        # they do NOT need to contain '_l2d_' in the filename.
        is_aux = (
            '_addanimations_' in low
            or re.search(r'_animations_scene\d+', low) is not None
            or '.fade.asset_' in low
        )
        if not (is_core or is_aux):
            continue
        try:
            cid = get_id_from_name(bundle.name)
        except Exception:
            continue
        groups.setdefault(cid, []).append(bundle)

    print(f'>>> Stage 3: Live2D groups={len(groups)}')
    for cid, paths in sorted(groups.items()):
        try:
            result = process_l2d_group(cid, sorted(paths, key=lambda x: x.name))
            motion_dir = legacy.OUTPUT_STORY_DIR / cid / 'motions'
            exported = {p.name[:-len('.motion3.json')] for p in motion_dir.glob('*.motion3.json')} if motion_dir.exists() else set()
            print(f'    [OK] {cid}: exported motion3={len(exported)}')
            if result and result.get('missing_parameter_paths'):
                print(f'    [WARN] {cid}: unresolved AnimationClip bindings={len(result["missing_parameter_paths"])}')
        except Exception as e:
            print(f'[FAILED] L2D {cid}: {e}')


def _story_ids_from_bundles_and_output():
    ids = set()
    # 1) Discover every story id from every relevant bundle name.
    for bundle in all_bundles():
        m = re.search(r"hmr_(\d{11})", bundle.name, re.IGNORECASE)
        if m:
            ids.add(m.group(1))
    # 2) Never lose a story that was already materialized by an earlier stage.
    story_root = legacy.OUTPUT_STORY_DIR
    if story_root.exists():
        for d in story_root.iterdir():
            if d.is_dir() and d.name.isdigit():
                ids.add(d.name)
    return sorted(ids)


def _reconcile_missing_story_scripts(story_ids):
    # Stage 4 is the final authority: every discovered story gets one chance
    # to recover its exact script bundle before story.json is built.
    bundles = all_bundles()
    for story_id in story_ids:
        text_file = legacy.OUTPUT_STORY_DIR / story_id / 'textassets' / f'hmr_{story_id}.txt'
        if text_file.is_file() and text_file.stat().st_size > 0:
            continue

        exact = re.compile(
            rf"mainchara_hmr_{re.escape(story_id)}_hmr_{re.escape(story_id)}\.txt_",
            re.IGNORECASE
        )
        candidates = [b for b in bundles if exact.search(b.name)]
        if not candidates:
            continue

        for bundle in candidates:
            try:
                print(f"  [*] Stage 4 补解析剧本: {story_id} <- {bundle.name}")
                legacy.parse_script_and_cache(bundle)
                text_file = legacy.OUTPUT_STORY_DIR / story_id / 'textassets' / f'hmr_{story_id}.txt'
                if text_file.is_file() and text_file.stat().st_size > 0:
                    break
            except Exception as e:
                print(f"  [WARN] Stage 4 剧本补解析失败 {bundle.name}: {e}")


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

        count = 0
        for story_id in story_ids:
            story_dir = legacy.OUTPUT_STORY_DIR / story_id
            if storymake.build_story_json(story_dir):
                count += 1
        print(f'    generated story.json: {count}')

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
        dep_summary = storydeps.run_story_dependency_scan(legacy.OUTPUT_STORY_DIR, request_id=ACTIVE_REQUEST_ID)
        for sid, details in dep_summary.items():
            total = sum(len(v) for v in details.values())
            if total:
                print(f'    [DEP] {sid}: ' + ', '.join(f'{k}={len(v)}' for k,v in details.items() if v))
    except Exception as e:
        print(f'[WARN] Story dependency recovery: {e}')


def stage_stand():
    print('>>> Stage 6: extract character stand portraits')
    for bundle in all_bundles():
        low = bundle.name.lower()
        if 'charastand' not in low:
            continue
        try:
            env = UnityPy.load(str(bundle))
            # stand_module.output_character writes directly to data/chara/<ID>/
            process_stand_env(env, bundle)
        except Exception as e:
            print(f'[FAILED] Stand {bundle.name}: {e}')


def main(argv=None):
    global ACTIVE_REQUEST_ID
    ap = argparse.ArgumentParser()
    ap.add_argument('bundles', nargs='*')
    ap.add_argument('--no-copy', action='store_true')
    ap.add_argument('--request-id', default=None, help='本次人物/资源下载任务的输入 ID；Stage 5 补充 Bundle 继续放入 raw_bundles/<此 ID>/')
    ap.add_argument('--version', default=None, help='CDN 版本字段，例如 8374')
    args = ap.parse_args(argv)

    RAW.mkdir(parents=True, exist_ok=True)
    if args.bundles and not args.no_copy:
        copy_inputs(args.bundles)

    # Prefer the explicit input ID. Otherwise infer the single request folder
    # created for this invocation. This keeps ALL bundles for one character/task
    # in raw_bundles/<input-id>/, even when Stage 5 discovers story IDs later.
    if args.version:
        catalogdl.BASE_URL = catalogdl.build_base_url(args.version)
    else:
        raise SystemExit('必须提供 CDN 版本字段。示例: https://api.abyss-prod-r18.dotabyss.dmmgames.com/resources/webgl/r18/aas/8374/aa/；请只输入版本号，例如 8374。')


    ACTIVE_REQUEST_ID = str(args.request_id).strip() if args.request_id else None
    # One input ID owns one complete request folder. All Stage 5 dependency
    # bundles for stories discovered under this request stay in that same folder.
    if not ACTIVE_REQUEST_ID:
        request_dirs = [p.name for p in RAW.iterdir() if p.is_dir()]
        if len(request_dirs) == 1:
            ACTIVE_REQUEST_ID = request_dirs[0]

    if not all_bundles():
        print(f'No .bundle files in {RAW}')
        return 2

    stage_legacy()
    stage_l2d()
    stage_story_json()
    stage_story_dependencies()
    stage_stand()
    print('=== COMPLETE ===')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
