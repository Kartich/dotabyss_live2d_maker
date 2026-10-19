# -*- coding: utf-8 -*-
"""Run the resource maintainer's canonical conversion stages for a downloaded character bundle set.
Outputs use the resource maintainer canonical layout directly under Characters/<id>, without a redundant 转换结果 wrapper.
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, tempfile, shutil
from pathlib import Path
from tool_paths import TOOL_DIR

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--manifest',required=True)
    ap.add_argument('--raw-dir',required=True)
    ap.add_argument('--output-root',required=True)
    ap.add_argument('--base-url',required=True)
    ap.add_argument('--workers',type=int,default=8)
    a=ap.parse_args()
    manifest=Path(a.manifest).resolve(); raw=Path(a.raw_dir).resolve(); out=Path(a.output_root).resolve()
    if not manifest.is_file(): raise SystemExit(f'人物提取清单不存在：{manifest}')
    data=json.loads(manifest.read_text(encoding='utf-8-sig'))
    names=data.get('downloaded') or []
    paths=[]
    for name in names:
        p=(raw/str(name)).resolve()
        if p.is_file() and p.suffix.lower()=='.bundle': paths.append(str(p))
    if not paths: raise SystemExit('人物下载清单中没有找到已下载的 Bundle，无法进入资源维护器转换流程。')
    out.mkdir(parents=True,exist_ok=True)
    # Work in a hidden temporary maintainer workspace so data/ and data_r18_all/
    # never leak into the final character package.
    work = raw / '.dot_workspaces' / str(data.get('character_id') or out.name)
    if work.exists(): shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',suffix='.json',delete=False,dir=str(work)) as f:
        json.dump({'bundles':[{'path':x} for x in paths]},f,ensure_ascii=False); temp=Path(f.name)
    env=os.environ.copy(); env['DOT_PROJECT_ROOT']=str(work); env['DOT_CATALOG']=str(data.get('catalog') or (TOOL_DIR/'catalog_1.bin'))
    env['PYTHONUTF8']='1'; env['PYTHONIOENCODING']='utf-8'; env['PYTHONUNBUFFERED']='1'
    cmd=[sys.executable,str(TOOL_DIR/'run_pipeline.py'),'--manifest',str(temp),'--base-url',a.base_url,'--workers',str(max(1,min(64,a.workers)))]
    print(f'[CHARACTER CONVERSION] bundles={len(paths)} output={out}',flush=True)
    try:
        rc=subprocess.call(cmd,cwd=str(work),env=env)
        if rc != 0: return rc
        cid = str(data.get('character_id') or out.name)
        # Gather portrait outputs produced by the validated stand converter.
        stand_root = work/'data'/'chara'
        if stand_root.exists():
            for d in stand_root.iterdir():
                if not d.is_dir(): continue
                for fpath in d.iterdir():
                    if fpath.is_file() and fpath.suffix.lower() in ('.png','.webp','.jpg','.jpeg') and fpath.name.lower() != 'body.png':
                        target=out/fpath.name
                        if not target.exists(): shutil.copy2(fpath,target)
                # split body/faces and JSON metadata are implementation details, not deliverables
        # Copy only character story/L2D payloads and thumbnails, excluding generic background/audio.
        allowed_roots = [work/'data_r18_all'/'stories', work/'data_r18_all'/'thumb', work/'data'/'stories', work/'data'/'thumb']
        for src in allowed_roots:
            if not src.exists(): continue
            for fpath in src.rglob('*'):
                if not fpath.is_file(): continue
                # Keep converted runtime JSON (story.json, model3.json, motion3.json,
                # etc.). Only index JSON files are implementation indexes, not assets.
                if fpath.name.lower() == 'index.json': continue
                low=fpath.as_posix().lower()
                if any(x in low for x in ('/bgm','/se/','/voice','/backgrounds/')): continue
                rel=fpath.relative_to(src)
                # Keep story IDs and filenames, but avoid recreating data/data_r18_all wrappers.
                target_root = out/'thumb' if 'thumb' in src.parts else out/'story'
                target=target_root/rel
                target.parent.mkdir(parents=True,exist_ok=True)
                if not target.exists(): shutil.copy2(fpath,target)
        # Remove index files only. Converted JSON assets (story/model/motion/config
        # JSON) are runtime data and must remain in the standalone character output.
        for pth in list(out.rglob('index.json')):
            try: pth.unlink()
            except OSError: pass
        for d in sorted((x for x in out.rglob('*') if x.is_dir()), key=lambda x:len(x.parts), reverse=True):
            try:
                if not any(d.iterdir()): d.rmdir()
            except OSError: pass
        print(f'[CHARACTER OUTPUT] flattened character assets -> {out}',flush=True)
        return 0
    finally:
        try: temp.unlink()
        except OSError: pass
if __name__=='__main__': raise SystemExit(main())
