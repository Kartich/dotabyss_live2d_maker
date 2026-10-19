资源表与 Catalog 设置（1.2）

设置入口：主窗口右上角“设置”。
Catalog 示例：
https://api.abyss-prod-r18.dotabyss.dmmgames.com/resources/webgl/r18/aas/12394/aa/catalog_1.bin
粘贴完整 URL 后，程序会识别版本号和 Bundle 基础 URL。点击“下载并更新 Catalog”可校验并覆盖本地 Catalog，并自动对比生成新增资源表。

资源表输出目录由设置项控制，默认 resource_tables。资源表.csv、资源表.json始终覆盖当前完整表；新增资源表.csv、新增资源表.json只在 Catalog 更新或首次建表时刷新。普通启动、下载资源后的状态刷新不会清空新增资源清单。所有文件覆盖写入，不生成带日期或编号的备份。

资源表字段：Bundle 名称、资源类型、范围、人物特征 ID、关联剧情 ID、原目录推断、已下载。不包含独立 Hash 和扩展名列。

跨页多选说明：选中行会跨页保留；“全选本页”将本页记录加入多选集合；“清空选择”清除累计选择。下载筛选全部与下载选中项是不同操作。
