# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from tool_paths import find_project_root, catalog_path
from l2d_motion_resolver import is_runtime_reset, trigger_resolvable, diagnose_trigger, catalog_motion_token, matching_catalog_sources, motion_stem

AUDIT_WORKERS = 30
AUDIO_EXTS = ('.ogg', '.wav', '.mp3', '.m4a', '.aac')
IMG_EXTS = ('.png', '.jpg', '.jpeg', '.webp')
BGM_RE = re.compile(r'\bbgm_?(\d+)\b', re.I)
SE_RE = re.compile(r'\bse(\d+)\b', re.I)
BG_RE = re.compile(r'\babg\d+[a-z]?\b', re.I)
VOICE_RE = re.compile(r'\b(?:vc|bgv)_[A-Za-z0-9_]+\b', re.I)
STAND_RE = re.compile(r'^(\d{9})([gx])$', re.I)


@dataclass
class Issue:
    story_id: str
    kind: str
    resource: str
    reason: str
    repair: str
    paths: list[str]
    details: dict | None = None


_CATALOG_L2D_CACHE = None
_CATALOG_STORY_IDS = set()

def _catalog_l2d_sources():
    global _CATALOG_L2D_CACHE, _CATALOG_STORY_IDS
    if _CATALOG_L2D_CACHE is not None:
        return _CATALOG_L2D_CACHE
    out={}
    try:
        import catalog_downloader as cd
        cp=Path(catalog_path())
        if cp.is_file():
            for name in cd.extract_bundle_names(cp):
                sm=re.search(r'_mainchara_hmr_(\d{11})_hmr_\1\.txt_', str(name), re.I)
                if sm: _CATALOG_STORY_IDS.add(sm.group(1))
                parsed=catalog_motion_token(name)
                if not parsed: continue
                sid,token=parsed
                row=out.setdefault(sid, {'tokens':set(), 'bundles':[]})
                row['tokens'].add(token); row['bundles'].append(name)
    except Exception as exc:
        print(f'[AUDIT WARN] Catalog L2D source index unavailable: {exc}', flush=True)
    _CATALOG_L2D_CACHE=out
    return out


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except Exception:
        return default


def iter_story_dirs(root: Path) -> list[Path]:
    base = root / 'data_r18_all' / 'stories'
    if not base.exists():
        return []
    return sorted((p for p in base.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: p.name)


def resolve_path(root: Path, story_dir: Path, value: str) -> Path:
    text = str(value or '').replace('\\', '/').lstrip('./')
    if not text:
        return story_dir / '__missing__'
    p = Path(text)
    if p.is_absolute():
        return p
    if text.startswith('audio/'):
        return story_dir / text
    if text.startswith(('data/', 'data_r18_all/', 'backgrounds/')):
        return root / text
    return story_dir / text


def path_exists(root: Path, story_dir: Path, value: str) -> bool:
    try:
        p = resolve_path(root, story_dir, value)
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def cue_candidates(root: Path, story_dir: Path, cue_id: str, entry: dict | None) -> list[Path]:
    values = []
    if isinstance(entry, dict):
        if entry.get('path'):
            values.append(entry['path'])
        for x in entry.get('paths') or []:
            if x and x not in values:
                values.append(x)
    cue = str(cue_id or '')
    low = cue.lower()
    if BGM_RE.search(low):
        cid = 'bgm' + BGM_RE.search(low).group(1)
        values += [
            f'data/audio/bgm/decoded/{cid}.ogg',
            f'data_r18_all/audio/BGM/{cid}.ogg',
            f'data_r18_all/audio/bgm/{cid}.ogg',
            f'audio/decoded/bgm/{cid}.ogg',
        ]
    elif SE_RE.search(low):
        cid = 'se' + SE_RE.search(low).group(1)
        values += [
            f'data/audio/se/decoded/misc/{cid}.ogg',
            f'data_r18_all/audio/SE/{cid}.ogg',
            f'data_r18_all/audio/se/{cid}.ogg',
            f'audio/decoded/se/{cid}.ogg',
            f'audio/decoded/misc/{cid}.ogg',
        ]
    elif low.startswith(('vc_', 'bgv_')):
        # Private audio may have suffixes/subsong numbers, so exact metadata paths are preferred.
        values += [f'audio/decoded/vc/{cue}.ogg', f'audio/decoded/bgv/{cue}.ogg']
    out = []
    seen = set()
    for v in values:
        p = resolve_path(root, story_dir, v)
        key = str(p).lower()
        if key not in seen:
            seen.add(key); out.append(p)
    return out


def local_audio_matches(story_dir: Path, cue: str) -> list[Path]:
    audio = story_dir / 'audio' / 'decoded'
    if not audio.exists():
        return []
    low = cue.lower()
    out=[]
    try:
        for p in audio.rglob('*'):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS and (p.stem.lower() == low or p.stem.lower().startswith(low + '_')):
                out.append(p)
    except OSError:
        pass
    return out


def _all_commands(story: dict):
    for script in story.get('scripts', []) or []:
        if not isinstance(script, dict):
            continue
        for cmd in script.get('commands', []) or []:
            if isinstance(cmd, dict):
                yield cmd
        # Some older generated stories only expose messages.
        for msg in script.get('messages', []) or []:
            if isinstance(msg, dict):
                yield msg


def _bool_arg(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text=str(value).strip().lower()
    if text in ('false','0','off','no','disable','disabled'):
        return False
    if text in ('true','1','on','yes','enable','enabled'):
        return True
    return default

def _runtime_only_motion(name: str) -> bool:
    low=str(name or '').strip().lower()
    return (not low) or is_runtime_reset(low)

def _motion_aliases(name: str) -> set[str]:
    text=Path(str(name or '').replace('\\','/')).name.lower().strip()
    for suffix in ('.motion3.json','.motion3','.json'):
        if text.endswith(suffix): text=text[:-len(suffix)]
    compact=re.sub(r'[^a-z0-9]+','',text)
    out={text,compact}
    for suffix in ('_loop','-loop','loop'):
        if text.endswith(suffix):
            base=text[:-len(suffix)].rstrip('_- ')
            out.add(base); out.add(re.sub(r'[^a-z0-9]+','',base))
    if compact.endswith('loop'):
        out.add(compact[:-4])
    return {x for x in out if x}

def command_requirements(story: dict):
    bgs=set(); bgms=set(); ses=set(); voices=set(); motions=set(); stands=set(); audio_ids=set()
    # audioIds are safe summary metadata. Motion summary metadata is NOT used
    # as authoritative because it also contains L2DMotion(..., false) stop calls.
    for script in story.get('scripts', []) or []:
        if isinstance(script, dict):
            for x in script.get('audioIds', []) or []:
                audio_ids.add(str(x))
    saw_motion_command=False
    for cmd in _all_commands(story):
        name = str(cmd.get('command', '')).lower()
        args = cmd.get('args') or []
        texts = [str(x) for x in args]
        if cmd.get('cue'):
            texts.append(str(cmd.get('cue')))
        if cmd.get('voice'):
            voices.add(str(cmd.get('voice'))); audio_ids.add(str(cmd.get('voice')))
        if name == 'charaload':
            asset = str(args[1] if len(args) > 1 else '').strip()
            if STAND_RE.fullmatch(asset):
                stands.add(asset.lower())
        if cmd.get('motion'):
            motion=str(cmd.get('motion'))
            if name in ('l2dmotion','asyncl2dmotion'):
                saw_motion_command=True
                # Original Story uses the second argument as enabled/disabled.
                # A disabled motion is a state-release command, not a file dependency.
                enabled=_bool_arg(args[1], True) if len(args) > 1 else True
                if enabled and not _runtime_only_motion(motion):
                    motions.add(motion)
            elif not _runtime_only_motion(motion):
                motions.add(motion)
        blob = ' '.join([name, *texts, str(cmd.get('rawCommand',''))])
        if name == 'bg' or name.startswith('bg'):
            bgs.update(m.group(0).lower() for m in BG_RE.finditer(blob))
        if name == 'bgmplay' or 'bgm' in name:
            bgms.update('bgm'+m.group(1) for m in BGM_RE.finditer(blob))
        if name in ('seplay','asyncseplay') or name.startswith('se'):
            ses.update('se'+m.group(1) for m in SE_RE.finditer(blob))
        if name in ('message','l2dmessage'):
            voices.update(m.group(0) for m in VOICE_RE.finditer(blob))
    if not saw_motion_command:
        # Compatibility with older story.json files that only have summary motions.
        for script in story.get('scripts', []) or []:
            if isinstance(script, dict):
                for x in script.get('motions', []) or []:
                    if not _runtime_only_motion(str(x)):
                        motions.add(str(x))

    for x in audio_ids:
        low=x.lower()
        m=BGM_RE.search(low)
        if m: bgms.add('bgm'+m.group(1)); continue
        m=SE_RE.search(low)
        if m: ses.add('se'+m.group(1)); continue
        if low.startswith(('vc_','bgv_')): voices.add(x)
    cues=((story.get('audio') or {}).get('cues') or {})
    if isinstance(cues,dict):
        for key,entry in cues.items():
            key=str(key); category=str((entry or {}).get('category','')).lower() if isinstance(entry,dict) else ''
            low=key.lower()
            m=BGM_RE.search(low)
            if category=='bgm' or m:
                if m: bgms.add('bgm'+m.group(1))
                continue
            m=SE_RE.search(low)
            if category=='se' or m:
                if m: ses.add('se'+m.group(1))
                continue
            if category in ('voice','vc','bgv') or low.startswith(('vc_','bgv_')):
                voices.add(key)
    if not stands:
        sid=str(story.get('id') or story.get('sourceId') or '')
        m=re.match(r'^(\d{9})', sid)
        if m:
            stands.add((m.group(1)+'g').lower())
    return {'background':bgs,'bgm':bgms,'se':ses,'voice':voices,'motion':motions,'stand':stands,'audio':audio_ids}



def _load_story_with_error(path: Path):
    try:
        text=path.read_text(encoding='utf-8-sig')
    except Exception as exc:
        return None, f'read-error: {type(exc).__name__}: {exc}'
    try:
        return json.loads(text), ''
    except Exception as exc:
        return None, f'json-error: {type(exc).__name__}: {exc}'


def _motion_command_locations(story: dict, trigger: str) -> list[dict]:
    wanted=str(trigger or '').casefold()
    out=[]
    for script in story.get('scripts', []) or []:
        if not isinstance(script, dict):
            continue
        script_id=str(script.get('id') or script.get('name') or '')
        for cmd in script.get('commands', []) or []:
            if not isinstance(cmd, dict):
                continue
            name=str(cmd.get('command') or '').casefold()
            if name not in ('l2dmotion','asyncl2dmotion'):
                continue
            motion=str(cmd.get('motion') or ((cmd.get('args') or [''])[0] if (cmd.get('args') or []) else ''))
            if motion.casefold() != wanted:
                continue
            args=cmd.get('args') or []
            enabled=_bool_arg(args[1], True) if len(args) > 1 else True
            out.append({
                'script': script_id,
                'commandIndex': cmd.get('index'),
                'sourceLine': cmd.get('line'),
                'command': name,
                'rawCommand': str(cmd.get('rawCommand') or ''),
                'args': [str(x) for x in args],
                'enabled': bool(enabled),
            })
    return out


def _print_one_diagnostic(issue, prefix='AUDIT DETAIL'):
    if isinstance(issue, Issue):
        data=asdict(issue)
    elif isinstance(issue, dict):
        data=issue
    else:
        return
    kind=str(data.get('kind',''))
    if kind not in ('l2d-motion-ref','story','story-orphan','story-audit'):
        return
    sid=str(data.get('story_id',''))
    resource=str(data.get('resource',''))
    print(f'[{prefix}] story={sid} kind={kind} resource={resource}', flush=True)
    print(f'  reason: {data.get("reason","")}', flush=True)
    details=data.get('details') or {}
    if kind == 'l2d-motion-ref':
        resolver=details.get('resolver') or {}
        print(f'  trigger: {resolver.get("trigger", resource)}', flush=True)
        print(f'  normalized: {resolver.get("normalized","")}', flush=True)
        print(f'  verdict: {resolver.get("verdict","")} resolved={resolver.get("resolved","") or "<none>"}', flush=True)
        print(f'  actual-motion-count: {resolver.get("actualCount",0)}', flush=True)
        candidates=resolver.get('candidates') or []
        if candidates:
            rendered=', '.join(f'{x.get("name")}[{"YES" if x.get("exists") else "no"}:{x.get("reason")}]' for x in candidates)
            print(f'  candidates: {rendered}', flush=True)
        related=resolver.get('relatedActualMotions') or []
        print('  related-actual: ' + (', '.join(str(x) for x in related) if related else '<none>'), flush=True)
        cm=resolver.get('catalogMatches') or []
        print('  catalog-match: ' + (', '.join(str(x) for x in cm) if cm else '<none/runtime-logical>'), flush=True)
        cb=details.get('catalogSourceBundles') or resolver.get('catalogSourceBundles') or []
        if cb:
            print(f'  catalog-source-bundles: {len(cb)}', flush=True)
            for _b in cb[:6]: print(f'    - {_b}', flush=True)
        locations=details.get('commands') or []
        if locations:
            print(f'  command-refs: {len(locations)}', flush=True)
            for loc in locations[:20]:
                print(
                    '    - script={script} commandIndex={commandIndex} sourceLine={sourceLine} '
                    'command={command} enabled={enabled} args={args}'.format(**loc),
                    flush=True
                )
            if len(locations) > 20:
                print(f'    ... +{len(locations)-20} more references (see JSON report)', flush=True)
        print(f'  motion-dir: {details.get("motionDir","")}', flush=True)
    else:
        for key in ('storyPath','exists','bytes','parseError','catalogHasStory'):
            if key in details:
                print(f'  {key}: {details.get(key)}', flush=True)
    paths=data.get('paths') or []
    if paths:
        print('  paths: ' + ' | '.join(str(x) for x in paths[:8]), flush=True)


def print_report_diagnostics(report: dict, prefix='AUDIT REMAINING'):
    rows=[]
    for issue in report.get('issues', []) or []:
        if str(issue.get('kind','')) in ('l2d-motion-ref','story','story-orphan','story-audit'):
            rows.append(issue)
    if not rows:
        print(f'[{prefix}] no l2d-motion-ref/story diagnostics', flush=True)
        return
    print(f'[{prefix}] detailed={len(rows)}', flush=True)
    for issue in rows:
        _print_one_diagnostic(issue, prefix=prefix)

def inspect_story(root: Path, story_dir: Path) -> dict:
    sid=story_dir.name
    story_path=story_dir/'story.json'
    story, story_error=_load_story_with_error(story_path)
    issues:list[Issue]=[]
    if not isinstance(story, dict):
        try:
            size=story_path.stat().st_size if story_path.is_file() else 0
        except OSError:
            size=0
        details={
            'storyPath': str(story_path),
            'exists': story_path.is_file(),
            'bytes': size,
            'parseError': story_error or 'story.json root is not an object',
        }
        # If Catalog has no Story bundle for this numeric directory, it is an
        # orphan/leftover folder rather than a downloadable missing Story. Keep
        # it visible for cleanup but never trigger the full private downloader.
        _catalog_l2d_sources()
        catalog_has_story = sid in _CATALOG_STORY_IDS
        details['catalogHasStory'] = catalog_has_story
        if catalog_has_story:
            issue=Issue(sid,'story',sid,'story.json 缺失或无法解析','private-story',[str(story_path)],details)
        else:
            issue=Issue(sid,'story-orphan',sid,'目录中没有 story.json，且 Catalog 不存在该剧情 ID；视为孤立目录，不自动下载','manual-review',[str(story_path)],details)
        return {'story_id':sid,'issues':[issue], 'requirements':{}}
    req=command_requirements(story)
    # Script text files declared by story.json.
    for script in story.get('scripts',[]) or []:
        if isinstance(script,dict) and script.get('text') and not path_exists(root,story_dir,script['text']):
            issues.append(Issue(sid,'script',str(script.get('id') or sid),'剧本文本文件缺失','private-story',[str(resolve_path(root,story_dir,script['text']))]))

    # Shared backgrounds.
    for rid in sorted(req['background']):
        candidates=[root/'data'/'backgrounds'/'novel'/f'{rid}.png', root/'backgrounds'/f'{rid}.png']
        if not any(p.is_file() and p.stat().st_size>0 for p in candidates):
            issues.append(Issue(sid,'background',rid,'剧情命令引用背景，但公共背景文件不存在','public-cue',[str(p) for p in candidates]))

    cues=((story.get('audio') or {}).get('cues') or {})
    if not isinstance(cues,dict): cues={}
    for kind in ('bgm','se'):
        for rid in sorted(req[kind]):
            entry=cues.get(rid) or cues.get(rid.replace('bgm','bgm_',1))
            candidates=cue_candidates(root,story_dir,rid,entry if isinstance(entry,dict) else None)
            if not any(p.is_file() and p.stat().st_size>0 for p in candidates):
                issues.append(Issue(sid,kind,rid,'story 引用了公共音频，但所有已知路径都缺失','public-cue',[str(p) for p in candidates]))
    for rid in sorted(req['voice']):
        entry=cues.get(rid)
        candidates=cue_candidates(root,story_dir,rid,entry if isinstance(entry,dict) else None)
        existing=any(p.is_file() and p.stat().st_size>0 for p in candidates) or bool(local_audio_matches(story_dir,rid))
        if not existing:
            issues.append(Issue(sid,'voice',rid,'剧情语音/BGV cue 缺失','private-story',[str(p) for p in candidates]))

    # Character Stand is a separate asset chain from Live2D.  Every story that
    # loads a character must have its generated stand body/face/meta available.
    # This check deliberately does not inspect L2D model files.
    for asset in sorted(req.get('stand') or []):
        folder = root / 'data' / 'chara' / asset.lower()
        body_candidates = [folder/'Body.png', folder/'body.png']
        faces = folder/'faces'
        meta = folder/'meta.json'
        body_ok = any(x.is_file() and x.stat().st_size > 0 for x in body_candidates)
        face_ok = False
        if faces.is_dir():
            try:
                face_ok = any(x.is_file() and x.suffix.lower() in IMG_EXTS and x.stat().st_size > 0 for x in faces.iterdir())
            except OSError:
                face_ok = False
        meta_ok = meta.is_file() and meta.stat().st_size > 0
        if not (body_ok and face_ok and meta_ok):
            missing=[]
            if not body_ok: missing.append('Body')
            if not face_ok: missing.append('faces')
            if not meta_ok: missing.append('meta')
            issues.append(Issue(
                sid,'stand',asset,
                '剧情对应角色立绘不完整: '+','.join(missing),
                'character-stand',[str(folder)]
            ))

    # Live2D file metadata.
    live=story.get('live2d') or {}
    if isinstance(live,dict):
        catalog_row=_catalog_l2d_sources().get(sid) or {}
        catalog_tokens=sorted(catalog_row.get('tokens') or [])
        catalog_bundles=list(catalog_row.get('bundles') or [])
        refs=[]
        moc=live.get('moc')
        if isinstance(moc,dict) and moc.get('path'): refs.append(('l2d-moc',str(moc['path'])))
        model3=live.get('model3')
        if model3: refs.append(('l2d-model3',str(model3)))
        for tex in live.get('textures',[]) or []:
            if isinstance(tex,dict) and tex.get('path'): refs.append(('l2d-texture',str(tex['path'])))
        for mot in live.get('motions',[]) or []:
            if isinstance(mot,dict) and mot.get('path'): refs.append(('l2d-motion',str(mot['path'])))
        for kind,path in refs:
            if not path_exists(root,story_dir,path):
                if kind == 'l2d-motion':
                    stem=motion_stem(path)
                    matches=matching_catalog_sources(stem,catalog_tokens)
                    bundles=[]
                    wanted=set(matches)
                    for b in catalog_bundles:
                        parsed=catalog_motion_token(b)
                        if parsed and parsed[1] in wanted:
                            bundles.append(b)
                    if bundles:
                        issues.append(Issue(
                            sid,kind,path,
                            'story.live2d 声明的 motion 文件不存在，但 Catalog 有对应源；仅定向重提取该 motion',
                            'private-story',[str(resolve_path(root,story_dir,path))],
                            {'catalogSourceBundles':bundles,'catalogMatches':matches,'motionStem':stem}
                        ))
                    else:
                        issues.append(Issue(sid,kind,path,'story.live2d 声明的 motion 文件不存在，且 Catalog 无独立对应源；保留为元数据告警','manual-review',[str(resolve_path(root,story_dir,path))]))
                else:
                    issues.append(Issue(sid,kind,path,'story.live2d 声明的文件不存在','private-story',[str(resolve_path(root,story_dir,path))]))
        # Command references should be checked against BOTH story metadata and
        # actual exported files. This avoids stale story.live2d metadata creating
        # false positives after a successful L2D conversion.
        motion_names=set()
        for m in live.get('motions',[]) or []:
            if isinstance(m,dict):
                for key in ('name','trigger','id'):
                    if m.get(key): motion_names.update(_motion_aliases(str(m[key])))
                if m.get('path'): motion_names.update(_motion_aliases(str(m['path'])))
        motion_dir=story_dir/'motions'
        if motion_dir.exists():
            for mp in motion_dir.glob('*.motion3.json'):
                motion_names.update(_motion_aliases(mp.name))
        actual_motion_names=[]
        # Only files that actually exist count as exported motions. Story metadata
        # can be stale and must not mask a deleted/failed conversion.
        for m in live.get('motions',[]) or []:
            if not isinstance(m,dict) or not m.get('path'):
                continue
            if path_exists(root, story_dir, str(m.get('path'))):
                actual_motion_names.append(str(m.get('name') or Path(str(m.get('path'))).name))
        if motion_dir.exists():
            actual_motion_names.extend(mp.name for mp in motion_dir.glob('*.motion3.json') if mp.is_file() and mp.stat().st_size > 0)
        for motion in sorted(req['motion']):
            if _runtime_only_motion(motion):
                continue
            if actual_motion_names and not trigger_resolvable(motion, actual_motion_names, catalog_tokens):
                diagnostic=diagnose_trigger(
                    motion, actual_motion_names, catalog_tokens=catalog_tokens, catalog_bundles=catalog_bundles
                )
                details={
                    'resolver': diagnostic,
                    'commands': _motion_command_locations(story, motion),
                    'motionDir': str(motion_dir),
                    'motionDirExists': motion_dir.is_dir(),
                    'metadataMotionCount': len(live.get('motions',[]) or []),
                    'catalogSourceBundles': diagnostic.get('catalogSourceBundles') or [],
                }
                repair='private-story' if diagnostic.get('verdict')=='source-missing' else 'manual-review'
                issues.append(Issue(
                    sid,'l2d-motion-ref',motion,
                    diagnostic.get('reason') or 'Live2D motion 引用无法解析', repair, [], details
                ))


    return {'story_id':sid,'issues':issues,'requirements':{k:sorted(v) for k,v in req.items()}}


def _emit_progress(percent, stage, detail='', current=None, total=None):
    payload={'percent':float(percent),'stage':str(stage),'detail':str(detail)}
    if current is not None: payload['current']=int(current)
    if total is not None: payload['total']=int(total)
    print('@@DOT_PROGRESS\t'+json.dumps(payload,ensure_ascii=False), flush=True)


def _inspect_dirs(root: Path, dirs: list[Path], workers: int, *, progress=True, stage='30线程资源检查'):
    results=[]; total=len(dirs)
    w=max(1,min(int(workers),30,total or 1))
    print(f'[AUDIT THREADS] workers={w} stories={total}', flush=True)
    if progress:
        _emit_progress(1, stage, f'准备检查 {total} 个 story.json', 0, total)
    # Build Catalog motion-source index once on the main thread; otherwise 30
    # workers could race to parse the same 12 MB catalog simultaneously.
    _catalog_l2d_sources()
    if dirs:
        with ThreadPoolExecutor(max_workers=w, thread_name_prefix='story-audit') as ex:
            future_map={ex.submit(inspect_story,root,d):d for d in dirs}
            done=0
            for fut in as_completed(future_map):
                d=future_map[fut]
                try:
                    row=fut.result()
                except Exception as exc:
                    row={'story_id':d.name,'issues':[Issue(d.name,'story-audit',d.name,f'检查异常: {exc}','private-story',[])],'requirements':{}}
                results.append(row); done+=1
                print(f'[AUDIT {done}/{total}] story={d.name} issues={len(row.get("issues",[]))}', flush=True)
                for _issue in row.get('issues',[]) or []:
                    _print_one_diagnostic(_issue, prefix='AUDIT DETAIL')
                if progress:
                    _emit_progress(2 + 96*done/max(1,total), stage, f'{d.name} 检查完成', done, total)
    results.sort(key=lambda r:str(r.get('story_id','')))
    return results,w


def _make_report(root: Path, results, *, workers: int, stories_total: int, base_report=None, refreshed_story_ids=None):
    refreshed={str(x) for x in (refreshed_story_ids or [])}
    issues=[]
    requirements={}
    if isinstance(base_report,dict):
        for item in base_report.get('issues',[]) or []:
            if str(item.get('story_id','')) not in refreshed:
                try: issues.append(Issue(**{k:item.get(k) for k in ('story_id','kind','resource','reason','repair','paths','details')}))
                except Exception: pass
        requirements.update(base_report.get('storyRequirements') or {})
        for sid in refreshed:
            requirements.pop(sid,None)
    for row in results:
        issues.extend(row.get('issues') or [])
        requirements[str(row.get('story_id',''))]=row.get('requirements') or {}
    by_kind={}
    for i in issues: by_kind[i.kind]=by_kind.get(i.kind,0)+1
    private_stories=sorted({i.story_id for i in issues if i.repair=='private-story'})
    public=[]; seen=set()
    for i in issues:
        if i.repair!='public-cue': continue
        key=(i.kind,i.resource)
        if key not in seen:
            seen.add(key); public.append({'kind':i.kind,'resource':i.resource,'story_id':i.story_id})
    return {
        'schemaVersion':3,
        'generatedAt':datetime.now().astimezone().isoformat(timespec='seconds'),
        'root':str(Path(root).resolve()),'workers':workers,'stories':int(stories_total),'issueCount':len(issues),
        'byKind':by_kind,'privateStories':private_stories,'publicResources':public,
        'issues':[asdict(i) for i in issues],
        'storyRequirements':requirements,
        'refreshedStoryIds':sorted(refreshed),
    }


def audit_project(root: Path, workers: int=AUDIT_WORKERS) -> dict:
    root=Path(root).resolve(); dirs=iter_story_dirs(root)
    results,w=_inspect_dirs(root,dirs,workers,progress=True)
    report=_make_report(root,results,workers=w,stories_total=len(dirs))
    _emit_progress(100, '资源检查完成', f'剧情 {len(dirs)}，缺失/异常 {report["issueCount"]}', len(dirs), len(dirs))
    return report


def refresh_report_subset(root: Path, report: dict, story_ids, workers: int=AUDIT_WORKERS) -> dict:
    """Recheck only stories referenced by an existing report, then merge the result.

    This is the linkage path used by "检查并修复": a previous full audit is reused
    instead of parsing every story.json a second time.
    """
    root=Path(root).resolve(); wanted=sorted({str(x) for x in story_ids if str(x).strip()})
    by_id={d.name:d for d in iter_story_dirs(root)}
    dirs=[by_id[sid] for sid in wanted if sid in by_id]
    print(f'[AUDIT REUSE] targeted={len(dirs)} previousStories={report.get("stories",0)}', flush=True)
    results,w=_inspect_dirs(root,dirs,workers,progress=False,stage='定向复核检查结果')
    refreshed=set(wanted)
    # Missing story directories are represented as a private-story issue.
    present={d.name for d in dirs}
    for sid in wanted:
        if sid not in present:
            expected=root/'data_r18_all'/'stories'/sid/'story.json'
            results.append({'story_id':sid,'issues':[Issue(
                sid,'story',sid,'剧情目录不存在','private-story',[str(expected)],
                {'storyPath':str(expected),'exists':False,'bytes':0,'parseError':'story directory missing'}
            )],'requirements':{}})
    total=len(iter_story_dirs(root))
    return _make_report(root,results,workers=w,stories_total=total,base_report=report,refreshed_story_ids=refreshed)

def main(argv=None):
    ap=argparse.ArgumentParser(description='Dot 离线资源维护器 - 30线程剧情资源检查')
    ap.add_argument('--root',default=None)
    ap.add_argument('--workers',type=int,default=AUDIT_WORKERS)
    ap.add_argument('--output',default=None)
    args=ap.parse_args(argv)
    root=Path(args.root).resolve() if args.root else find_project_root(Path(__file__))
    report=audit_project(root,args.workers)
    out=Path(args.output) if args.output else root/'resource_audit.json'
    out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f"[AUDIT] stories={report['stories']} issues={report['issueCount']} workers={report['workers']}")
    for kind,count in sorted(report['byKind'].items()): print(f'  {kind}: {count}')
    print_report_diagnostics(report, prefix='AUDIT REMAINING')
    print(f'[REPORT] {out}')
    return 1 if report['issueCount'] else 0

if __name__=='__main__':
    raise SystemExit(main())
