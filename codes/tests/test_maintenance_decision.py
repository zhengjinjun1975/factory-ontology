# -*- coding: utf-8 -*-
"""test_maintenance_decision.py — 预测性维护/优先检修决策插件 pytest。

阶段1 迁入 factory-ontology：从 ontology-analysis deep 语义决策移植的确定性
决策插件。验证打分公式/排序/动作分层/只列需动作设备/参数覆盖(阈值外置)。

用法(CI): cd codes && python -m pytest tests/test_maintenance_decision.py -q
"""
import os
import sys
import importlib.util

_CODES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # codes/
_PLUG = os.path.join(_CODES, "plugins", "maintenance_decision", "plugin.py")


def _plugin():
    sys.path.insert(0, _CODES)
    spec = importlib.util.spec_from_file_location("md_plugin_test", _PLUG)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.Plugin()


DEMO = {
    "today": "2026-09-02",
    "devices": [
        {"device_id": "EQ-0001", "device_name": "AGV小车", "status": "alarm",
         "priority": "P1", "maintenance_cycle_days": 180,
         "last_maintenance": "2026-01-10", "next_maintenance": "2026-09-20",
         "recent_faults": [{"fault_date": "2026-08-05", "severity": "严重"}],
         "spare_parts": [{"part_name": "激光雷达", "stock_qty": 1, "reorder_level": 3}]},
        {"device_id": "EQ-0002", "device_name": "输送线", "status": "running",
         "priority": "P3", "maintenance_cycle_days": 180,
         "last_maintenance": "2026-06-01", "next_maintenance": "2026-10-01",
         "recent_faults": [], "spare_parts": []},
        {"device_id": "EQ-0003", "device_name": "注塑机", "status": "offline",
         "priority": "P2", "maintenance_cycle_days": 120,
         "last_maintenance": "2025-12-01", "next_maintenance": "2026-04-01",
         "recent_faults": [{"fault_date": "2026-07-01", "severity": "中等"}],
         "spare_parts": [{"part_name": "伺服模块", "stock_qty": 2, "reorder_level": 3}]},
    ],
}


def test_run_shape_ok():
    r = _plugin().run(DEMO)
    assert r.get("ok") is True
    assert r["count"] == 2          # 只列需动作设备
    assert r["top"].startswith("AGV小车")


def test_score_and_reasons():
    r = _plugin().run(DEMO)
    d0 = r["decisions"][0]
    assert d0["name"] == "AGV小车"
    assert d0["score"] == 9         # 报警3 + 严重3 + 超周期2 + 备件1
    assert d0["action"] == "建议优先检修"
    assert any("超过" in x and "周期" in x for x in d0["reasons"])
    assert any("备件" in x and "告急" in x for x in d0["reasons"])


def test_sorted_desc():
    scores = [x["score"] for x in _plugin().run(DEMO)["decisions"]]
    assert all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1))


def test_weight_override():
    demo = dict(DEMO, fault_weight={"严重": 9, "中等": 2, "轻微": 1})
    agv = [x for x in _plugin().run(demo)["decisions"] if x["name"] == "AGV小车"][0]
    assert agv["score"] == 15       # 报警3 + 严重9 + 超周期2 + 备件1


def test_healthy_device_excluded():
    demo = {"today": "2026-09-02", "devices": [
        {"device_id": "E", "device_name": "X", "status": "running",
         "maintenance_cycle_days": 180, "last_maintenance": "2026-08-01",
         "next_maintenance": "2026-10-01", "recent_faults": [], "spare_parts": []}]}
    assert _plugin().run(demo)["count"] == 0
