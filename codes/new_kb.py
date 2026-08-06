#!/usr/bin/env python3
"""new_kb.py — 新知识库引导(落地: 怎么加你的工厂数据)

自动搭建一个企业知识库骨架:
- 在 config/kbs.json 注册(品牌/图标/示例/数据目录/词典)
- 建数据目录 + 数据表结构说明 + 词典模板
- 打印后续步骤

用法:
  python new_kb.py valve
  python new_kb.py valve --name "阀门厂" --icon "🔧"

后续:
  cd data_kb_valve && 按 README 的表结构放你的 CSV
  设 FOOD_KB=valve 启动 python api_server.py
"""
import os
import sys
import json
import argparse

ROOT = os.path.dirname(os.path.abspath(__file__))
KBS_FILE = os.path.join(ROOT, "config", "kbs.json")

# 默认数据表结构(与 multi_table/食品 KB 对齐)
TABLE_SCHEMA = {
    "products": "产品表: id,product_name,category,spec,expiry_days,storage,price",
    "raw_materials": "原料/零部件表: id,raw_name,supplier,batch,expiry_date,stock_qty,status,qc_result",
    "batches": "生产批次表: id,product_id,produce_date,quantity,raw_batches,team,status",
    "batch_ingredient": "批次-原料关联表: batch_id,raw_id  (一行=该批次用了哪个原料, 溯源关键)",
    "qc": "质检表: id,batch_id,check_item,result,checker,check_date",
    "equipment": "设备表: id,device_name,device_type,workshop,power_kw,status",
}

LEXICON_TEMPLATE = {
    "description": "知识库词典模板(换企业只改这里的中文字段名)",
    "attr_cn2en": {"产品名": "product_name", "品类": "category", "价格": "price"},
    "attr_en2cn": {"product_name": "产品名", "category": "品类", "price": "价格"},
    "status_cn2en": {},
    "type_cn2en": {"A类": "A类", "B类": "B类"},
    "field_aliases": {"deviceName": ["product_name"], "status": [], "deviceType": ["category"], "location": []},
    "value_fields": [],
}


def new_kb(kb, name=None, icon="🏭"):
    kbs = json.load(open(KBS_FILE, encoding="utf-8"))
    kbs.setdefault("kbs", {})
    if kb in kbs["kbs"]:
        print(f"⚠️ 知识库 '{kb}' 已存在, 跳过注册")
    else:
        kbs["kbs"][kb] = {
            "name": name or f"{kb} 知识库",
            "icon": icon,
            "examples": [f"{kb} 相关问题示例1", f"{kb} 相关问题示例2"],
            "data_dir": f"data_{kb}",
            "lexicon": f"lexicon_{kb}.json",
        }
        json.dump(kbs, open(KBS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"✅ 已注册知识库 '{kb}' 到 kbs.json")

    # 数据目录 + 表结构说明
    data_dir = os.path.join(ROOT, kbs["kbs"][kb]["data_dir"])
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write(f"# {name or kb} 知识库 — 数据目录\n\n")
        f.write("把企业数据按以下表结构放成 CSV 文件(food_ 前缀, 如 food_products.csv):\n\n")
        for t, desc in TABLE_SCHEMA.items():
            f.write(f"- `food_{t}.csv` — {desc}\n")
        f.write("\n**溯源**: batch_ingredient 表决定正/反向追溯能力\n")
        f.write(f"\n词典: `config/lexicon_{kb}.json`\n")

    # 词典模板
    lex = os.path.join(ROOT, "config", f"lexicon_{kb}.json")
    if not os.path.exists(lex):
        json.dump(LEXICON_TEMPLATE, open(lex, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"✅ 已建词典模板 config/lexicon_{kb}.json")

    print("\n" + "=" * 50)
    print(f"知识库 '{kb}' 已搭建。后续步骤:")
    print(f"  1. 把企业数据放进 {data_dir}/(按 README 的表结构)")
    print(f"  2. 编辑 config/lexicon_{kb}.json 设中文字段名")
    print(f"  3. 设 FOOD_KB={kb} 启动:  python api_server.py")
    print(f"  4. 验证: python data_quality.py && python benchmark_graphrag.py")
    print("=" * 50)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kb", help="知识库名(如 valve)")
    ap.add_argument("--name", help="显示名(如 阀门厂)")
    ap.add_argument("--icon", default="🏭")
    args = ap.parse_args()
    new_kb(args.kb, args.name, args.icon)


if __name__ == "__main__":
    main()
