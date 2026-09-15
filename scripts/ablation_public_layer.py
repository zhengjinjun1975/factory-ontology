#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""消融：公共/行业词典到底贡献了多少问答命中率（工厂自建词典 vs +公共层）。

问题来源：eval_hit_rate 的确定性问句集（同评测集/同判法，口径唯一，不另造一套）。
判据：judge()（答案里出现 golden，确定性比对，不调模型）。

用法：python scripts/ablation_public_layer.py --kb valve --n 12
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CODES = os.path.join(ROOT, "codes")
sys.path.insert(0, CODES)
sys.path.insert(0, HERE)

import industrial_dict_loader as idl   # noqa: E402
import schema_ontology as so           # noqa: E402
import multi_model as mm               # noqa: E402
import ontology_qa_v3 as v3            # noqa: E402
import eval_hit_rate as ehr            # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", default="valve")
    ap.add_argument("--n", type=int, default=12, help="每类问题数")
    a = ap.parse_args()

    data_dir = os.path.join(CODES, "data_%s" % a.kb)
    nt = os.path.join(CODES, "output", "%s.nt" % a.kb)
    tables = ehr.load_tables(data_dir)
    schema = so.suggest_schema(so.load_all(data_dir), use_llm=False, industry=a.kb)

    # 纯工厂词典：把公共层合并打桩成 no-op（零副作用，不动文件）
    orig = idl.merge_industrial_dict
    idl.merge_industrial_dict = lambda d, files=None, industry=None: dict(d)
    try:
        D_pure = mm._build_lexicon(schema, so.load_all(data_dir), industry="")
    finally:
        idl.merge_industrial_dict = orig
    D_full = mm._build_lexicon(schema, so.load_all(data_dir), industry=a.kb)
    D_load = v3.load_dict(os.path.join(CODES, "config", "lexicon_%s.json" % a.kb), industry=None)

    ehr.LEX_ENT = {v: k for k, v in (D_full.get("entity_cn2en") or {}).items()}
    ehr.LEX_ATTR = {v: k for k, v in (D_full.get("attr_cn2en") or {}).items()}
    qs = ehr.gen_questions(tables, n_per_type=a.n)

    def words(D):
        return sum(len(v) for k, v in D.items() if isinstance(v, dict) and k in idl._MERGE_KEYS)

    print("=== 词典规模 ===")
    for name, D in (("纯工厂词典（无公共层）", D_pure),
                    ("工厂词典 + 公共/行业层", D_full),
                    ("问答实际加载（含兜底）", D_load)):
        print("  %-22s 词表键合计 %d 词" % (name, words(D)))
    print("  问句集 %d 句（每类 %d）" % (len(qs), a.n))

    print("\n=== 分组命中率（同问句集/同判法）===")
    results = {}
    for name, D in (("G1 纯工厂词典", D_pure),
                    ("G2 +公共/行业层", D_full),
                    ("G3 问答实际加载", D_load)):
        QD = v3.build_data(v3.parse_nt(nt), D)
        hit, miss = [], []
        for q in qs:
            ok = ehr.judge(q, v3.answer(q["q"], QD, D))
            (hit if ok else miss).append(q["type"])
        rate = 100.0 * len(hit) / max(1, len(qs))
        results[name] = rate
        print("  %-16s %2d/%2d = %5.1f%%   未命中类型分布: %s"
              % (name, len(hit), len(qs), rate,
                 {t: miss.count(t) for t in sorted(set(miss))}))
    delta = results["G2 +公共/行业层"] - results["G1 纯工厂词典"]
    print("\n公共/行业层贡献：%+.1f 个百分点（%d 句问句集）" % (delta, len(qs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
