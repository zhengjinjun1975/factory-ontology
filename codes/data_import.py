#!/usr/bin/env python3
"""data_import.py — 数据接入自动化

把企业的 Excel/DB/CSV 台账同步进食品知识库：
- 读取源(Excel 每个 sheet / DB 每张表 / CSV)
- 按映射配置把源列 → 食品表字段
- 写出 food_*.csv → 触发本体重建(增量检测自动生效)
- 支持定时同步(--schedule N秒循环)

用法:
  python data_import.py <源文件> [--mapping 映射.json] [--rebuild]
  python data_import.py data/raw_products.xlsx --mapping config/data_import_config.json --rebuild
  python data_import.py --schedule 3600        # 每小时同步一次

依赖: openpyxl(读Excel) 可选; CSV/SQLite 标准库
"""
import os
import sys
import json
import time
import argparse

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
DATA = os.path.join(ROOT, "data")

# 食品表 → 文件 + 主键(对齐 multi_table/api_server)
FOOD_TABLES = {
    "food_products": ("food_products.csv", "id"),
    "food_raw_materials": ("food_raw_materials.csv", "id"),
    "food_batches": ("food_batches.csv", "id"),
    "food_batch_ingredient": ("food_batch_ingredient.csv", "batch_id"),
    "food_qc": ("food_qc.csv", "id"),
    "food_equipment": ("food_equipment.csv", "id"),
}
DEFAULT_MAPPING = os.path.join(ROOT, "config", "data_import_config.json")


def load_mapping(path=DEFAULT_MAPPING):
    """读列映射: {食品表: {源列: 目标列}}。无映射文件则同名直接透传。"""
    if os.path.exists(path):
        return json.load(open(path, encoding="utf-8"))
    return None


def import_source(src, mapping=None, rebuild=True, dry_run=False):
    """导入单个源文件到指定食品表。返回导入行数。"""
    from data_loader import load_table
    mapping = mapping or load_mapping() or {}
    try:
        src_name, headers, rows = load_table(src)
    except Exception as e:
        return {"error": f"无法读取 {src}: {e}"}

    # 确定目标食品表: mapping.source_map > 源文件名同名 > 默认 food_products
    base = os.path.splitext(os.path.basename(src))[0]
    source_map = mapping.get("source_map", {})
    target = (source_map.get(base) or source_map.get(src_name)
              or (base if base in FOOD_TABLES else "food_products"))
    if target not in FOOD_TABLES:
        return {"error": f"未知目标表: {target}"}
    fname, _ = FOOD_TABLES[target]

    colmap = mapping.get(target, {})
    target_headers = [colmap.get(c, c) for c in headers]
    new_rows = [{colmap.get(c, c): v for c, v in r.items()} for r in rows]

    if dry_run:
        return {"ok": True, "dry_run": f"将写 {len(new_rows)} 行 -> {fname}({target})", "headers": target_headers}
    _write_csv(os.path.join(DATA, fname), target_headers, new_rows)
    if rebuild:
        from api_server import _load
        _load()
    return {"ok": True, "table": target, "rows": len(new_rows), "file": fname, "rebuild": rebuild}


def _write_csv(path, headers, rows):
    """写 CSV(统一 utf-8-sig)。"""
    import csv
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        for r in rows:
            w.writerow({h: r.get(h, "") for h in headers})


def main():
    ap = argparse.ArgumentParser(description="食品知识库数据接入")
    ap.add_argument("source", nargs="?", help="源文件(Excel/DB/CSV)")
    ap.add_argument("--mapping", default=DEFAULT_MAPPING)
    ap.add_argument("--no-rebuild", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--schedule", type=int, help="定时同步间隔(秒), 循环执行")
    args = ap.parse_args()

    if args.schedule:
        print(f"🔄 定时同步模式, 每 {args.schedule} 秒一次 (Ctrl+C 停止)")
        while True:
            if args.source:
                print(json.dumps(import_source(args.source, rebuild=not args.no_rebuild), ensure_ascii=False))
            time.sleep(args.schedule)
        return
    if not args.source:
        print("用法: python data_import.py <源文件> [--mapping 映射.json] [--schedule N]")
        sys.exit(1)
    print(json.dumps(import_source(args.source, rebuild=not args.no_rebuild,
                                   dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
