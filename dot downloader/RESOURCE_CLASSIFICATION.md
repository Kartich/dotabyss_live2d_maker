# Bundle 资源分类规则（目录树驱动）

本文件明确规定：Bundle 分类不依赖“看起来像资源 ID”的正则猜测，而以你提供的 `directory_tree(1).txt` 中实际出现的**目录层级 + 同级文件结构 + Bundle 内部资源命名路径**为准。

## 可用于 Story/播放器的功能资源

| 分类 | 来源路径模式 | 播放器落盘位置 |
|---|---|---|
| background | `general-ui-bg-novel/.../general_ui_bg_novel_<abg>.png_<hash>.bundle` | `data/backgrounds/novel/` |
| se | `...workunit_novel_se_<se>.acb_<hash>.bundle` | `data/audio/se/decoded/misc/` |
| bgm | `...workunit_novel_bgm_<bgm>.acb_<hash>.bundle` | `data_r18_all/stories/<story>/audio/decoded/bgm/` |
| bgv | `...workunit_novel_r18_backgroundvoice/..._<story>_bgv.acb_<hash>.bundle` | `data_r18_all/stories/<story>/audio/decoded/bgv/` |
| voice | `...workunit_novel[_r18]_voice_chara/..._<story>.acb_<hash>.bundle` | `data_r18_all/stories/<story>/audio/decoded/voice/` |
| story | `r18-only_novel_mainchara_hmr_<story>_hmr_<story>.txt_<hash>.bundle` | `data_r18_all/stories/<story>/` |
| stand | `r18-only_charastand_novel_prefabs_g_charastand<character>.prefab_<hash>.bundle` | `data/chara/<character>/` |
| thumb | `r18-only_icon_story_s/...story_s_hmr_<story10>.png_<hash>.bundle` | `data_r18_all/thumb/` |
| emotion | `general_ui_emotion_charastand_...` | `data/emotion/charastand/` |
| l2d_motion | `..._l2d_<id>_addanimations_*` / `..._animations_*` `.fade.asset` | Story `motions/` |
| l2d_fade | `...l2d...l2d...fademotionlist.asset` 或其他 fade 资产 | Story L2D 元数据 |
| l2d_core | `..._l2d_<id>_...asset/prefab` | Story L2D 元数据 |

## 非 Story 依赖资源

其它目录族也全部分类，例如 `tutorial_icon`、`character_icon`、`character_cutout`、`banner`、`shop_icon`、`login_ui`、`movie`、`unitcutin`、`scene`、`event_bgasset`、`ui_common`、`general`、`common`、`r18_other` 等。它们不会被 Stage 5 当作 BG/BGM/SE 依赖下载。

## 当前 Catalog 覆盖

当前 `catalog_1.bin` 共识别 Bundle 19,671 个，分类器无 `other`/未分类项；分类统计会由 `validate_resource_classification.py` 重新生成。
