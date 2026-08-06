#!/usr/bin/env python3
"""ops_agent.py — 原子智能体：运维分析。

基于本体数据做三类分析（规则引擎，零依赖，不依赖LLM）：
  1. 异常设备: 状态=alarm / maintenance 的设备清单
  2. 维护提醒: 下次维护已到期或临近(30天内)的设备
  3. 产能统计: 按区域/类型/状态分布
task 结构:
  {"nt_file": "...", "report_type": "anomaly|maintenance|stats|all", "days_until_due": 30}

v2 修复：
- 移除硬编码 本仓库 绝对路径（违反"零依赖可迁移"宣称）
- 复用本套件 ontology_qa_v3 的 parse_nt/build_data（不再依赖外部不存在的 src/ontology_qa.py）
- 字段访问走 v3._field（别名+驼峰兜底），适配不同数据源
"""

import os
import sys
import importlib.util
from datetime import datetime, timedelta
from collections import Counter

_APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP)
sys.path.insert(0, os.path.join(_APP, ".."))

from core.base_agent import BaseAgent, AgentResult


def _load_v3():
    """动态加载本套件的 ontology_qa_v3（路径基于脚本位置自适应，可迁移）。"""
    v3_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ontology_qa_v3.py")
    spec = importlib.util.spec_from_file_location("ontology_qa_v3", v3_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


class OpsAgent(BaseAgent):
    name = "ops"

    def run(self, task: dict) -> AgentResult:
        return self._timed(self._run, task)

    def _run(self, task):
        # 路径基于套件根目录自适应
        codes_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default_nt = os.path.join(codes_root, "output", "equipment.nt")
        nt = task.get("nt_file") or default_nt
        if not os.path.exists(nt):
            # 兼容相对路径
            cand = os.path.join(codes_root, nt) if not os.path.isabs(nt) else nt
            if os.path.exists(cand):
                nt = cand
        rtype = task.get("report_type", "all")
        due_days = int(task.get("days_until_due", 30))

        v3 = _load_v3()
        D = task.get("lexicon")
        aliases = {}
        if D and os.path.exists(D):
            try:
                aliases = v3.load_dict(D).get("field_aliases", {})
            except Exception:
                aliases = {}
        triples = v3.parse_nt(nt)
        data = v3.build_data(triples, D or {})
        if not data:
            return self._err(f"本体解析失败或空: {nt}")

        def f(rec, canonical):
            """用 v3 字段别名+驼峰兜底取值。"""
            return v3._field(rec, canonical, aliases)

        report = {"total_devices": len(data), "report_type": rtype}

        # 异常检测: 状态 in (alarm/maintenance/offline) 或 故障标记字段=True
        if rtype in ("anomaly", "all"):
            abnormal = []
            for name, d in data.items():
                st = f(d, "status").lower()
                fault_keys = ("MachineFailure", "machineFailure", "TWF", "HDF", "PWF", "OSF", "RNF")
                is_fault = any(str(d.get(k, "")).lower() in ("true", "1") for k in fault_keys)
                if st in ("alarm", "maintenance", "offline", "故障", "停机") or is_fault:
                    abnormal.append({
                        "id": f(d, "deviceId") or f(d, "id") or name,
                        "name": f(d, "deviceName"),
                        "status": st or "fault",
                        "zone": f(d, "location"),
                    })
            report["anomalies"] = abnormal
            report["anomaly_count"] = len(abnormal)

        if rtype in ("maintenance", "all"):
            today = datetime.now()
            due = []
            for name, d in data.items():
                nd = _parse_date(f(d, "lastMaintenance") or f(d, "nextMaintenance"))
                if nd:
                    days_left = (nd - today).days
                    if days_left <= due_days:
                        due.append({"id": f(d, "deviceId") or f(d, "id") or name,
                                    "name": f(d, "deviceName"),
                                    "next_maintenance": nd.strftime("%Y-%m-%d"),
                                    "days_left": days_left,
                                    "status": f(d, "status")})
            due.sort(key=lambda x: x["days_left"])
            report["maintenance_due"] = due
            report["maintenance_count"] = len(due)

        if rtype in ("stats", "all"):
            report["by_status"] = dict(Counter(f(d, "status") or "?" for d in data.values()))
            report["by_zone"] = dict(Counter(f(d, "location") or f(d, "zone") or "?" for d in data.values()))
            report["by_type"] = dict(Counter(f(d, "deviceType") or "?" for d in data.values()))

        return self._ok(report, "ops")


def main():
    import json
    codes_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    task = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {
        "nt_file": os.path.join(codes_root, "output", "equipment.nt"), "report_type": "all"}
    r = OpsAgent().run(task)
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
