# -*- coding: utf-8 -*-
"""maintenance_decision — 预测性维护/优先检修决策插件（决策类扩展点，纯规则零 LLM）。

把本体驱动的"跨实体行为决策语义"落地成 factory-ontology 的一个 decision 插件：
输入每台设备的当前状态 + 近期故障史 + 保养周期/上次/下次 + 所用关键备件库存，
输出确定性、可解释的优先检修排序清单（"该拿什么主意"，而非只报状态）。

移植自 ontology-analysis 的 deep 语义决策（decide_priority_maintenance），
打分公式/默认权重与本体 schema 一致：score = 状态拉力(status_pull)
+ 近期故障加权分(fault_weight，按严重度) + 超保养周期(+2)/下次保养逾期(+2)/临近(+1)
+ 关键备件缺货风险(+1)。权重/窗口/告警阈值均可在 params 覆盖（决策阈值外置）。

params 格式：
{
  "today": "2026-09-02",            # 可选, 缺省当天
  "recent_days": 120,               # 近期故障窗口
  "alert_days": 7,                  # 下次保养临近窗口
  "fault_weight": {"严重":3,"中等":2,"轻微":1},   # 可选覆盖
  "status_pull": {"alarm":3,"maintenance":3,"offline":2,"idle":1,"running":0},
  "devices": [                      # 每台设备(调用方负责聚合其近期故障与所用备件)
    {"device_id":"EQ-0001","device_name":"AGV小车","status":"alarm",
     "priority":"P1","maintenance_cycle_days":180,
     "last_maintenance":"2026-01-10","next_maintenance":"2026-09-20",
     "recent_faults":[{"fault_date":"2026-08-05","severity":"严重"}],
     "spare_parts":[{"part_name":"激光雷达","stock_qty":1,"reorder_level":3}]},
    ...
  ]
}
run() -> {"ok": true, "count": N, "top": "<最优先设备>", "decisions":[...排序...]}
每项 decision: {id,name,score,status,priority,action,reasons}
"""

import os
import sys
from datetime import date, datetime

# 允许独立运行调试：python plugin.py
if __package__ in (None, ""):
    _ROOT = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))  # 向上三级 = codes/
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

from plugin_framework import BasePlugin  # noqa: E402

# ---- 决策默认值（与本体 schema deep.decision 一致；可在 params 覆盖）----
DEFAULT_FAULT_WEIGHT = {"严重": 3, "中等": 2, "轻微": 1}
DEFAULT_STATUS_PULL = {"alarm": 3, "maintenance": 3, "offline": 2, "idle": 1, "running": 0}
RECENT_DAYS = 120
ALERT_DAYS = 7
# 检修动作分层（score 阈值，同样可覆盖）
HIGH_ACTION = 5   # >=5 优先检修
MID_ACTION = 3    # >=3 尽快安排处理
# action 覆盖传入后不再用阈值

def _to_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


class Plugin(BasePlugin):
    name = "maintenance_decision"
    kind = "decision"
    version = "1.0.0"

    def register(self, reg):
        reg.register("decision", "maintenance",
                     self._maintenance_decision, plugin=self.name)

    # ------------------------------------------------------------ 决策
    def _decide(self, dev, cfg):
        """对单台设备打分：返回 (score, reasons)；score<=0 表示无需动作。"""
        today = cfg["today"]
        fweight = cfg["fault_weight"]
        status_pull = cfg["status_pull"]
        recent_days = cfg["recent_days"]
        alert = cfg["alert_days"]

        st = dev.get("status", "")
        st_pull = status_pull.get(st, 0)

        # 近期故障加权分
        fc, fscore = 0, 0
        for fa in dev.get("recent_faults", []) or []:
            fd = _to_date(fa.get("fault_date"))
            if fd and 0 <= (today - fd).days <= recent_days:
                fc += 1
                fscore += fweight.get(fa.get("severity", ""), 1)

        # 保养周期
        def _d_since(key):
            v = _to_date(dev.get(key))
            return None if v is None else (today - v).days
        days_since = _d_since("last_maintenance")
        nm_days = None
        v = _to_date(dev.get("next_maintenance"))
        if v is not None:
            nm_days = (v - today).days
        try:
            cycle = int(dev.get("maintenance_cycle_days") or 0) or None
        except (TypeError, ValueError):
            cycle = None
        over_cycle = bool(cycle and days_since is not None and days_since > cycle)
        near_maint = bool(nm_days is not None and 0 <= nm_days <= alert)
        overdue_maint = bool(nm_days is not None and nm_days < 0)

        # 关键备件缺货风险（任一所用备件库存低于再订购点）
        part_risk = False
        for p in dev.get("spare_parts", []) or []:
            try:
                if int(p.get("stock_qty", 0)) < int(p.get("reorder_level", 0)):
                    part_risk = True
                    break
            except (TypeError, ValueError):
                continue

        score = st_pull + fscore
        reasons = []
        if st in ("alarm", "maintenance"):
            reasons.append(f"当前处于{'报警/维修' if st == 'maintenance' else '报警'}状态")
        if st == "offline":
            reasons.append("当前离线")
        if fc > 0:
            reasons.append(f"近{recent_days}天发生{fc}次故障")
        if over_cycle:
            score += 2
            reasons.append(f"距上次保养已{days_since}天超过{cycle}天周期")
        if overdue_maint:
            score += 2
            reasons.append(f"下次保养已逾期{-nm_days}天")
        elif near_maint:
            score += 1
            reasons.append(f"{nm_days}天内到保养点")
        if part_risk:
            score += 1
            reasons.append("所用关键备件库存告急")
        return score, reasons

    @staticmethod
    def _action(score, high, mid):
        if score >= high:
            return "建议优先检修"
        if score >= mid:
            return "建议尽快安排处理"
        return "建议关注并预防性保养"

    def _maintenance_decision(self, params):
        params = params or {}
        cfg = {
            "today": _to_date(params.get("today")) or date.today(),
            "fault_weight": dict(DEFAULT_FAULT_WEIGHT, **params.get("fault_weight", {})),
            "status_pull": dict(DEFAULT_STATUS_PULL, **params.get("status_pull", {})),
            "recent_days": int(params.get("recent_days", RECENT_DAYS)),
            "alert_days": int(params.get("alert_days", ALERT_DAYS)),
        }
        high = int(params.get("high_action", HIGH_ACTION))
        mid = int(params.get("mid_action", MID_ACTION))

        decisions = []
        for dev in params.get("devices", []) or []:
            dev = dev or {}
            score, reasons = self._decide(dev, cfg)
            if score <= 0:
                continue
            decisions.append({
                "id": dev.get("device_id", ""),
                "name": dev.get("device_name", ""),
                "score": score,
                "status": dev.get("status", ""),
                "priority": dev.get("priority", "P3"),
                "action": self._action(score, high, mid),
                "reasons": reasons,
            })
        decisions.sort(key=lambda r: (-r["score"], r["priority"], r["name"]))
        top = decisions[0]["name"] if decisions else None
        return {"ok": True, "count": len(decisions),
                "top": f"{top}(score={decisions[0]['score']})" if top else None,
                "decisions": decisions}

    def run(self, params=None):
        return self._maintenance_decision(params or {})


if __name__ == "__main__":
    # 独立运行自测：python plugin.py
    import json
    demo = {
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
    print(json.dumps(Plugin().run(demo), ensure_ascii=False, indent=2))
