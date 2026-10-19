# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tool_paths import TOOL_DIR, catalog_path, find_project_root, raw_bundles_dir, runtime_dir
from resource_audit import audit_project, refresh_report_subset, print_report_diagnostics
from parallel_utils import IO_WORKERS, workers_for


def run_cmd(cmd, root: Path, env: dict) -> int:
    print('> ' + subprocess.list2cmdline([str(x) for x in cmd]), flush=True)
    p=subprocess.Popen(cmd,cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1)
    assert p.stdout is not None
    for line in p.stdout:
        print(line,end='',flush=True)
    return p.wait()


def _issue_story_ids(report: dict) -> list[str]:
    return sorted({str(x.get('story_id','')) for x in report.get('issues',[]) or [] if str(x.get('story_id','')).strip()})


def repair_public(root: Path, version: str, rows: list[dict], workers: int = IO_WORKERS, base_url: str = '') -> int:
    if not rows: return 0
    import catalog_downloader as catalogdl
    active_base=(base_url or os.environ.get('DOT_CATALOG_BASE_URL','')).strip()
    catalogdl.BASE_URL=active_base.rstrip('/') + '/' if active_base else catalogdl.build_base_url(version)
    import story_dependency_assets as deps
    unique={}
    for row in rows:
        key=(str(row.get('kind','')),str(row.get('resource','')).lower())
        if key[0] and key[1] and key not in unique: unique[key]=row
    items=list(unique.values()); failures=0
    w=workers_for(len(items),requested=workers)
    print(f'>>> 精确补全公共资源: issues={len(rows)} unique={len(items)} workers={w}', flush=True)
    def one(row):
        kind=row['kind']; rid=row['resource']; sid=row.get('story_id') or 'resource_repair'
        try:
            status,path=deps.materialize_dependency(kind,rid,sid)
            return row,status,path,None
        except Exception as exc: return row,'failed',None,exc
    with ThreadPoolExecutor(max_workers=w,thread_name_prefix='public-repair') as ex:
        futs=[ex.submit(one,row) for row in items]
        for idx,fut in enumerate(as_completed(futs),1):
            row,status,path,exc=fut.result(); kind=row['kind']; rid=row['resource']
            print(f'[{idx}/{len(items)}] {kind} {rid} [{status}] {path or ""}', flush=True)
            if exc: print(f'    [FAIL] {exc}', flush=True)
            if status in ('failed','not-found'): failures+=1
    return failures


def _private_kind_plan(report: dict) -> dict[str,set[str]]:
    """Map each problematic story to only the Bundle families needed by its issues."""
    plan={}
    all_private={'story','voice','bgv','l2d_core','l2d_motion','l2d_fade'}
    for issue in report.get('issues',[]) or []:
        if issue.get('repair')!='private-story':
            continue
        sid=str(issue.get('story_id','')).strip()
        if not sid: continue
        kind=str(issue.get('kind','')).lower()
        wanted=plan.setdefault(sid,set())
        if kind in ('script',):
            wanted.add('story')
        elif kind == 'voice':
            rid=str(issue.get('resource','')).lower()
            if rid.startswith('vc_'): wanted.add('voice')
            elif rid.startswith('bgv_'): wanted.add('bgv')
            else: wanted.update(('voice','bgv'))
        elif kind in ('l2d-motion-ref','l2d-motion'):
            # handled by exact Catalog source bundle plan below
            continue
        elif kind in ('l2d-moc','l2d-model3','l2d-texture'):
            # Core/model/texture loss only needs the Core family. Motion bundles
            # are independent and are repaired by exact command references.
            wanted.add('l2d_core')
        elif kind.startswith('l2d'):
            wanted.add('l2d_core')
        elif kind in ('story','story-audit'):
            wanted.update(all_private)
        else:
            wanted.update(all_private)
    return {sid:kinds for sid,kinds in plan.items() if kinds}



def _l2d_motion_source_plan(report: dict) -> dict[str,set[str]]:
    plan={}
    for issue in report.get('issues',[]) or []:
        if str(issue.get('kind','')).lower() not in ('l2d-motion-ref','l2d-motion'): continue
        if issue.get('repair') != 'private-story': continue
        sid=str(issue.get('story_id','')).strip()
        details=issue.get('details') or {}
        bundles=details.get('catalogSourceBundles') or ((details.get('resolver') or {}).get('catalogSourceBundles')) or []
        if sid and bundles:
            plan.setdefault(sid,set()).update(str(x) for x in bundles if x)
    return plan

def _stand_plan(report: dict) -> dict[str,set[str]]:
    plan={}
    for issue in report.get('issues',[]) or []:
        if issue.get('repair') != 'character-stand':
            continue
        sid=str(issue.get('story_id','')).strip()
        asset=str(issue.get('resource','')).strip().lower()
        if sid and asset:
            plan.setdefault(sid,set()).add(asset)
    return plan


def repair_private(root: Path, version: str, report: dict, env: dict, workers: int = IO_WORKERS, base_url: str = '') -> int:
    plan=_private_kind_plan(report)
    motion_plan=_l2d_motion_source_plan(report)
    stand_plan=_stand_plan(report)
    if not plan and not motion_plan and not stand_plan: return 0
    import catalog_downloader as cd
    active_base=(base_url or os.environ.get('DOT_CATALOG_BASE_URL','')).strip()
    cd.BASE_URL=active_base.rstrip('/') + '/' if active_base else cd.build_base_url(version)
    catalog=Path(catalog_path())
    names=cd.extract_bundle_names(catalog)
    cache=raw_bundles_dir(); cache.mkdir(parents=True,exist_ok=True)
    cd.migrate_legacy_bundle_cache(cache)

    selected={}
    for sid,wanted in sorted(plan.items()):
        rows,_=cd.filter_relevant(names,sid)
        rows=[(name,kind) for name,kind in rows if kind in wanted]
        print(f'[PRIVATE PLAN] story={sid} kinds={",".join(sorted(wanted))} bundles={len(rows)}', flush=True)
        for name,kind in rows:
            selected.setdefault(name,(kind,sid))

    # l2d-motion-ref is repaired by exact Catalog source only. Reuse cached
    # Core/Prefab bundles needed by l2d_module; download Core only when the
    # flat cache has none for that story. Never fetch every motion/fade bundle.
    cached_manifest_rows=[]
    for sid,bundles in sorted(motion_plan.items()):
        rows,_=cd.filter_relevant(names,sid)
        rowmap={name:kind for name,kind in rows}
        exact=[b for b in bundles if b in rowmap]
        print(f'[L2D MOTION PLAN] story={sid} exactSources={len(exact)}', flush=True)
        for name in exact:
            selected.setdefault(name,('l2d_motion',sid))
        cores=[(name,kind) for name,kind in rows if kind=='l2d_core']
        cached=[(name,kind) for name,kind in cores if (cache/name).is_file() and (cache/name).stat().st_size>0]
        if cached:
            for name,kind in cached:
                cached_manifest_rows.append((name,kind,'exists',cache/name))
        else:
            # Prefer the main model prefab + model asset; effect/postprocess are
            # not required for motion conversion.
            core_needed=[x for x in cores if (f'l2d_{sid}.prefab_' in x[0].lower() or f'l2d_{sid}.asset_' in x[0].lower())]
            if not core_needed: core_needed=cores
            for name,kind in core_needed:
                selected.setdefault(name,(kind,sid))

    # Stand/charastand is character-level shared data, not a Live2D story file.
    # Download it by the exact 9-digit+G/X asset id and let the normal pipeline
    # classify/convert the prefab into data/chara/<asset>/.
    for sid,assets in sorted(stand_plan.items()):
        for asset in sorted(assets):
            rows,_=cd.filter_relevant(names,asset)
            rows=[(name,kind) for name,kind in rows if kind=='stand']
            print(f'[STAND PLAN] story={sid} asset={asset} bundles={len(rows)}', flush=True)
            for name,kind in rows:
                selected.setdefault(name,(kind,sid))

    if not selected:
        print('[WARN] 私有资源缺失，但 Catalog 未找到匹配的目标 Bundle。', flush=True)
        return 1

    jobs=[]
    for name,(kind,sid) in selected.items():
        jobs.append((name,kind,sid,cd.make_url(name),cache/name))
    w=workers_for(len(jobs),requested=workers)
    print(f'>>> 定向补全剧情私有资源: stories={len(set(plan)|set(motion_plan)|set(stand_plan))} bundles={len(jobs)} workers={w} cache={cache}', flush=True)
    rows=list(cached_manifest_rows); failed=0
    with ThreadPoolExecutor(max_workers=w,thread_name_prefix='private-repair-dl') as ex:
        futs={ex.submit(cd.download_one,url,dst):(name,kind,sid,dst) for name,kind,sid,url,dst in jobs}
        done=0
        for fut in as_completed(futs):
            name,kind,sid,dst=futs[fut]
            try: status=fut.result()
            except Exception as exc: status=f'failed:{exc}'
            done+=1
            print(f'[GET {done}/{len(jobs)}] [{kind}] [{status}] story={sid} {name}', flush=True)
            if str(status).startswith('failed:'): failed+=1
            else: rows.append((name,kind,status,dst))

    if not rows:
        return max(1,failed)
    manifest=runtime_dir()/'resource_repair_manifest.json'
    query_list=sorted(set(plan) | set(motion_plan) | {asset for assets in stand_plan.values() for asset in assets})
    cd.write_manifest(manifest,rows,queries=query_list)
    pipeline_url_args = (
        ['--base-url', active_base.rstrip('/') + '/']
        if active_base else ['--version', version]
    )
    code=run_cmd([
        sys.executable,str(TOOL_DIR/'run_pipeline.py'),
        '--manifest',str(manifest),*pipeline_url_args,'--workers',str(w)
    ],root,env)
    if code!=0:
        print(f'[FAIL] 定向剧情处理阶段退出码 {code}', flush=True)
        failed+=1
    return failed


def main(argv=None):
    ap=argparse.ArgumentParser(description='Dot 离线资源维护器 - 缺失资源自动补全')
    ap.add_argument('--root',default=None)
    ap.add_argument('--version',default='')
    ap.add_argument('--base-url',default='',help='完整 Catalog URL 对应的 Bundle 基础地址')
    ap.add_argument('--report',default=None,help='可选：复用已生成的 audit JSON；只定向复核其中有问题的剧情')
    ap.add_argument('--output',default=None,help='修复后合并报告输出路径')
    ap.add_argument('--workers',type=int,default=IO_WORKERS,help=f'并发线程，默认 {IO_WORKERS}')
    args=ap.parse_args(argv)
    # 新界面优先传入由完整 catalog_1.bin 地址解析出的 Bundle base URL。
    # 旧版只校验 --version，导致即使 --base-url 有效仍报“版本必须为纯数字”。
    base_url = str(args.base_url or os.environ.get('DOT_CATALOG_BASE_URL', '')).strip()
    version = str(args.version or '').strip()
    if base_url:
        from urllib.parse import urlsplit
        parsed = urlsplit(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise SystemExit('Bundle 基础地址无效，请检查设置中的完整 catalog_1.bin 地址。')
        if not parsed.path.rstrip('/').endswith('/aa') and '/aas/' not in parsed.path:
            raise SystemExit('Bundle 基础地址路径异常，请使用完整 catalog_1.bin 地址自动识别。')
        import re
        match = re.search(r'/aas/(\d+)/aa/?$', parsed.path, re.I)
        if match:
            version = match.group(1)
    elif not version.isdigit():
        raise SystemExit('请在设置中填写有效的 Catalog 地址，或提供纯数字 CDN 版本号，例如 8374。')
    root=Path(args.root).resolve() if args.root else find_project_root(Path(__file__))
    os.environ['DOT_PROJECT_ROOT']=str(root)
    os.environ['DOT_CATALOG']=str(catalog_path())
    env=os.environ.copy(); env['PYTHONUTF8']='1'; env['PYTHONIOENCODING']='utf-8'; env['PYTHONUNBUFFERED']='1'
    if base_url:
        env['DOT_CATALOG_BASE_URL'] = base_url.rstrip('/') + '/'
    args.version = version
    args.base_url = base_url

    reused=False
    if args.report and Path(args.report).is_file():
        report=json.loads(Path(args.report).read_text(encoding='utf-8-sig'))
        if str(Path(report.get('root','')).resolve()) == str(root.resolve()):
            affected=_issue_story_ids(report)
            print(f'[AUDIT REUSE] 复用已有完整检查报告，仅定向复核 {len(affected)} 个有问题剧情，不重新扫描全部 story.json。', flush=True)
            report=refresh_report_subset(root,report,affected,args.workers)
            reused=True
        else:
            print('[AUDIT REUSE] 报告属于其他项目，改为重新检查。', flush=True)
            report=audit_project(root,workers_for(requested=args.workers))
    else:
        report=audit_project(root,workers_for(requested=args.workers))
    print(f"[AUDIT BEFORE] stories={report['stories']} issues={report['issueCount']} reused={reused}", flush=True)

    affected=_issue_story_ids(report)
    public=report.get('publicResources') or []
    failures=repair_public(root,args.version,public,args.workers,args.base_url)
    failures += repair_private(root,args.version,report,env,args.workers,args.base_url)

    print('\n>>> 同步更新剧情 cue 与全局索引', flush=True)
    try:
        import maintenance_tool
        maintenance_tool.run_index_update(root, prefer_pool=True)
    except Exception as exc:
        failures += 1
        print('[FAIL] index update:',exc, flush=True)

    # Do not perform a second full-project scan. All pre-existing issues belong to
    # affected stories, so rechecking just that subset is sufficient to merge a new
    # full-project report while retaining unaffected stories from the previous audit.
    after=refresh_report_subset(root,report,affected,args.workers) if affected else report
    out=Path(args.output) if args.output else (TOOL_DIR/'reports'/'resource_audit_latest.json')
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(after,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f"\n[AUDIT AFTER] stories={after['stories']} issues={after['issueCount']}", flush=True)
    for kind,count in sorted(after.get('byKind',{}).items()): print(f'  {kind}: {count}', flush=True)
    print_report_diagnostics(after, prefix='AUDIT REMAINING')
    print('[REPORT]',out, flush=True)
    return 0 if after['issueCount']==0 and failures==0 else 1

if __name__=='__main__':
    raise SystemExit(main())
