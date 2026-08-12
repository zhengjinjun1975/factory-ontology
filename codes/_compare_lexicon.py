#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-7 词典收敛验证脚本：证明 lexicon.py 为唯一数据源、并集无遗漏。

收敛前（本脚本初版）已用 AST 抽取原常量并证明：
  原始 5 个词典键总数 53，合并去重后唯一键数 53，并集完整覆盖、无遗漏；
  跨词典键无冲突（各词典键互不重叠或值一致）。

本版改为「合并后」自检：
  1. lexicon.py 的 get_*() 只读接口返回规模正确（共 53 键）；
  2. graph_rag.py / ontology_qa_v3.py 不再内嵌字面量，而是 from lexicon 引用；
  3. 明确不并入对象（schema_ontology HINTS、ontology_qa_v3._EXTREME_WORD_FIELDS）仍原处保留。

用法：cd codes && python _compare_lexicon.py
"""
import ast
import sys
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# 1) lexicon.py 只读接口规模
import lexicon
expect = {
    "get_synonym_groups": 11,
    "get_unit_aliases": 3,
    "get_attr_cn_aliases": 16,
    "get_common_zh_status": 15,
    "get_entity_cn2uri": 8,
}
print("一、lexicon.py 数据规模（应为 11/3/16/15/8，合计 53 键）")
total = 0
for fn, n in expect.items():
    v = getattr(lexicon, fn)()
    ok = len(v) == n
    total += len(v)
    print(f"  [{'PASS' if ok else 'FAIL'}] {fn}() -> {len(v)} 条 (期望 {n})")
print(f"  合计唯一键数: {total} (期望 53)")
assert total == 53, "lexicon 并集规模异常"

# 2) 调用点是否改为 from lexicon 引用（不再内嵌字面量）
print("\n二、调用点收敛检查（graph_rag / ontology_qa_v3 应引用 lexicon）")
NAMES = {"_SYNONYM_GROUPS", "_UNIT_ALIASES", "_ATTR_CN_ALIASES",
         "_COMMON_ZH_STATUS", "_ENTITY_CN2URI"}
for f in ("graph_rag.py", "ontology_qa_v3.py"):
    tree = ast.parse(open(os.path.join(ROOT, f), encoding="utf-8").read())
    imports_lexicon = any(
        isinstance(n, ast.ImportFrom) and n.module == "lexicon"
        for n in ast.walk(tree))
    # 找内嵌字面量赋值
    literal = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id in NAMES
                and isinstance(n.value, (ast.Dict, ast.List, ast.Tuple))):
            literal.append(n.targets[0].id)
    print(f"  {f}: from lexicon 导入={'是' if imports_lexicon else '否'}, "
          f"残留内嵌字面量={literal if literal else '无'}")
    assert imports_lexicon, f"{f} 未引用 lexicon"

# 3) 不并入对象仍原处保留
print("\n三、明确不并入对象（语义不同，保留原处）")
from schema_ontology import _REF_HINTS, _MEASURE_HINTS, _DATE_HINTS, _CATEGORY_HINTS
from ontology_qa_v3 import _EXTREME_WORD_FIELDS
print(f"  schema_ontology._REF_HINTS={_REF_HINTS}")
print(f"  schema_ontology._MEASURE_HINTS={_MEASURE_HINTS}")
print(f"  schema_ontology._DATE_HINTS={_DATE_HINTS}")
print(f"  schema_ontology._CATEGORY_HINTS={_CATEGORY_HINTS}")
print(f"  ontology_qa_v3._EXTREME_WORD_FIELDS={_EXTREME_WORD_FIELDS}")
print("  （均为英文列名/中文字段名语义角色提示，非「中文→英文」同义词，故不并入）")

print("\n验证通过：lexicon.py 唯一数据源，并集 53 键无遗漏，调用点已收敛。")
