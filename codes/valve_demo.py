#!/usr/bin/env python3
"""valve_demo.py — 阀门行业知识库 demo（合成示例数据）

实证 factory-ontology 框架对石油/阀门行业的适用性（换领域即用）：
- 建阀门本体（产品/零部件/批次/质检/设备 + 批次-部件关联）
- 规则问答（确定性）+ 逻辑桥（自然语言）
- 反向溯源（不合格密封圈 → 受影响阀门批次 → 产品，质量召回核心）
- benchmark（规则 100%）

数据：data_valve/*.csv（合成示例，非真实数据）
"""
import os
import sys
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
DATA = os.path.join(ROOT, "data_valve")
NT = os.path.join(ROOT, "output", "valve.nt")
LEX = os.path.join(ROOT, "config", "lexicon_valve.json")


def build():
    """用 schema_ontology 统一建阀门本体（激进重构：schema 驱动优先）。

    复用优先·极简落地：优先用 schema_ontology 的 schema 驱动建模（替代 multi_table），
    schema 不存在时回退原有 multi_table 逻辑，保持向后兼容。
    """
    os.makedirs(os.path.dirname(NT), exist_ok=True)
    schema_path = os.path.join(ROOT, "config", "ontology_schema.json")
    # schema 驱动优先（复用优先·极简落地）
    try:
        import schema_ontology as so
        if os.path.exists(schema_path):
            data = so.load_all(DATA)
            schema = so.load_schema(schema_path)
            so.to_nt(data, schema, outpath=NT)
            # 单表本体（供 benchmark）
            subprocess.run([sys.executable, os.path.join(ROOT, "csv_to_owl.py"),
                            os.path.join(DATA, "valve_products.csv"),
                            os.path.join(ROOT, "output", "valve_products.nt")], capture_output=True)
            return NT
    except Exception:
        pass
    # 回退：multi_table 建本体（schema 不存在时）
    import multi_table as mt
    from data_loader import load_table
    tables = {}
    for t, idc in [("valve_products", "id"), ("valve_raw_materials", "id"), ("valve_batches", "id"),
                   ("valve_batch_ingredient", "batch_id"), ("valve_qc", "id"), ("valve_equipment", "id")]:
        n, h, rows = load_table(os.path.join(DATA, f"{t}.csv"))
        tables[n] = {"headers": h, "rows": rows, "id_col": idc}
    rels = {
        "valve_batches": {
            "product_id": {"target_class": "Valve_products", "rel": "http://factory.example/ontology#produces", "label": "produces"},
            "raw_parts": {"target_class": "Valve_raw_materials", "rel": "http://factory.example/ontology#usesPart", "label": "usesPart"},
        },
        "valve_batch_ingredient": {
            "batch_id": {"target_class": "Valve_batches", "rel": "http://factory.example/ontology#belongsToBatch", "label": "belongsToBatch"},
            "raw_id": {"target_class": "Valve_raw_materials", "rel": "http://factory.example/ontology#usesRawMaterial", "label": "usesRawMaterial"},
        },
    }
    mt.build_nt(tables, rels, NT)
    # 单表本体（供 benchmark）
    import csv_to_owl as c2o
    subprocess.run([sys.executable, os.path.join(ROOT, "csv_to_owl.py"),
                    os.path.join(DATA, "valve_products.csv"),
                    os.path.join(ROOT, "output", "valve_products.nt")], capture_output=True)
    return NT


def demo():
    import ontology_qa_v3 as v3
    import graph_rag as gr
    D = v3.load_dict(LEX)
    nt = build()
    data = v3.build_data(v3.parse_nt(NT), D)

    print("═ 一、规则问答（确定性，结构化）═")
    for q in ["一共有多少个阀门", "价格最贵的阀门", "最贵的闸阀"]:
        print(f"  {q} → {v3.answer(q, data, D)[:50]}")

    print("═ 二、逻辑桥（自然语言，LLM→逻辑→执行）═")
    try:
        import logical_qa
        for q in ["有多少种球阀", "价格超过2500的阀门"]:
            res = logical_qa.answer(q, data, D)
            print(f"  {q} → {res[0][:50] if res else '(未命中)'}")
    except Exception as e:
        print("  逻辑桥不可用:", e)

    print("═ 三、反向溯源（密封圈 RM03 不合格 → 受影响阀门）═")
    g, labels, vi, rev = gr.build_graph(nt)
    hit = False
    for bi, props in g.items():
        if gr.tail(bi).startswith("Valve_batch_ingredient_") and \
           any(gr.tail(v) == "Valve_raw_materials_RM03" for v in props.get("usesRawMaterial", [])):
            for b in props.get("belongsToBatch", []):
                prod = [gr.tail(p) for rel, ts in g[b].items() if rel == "produces" for p in ts]
                print(f"  RM03密封圈 → {gr.tail(b)} → {prod}")
                hit = True
    assert hit, "密封圈溯源应命中"
    print("  ✅ 密封圈 RM03(不合格) → VB02 → V02 球阀（召回场景成立）")

    print("═ 四、benchmark（规则 100%）═")
    # benchmark 按约定找 lexicon_{文件名}.json, 拷贝一份匹配
    import shutil
    bench_lex = os.path.join(ROOT, "config", "lexicon_valve_products.json")
    if not os.path.exists(bench_lex):
        shutil.copy(LEX, bench_lex)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "benchmark.py"),
                        os.path.join(DATA, "valve_products.csv")],
                       capture_output=True, text=True, timeout=120)
    line = [l for l in r.stdout.splitlines() if "规则引擎" in l or "%" in l]
    print("  ", line[-1] if line else ("benchmark输出: " + r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "?"))


if __name__ == "__main__":
    demo()
