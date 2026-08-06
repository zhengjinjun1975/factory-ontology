#!/usr/bin/env python3
"""证据提取单测（pytest）— 自包含，无需外部模型。

运行：cd codes && python -m pytest tests/test_evidence.py -q
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # codes/
sys.path.insert(0, ROOT)

import csv_to_owl as c2o
import ontology_qa_v3 as v3
from evidence import extract_evidence


def _build_food(tmp_path):
    """用食品产品数据建本体 + 词典, 返回 (data, D)。"""
    ntp = str(tmp_path / "food.nt")
    c2o.build_nt(os.path.join(ROOT, "data", "food_products.csv"), ntp)
    lex = os.path.join(ROOT, "config", "lexicon_food_products.json")
    D = v3.load_dict(lex)
    data = v3.build_data(v3.parse_nt(ntp), D)
    return data, D


def test_evidence_count_by_type(tmp_path):
    """'乳制品的数量' 应命中 count_by_type 规则, 并提取出乳制品实体证据。"""
    data, D = _build_food(tmp_path)
    q = "乳制品的数量"
    ans = v3.answer(q, data, D)
    ev = extract_evidence(q, data, D, ans)

    assert ev["rule"] == "count_by_type"
    assert ev["entities"], "应提取到非空实体证据"
    # 乳制品实体 = 原味酸奶/草莓酸奶/鲜牛奶 (category=乳制品)
    names = [e["name"] for e in ev["entities"]]
    assert len(names) == 3
    assert "原味酸奶" in names and "鲜牛奶" in names
    # 每个证据都应带 prop + value
    for e in ev["entities"]:
        assert e["prop"] == "category"
        assert e["value"] == "乳制品"


def test_evidence_extreme(tmp_path):
    """'保质期最长的产品' 应命中 extreme 规则, 提取到最长保质期实体证据。"""
    data, D = _build_food(tmp_path)
    q = "保质期最长的产品"
    ans = v3.answer(q, data, D)
    ev = extract_evidence(q, data, D, ans)

    assert ev["rule"] == "extreme"
    assert ev["entities"], "应提取到非空实体证据"
    e = ev["entities"][0]
    assert e["prop"] == "保质期"  # attr_en2cn 还原中文属性名
    # 最长保质期 180 天, 鸡蛋灌饼/手工水饺 都是 180
    assert float(e["value"]) == 180.0
    assert e["name"] in ("鸡蛋灌饼", "手工水饺")
