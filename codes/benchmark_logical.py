#!/usr/bin/env python3
"""benchmark_logical.py — 逻辑推理桥命中率评测

规则引擎覆盖结构化模板; 逻辑桥(LLM转逻辑查询→确定性执行)覆盖更多开放式问题而不失确定性。
评测: 对(问题, 期望关键事实)跑 logical_qa.answer, 检查答案含期望事实。

用法:
  python benchmark_logical.py [--limit N]
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# 规则引擎可能 miss、但逻辑桥能答的开放式问题 + 期望答案含的关键事实
# (期望值按 data/food_products.csv 实际数据: 最贵=手工水饺18元; >5元含手工水饺等)
# 注: 值阈值比较(如"保质期>10天")是逻辑桥暂不支持的类型, 不列入评测
CASES = [
    ("有多少种乳制品", ["3"]),
    ("价格超过5元的产品", ["手工水饺"]),
    ("一共有多少个产品", ["8"]),
    ("售价最贵的产品是什么", ["手工水饺"]),
    ("保质期最长的产品", ["180"]),
]


def evaluate(nt, lex, limit=None):
    import logical_qa
    import ontology_qa_v3 as v3
    D = v3.load_dict(lex)
    data = v3.build_data(v3.parse_nt(nt), D)
    cases = CASES[:limit] if limit else CASES
    results = []
    for q, expects in cases:
        try:
            res = logical_qa.answer(q, data, D)
            ans = res[0] if res else ""
            mode = res[1] if res else "none"
        except Exception as e:
            ans, mode = "", f"err:{e}"
        hit = any(exp in ans for exp in expects)
        results.append((q, hit, mode, ans[:40]))
    total = len(results)
    passed = sum(1 for _, h, _, _ in results if h)
    return passed, total, results


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--nt", default=os.path.join(ROOT, "output", "food_products.nt"))
    ap.add_argument("--lexicon", default=os.path.join(ROOT, "config", "lexicon_food_products.json"))
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    # 自动构建: NT 缺失时用 csv_to_owl 从 data/food_products.csv 生成(鲁棒性)
    if not os.path.exists(args.nt):
        os.makedirs(os.path.dirname(args.nt), exist_ok=True)
        import csv_to_owl
        csv_to_owl.build_nt(os.path.join(ROOT, "data", "food_products.csv"), args.nt)
    passed, total, results = evaluate(args.nt, args.lexicon, args.limit)
    print("问题".ljust(22), "结果", "模式", "答案")
    print("-" * 60)
    for q, h, mode, ans in results:
        print(f"{q[:20]:22} {'✅' if h else '❌'} {mode[:8]:10} {ans}")
    print("-" * 60)
    rate = passed / total * 100 if total else 0
    print(f"逻辑推理桥命中率: {passed}/{total} = {rate:.0f}%")
    return 0 if passed / max(total, 1) >= 0.5 else 1


if __name__ == "__main__":
    sys.exit(main())
