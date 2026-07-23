import pathlib

def generate_tree_to_string(dir_path: pathlib.Path, prefix: str = "", tree_str: str = "") -> str:
    """
    递归生成目录树结构并返回字符串
    """
    try:
        contents = list(dir_path.iterdir())
    except PermissionError:
        return tree_str

    contents.sort(key=lambda x: (not x.is_dir(), x.name.lower()))

    for index, path in enumerate(contents):
        is_last = index == len(contents) - 1
        connector = "└── " if is_last else "├── "
        
        tree_str += f"{prefix}{connector}{path.name}\n"

        if path.is_dir():
            extension = "    " if is_last else "│   "
            tree_str = generate_tree_to_string(path, prefix + extension, tree_str)
            
    return tree_str

def save_tree_to_file(target_path: str = ".", output_filename: str = "directory_tree.txt"):
    target_dir = pathlib.Path(target_path)
    
    # 构建树状结构字符串
    tree_content = f"Directory tree for: {target_dir.resolve()}\n"
    tree_content += f"{target_dir.name}/\n"
    tree_content += generate_tree_to_string(target_dir)
    
    # 保存到文件
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write(tree_content)
        
    return output_filename

# 生成文件
output_file = save_tree_to_file(".", "directory_tree.txt")
print(f"File saved as: {output_file}")