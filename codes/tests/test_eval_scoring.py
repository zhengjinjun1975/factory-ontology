#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评测判分口径回归（代码审查 P2-2）。

背景：`benchmark.fuzzy_match()` 与 `scripts/eval_hit_rate.judge()` 原先用纯子串判命中，
数字型标准答案会假命中（"1200" 命中 "12000"、"12" 命中 "120"）。本测试把"数字型 golden
必须按数字边界相等、文本型 golden 仍子串匹配"这条口径钉住，防以后改回去。

运行: cd codes && python -m pytest tests/test_eval_scoring.py -q
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # codes/
REPO = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)


def _load_eval_hit_rate():
    path = os.path.join(REPO, "scripts", "eval_hit_rate.py")
    spec = importlib.util.spec_from_file_location("eval_hit_rate_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fuzzy_match_number_boundary():
    from benchmark import fuzzy_match

    assert fuzzy_match("总共 1200 台", "1200") is True
    # 旧实现 `g in answer` 会判 True（"1200" 是 "12000" 的子串）
    assert fuzzy_match("总共 12000 台", "1200") is False
    assert fuzzy_match("1200", "1200") is True
    assert fuzzy_match("共1,200台", "1200") is True          # 千分位走数值容错分支
    # 旧实现假命中："12" 是 "120" 的前缀
    assert fuzzy_match("温度 120 度", "12") is False
    assert fuzzy_match("空压机 3 台", "空压机") is True        # 文本型 golden 仍子串匹配
    assert fuzzy_match("[模型未启用]", "1200") is False


def test_judge_contains_all_number_boundary():
    m = _load_eval_hit_rate()

    assert m.judge({"check": "contains_all", "golden_vals": ["1200"]}, "共有 1200 台") is True
    assert m.judge({"check": "contains_all", "golden_vals": ["1200"]}, "共有 12000 台") is False
    # 混搭：文本项子串匹配、数字项边界相等
    q = {"check": "contains_all", "golden_vals": ["空压机", "3"]}
    assert m.judge(q, "空压机 3 台") is True
    assert m.judge(q, "空压机 30 台") is False


def test_judge_num_branch_unchanged():
    m = _load_eval_hit_rate()

    assert m.judge({"check": "num", "golden_num": 1200.0}, "共 1200 台") is True
    assert m.judge({"check": "num", "golden_num": 1195.0}, "共 1200 台") is True   # 1% 容错
    assert m.judge({"check": "num", "golden_num": 1000.0}, "共 1200 台") is False
