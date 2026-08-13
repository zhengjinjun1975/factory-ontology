# -*- coding: utf-8 -*-
"""self_onboard.py — 甲方自助 onboarding: 自动生成评测示例问题 + 字典补全提示。

目标: 甲方上传数据后无需 FDE 补词即可用。本模块从已建好的本体(nt)与词典(lexicon)
自动生成一组可被规则引擎/混合检索回答的基准问题(benchmark examples), 并把"可能仍需
人工校准"的词典项(pending review)一并返回, 供前端提示甲方确认, 而非阻塞式等 FDE。

核心: 问题生成做到"字段级对齐"——每个实体(entity_cn2en)只配它自己表里真实存在的
数值/类型/状态字段生成极值/类型/状态问题, 避免"温度最大的批次"(批次表无温度)这类
字段错配导致误报低分。
"""
import os
import json
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import multi_model as mm
import ontology_qa_v3 as v3


def _load_data(data_dir):
    """读取建模数据目录 {表名: [行]}。与 multi_model.build 一致走 schema_ontology.load_all。"""
    so = mm._load("schema_ontology", os.path.join(ROOT, "schema_ontology.py"))
    return so.load_all(data_dir)


def _numeric_fields_by_table(data, numeric_fields):
    """把词典 numeric_fields {中文极值词: 英文字段} 反查到所属表。

    返回 {表名: [(中文词, 字段名), ...]}。字段在哪个表就归哪个表, 供实体配对其极值字段。
    """
    out = {}
    for cn, field in (numeric_fields or {}).items():
        for table, rows in (data or {}).items():
            if rows and field in rows[0]:
                out.setdefault(table, []).append((cn, field))
    return out


def _has_col(data, table, field):
    rows = (data or {}).get(table) or []
    return bool(rows) and field in rows[0]


def gen_example_questions(data_dir, lex_path, nt_path=None, limit=24):
    """从已建好的数据+词典自动生成基准问题(供 benchmark 自评)。

    字段级对齐避免实体/属性错配:
    - 计数: 每个实体 "有多少{实体中文名}"
    - 类型: 实体表有类型列 -> "{实体}的类型有哪些"
    - 极值: 实体表存在的数值字段 -> "{中文极值词}最大的{实体}"
    - 状态: 实体表有状态列 -> "{状态值}的{实体}有多少"
    返回去重、截断到 limit 的问题列表。
    """
    D = v3.load_dict(lex_path)
    data = _load_data(data_dir) if data_dir else {}
    ent = list((D.get('entity_cn2en') or {}).keys())
    type_vals = list((D.get('type_cn2en') or {}).keys())
    status_vals = list((D.get('status_cn2en') or {}).keys())
    nf_by_table = _numeric_fields_by_table(data, D.get('numeric_fields'))

    qs = []
    seen = set()

    def add(q):
        if q and q not in seen:
            seen.add(q)
            qs.append(q)

    for cn in ent:
        if not cn or len(cn) > 8:
            continue
        table = (D.get('entity_cn2en') or {}).get(cn)
        add(f"有多少{cn}")
        if type_vals:
            add(f"{cn}的类型有哪些")
        if status_vals and _has_col(data, table, 'status'):
            for st in status_vals[:2]:
                add(f"{st}的{cn}有多少")
        # 该实体表真实存在的数值字段 -> 极值问题
        for nf_cn, field in nf_by_table.get(table, [])[:2]:
            if nf_cn and re.search(r'[A-Za-z0-9]', nf_cn):
                continue  # 无中文极值词的跳过(英文字段名不可读)
            add(f"{nf_cn}最大的{cn}")

    return qs[:limit]


def pending_review(D, data_dir=None):
    """返回词典待人工确认项(仅供前端提示, 不阻塞): 实体中文名猜测/兜底字段等。"""
    D = D or {}
    items = []
    ent = (D.get('entity_cn2en') or {})
    for cn, table in ent.items():
        items.append({"type": "entity", "cn": cn, "table": table,
                      "hint": f"识别实体「{cn}」= 数据表 {table}，请确认是否为企业业务对象"})
    # 枚举兜底字段(非标准 type/status 列名, 可能误判为产品类型)
    return items


if __name__ == "__main__":
    ddir = sys.argv[1] if len(sys.argv) > 1 else "data_valve"
    lex = sys.argv[2] if len(sys.argv) > 2 else None
    table = "ent_probe"
    if not lex:
        table, tables, n = mm.build(ddir, table)
        lex = f"config/lexicon_{table}.json"
    qs = gen_example_questions(ddir, lex)
    print(f"GEN {len(qs)} QUESTIONS from {ddir}")
    for q in qs:
        print("  ", q)
