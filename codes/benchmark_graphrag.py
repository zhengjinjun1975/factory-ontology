#!/usr/bin/env python3
"""benchmark_graphrag.py — GraphRAG 开放式问题命中率评测（LLM 兜底实证）

规则引擎已验证结构化查询 100%(benchmark.py);
这里补上开放式/关系问题的 GraphRAG 生成质量评测, 完善方法论实证。

方法: 对每组(问题, 期望关键事实)跑 answer_graph, 检查答案是否含期望事实。
需配置模型(model_config.json 或 env)。

用法:
  python benchmark_graphrag.py [--limit N] [--lexicon config/lexicon_food_products.json]
"""
import os
import sys
import json
import argparse

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# 开放式问题 + 期望答案含的关键事实(实体/值/关系)
CASES = [
    ("B001 用了哪些原料", ["RM001", "生牛乳"]),
    ("原味酸奶是什么", ["乳制品", "200g"]),
    ("哪些批次用了盐", ["B005"]),
    ("RM008 盐影响了哪些产品", ["P003", "全麦面包"]),
    ("全麦面包的保质期", ["7"]),
    ("价格最贵的乳制品", ["鲜牛奶"]),
    ("草莓酸奶的保质期", ["21"]),
    ("鲜牛奶的储存条件", ["冷藏"]),
]


def evaluate(nt, lexicon_path, limit=None):
    import graph_rag as gr
    import ontology_qa_v3 as v3
    D = v3.load_dict(lexicon_path)
    cases = CASES[:limit] if limit else CASES
    results = []
    for q, expects in cases:
        ans, ctx = gr.answer_graph(q, nt, depth=2, max_nodes=40, lexicon=D)
        hit = any(exp in ans for exp in expects)
        results.append((q, hit, ans[:50]))
    total = len(results)
    passed = sum(1 for _, h, _ in results if h)
    return passed, total, results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nt", default=os.path.join(ROOT, "output", "food.nt"))
    ap.add_argument("--lexicon", default=os.path.join(ROOT, "config", "lexicon_food_products.json"))
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    passed, total, results = evaluate(args.nt, args.lexicon, args.limit)
    print("问题".ljust(24), "结果", "答案")
    print("-" * 60)
    for q, h, ans in results:
        print(f"{q[:22]:24} {'✅' if h else '❌'} {ans[:34]}")
    print("-" * 60)
    rate = passed / total * 100 if total else 0
    print(f"GraphRAG 开放式问题命中率: {passed}/{total} = {rate:.0f}%")
    return 0 if passed / max(total, 1) >= 0.5 else 1


if __name__ == "__main__":
    sys.exit(main())
