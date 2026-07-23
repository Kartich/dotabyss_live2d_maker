import os
import re
import json
from pathlib import Path
from PIL import Image
import UnityPy

# ==========================================
# 1. 基础路径与开关配置
# ==========================================
RAW_BUNDLES_DIR = Path("./raw_bundles")       
OUTPUT_STORY_DIR = Path("./data_r18_all/stories") 

# 强制覆盖开关（置为 True 时，若遇到旧文件将先强制清除再写出，避免残留/占用问题）
FORCE_OVERWRITE = True  

OUTPUT_STORY_DIR.mkdir(parents=True, exist_ok=True)

# ==========================================
# 2. 贴图绝对红底消除模块 (精准锁定实测背景色 #FF0005)
# ==========================================
def remove_pure_red_background_pillow(pil_image, out_png_path, force_overwrite=True):
    """
    无损像素级抠像：精准锁定实测背景色 #FF0005 (R255, G0, B5)，允许微小压损色漂 (B <= 5)。
    100% 保护立绘原本的腮红、肤色与暖色像素。
    """
    try:
        img = pil_image.convert("RGBA")
        datas = img.getdata()  # 使用 Pillow 标准 getdata() 稳健遍历像素
        
        # 精准匹配实测背景色 #FF0005 (R=255, G=0, B<=5)
        new_data = [
            (0, 0, 0, 0) if (r == 255 and g == 0 and b <= 5) else (r, g, b, a) 
            for r, g, b, a in datas
        ]
        
        img.putdata(new_data)
        
        out_path = Path(out_png_path)
        os.makedirs(out_path.parent, exist_ok=True)
        
        # 【强制覆盖逻辑】如果旧文件存在，主动 unlink 确保写入
        if force_overwrite and out_path.exists():
            try:
                out_path.unlink()
            except Exception as del_err:
                print(f"    [!] 清理旧贴图失败: {del_err}")

        img.save(out_path, "PNG")
        return True
    except Exception as e:
        print(f"    [-] 贴图背景消除失败: {e}")
        return False

# ==========================================
# 3. 单 Bundle 流式解析与导出
# ==========================================
def process_story_bundle(bundle_path: Path):
    # 匹配故事 ID (兼容 l2d_11位数字 或 文件名中包含11位数字)
    id_match = re.search(r"(\d{11})", bundle_path.name)
    if not id_match:
        return None
        
    story_id = id_match.group(1)
    
    try:
        env = UnityPy.load(str(bundle_path))
    except Exception as e:
        print(f"[-] 无法载入 Bundle {bundle_path.name}: {e}")
        return None

    # 标准资产收集容器
    textures = {}      # 存放名称 -> PIL Image
    moc3_bytes_data = None

    # 使用 obj.type.name 进行精准类型识别
    for obj in env.objects:
        type_name = getattr(obj.type, "name", str(obj.type))
        
        if type_name == "Texture2D":
            data = obj.read()
            tex_name = getattr(data, "name", "") or getattr(data, "m_Name", "")
            if tex_name and data.image:
                textures[tex_name] = data.image
                
        elif type_name in ["TextAsset", "MonoBehaviour"]:
            try:
                try: as_json_dict = obj.read_typetree()
                except: as_json_dict = {}
                
                raw_moc = as_json_dict.get("m_Script", as_json_dict.get("script", b""))
                if isinstance(raw_moc, str): raw_moc = raw_moc.encode('utf-8', 'ignore')
                
                if b"MOC3" not in raw_moc:
                    raw_bytes = obj.get_raw_data()
                    idx = raw_bytes.find(b"MOC3")
                    if idx != -1: raw_moc = raw_bytes[idx:]
                    
                if isinstance(raw_moc, bytes) and raw_moc.startswith(b"MOC3") and len(raw_moc) > 10000:
                    moc3_bytes_data = raw_moc
            except:
                pass

    if not textures and not moc3_bytes_data:
        return None

    # 创建故事流输出路径
    story_path = OUTPUT_STORY_DIR / story_id
    moc_dir = story_path / "moc"
    textures_dir = story_path / "textures"
    moc_dir.mkdir(parents=True, exist_ok=True)
    textures_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[*] 正在处理故事单元: {story_id} (来源包: {bundle_path.name})")

    # 导出 MOC3（带强制覆盖逻辑）
    if moc3_bytes_data:
        moc3_path = moc_dir / f"l2d_{story_id}.moc3"
        if FORCE_OVERWRITE and moc3_path.exists():
            try:
                moc3_path.unlink()
            except Exception as del_err:
                print(f"    [!] 清理旧 moc3 模型失败: {del_err}")

        with open(moc3_path, "wb") as f_moc:
            f_moc.write(moc3_bytes_data)
        print(f"  [✓] 保存 Live2D 模型 -> moc/{moc3_path.name}")

    # 导出贴图并执行绝对红底消除（带强制覆盖逻辑）
    model_textures_config = []
    for name, img_obj in textures.items():
        if "grabmask" in name.lower():
            mask_path = textures_dir / "GrabMask.png"
            if FORCE_OVERWRITE and mask_path.exists():
                try: mask_path.unlink()
                except: pass
            img_obj.save(mask_path)
            print("  [✓] 保存遮罩 -> textures/GrabMask.png")
        else:
            clean_img_name = f"{name}.png"
            out_png_path = textures_dir / clean_img_name
            
            if remove_pure_red_background_pillow(img_obj, out_png_path, force_overwrite=FORCE_OVERWRITE):
                model_textures_config.append(f"textures/{clean_img_name}")
                print(f"  [✓] 贴图背景消除 (#FF0005) 并导出 -> textures/{clean_img_name}")

    # 生成 .model3.json 配置（带强制覆盖逻辑）
    if model_textures_config or moc3_bytes_data:
        cfg_path = story_path / f"{story_id}.model3.json"
        if FORCE_OVERWRITE and cfg_path.exists():
            try: cfg_path.unlink()
            except: pass

        model3_cfg = {
            "Version": 3,
            "FileReferences": {
                "Moc": f"moc/l2d_{story_id}.moc3",
                "Textures": sorted(list(set(model_textures_config))),
                "Motions": {}
            },
            "Groups": []
        }
        with open(cfg_path, 'w', encoding='utf-8') as f_cfg:
            json.dump(model3_cfg, f_cfg, indent=2, ensure_ascii=False)
            
        return {"id": story_id, "type": "live2d", "title": f"Story {story_id}", "hasLive2D": True}
        
    return None

# ==========================================
# 4. 管线总入口
# ==========================================
def extract_live2d_clean_pipeline():
    if not RAW_BUNDLES_DIR.exists():
        print(f"[-] 错误: 未找到原始资产包文件夹: '{RAW_BUNDLES_DIR.resolve()}'")
        return
        
    print(f"[*] 开始扫描目录: {RAW_BUNDLES_DIR.resolve()} (强制覆盖模式: {'开启' if FORCE_OVERWRITE else '关闭'})")
    story_list = []

    # 遍历所有 Bundle 文件
    for bundle_path in RAW_BUNDLES_DIR.rglob("*"):
        if not bundle_path.is_file(): continue
        
        res = process_story_bundle(bundle_path)
        if res and not any(s["id"] == res["id"] for s in story_list):
            story_list.append(res)

    # 全量更新 index.json
    if story_list:
        index_path = Path("./data_r18_all/index.json")
        existing_stories = []
        if index_path.exists():
            try:
                with open(index_path, "r", encoding="utf-8") as f: 
                    existing_stories = json.load(f).get("stories", [])
            except: pass
            
        for s in story_list:
            if not any(ex["id"] == s["id"] for ex in existing_stories): 
                existing_stories.append(s)
                
        # 写入最新的 index.json
        with open(index_path, "w", encoding="utf-8") as f: 
            json.dump({"stories": sorted(existing_stories, key=lambda x: x["id"])}, f, indent=2, ensure_ascii=False)
            
        print("\n" + "="*60 + "\n[SUCCESS] 全部 Live2D 贴图与模型解包完毕！")
    else:
        print("\n[-] 未能识别导出任何图片，请检查 RAW_BUNDLES_DIR 目录下是否存在 Bundle 文件。")

if __name__ == "__main__":
    extract_live2d_clean_pipeline()