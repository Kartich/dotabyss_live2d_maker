# -*- coding: utf-8 -*-
"""Bundle/resource classifier derived from the supplied offline directory tree.

Rules are schema-based: source package folder + inner Unity asset folder + filename.
Resource-id regexes are used only to validate/extract the token *after* the asset
family has been identified from its path structure.
"""
from __future__ import annotations
import re
from dataclasses import dataclass

@dataclass(frozen=True)
class ResourceInfo:
    kind: str
    token: str | None = None
    scope: str = 'common'
    dest: str = 'not-extracted'
    confidence: str = 'exact'

HEX = r'[0-9a-f]{32}'

def _rx(pattern, flags=re.I): return re.compile(pattern, flags)

def classify_bundle(name: str) -> ResourceInfo:
    n = str(name).strip()

    # ---- Exact functional families reflected directly in the output tree ----
    m = _rx(r'^general-ui-bg-novel_assets_assets_project_lazyassets_general_ui_bg_novel_(abg\d+[a-z]?)\.png_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('background', m.group(1).lower(), 'common', 'data/backgrounds/novel')

    m = _rx(r'^(?:general-sound-cri|r18-only-sound-cri)_assets_assets_project_lazyassets_(?:general_sound_cri|r18-only_sound_cri)_webgl_workunit_novel_(?:r18_)?se_(se\d+)\.acb_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('se', m.group(1).lower(), 'common' if n.startswith('general-') else 'r18', 'data/audio/se/decoded/misc')

    # BGM is a paired CRI asset: the ACB carries cue/metadata and the AWB
    # carries the actual audio payload. Both must be retained under the same
    # request folder; Stage 5 uses the AWB for conversion and may use the ACB
    # as the companion metadata asset.
    m = _rx(r'^general-sound-cri_assets_assets_project_lazyassets_general_sound_cri_webgl_workunit_novel_bgm_(bgm\d+)\.acb_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('bgm', m.group(1).lower(), 'common', 'data/audio/bgm/decoded')
    m = _rx(r'^general-sound-cri_assets_assets_project_lazyassets_general_sound_cri_webgl_workunit_novel_bgm_(bgm\d+)\.awb_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('bgm', m.group(1).lower(), 'common', 'data/audio/bgm/decoded')

    m = _rx(r'^r18-only-sound-cri_assets_assets_project_lazyassets_r18-only_sound_cri_webgl_workunit_novel_r18_bgm_(bgm\d+)\.acb_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('bgm', m.group(1).lower(), 'r18', 'data/audio/bgm/decoded')
    m = _rx(r'^r18-only-sound-cri_assets_assets_project_lazyassets_r18-only_sound_cri_webgl_workunit_novel_r18_bgm_(bgm\d+)\.awb_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('bgm', m.group(1).lower(), 'r18', 'data/audio/bgm/decoded')

    # CDN can also expose the companion AWB as a standalone bundle named bgm####.awb_<hash>.bundle.
    # It is the actual audio payload paired with the ACB metadata bundle.
    m = _rx(r'^(bgm\d+)\.awb_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('bgm', m.group(1).lower(), 'common', 'data/audio/bgm/decoded')

    m = _rx(r'^general-sound-cri_assets_assets_project_lazyassets_general_sound_cri_webgl_workunit_ingame_bgm_(bgm\d+)\.acb_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('bgm_ingame', m.group(1).lower(), 'common', 'not-used-by-novel-player')

    m = _rx(r'^(?:general-sound-cri|r18-only-sound-cri)_assets_assets_project_lazyassets_(?:general_sound_cri|r18-only_sound_cri)_webgl_workunit_novel(?:_r18)?_voice_chara_chara_\d{9}_(?:hmn|hmr)_(\d{11})\.acb_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('voice', f'vc_{m.group(1).lower()}', 'common' if n.startswith('general-') else 'r18', 'data_r18_all/stories/<story>/audio/decoded/voice')

    m = _rx(r'^r18-only-sound-cri_assets_assets_project_lazyassets_r18-only_sound_cri_webgl_workunit_novel_r18_backgroundvoice_chara_chara_\d{9}_hmr_(\d{11})_bgv\.acb_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('bgv', f'bgv_{m.group(1).lower()}', 'r18', 'data_r18_all/stories/<story>/audio/decoded/bgv')

    # Story TextAsset: hmr_<story>_hmr_<story>.txt
    m = _rx(r'^r18-only-novel_assets_assets_project_lazyassets_r18-only_novel_mainchara_hmr_(\d{11})_hmr_\1\.txt_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('story', m.group(1).lower(), 'r18', 'data_r18_all/stories/<story>')

    # Other r18-only-novel TextAssets are still Story-family assets, but not the
    # normal numeric story records (e.g. Live2DCheck).
    m = _rx(r'^r18-only-novel_assets_assets_project_lazyassets_r18-only_novel_mainchara_hmr_([^_]+)_hmr_([^_]+)\.txt_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('story_other', m.group(2).lower(), 'r18', 'data_r18_all/stories/<story>')

    # Exact novel character stand assets. Shared sub-prefabs are a different family.
    m = _rx(r'^r18-only-charastand_assets_assets_project_lazyassets_r18-only_charastand_novel_prefabs_[gx]_charastand(\d{9}[gx]?)\.prefab_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('stand', m.group(1).lower(), 'r18', 'data/chara/<character>')

    if _rx(r'^r18-only-charastand_assets_assets_project_lazyassets_r18-only_charastand_novel_').search(n):
        return ResourceInfo('stand_helper', None, 'r18', 'not-extracted')

    m = _rx(r'^r18-only-icon-story-s_assets_assets_project_lazyassets_r18-only_icon_story_s_story_s_hmr_(\d{10})\.png_'+HEX+r'\.bundle$').search(n)
    if m: return ResourceInfo('thumb', m.group(1).lower(), 'r18', 'data_r18_all/thumb')

    if _rx(r'^general-ui_assets_assets_project_lazyassets_general_ui_emotion_charastand_').search(n):
        return ResourceInfo('emotion', None, 'common', 'data/emotion/charastand')

    # Live2D: order matters. fadeMotionList is l2d_fade, while concrete
    # animations/addanimations are l2d_motion.
    if _rx(r'^r18-only-novel_assets_assets_project_lazyassets_r18-only_novel_mainchara_hmr_\d{11}_l2d_\d{11}_l2d_.*\.fademotionlist\.asset_'+HEX+r'\.bundle$').search(n):
        return ResourceInfo('l2d_fade', None, 'r18', 'data_r18_all/stories/<story>')
    if _rx(r'^r18-only-novel_assets_assets_project_lazyassets_r18-only_novel_mainchara_hmr_\d{11}_l2d_\d{11}_(?:addanimations|animations)_.*\.fade\.asset_'+HEX+r'\.bundle$').search(n):
        return ResourceInfo('l2d_motion', None, 'r18', 'data_r18_all/stories/<story>/motions')
    if _rx(r'^r18-only-novel_assets_assets_project_lazyassets_r18-only_novel_mainchara_hmr_\d{11}_l2d_\d{11}_.*\.fade\.asset_'+HEX+r'\.bundle$').search(n):
        return ResourceInfo('l2d_fade', None, 'r18', 'data_r18_all/stories/<story>')
    if _rx(r'^r18-only-novel_assets_assets_project_lazyassets_r18-only_novel_mainchara_hmr_\d{11}_l2d_\d{11}_.+\.(?:asset|prefab)_'+HEX+r'\.bundle$').search(n):
        return ResourceInfo('l2d_core', None, 'r18', 'data_r18_all/stories/<story>')

    # ---- Other directory-tree families (classified, but not part of Story dependency recovery) ----
    prefix_map = [
        ('r18-icon-tutorial-howto', 'tutorial_icon', 'r18'),
        ('r18-only-tutorial-chara-thumbnail', 'tutorial_icon', 'r18'),
        ('tutorial-', 'tutorial', 'common'),
        ('r18-only-icon-charaicon', 'character_icon', 'r18'),
        ('r18-only-icon-characutin', 'character_cutout', 'r18'),
        ('r18-only-icon-bannerl-', 'banner', 'r18'),
        ('r18-only-icon-shop-', 'shop_icon', 'r18'),
        ('r18-only-loginui-', 'login_ui', 'r18'),
        ('r18-only-recommend-bg', 'recommend_bg', 'r18'),
        ('r18-only-recommend-title', 'recommend_title', 'r18'),
        ('r18-only-resourceasset-unitcutin', 'unitcutin', 'r18'),
        ('r18-only-movie-cri', 'movie', 'r18'),
        ('home-bgassets-', 'event_bgasset', 'common'),
        ('scenes_', 'scene', 'common'),
        ('event.', 'event', 'common'),
        ('common-', 'common', 'common'),
        ('common_', 'common', 'common'),
        ('general-', 'general', 'common'),
        ('r18-only-live2d', 'live2d_shared', 'r18'),
        ('r18-only-expeditionstart', 'expedition_start', 'r18'),
        ('r18-only-sound-cri', 'sound_other', 'r18'),
        ('r18-only-', 'r18_other', 'r18'),
    ]
    low=n.lower()
    for prefix,kind,scope in prefix_map:
        if low.startswith(prefix):
            return ResourceInfo(kind, None, scope, 'not-extracted', 'family')
    return ResourceInfo('other', None, 'unknown', 'not-extracted', 'fallback')


def resource_id_info(value: str):
    s=str(value).strip().lower()
    if re.fullmatch(r'abg\d+[a-z]?',s): return 'background'
    if re.fullmatch(r'se\d+',s): return 'se'
    if re.fullmatch(r'bgm\d+',s): return 'bgm'
    if re.fullmatch(r'bgv_[a-z0-9_]+',s): return 'bgv'
    if re.fullmatch(r'vc_[a-z0-9_]+',s): return 'voice'
    if re.fullmatch(r'\d{11}',s): return 'story'
    if re.fullmatch(r'\d{9}[gx]?',s): return 'character'
    return None
