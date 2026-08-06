#!/usr/bin/env python3
"""food_demo.py — 食品企业溯源知识库可复现案例

一键运行，打印食品溯源 + 食品安全场景的完整报告：
- 建知识库（产品/原料/批次/质检/设备 + 溯源 join 表）
- 规则问答（品类计数）
- 正向溯源（批次 → 产品 + 原料）
- 反向溯源（食品安全核心：不合格原料 → 受影响批次 → 产品）
- 扫码溯源

用法:
  python food_demo.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import multi_table as mt
import graph_rag as gr
import ontology_qa_v3 as v3

DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "output")
FOOD_NT = os.path.join(OUT, "food.nt")


def _build_kb():
    """构建食品本体(多表 + 溯源关系)。"""
    def load(t):
        return mt.load_table(os.path.join(DATA, f"{t}.csv"))
    tables = {}
    for t, idc in [("food_products","id"),("food_raw_materials","id"),("food_batches","id"),
                   ("food_batch_ingredient","batch_id"),("food_qc","id"),("food_equipment","id")]:
        n, h, rows = load(t)
        tables[n] = {"headers": h, "rows": rows, "id_col": idc}
    rels = {
        "food_batches": {"product_id": {"target_class":"Food_products","rel":"http://food.example/ontology#produces","label":"生产产品"}},
        "food_batch_ingredient": {
            "batch_id": {"target_class":"Food_batches","rel":"http://food.example/ontology#belongsToBatch","label":"属于批次"},
            "raw_id": {"target_class":"Food_raw_materials","rel":"http://food.example/ontology#usesRawMaterial","label":"使用原料"}},
    }
    os.makedirs(OUT, exist_ok=True)
    mt.build_nt(tables, rels, FOOD_NT)


def main():
    print("=" * 56)
    print("食品企业溯源知识库 — 可复现案例")
    print("=" * 56)

    # 1. 建知识库
    if not os.path.exists(FOOD_NT):
        print("\n[1/4] 构建食品本体(多表+溯源关系)...")
        _build_kb()
    else:
        print("\n[1/4] 使用已建本体")
    g, labels, vi, rev = gr.build_graph(FOOD_NT)
    print(f"  知识库: {len(g)} 节点, 6 表(产品/原料/批次/质检/设备/溯源join)")

    # 2. 规则问答
    print("\n[2/4] 规则问答(品类计数)...")
    D = v3.load_dict(os.path.join(ROOT, "config", "lexicon_food_products.json"))
    data = v3.build_data(v3.parse_nt(FOOD_NT), D)
    for q in ["乳制品的数量", "速冻食品的数量", "价格的最大值"]:
        print(f"  问: {q} → {v3.answer(q, data, D)}")

    # 3. 正向溯源
    print("\n[3/4] 正向溯源(批次→产品+原料)...")
    def tail(x):
        return x.split("#")[-1]
    def fwd(batch_id):
        b = next((k for k in g if tail(k) == f"Food_batches_{batch_id}"), None)
        product = [tail(p) for r, ts in g[b].items() if r == "produces" for p in ts]
        raws = set()
        for bi, rels in g.items():
            if tail(bi).startswith("Food_batch_ingredient_") and b in rels.get("belongsToBatch", []):
                for r in rels.get("usesRawMaterial", []):
                    raws.add(tail(r).replace("Food_raw_materials_", ""))
        return product, sorted(raws)
    prod, raws = fwd("B001")
    print(f"  B001(原味酸奶): 产品={prod}, 原料={raws}")

    # 4. 反向溯源(食品安全)
    print("\n[4/4] 反向溯源(食品安全: 不合格原料→受影响批次→产品)...")
    def rev(raw_id):
        r = next((k for k in g if tail(k) == f"Food_raw_materials_{raw_id}"), None)
        affected = []
        for bi, rels in g.items():
            if tail(bi).startswith("Food_batch_ingredient_") and r in rels.get("usesRawMaterial", []):
                for b in rels.get("belongsToBatch", []):
                    product = [tail(p).replace("Food_products_","") for rel, ts in g[b].items() if rel == "produces" for p in ts]
                    affected.append({"batch": tail(b).replace("Food_batches_",""), "product": product[0] if product else ""})
        return affected
    for raw_id, why in [("RM008", "盐·质检不合格"), ("RM010", "猪肉馅·库存告急")]:
        affected = rev(raw_id)
        print(f"  {raw_id}({why}) → 受影响批次: {[a['batch']+'('+a['product']+')' for a in affected] or '无'}")

    # 5. 扫码溯源
    print("\n[+] 扫码溯源: P003-B005(全麦面包)")
    prod, raws = fwd("B005")
    print(f"  全麦面包批次B005: 原料={raws}")

    print("\n" + "=" * 56)
    print("案例完成。REST API: python api_server.py")
    print("=" * 56)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ 执行失败: {e}")
        sys.exit(1)
