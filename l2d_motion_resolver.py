# -*- coding: utf-8 -*-
from __future__ import annotations
import re
from pathlib import Path
from typing import Iterable

_SUFFIXES = ('.motion3.json', '.motion3', '.json')

def motion_stem(value: str) -> str:
    text = Path(str(value or '').replace('\\', '/')).name.strip()
    low = text.casefold()
    for suffix in _SUFFIXES:
        if low.endswith(suffix):
            return text[:-len(suffix)]
    return text

def norm(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', motion_stem(value).casefold())

def is_runtime_reset(value: str) -> bool:
    text = motion_stem(value).casefold().strip()
    return bool(re.search(r'reset(?:_n)?$', text)) or text in {'reset','none','null','off'}

def _actual_maps(actual_names: Iterable[str]):
    by_norm: dict[str, str] = {}; names=[]
    for raw in actual_names:
        stem=motion_stem(raw)
        if not stem: continue
        names.append(stem); by_norm.setdefault(norm(stem), stem)
    return names, by_norm

def variant_candidates(trigger: str) -> list[str]:
    stem=motion_stem(trigger)
    m=re.fullmatch(r'(EyeOpen|EyebrowEmotion|FaceEmotion|MouthEmotion)(\d+)(?:(_N))?', stem, re.I)
    if not m: return []
    family,digits,nflag=m.group(1),m.group(2),m.group(3)
    variant='N' if nflag else 'F'
    return [f'{family}{variant}{digits}', f'{family}{digits}']

def _scene_candidates(stem: str) -> list[str]:
    out=[]
    # Story occasionally uses _1 while exported clips use zero-padded _01 and
    # often put the variant on the loop clip.
    m=re.fullmatch(r'(Scene\d+)_0?1', stem, re.I)
    if m:
        out += [f'{m.group(1)}_01', f'{m.group(1)}_loop_01', f'{m.group(1)}_loop']
    # "Scene02b_loop" / "Scene03b_loop" are Animator state names.  The
    # resource package contains the normal SceneXX_loop clip, not a b file.
    m=re.fullmatch(r'(Scene\d+)b_loop', stem, re.I)
    if m:
        out += [f'{m.group(1)}_loop', m.group(1)]
    return out

def _typo_candidates(stem: str) -> list[str]:
    # A real Catalog family contains eyeopne00..03 (source typo).
    if re.fullmatch(r'EyeOpen\d+(?:_N)?', stem, re.I):
        return [re.sub(r'^EyeOpen', 'EyeOpne', stem, flags=re.I)]
    if re.fullmatch(r'EyeOpne\d+(?:_N)?', stem, re.I):
        return [re.sub(r'^EyeOpne', 'EyeOpen', stem, flags=re.I)]
    return []

def catalog_motion_token(bundle_name: str) -> tuple[str,str] | None:
    low=str(bundle_name or '').replace('\\','/').split('/')[-1].casefold()
    m=re.search(r'_hmr_(\d{11})_l2d_\d{11}_(?:addanimations|animations)_(.+?)\.fade\.asset_[0-9a-f]{32}\.bundle$', low)
    if not m: return None
    token=m.group(2)
    while True:
        new=re.sub(r'^(?:addanimations|animations)_+', '', token, flags=re.I)
        if new==token: break
        token=new
    return m.group(1), token

def _catalog_trigger_norms(trigger: str) -> set[str]:
    stem=motion_stem(trigger)
    vals={stem, *variant_candidates(stem), *_scene_candidates(stem), *_typo_candidates(stem)}
    # F/N is an exported clip distinction; Catalog source names generally use
    # the logical Story trigger without F/N.
    vals.add(re.sub(r'_N$', '', stem, flags=re.I))
    exported=re.fullmatch(r'(EyeOpen|EyebrowEmotion|FaceEmotion|MouthEmotion)(?:F|N)(\d+)', stem, re.I)
    if exported:
        vals.add(f'{exported.group(1)}{exported.group(2)}')
    return {norm(x) for x in vals if x}

def matching_catalog_sources(trigger: str, catalog_tokens: Iterable[str]) -> list[str]:
    wanted=_catalog_trigger_norms(trigger)
    out=[]
    for token in catalog_tokens or []:
        if norm(token) in wanted: out.append(str(token))
    return out

def _runtime_family_reason(stem: str, names: list[str]) -> str:
    """Known Animator/controller logical states that do not require a clip file."""
    # Numeric logical presets are common even when a specific model only ships
    # a subset (or no standalone files) for that family.
    if re.fullmatch(r'(EyeOpen|EyeEmotion|FaceEmotion|MouthEmotion|ExEmotion)\d+(?:_N)?', stem, re.I):
        return 'animator-family-preset'
    # Directional states can be parameter blends of the cardinal clips.
    m=re.fullmatch(r'(FaceAngle|EyeEmotion)(TopLeft|TopRight|BottomLeft|BottomRight)', stem, re.I)
    if m:
        family, direction=m.group(1),m.group(2)
        vertical='Top' if direction.startswith('Top') else 'Bottom'
        horizontal='Left' if direction.endswith('Left') else 'Right'
        keys={norm(x) for x in names}
        if norm(family+vertical) in keys and norm(family+horizontal) in keys:
            return 'animator-direction-blend'
    # Scene state aliases such as Scene05_01 or Scene02b_loop are controller
    # names; if the base/loop scene exists, no separate resource is required.
    if re.fullmatch(r'Scene\d+(?:_0?1|b_loop)', stem, re.I):
        return 'animator-scene-alias'
    return ''

def resolve_trigger(trigger: str, actual_names: Iterable[str], catalog_tokens: Iterable[str] | None=None) -> str | None:
    stem=motion_stem(trigger)
    if not stem: return None
    if is_runtime_reset(stem): return None
    names,by_norm=_actual_maps(actual_names)
    candidates=[stem, stem+'_loop', *variant_candidates(stem), *_scene_candidates(stem), *_typo_candidates(stem)]
    for candidate in candidates:
        hit=by_norm.get(norm(candidate))
        if hit: return hit

    em=re.fullmatch(r'EyeEmotion(Reset|Left|Right|Top|Bottom|TopLeft|TopRight|BottomLeft|BottomRight)(?:_N)?', stem, re.I)
    if em:
        numbered=[name for name in names if re.fullmatch(r'EyeEmotion\d+', name, re.I)]
        if numbered: return numbered[0]

    # Catalog is the authority for "is a downloadable motion source supposed
    # to exist?".  If a matching source exists but no motion3 was exported,
    # leave it unresolved so repair can fetch/extract exactly that source.
    if catalog_tokens is not None:
        if matching_catalog_sources(stem, catalog_tokens):
            return ''
        runtime=_runtime_family_reason(stem,names)
        if runtime: return '@runtime:'+runtime
    return ''

def trigger_resolvable(trigger: str, actual_names: Iterable[str], catalog_tokens: Iterable[str] | None=None) -> bool:
    if is_runtime_reset(trigger): return True
    return bool(resolve_trigger(trigger,actual_names,catalog_tokens))

def _family_prefix(stem: str) -> str:
    m=re.match(r'([A-Za-z]+)', stem or '')
    return (m.group(1) if m else '').casefold()

def diagnose_trigger(trigger: str, actual_names: Iterable[str], *, catalog_tokens: Iterable[str] | None=None, catalog_bundles: Iterable[str] | None=None, related_limit: int=24) -> dict:
    stem=motion_stem(trigger); names,by_norm=_actual_maps(actual_names)
    unique=[]; seen=set()
    for name in names:
        k=name.casefold()
        if k not in seen: seen.add(k); unique.append(name)
    candidates=[]
    def add(value,why):
        if not value:return
        item={'name':value,'reason':why,'exists':norm(value) in by_norm}
        if item not in candidates:candidates.append(item)
    add(stem,'exact'); add(stem+'_loop','scene-loop-fallback')
    for c in variant_candidates(stem): add(c,'F/N-family-alias')
    for c in _scene_candidates(stem): add(c,'scene-controller-alias')
    for c in _typo_candidates(stem): add(c,'known-Catalog-typo')
    cat_matches=matching_catalog_sources(stem,catalog_tokens or []) if catalog_tokens is not None else []
    resolved=resolve_trigger(stem,unique,catalog_tokens)
    runtime=is_runtime_reset(stem)
    family=_family_prefix(stem); related=[n for n in unique if n.casefold().startswith(family)][:related_limit] if family else []
    if runtime:
        verdict='runtime-reset'; reason='Reset/Off 是运行时状态复位，不需要独立 motion3。'
    elif resolved and str(resolved).startswith('@runtime:'):
        verdict='runtime-logical'; reason='Catalog 无独立对应 motion 源；这是 Animator/控制器逻辑状态，不属于缺失资源。'
    elif resolved:
        verdict='resolved'; reason=f'触发器已映射到导出 motion: {resolved}'
    elif cat_matches:
        verdict='source-missing'; reason='Catalog 存在对应 motion 源，但本地没有可映射 motion3；应定向下载/重提取该 motion 源。'
    else:
        verdict='unresolved'; reason='未找到导出 motion，也未能确认运行时逻辑规则。'
    bundles=[]
    if catalog_bundles:
        wanted=set(cat_matches)
        for b in catalog_bundles:
            parsed=catalog_motion_token(str(b))
            if parsed and parsed[1] in wanted: bundles.append(str(b))
    return {'trigger':str(trigger),'stem':stem,'normalized':norm(stem),'runtimeReset':runtime,
            'resolved':resolved or '','verdict':verdict,'reason':reason,'candidates':candidates,
            'actualCount':len(unique),'relatedActualMotions':related,'catalogMotionTokens':sorted(set(str(x) for x in (catalog_tokens or []))),
            'catalogMatches':cat_matches,'catalogSourceBundles':bundles}
