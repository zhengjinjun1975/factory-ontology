#!/usr/bin/env python3
"""test_logical.py — logical_qa 确定性执行器单测（不依赖 LLM/网络）。

直接构造逻辑查询 JSON，验证 execute_query 的确定性结算：
  - count   过滤后计数
  - extreme 求最值（max/min）
  - total   总记录数
  - filter  属性过滤
  - topn    前 N 个
  - 无法执行（非法 intent / 属性缺失）返回 None

运行：cd codes && python -m pytest tests/test_logical.py -q
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # codes/
sys.path.insert(0, ROOT)

from logical_qa import execute_query, _extract_json, _matches_filter  # noqa: E402


# 合成小数据：{实体: {属性: 值}}，属性用英文标准字段
def _data():
    return {
        "A": {"product_name": "纯牛奶", "category": "乳制品", "expiry_days": "90", "price": "12.0"},
        "B": {"product_name": "酸奶", "category": "乳制品", "expiry_days": "21", "price": "8.0"},
        "C": {"product_name": "面包", "category": "烘焙", "expiry_days": "3", "price": "5.0"},
        "D": {"product_name": "奶酪", "category": "乳制品", "expiry_days": "180", "price": "25.0"},
    }


def _D():
    return {
        "attr_cn2en": {"保质期": "expiry_days", "价格": "price", "分类": "category"},
        "attr_en2cn": {"expiry_days": "保质期", "price": "价格", "category": "分类"},
        "field_aliases": {"deviceName": ["product_name", "name"]},
    }


# ── count: 过滤后计数 ──
def test_execute_count_with_filter():
    q = {"intent": "count", "attr": None, "filter_cn": "乳制品", "rel": None, "n": None}
    ans = execute_query(q, _data(), _D())
    assert ans is not None and "3" in ans


def test_execute_count_no_filter_counts_all():
    q = {"intent": "count", "attr": None, "filter_cn": None, "rel": None, "n": None}
    ans = execute_query(q, _data(), _D())
    assert ans is not None and "4" in ans


# ── extreme: 求最值 ──
def test_execute_extreme_max():
    q = {"intent": "extreme", "attr": "expiry_days", "extreme_dir": "max",
         "filter_cn": None, "rel": None, "n": None}
    ans = execute_query(q, _data(), _D())
    assert ans is not None and "奶酪" in ans and "180" in ans


def test_execute_extreme_min_with_filter():
    q = {"intent": "extreme", "attr": "price", "extreme_dir": "min",
         "filter_cn": "乳制品", "rel": None, "n": None}
    ans = execute_query(q, _data(), _D())
    assert ans is not None and "8.0" in ans and "酸奶" in ans


# ── total: 总记录数 ──
def test_execute_total():
    q = {"intent": "total", "attr": None, "filter_cn": None, "rel": None, "n": None}
    ans = execute_query(q, _data(), _D())
    assert ans is not None and "4" in ans


# ── filter: 属性过滤 ──
def test_execute_filter():
    q = {"intent": "filter", "attr": "category", "filter_cn": "乳制品", "rel": None, "n": None}
    ans = execute_query(q, _data(), _D())
    assert ans is not None and "3" in ans


# ── topn: 前 N 个 ──
def test_execute_topn():
    q = {"intent": "topn", "attr": "expiry_days", "filter_cn": None, "rel": None, "n": 2}
    ans = execute_query(q, _data(), _D())
    assert ans is not None and "奶酪" in ans and "纯牛奶" in ans


# ── 无法执行 → None ──
def test_execute_invalid_intent_returns_none():
    q = {"intent": "unknown", "attr": "expiry_days", "filter_cn": None, "rel": None, "n": None}
    assert execute_query(q, _data(), _D()) is None


def test_execute_missing_attr_returns_none():
    q = {"intent": "extreme", "attr": "not_exist", "extreme_dir": "max",
         "filter_cn": None, "rel": None, "n": None}
    assert execute_query(q, _data(), _D()) is None


# ── 内部解析辅助：_extract_json 从 LLM 文本里挖 JSON ──
def test_extract_json_tolerates_code_fence():
    raw = '```json\n{"intent": "count", "attr": null, "filter_cn": "乳制品", "rel": null, "n": null}\n```'
    assert _extract_json(raw)["intent"] == "count"


def test_extract_json_nonjson_returns_none():
    assert _extract_json("抱歉我不懂") is None


# ── 过滤匹配辅助 ──
def test_matches_filter():
    rec = {"category": "乳制品", "expiry_days": "90"}
    assert _matches_filter(rec, "乳制品", _D()) is True
    assert _matches_filter(rec, "烘焙", _D()) is False
