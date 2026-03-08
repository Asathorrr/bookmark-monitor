"""
运行此脚本可修复 config.json（删除其中的 bookmark_snapshot 字段）。
在项目目录下双击或执行：python fix_config.py
"""
import json, os, shutil

base = os.path.dirname(os.path.abspath(__file__))
cfg_path = os.path.join(base, "config.json")
snap_path = os.path.join(base, "bookmark_snapshot.json")

if not os.path.exists(cfg_path):
    print("未找到 config.json，无需修复。")
    input("按回车退出")
    exit()

# 备份
shutil.copy(cfg_path, cfg_path + ".bak")
print(f"已备份到 config.json.bak")

try:
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
except Exception as e:
    print(f"config.json 解析失败: {e}")
    print("尝试重建空配置...")
    cfg = {"monitors": [], "cookies": {}, "selector_rules": {}}

snap = cfg.pop("bookmark_snapshot", None)
if snap:
    print(f"找到 bookmark_snapshot，共 {len(snap)} 条，迁移到 bookmark_snapshot.json")
    with open(snap_path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False)
else:
    print("config.json 中没有 bookmark_snapshot 字段")

orig_size = os.path.getsize(cfg_path)
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
new_size = os.path.getsize(cfg_path)

print(f"config.json 修复完成：{orig_size//1024} KB → {new_size//1024} KB")
print(f"monitors: {len(cfg.get('monitors',[]))}")
print(f"selector_rules: {len(cfg.get('selector_rules',{}))}")
print(f"cookies: {len(cfg.get('cookies',{}))}")
print("\n✅ 完成！请重启 app.py")
input("按回车退出")
