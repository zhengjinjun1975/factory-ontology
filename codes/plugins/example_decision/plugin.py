# -*- coding: utf-8 -*-
"""example_decision — 设备维护决策规则插件（决策类扩展点示例）。

给第三方开发者演示「如何写一个 decision 插件」：
  1. 定义 Plugin 类（可继承 BasePlugin，也可实现同名接口）
  2. 在 register() 里用 reg.register(kind, id, handler) 对外暴露决策能力
  3. handler 统一签名 fn(params: dict) -> 可 JSON 序列化结果

本插件基于设备台账做确定性维护决策：
  - 空气温度过高 / 刀具磨损超标 / 转速过低 → 触发维护或预警
  - 纯规则，零 LLM，输出可复现、带判断依据，契合本项目「确定性优先」。
"""

import sys
import os

# 允许独立运行调试：python plugin.py（此时能 import 到框架）
if __package__ in (None, ""):
    _ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

from plugin_framework import BasePlugin  # noqa: E402


class Plugin(BasePlugin):
    name = "example_decision"
    kind = "decision"
    version = "1.0.0"

    def register(self, reg):
        """把本插件的决策能力登记进扩展点注册表。

        - decision/maintenance_priority：输入设备台账记录，输出维护优先级建议
        - decision/failure_alert：输入记录，判断是否需立即告警
        """
        reg.register("decision", "maintenance_priority",
                     self._maintenance_priority, plugin=self.name,
                     meta={"description": "按阈值给出设备维护优先级"})
        reg.register("decision", "failure_alert",
                     self._failure_alert, plugin=self.name,
                     meta={"description": "判断是否触发设备故障告警"})

    def run(self, params):
        """插件主逻辑：对入参中的每条记录做维护决策。"""
        records = params.get("records")
        if records is None:
            # 无 records 时输出阈值说明，便于体验
            return {"ok": True, "note": "请传 records 参数查看逐条决策",
                    "thresholds": self._thresholds()}
        if not isinstance(records, list):
            return {"ok": False, "error": "records 必须是列表"}

        decisions = [self._decide(r) for r in records]
        urgent = [d for d in decisions if d["priority"] == "紧急"]
        return {
            "ok": True,
            "total": len(decisions),
            "urgent": len(urgent),
            "decisions": decisions,
        }

    # ── 决策规则内核 ─────────────────────────────
    def _thresholds(self):
        return {
            "air_temperature": {"high": 300, "unit": "K"},   # 空气温度上限
            "tool_wear": {"high": 200, "unit": "min"},       # 刀具磨损上限
            "rotational_speed": {"low": 1000, "unit": "rpm"},# 转速下限
            "target": {"label": "维护优先级",
                       "levels": ["正常", "关注", "预警", "紧急"]},
        }

    def _decide(self, r):
        """对单条记录评分并定级。r 为 dict（设备台账字段）。"""
        score = 0
        reasons = []
        air = r.get("air_temperature")
        wear = r.get("tool_wear")
        speed = r.get("rotational_speed")

        if air is not None and air >= self._thresholds()["air_temperature"]["high"]:
            score += 2
            reasons.append(f"空气温度 {air}K 超上限")
        if wear is not None and wear >= self._thresholds()["tool_wear"]["high"]:
            score += 2
            reasons.append(f"刀具磨损 {wear}min 超限")
        if speed is not None and speed <= self._thresholds()["rotational_speed"]["low"]:
            score += 1
            reasons.append(f"转速 {speed}rpm 过低")
        if r.get("failure_type") and r["failure_type"] not in (None, "", "无"):
            score += 3
            reasons.append(f"已记录故障类型 {r['failure_type']}")

        if score >= 3:
            priority, advice = "紧急", "立即停机检修"
        elif score == 2:
            priority, advice = "预警", "本班内安排检修"
        elif score == 1:
            priority, advice = "关注", "加强巡检"
        else:
            priority, advice = "正常", "按计划维护"

        return {
            "device": r.get("device_id") or r.get("uid") or "未知设备",
            "priority": priority,
            "score": score,
            "advice": advice,
            "reasons": reasons,
        }

    # 扩展点 handler：maintenance_priority
    def _maintenance_priority(self, params):
        records = (params or {}).get("records", [])
        return {"ok": True, "decisions": [self._decide(r) for r in records]}

    # 扩展点 handler：failure_alert
    def _failure_alert(self, params):
        records = (params or {}).get("records", [])
        alerts = []
        for r in records:
            d = self._decide(r)
            if d["priority"] in ("预警", "紧急"):
                alerts.append(d)
        return {"ok": True, "alert_count": len(alerts), "alerts": alerts}


if __name__ == "__main__":
    # 独立运行自测：python plugin.py
    demo = [
        {"device_id": "D001", "air_temperature": 302, "tool_wear": 205,
         "rotational_speed": 980},
        {"device_id": "D002", "air_temperature": 290, "tool_wear": 40,
         "rotational_speed": 1500},
        {"device_id": "D003", "air_temperature": 315, "tool_wear": 40,
         "rotational_speed": 1500, "failure_type": "Tool Wear Failure"},
    ]
    import json
    print(json.dumps(Plugin().run({"records": demo}),
                     ensure_ascii=False, indent=2))
