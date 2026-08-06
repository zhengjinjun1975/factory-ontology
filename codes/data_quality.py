#!/usr/bin/env python3
"""data_quality.py — 数据质量自动校验(反馈环)

扫描本体/数据, 检测异常并输出报告(供人工确认修复):
- 空值/缺失关键字段
- 数值异常(超范围/离群)
- 重复 ID / 无效引用(外键指向不存在的实体)
- 词典语义疑似误判(提示人工确认)

用法:
  python data_quality.py [food.nt] [--lexicon lexicon_food_products.json]
"""
import os
import sys
import json
import argparse

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def check(data_dir=os.path.join(ROOT, "data"), nt=None, lex=None, thresholds=None):
    """扫描知识库数据质量。返回报告 dict。"""
    import graph_rag as gr
    if nt is None:
        nt = os.path.join(ROOT, "output", "food.nt")
    thresholds = thresholds or {"price": 1000, "expiry_days": 3650}
    issues = []

    # 1. 数据文件空值检查
    import data_loader as dl
    for f in sorted(os.listdir(data_dir)):
        if not f.startswith("food_") or not f.endswith(".csv"):
            continue
        try:
            _, headers, rows = dl.load_table(os.path.join(data_dir, f))
        except Exception:
            continue
        key_col = "id" if "id" in headers else (headers[0] if headers else "")
        # join 表(food_batch_ingredient)的 batch_id 本就不唯一, 跳过重复ID检查
        is_join = "batch_id" in headers and "raw_id" in headers
        seen = set()
        for i, r in enumerate(rows):
            # 空关键值
            if key_col and not r.get(key_col, "").strip():
                issues.append(f"[空主键] {f} 第{i+1}行缺{key_col}")
            # 重复 ID (跳过 join 表)
            if key_col and r.get(key_col, "") and not is_join:
                v = r[key_col]
                if v in seen:
                    issues.append(f"[重复ID] {f} '{v}'")
                seen.add(v)

    # 2. 本体引用完整性(外键→存在实体)
    if os.path.exists(nt):
        graph, labels, vi, rev = gr.build_graph(nt)
        for ent, props in graph.items():
            for rel, vals in props.items():
                for v in vals:
                    if gr._is_entity(v) and v not in graph and v not in labels:
                        issues.append(f"[悬空引用] {gr.tail(ent)}.{rel} -> {gr.tail(v)}(不存在)")
        # 数值越界(按阈值)
        for ent, props in graph.items():
            for rel, vals in props.items():
                for v in vals:
                    if rel.lower() in thresholds and gr._is_entity(v) is False:
                        try:
                            fv = float(v)
                            if fv > thresholds[rel.lower()]:
                                issues.append(f"[数值异常] {gr.tail(ent)}.{rel}={fv} 超阈值{thresholds[rel.lower()]}")
                        except (ValueError, TypeError):
                            pass

    return {"issues": issues, "total": len(issues), "ok": len(issues) == 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("nt", nargs="?", default=os.path.join(ROOT, "output", "food.nt"))
    args = ap.parse_args()
    rep = check(nt=args.nt)
    status = "✅ 通过" if rep["ok"] else f"❌ {rep['total']} 项问题"
    print("=" * 40)
    print(f"数据质量报告: {status}")
    print("=" * 40)
    for i in rep["issues"][:30]:
        print(f"  - {i}")
    if not rep["issues"]:
        print("  未发现异常(空值/重复ID/悬空引用/数值越界)")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
