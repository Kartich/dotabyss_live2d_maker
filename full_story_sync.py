# -*- coding: utf-8 -*-
"""Catalog-driven full Story supplement for Dot offline resources.

Workflow:
1. Confirm 9-digit character IDs from charastand bundles first.
2. Build character -> 11-digit Story mappings from the same catalog.
3. Compare mappings with local story.json / stand / thumbnail resources.
4. Download only missing Story-private bundles, stands and thumbnails.
5. Reuse the existing extraction pipeline, which also restores Story BG/BGM/SE deps.
6. Rebuild indexes and verify the result.

The script emits machine-readable progress lines beginning with ``@@DOT_PROGRESS``.
The GUI consumes them for a real-time determinate progress bar; normal console use
still receives the human-readable log lines printed alongside them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from parallel_utils import IO_WORKERS, workers_for
from tool_paths import TOOL_DIR, raw_bundles_dir, runtime_dir
STORY_ID_RE = re.compile(r"hmr_(\d{11})", re.I)
PROGRESS_PREFIX = "@@DOT_PROGRESS\t"


def emit_progress(percent, stage, detail="", current=None, total=None):
    payload = {
        "percent": max(0.0, min(100.0, float(percent))),
        "stage": str(stage or ""),
        "detail": str(detail or ""),
    }
    if current is not None:
        payload["current"] = int(current)
    if total is not None:
        payload["total"] = int(total)
    print(PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _valid_story(root: Path, story_id: str) -> bool:
    path = root / "data_r18_all" / "stories" / story_id / "story.json"
    data = _read_json(path)
    if not isinstance(data, dict):
        return False
    scripts = data.get("scripts")
    return isinstance(scripts, list) and len(scripts) > 0


def _valid_stand(root: Path, stand_id: str) -> bool:
    folder = root / "data" / "chara" / stand_id
    if not folder.is_dir():
        return False
    return any(
        p.is_file() and p.suffix.lower() in {".png", ".webp", ".jpg", ".jpeg"}
        for p in folder.rglob("*")
    )


def _valid_thumb(root: Path, thumb_id: str) -> bool:
    folder = root / "data_r18_all" / "thumb"
    if not folder.is_dir():
        return False
    token = str(thumb_id).lower()
    return any(
        p.is_file()
        and token in p.stem.lower()
        and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        for p in folder.iterdir()
    )


def build_mapping(catalog: Path):
    """Build mappings character-first, then attach Story IDs.

    The primary character set comes from exact ``charastand<9-digit>`` records.
    Story prefixes that do not have a stand record are retained as a fallback so
    an unusual catalog cannot silently hide valid Story packages.
    """
    import catalog_downloader as cd
    from resource_classifier import classify_bundle

    emit_progress(2, "读取 Catalog", "解析 catalog_1.bin 中的 Bundle 名称")
    names = cd.extract_bundle_names(catalog)
    emit_progress(5, "识别角色", f"Catalog 共 {len(names)} 个 Bundle，先确认人物立绘/角色 ID")

    classified = [(name, classify_bundle(name)) for name in names]
    stand_by_char = defaultdict(set)
    thumb_by_char = defaultdict(set)
    story_tokens = set()

    # Pass 1: character IDs are confirmed from stand bundles before Story search.
    for _name, info in classified:
        if info.kind == "stand" and info.token:
            m = re.match(r"(\d{9})", str(info.token))
            if m:
                stand_by_char[m.group(1)].add(str(info.token).lower())
        elif info.kind == "thumb" and info.token:
            token = str(info.token).lower()
            if len(token) >= 9 and token[:9].isdigit():
                thumb_by_char[token[:9]].add(token)
        elif info.kind == "story" and info.token and re.fullmatch(r"\d{11}", str(info.token)):
            story_tokens.add(str(info.token).lower())

    confirmed_chars = set(stand_by_char)
    emit_progress(
        7,
        "检索角色剧情",
        f"已确认 {len(confirmed_chars)} 个立绘角色 ID；按角色前缀检索 11 位剧情 ID",
    )

    story_by_char = defaultdict(set)
    fallback_chars = set()
    for sid in sorted(story_tokens):
        cid = sid[:9]
        story_by_char[cid].add(sid)
        if cid not in confirmed_chars:
            fallback_chars.add(cid)

    all_chars = sorted(confirmed_chars | fallback_chars)
    roles = {}
    for cid in all_chars:
        roles[cid] = {
            "characterId": cid,
            "source": "stand" if cid in confirmed_chars else "story-prefix-fallback",
            "stories": sorted(story_by_char.get(cid, ())),
            "standVariants": sorted(stand_by_char.get(cid, ())),
            "thumbIds": sorted(thumb_by_char.get(cid, ())),
        }
    emit_progress(
        9,
        "剧情映射完成",
        f"角色 {len(roles)} 个，Catalog 剧情 {sum(len(x['stories']) for x in roles.values())} 个",
    )
    return names, classified, roles


def _evaluate_one(root: Path, cid: str, role: dict):
    expected_stories = role["stories"]
    missing_stories = [sid for sid in expected_stories if not _valid_story(root, sid)]
    missing_set = set(missing_stories)
    local_stories = [sid for sid in expected_stories if sid not in missing_set]
    missing_stands = [sid for sid in role["standVariants"] if not _valid_stand(root, sid)]
    missing_thumbs = [tid for tid in role["thumbIds"] if not _valid_thumb(root, tid)]
    return {
        **role,
        "localStories": local_stories,
        "missingStories": missing_stories,
        "missingStandVariants": missing_stands,
        "missingThumbIds": missing_thumbs,
    }


def evaluate_local(root: Path, roles: dict, *, progress_start=9.0, progress_end=15.0, workers=30, stage="对比本地资源"):
    """Compare all mapped roles with local files in parallel."""
    items = list(sorted(roles.items()))
    rows_by_cid = {}
    total = len(items)
    if total == 0:
        return [], {
            "characters": 0,
            "catalogStories": 0,
            "missingStories": 0,
            "missingStandVariants": 0,
            "missingThumbs": 0,
        }

    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 30, total)), thread_name_prefix="story-map-check") as pool:
        futs = {pool.submit(_evaluate_one, root, cid, role): cid for cid, role in items}
        done = 0
        for fut in as_completed(futs):
            cid = futs[fut]
            rows_by_cid[cid] = fut.result()
            done += 1
            pct = progress_start + (progress_end - progress_start) * done / total
            emit_progress(pct, stage, f"检查角色 {cid}", done, total)

    rows = [rows_by_cid[cid] for cid, _role in items]
    stats = {
        "characters": len(rows),
        "catalogStories": sum(len(r["stories"]) for r in rows),
        "missingStories": sum(len(r["missingStories"]) for r in rows),
        "missingStandVariants": sum(len(r["missingStandVariants"]) for r in rows),
        "missingThumbs": sum(len(r["missingThumbIds"]) for r in rows),
    }
    return rows, stats


def _bundle_story_id(name: str):
    m = STORY_ID_RE.search(name)
    return m.group(1) if m else None


def select_missing_bundles(classified, row):
    """Select Story-private families using the same matching policy as downloader.

    Common BG/BGM/SE are intentionally not preselected here: after the Story is
    materialized, the existing pipeline reads its references and recovers only the
    common assets that Story actually calls.
    """
    missing_stories = set(row.get("missingStories") or [])
    missing_stands = set(row.get("missingStandVariants") or [])
    missing_thumbs = set(row.get("missingThumbIds") or [])
    selected = []
    seen = set()

    for name, info in classified:
        hit = False
        if info.kind == "story" and info.token in missing_stories:
            hit = True
        elif info.kind in {"l2d_core", "l2d_motion", "l2d_fade", "voice", "bgv"}:
            sid = _bundle_story_id(name)
            hit = bool(sid and sid in missing_stories)
        elif info.kind == "stand" and info.token in missing_stands:
            hit = True
        elif info.kind == "thumb" and info.token in missing_thumbs:
            hit = True
        if hit and name not in seen:
            seen.add(name)
            selected.append((name, info.kind))
    return selected


def _write_report(path: Path, root: Path, catalog: Path, rows, stats, *, repaired=False, failures=None, plan=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "projectRoot": str(root),
        "catalog": str(catalog),
        "repaired": bool(repaired),
        "stats": stats,
        "characters": rows,
        "failures": failures or [],
    }
    if plan is not None:
        payload["downloadPlan"] = plan
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _print_summary(stats, prefix="MAP"):
    print(
        f'[{prefix}] characters={stats.get("characters",0)} '
        f'catalogStories={stats.get("catalogStories",0)} '
        f'missingStories={stats.get("missingStories",0)} '
        f'missingStands={stats.get("missingStandVariants",0)} '
        f'missingThumbs={stats.get("missingThumbs",0)}',
        flush=True,
    )


def _stream_process(cmd, *, cwd: Path, env: dict, progress_base=None, progress_span=0.0, stage="提取剧情资源", detail_prefix=""):
    """Run a child process while forwarding its output and optional Stage progress."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=flags,
    )
    assert proc.stdout is not None
    stage_re = re.compile(r">>>\s*Stage\s*(\d+)", re.I)
    for line in proc.stdout:
        print(line, end="", flush=True)
        if progress_base is not None:
            m = stage_re.search(line)
            if m:
                stage_no = max(1, min(int(m.group(1)), 7))
                pct = float(progress_base) + float(progress_span) * ((stage_no - 1) / 7.0)
                emit_progress(pct, stage, f"{detail_prefix} · 提取阶段 {stage_no}/7")
    return proc.wait()


def _build_plan(root: Path, classified, rows):
    work = []
    for row in rows:
        if not (row["missingStories"] or row["missingStandVariants"] or row["missingThumbIds"]):
            continue
        selected = select_missing_bundles(classified, row)
        unique=[]; seen=set()
        for name,kind in selected:
            if name in seen: continue
            seen.add(name); unique.append((name,kind))
        work.append({"row": row, "bundles": unique})
    return work


def repair(root: Path, catalog: Path, version: str, classified, rows, download_workers: int, base_url: str = ''):
    import catalog_downloader as cd

    active_base=(base_url or os.environ.get('DOT_CATALOG_BASE_URL','')).strip()
    cd.BASE_URL = active_base.rstrip('/') + '/' if active_base else cd.build_base_url(version)
    failures = []
    work = _build_plan(root, classified, rows)
    cache=raw_bundles_dir(); cache.mkdir(parents=True,exist_ok=True)
    cd.migrate_legacy_bundle_cache(cache)
    # One filename = one cache entry. Multiple characters can refer to the same
    # Bundle without creating duplicate task folders.
    global_bundles={}
    for w in work:
        for name,kind in w['bundles']:
            global_bundles.setdefault(name,kind)
    total_bundles=len(global_bundles)
    print(f"[FULL] characters needing supplement: {len(work)}, unique bundles planned: {total_bundles}", flush=True)

    emit_progress(16, "准备下载", f"缺失角色组 {len(work)} 个，需要唯一 Bundle {total_bundles} 个")
    jobs=[(name,kind,cd.make_url(name),cache/name) for name,kind in global_bundles.items()]
    rows_manifest=[]
    if jobs:
        workers=workers_for(len(jobs),requested=download_workers)
        with ThreadPoolExecutor(max_workers=workers,thread_name_prefix='full-story-dl') as pool:
            futs={pool.submit(cd.download_one,url,dst):(name,kind,dst) for name,kind,url,dst in jobs}
            done=0
            for fut in as_completed(futs):
                name,kind,dst=futs[fut]
                try: status=fut.result()
                except Exception as exc: status=f'failed:{exc}'
                done+=1
                emit_progress(16+44*done/max(1,len(jobs)), '下载缺失剧情资源', f'{kind} · {name}', done, len(jobs))
                print(f'[GET] [{kind}] [{status}] {name}', flush=True)
                if str(status).startswith('failed:'):
                    failures.append({'reason':'download-failed','bundle':name})
                else:
                    rows_manifest.append((name,kind,status,dst))
    else:
        emit_progress(60,'下载缺失剧情资源','没有需要下载的 Bundle')

    # Process exactly the Bundles selected by this supplement pass. The flat cache
    # may contain thousands of historical Bundles, but they are never rescanned.
    if rows_manifest:
        manifest=runtime_dir()/'full_story_sync_manifest.json'
        cd.write_manifest(manifest,rows_manifest,queries=[w['row']['characterId'] for w in work])
        env=os.environ.copy(); env['PYTHONUNBUFFERED']='1'
        emit_progress(60,'提取并分类剧情资源',f'本次精确处理 {len(rows_manifest)} 个 Bundle，不扫描历史缓存')
        cmd=[
            sys.executable,'-u',str(TOOL_DIR/'run_pipeline.py'),
            '--manifest',str(manifest),'--version',str(version),
            '--workers',str(workers_for(requested=download_workers)),
        ]
        print('> '+subprocess.list2cmdline(cmd),flush=True)
        code=_stream_process(cmd,cwd=root,env=env,progress_base=60,progress_span=28,stage='提取并分类剧情资源',detail_prefix='批量补充')
        if code!=0:
            failures.append({'reason':'pipeline-failed','exitCode':code})
    elif work:
        failures.append({'reason':'no-usable-bundles'})

    emit_progress(89, "更新索引", "根据补充后的 story.json 同步剧情/BGM/SE/缩略图索引")
    print("\n[FULL] rebuilding story/audio/thumb indexes ...", flush=True)
    env=os.environ.copy(); env['PYTHONUNBUFFERED']='1'
    cmd = [sys.executable, "-u", str(TOOL_DIR / "maintenance_tool.py"), "--root", str(root), "--action", "index"]
    code = _stream_process(cmd, cwd=root, env=env, progress_base=89, progress_span=5, stage="更新索引", detail_prefix="索引")
    if code != 0:
        failures.append({"reason": "index-update-failed", "exitCode": code})
    emit_progress(94, "更新索引", "索引刷新完成")
    return failures, work


def main(argv=None):
    ap = argparse.ArgumentParser(description="Catalog 全量剧情映射/缺失剧情自动补充")
    ap.add_argument("--root", required=True, help="播放器项目根目录")
    ap.add_argument("--catalog", required=True, help="catalog_1.bin")
    ap.add_argument("--version", default="", help="兼容旧配置：CDN 版本号")
    ap.add_argument("--base-url", default="", help="完整 Catalog URL 对应的 Bundle 基础地址")
    ap.add_argument("--scan-only", action="store_true", help="只建立映射并比较，不下载")
    ap.add_argument("--download-workers", type=int, default=IO_WORKERS, help=f"Bundle 下载并发，默认 {IO_WORKERS}")
    ap.add_argument("--output", default="", help="报告 JSON 输出路径")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    catalog = Path(args.catalog).expanduser().resolve()
    if not root.exists():
        raise SystemExit(f"项目目录不存在: {root}")
    if not catalog.is_file():
        raise SystemExit(f"Catalog 不存在: {catalog}")
    if not (root / "data_r18_all").exists():
        raise SystemExit("项目根目录至少应包含 data_r18_all")

    os.environ["DOT_PROJECT_ROOT"] = str(root)
    os.environ["DOT_CATALOG"] = str(catalog)
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    if not args.scan_only and not str(args.version).strip().isdigit():
        raise SystemExit("全量补充需要 CDN 版本号，例如 8374。")

    default_report = TOOL_DIR / "reports" / ("catalog_story_mapping.json" if args.scan_only else "full_story_supplement.json")
    output = Path(args.output).expanduser().resolve() if args.output else default_report

    print("[FULL] 1/3 从 Catalog 确认角色 ID，并建立角色 -> 剧情映射 ...", flush=True)
    _names, classified, roles = build_mapping(catalog)
    rows, stats = evaluate_local(
        root,
        roles,
        progress_start=9,
        progress_end=95 if args.scan_only else 15,
        workers=workers_for(requested=args.download_workers),
        stage="对比现有剧情" if not args.scan_only else "扫描本地缺口",
    )
    _print_summary(stats)
    for row in rows:
        if row["missingStories"] or row["missingStandVariants"] or row["missingThumbIds"]:
            print(
                f'[MISS] char={row["characterId"]} '
                f'stories={",".join(row["missingStories"]) or "-"} '
                f'stands={",".join(row["missingStandVariants"]) or "-"} '
                f'thumbs={",".join(row["missingThumbIds"]) or "-"}',
                flush=True,
            )

    if args.scan_only:
        _write_report(output, root, catalog, rows, stats, repaired=False)
        emit_progress(100, "扫描完成", f'缺剧情 {stats["missingStories"]} · 缺立绘 {stats["missingStandVariants"]} · 缺缩略图 {stats["missingThumbs"]}')
        print(f"[DONE] 映射/缺口报告: {output}", flush=True)
        return 0 if stats["missingStories"] == 0 and stats["missingStandVariants"] == 0 and stats["missingThumbs"] == 0 else 1

    print("\n[FULL] 2/3 下载并提取缺失剧情包/立绘/缩略图 ...", flush=True)
    failures, plan = repair(root, catalog, args.version.strip(), classified, rows, args.download_workers, args.base_url.strip())

    print("\n[FULL] 3/3 重新扫描本地结果 ...", flush=True)
    emit_progress(95, "最终复检", "重新核对剧情、人物立绘和缩略图")
    rows_after, stats_after = evaluate_local(root, roles, progress_start=95, progress_end=99, workers=workers_for(requested=args.download_workers), stage="最终复检")
    _print_summary(stats_after, "AFTER")
    plan_summary = {
        "characters": len(plan),
        "bundles": sum(len(w["bundles"]) for w in plan),
        "downloadWorkers": workers_for(requested=args.download_workers),
    }
    _write_report(output, root, catalog, rows_after, stats_after, repaired=True, failures=failures, plan=plan_summary)
    print(f"[DONE] 全量补充报告: {output}", flush=True)
    if failures or stats_after["missingStories"] or stats_after["missingStandVariants"] or stats_after["missingThumbs"]:
        emit_progress(
            100,
            "补充完成（仍有缺失）",
            f'缺剧情 {stats_after["missingStories"]} · 缺立绘 {stats_after["missingStandVariants"]} · 缺缩略图 {stats_after["missingThumbs"]} · failures {len(failures)}',
        )
        print("[WARN] 仍有未补齐项目，请查看报告 failures / missing* 字段。", flush=True)
        return 1
    emit_progress(100, "全量补充完成", "Catalog 映射剧情、人物立绘和缩略图均已补齐，索引已同步")
    print("[OK] Catalog 中映射到的剧情包、人物立绘和缩略图均已补齐。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
