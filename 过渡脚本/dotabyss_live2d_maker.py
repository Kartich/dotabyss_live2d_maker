import os
import re
import json
from pathlib import Path
from PIL import Image
import UnityPy

# ==========================================
# 基础配置 (保持与原脚本一致的分类格式)
# ==========================================
BUNDLE_DIR = "./raw_bundles"           # 存放原始 .bundle 文件的目录
OUTPUT_CHARA_DIR = "./data/chara"      # 立绘输出路径
OUTPUT_STORY_DIR = "./data_r18_all/stories"  # 故事输出路径

Path(OUTPUT_CHARA_DIR).mkdir(parents=True, exist_ok=True)
Path(OUTPUT_STORY_DIR).mkdir(parents=True, exist_ok=True)

def process_bundle(bundle_path: Path):
    print(f"[*] 正在解析解包 Bundle: {bundle_path.name}")
    try:
        env = UnityPy.load(str(bundle_path))
    except Exception as e:
        print(f"[-] 无法载入 Bundle {bundle_path.name}: {e}")
        return

    # 预变量，用于收集当前单包内的组件
    textures = {}
    sprites_meta = {}
    text_assets = {}
    
    # 状态判定：根据包名路由
    folder_name = bundle_path.stem
    is_chara = re.search(r"charastand(\d{9}[a-zA-Z])", folder_name, re.IGNORECASE) or re.match(r"^(\d{9}[gx])$", folder_name)
    is_l2d = re.search(r"l2d_(\d{11})", folder_name)
    
    # 1. 遍历解包内所有资产
    for obj in env.objects:
        if obj.type.name == "Texture2D":
            data = obj.read()
            textures[data.name.lower()] = data.image
        elif obj.type.name == "Sprite":
            # Sprite 包含重要的 Rect 和 Pivot 物理坐标！
            data = obj.read()
            sprites_meta[data.name.lower()] = {
                "x": data.m_Rect.x,
                "y": data.m_Rect.y,
                "width": data.m_Rect.width,
                "height": data.m_Rect.height,
                "pivot_x": data.m_Pivot.x,
                "pivot_y": data.m_Pivot.y
            }
        elif obj.type.name == "TextAsset":
            data = obj.read()
            text_assets[data.name.lower()] = data.script
        elif obj.type.name == "MonoBehaviour":
            # 很多时候，Layout Rect 坐标存在 MonoBehaviour 的配置 Json 里
            if obj.serialized_type and obj.serialized_type.nodes:
                try:
                    tree = obj.read_typetree()
                    # 这里可以打印树结构，观察是否有 rootRect / bodyRect
                    if "bodyRect" in str(tree):
                        sprites_meta["layout_tree"] = tree
                except:
                    pass

    # 2. 路由分类处理
    if is_chara:
        chara_id = (is_chara.group(1)).lower()
        export_chara_standalone(chara_id, textures, sprites_meta, text_assets)
        
    elif is_l2d:
        story_id = is_l2d.group(1)
        export_story_live2d(story_id, textures, text_assets, env)

def export_chara_standalone(chara_id, textures, sprites_meta, text_assets):
    """ 处理普通立绘，合成 Alpha，并生成播放器急需的 Rect 矩阵 """
    dest_dir = Path(OUTPUT_CHARA_DIR) / chara_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "faces").mkdir(exist_ok=True)
    
    print(f"  [+] 路由立绘流 -> chara/{chara_id}")
    
    # Alpha 合并逻辑 (示例)
    for name, img in textures.items():
        if "body" in name and not name.endswith(".alpha"):
            alpha_name = name + ".alpha"
            if alpha_name in textures:
                # 完美还原透明通道
                combined = Image.new("RGBA", img.size)
                combined.paste(img, (0,0))
                combined.putalpha(textures[alpha_name].convert("L"))
                combined.save(dest_dir / "Body.png")
            else:
                img.save(dest_dir / "Body.png")
        elif "face" in name or name[0].isupper():
            # 表情导出逻辑...
            img.save(dest_dir / "faces" / f"{name}.png")

    # 【核心修复】构建真正符合播放器标准的 meta.json
    meta_path = dest_dir / "meta.json"
    
    # 提示：你需要根据 "layout_tree" 里的实际 Unity 键名映射以下数据
    # 这里提供一个符合 app.js 预期的 Mock 映射结构
    layout_data = sprites_meta.get("layout_tree", {})
    
    meta_json = {
        "charaId": chara_id,
        "name": chara_id,
        "files": {
            "body": "Body.png"
        },
        "faces": { f.stem: f"faces/{f.name}" for f in (dest_dir / "faces").glob("*.png") },
        # 以下是 app.js 核心对齐文件缺少的 Rect 数据，需要从组建树中提取填入
        "rootRect": layout_data.get("rootRect", {"sizeDelta": {"x": 2048, "y": 2048}}),
        "bodyRect": layout_data.get("bodyRect", {"anchoredPosition": {"x": 0, "y": 0}, "sizeDelta": {"x": 1024, "y": 1600}, "pivot": {"x": 0.5, "y": 0}}),
        "faceContentRect": layout_data.get("faceContentRect", {"anchoredPosition": {"x": 0, "y": 500}, "sizeDelta": {"x": 256, "y": 256}, "pivot": {"x": 0.5, "y": 0.5}}),
        "zoomRect": layout_data.get("zoomRect", {"localScale": {"x": 1, "y": 1}})
    }
    
    with open(meta_path, 'w', encoding='utf-8') as mf:
        json.dump(meta_json, mf, indent=2, ensure_ascii=False)

def export_story_live2d(story_id, textures, text_assets, env):
    """ 处理 Live2D 资产，直接拉取底层原生 Moc3 和 Motion3 """
    dest_dir = Path(OUTPUT_STORY_DIR) / story_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"  [+] 路由 Live2D 流 -> stories/{story_id}")
    
    # 寻找原生 .moc3 和 .motion3.json
    # 如果游戏将其包装在 TextAsset 或者是编译过的 MonoBehaviour 中：
    # 标准 Live2D 插件常把原名保持在 TextAsset 中
    for name, raw_text in text_assets.items():
        if ".moc3" in name:
            (dest_dir / "moc").mkdir(exist_ok=True)
            with open(dest_dir / "moc" / f"l2d_{story_id}.moc3", "wb") as f:
                f.write(raw_text if isinstance(raw_text, bytes) else raw_text.encode('utf-8'))
        elif ".motion3" in name:
            (dest_dir / "motions").mkdir(exist_ok=True)
            # 直接写入原生的 motion3，绝不手工换算
            with open(dest_dir / "motions" / f"{name}.json", "w", encoding="utf-8") as f:
                f.write(raw_text if isinstance(raw_text, str) else raw_text.decode('utf-8'))

    # 红底擦除与贴图导出...
    # 构建最终的 story.json 和 model3.json 配置文件...

if __name__ == "__main__":
    # 遍历处理你现存的原始 bundle 文件
    for bundle in Path(BUNDLE_DIR).glob("*.bundle"):
        process_bundle(bundle)