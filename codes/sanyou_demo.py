#!/usr/bin/env python3
"""sanyou_demo.py — 三油科技内部知识管理 demo（合成示例数据）

实证 B4 内部使用方案：用框架管三油科技的设备台账 + 合同台账。
- 设备问答（数量/极值/过滤）+ 合同问答（金额/状态）
- 保修/回款提醒（状态过滤）

数据：data_sanyou/*.csv（合成示例，非真实数据）
"""
import os
import sys
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
DATA = os.path.join(ROOT, "data_sanyou")
NT = os.path.join(ROOT, "output", "sanyou.nt")
LEX = os.path.join(ROOT, "config", "lexicon_sanyou.json")


def build():
    import multi_table as mt
    from data_loader import load_table
    tables = {}
    for t, idc in [("sanyou_equipment", "id"), ("sanyou_contracts", "id")]:
        n, h, rows = load_table(os.path.join(DATA, f"{t}.csv"))
        tables[n] = {"headers": h, "rows": rows, "id_col": idc}
    os.makedirs(os.path.dirname(NT), exist_ok=True)
    mt.build_nt(tables, {}, NT)
    return NT


def demo():
    import ontology_qa_v3 as v3
    D = v3.load_dict(LEX)
    nt = build()
    data = v3.build_data(v3.parse_nt(nt), D)

    def ask(q):
        # 规则引擎(结构化) → 逻辑桥(自然语言)
        ans = v3.answer(q, data, D)
        if ans != "暂不支持该问题":
            return ans, "rule"
        try:
            import logical_qa
            res = logical_qa.answer(q, data, D)
            if res:
                return res[0], "logical"
        except Exception:
            pass
        return "暂不支持", "miss"

    print("═ 一、设备台账问答 ═")
    for q in ["一共有多少台设备", "压力等级4.0的设备", "不锈钢的设备有哪些", "华北炼油的设备有哪些", "保修期内的设备"]:
        ans, mode = ask(q)
        print(f"  [{mode}] {q} → {ans[:45]}")
    print("═ 二、合同台账问答 ═")
    for q in ["金额超过100万的合同", "未回款的合同", "给华东化工签过哪些合同"]:
        ans, mode = ask(q)
        print(f"  [{mode}] {q} → {ans[:45]}")


if __name__ == "__main__":
    demo()
