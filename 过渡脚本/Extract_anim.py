import os
import re
import subprocess

# ================= 配置区域 =================
# 1. CLI 命令名称
ASSET_STUDIO_CLI = "AssetStudio.CLI.exe"

# 2. 遍历目标：当前工作目录
INPUT_DIR = os.getcwd()

# 3. 导出目标路径
OUTPUT_DIR = os.path.join(INPUT_DIR, "Exported_Live2D_Anims")

# 4. 只提取动画 Clip
ASSET_TYPES = ["AnimationClip"]

# 5. 指定游戏类型（必填）
GAME_TYPE = "Normal"

# 6. 筛选文件名的正则：只处理带有 _l2d_ 和 .prefab 的 bundle 文件
L2D_BUNDLE_PATTERN = re.compile(r".*_l2d_.*\.prefab.*", re.IGNORECASE)
# ===========================================

def run_asset_studio_cli():
    print(f"正在扫描当前文件夹: {INPUT_DIR} ...")
    
    matched_files = []
    for root, _, files in os.walk(INPUT_DIR):
        # 跳过导出目录
        if os.path.commonpath([root, OUTPUT_DIR]) == OUTPUT_DIR:
            continue

        for file in files:
            if file.endswith(".py"):
                continue
            # 筛选符合 Live2D 命名规则的 Bundle
            if L2D_BUNDLE_PATTERN.match(file):
                matched_files.append(os.path.join(root, file))

    print(f"找到 {len(matched_files)} 个匹配的目标 Bundle 文件，准备提取...\n")

    if not matched_files:
        print("未检测到符合条件的 Bundle 文件。")
        return

    success_count = 0

    for idx, file_path in enumerate(matched_files, start=1):
        relative_path = os.path.relpath(file_path, INPUT_DIR)
        file_out_dir = os.path.join(OUTPUT_DIR, os.path.dirname(relative_path))

        print(f"[{idx}/{len(matched_files)}] 处理中: {relative_path}")

        # 组装干净简洁的 CLI 命令
        cmd = [
            ASSET_STUDIO_CLI,
            file_path,
            file_out_dir,
            "--game", GAME_TYPE,
            "--types", ",".join(ASSET_TYPES)
        ]

        try:
            # 执行 CLI 命令
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            # 检查是否有导出文件
            if os.path.exists(file_out_dir) and len(os.listdir(file_out_dir)) > 0:
                print(f"  └─> [导出成功]")
                success_count += 1
            else:
                print(f"  └─> [跳过] 该 Bundle 中未找到 AnimationClip 资源")
                if os.path.exists(file_out_dir) and not os.listdir(file_out_dir):
                    os.rmdir(file_out_dir)

        except FileNotFoundError:
            print(f"[错误] 系统无法找到 '{ASSET_STUDIO_CLI}'，请检查 Path 环境变量或将其放在当前目录。")
            return
        except Exception as e:
            print(f"[错误] 处理失败: {e}")

    print("\n" + "=" * 50)
    print(f"处理完成！成功从 {success_count}/{len(matched_files)} 个 Bundle 中提取动画。")
    print(f"导出路径: {OUTPUT_DIR}")
    print("=" * 50)

if __name__ == "__main__":
    run_asset_studio_cli()