#!/usr/bin/env python3
"""aggregate.py — 本体多表聚合统计（设备+产线 join），供 Web 前端可视化。

输出 JSON 结构：
{
  "device_type_dist":  [{"type": "空压机", "count": 2}, ...],      # 设备类型分布
  "status_dist":       [{"status": "running", "count": 7}, ...],   # 状态分布
  "zone_dist":         [{"zone": "车间A", "count": 2}, ...],       # 区域分布
  "line_stats":        [{"line": "L1", "name": "一号装配线", "area": "车间A",
                          "supervisor": "张工", "device_count": 2, "running": 2,
                          "alarm": 0, "total_power_kw": 45.0}, ...],  # 产线join设备
  "fault_rate": 0.1,                                              # 故障率(alarm+maintenance)/总
  "total_devices": 10
}

用法: python aggregate.py <equipment.csv> <line.csv>
零依赖，纯标准库。
"""
import sys
import os
import csv
import json
from collections import Counter, defaultdict


def load_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def cn_type(t):
    """设备类型英文->中文（简单映射，可扩展）"""
    m = {
        "machine_tool": "机床", "injection_molding": "注塑机", "robot_welder": "焊接机器人",
        "conveyor": "输送带", "compressor": "空压机", "agv": "AGV小车",
    }
    return m.get(t, t)


def main():
    if len(sys.argv) < 2:
        print("用法: python aggregate.py <equipment.csv> [line.csv]")
        sys.exit(1)
    eq_path = sys.argv[1]
    line_path = sys.argv[2] if len(sys.argv) > 2 else None

    eqs = load_csv(eq_path)
    total = len(eqs)

    # 设备类型分布（中文）
    type_cnt = Counter()
    for e in eqs:
        t = e.get("device_type", "") or e.get("Type", "?")
        type_cnt[cn_type(t)] += 1
    device_type_dist = [{"type": t, "count": c} for t, c in type_cnt.most_common()]

    # 状态分布
    status_cnt = Counter((e.get("status", "?").strip() or "?") for e in eqs)
    status_dist = [{"status": s, "count": c} for s, c in status_cnt.most_common()]

    # 区域分布
    zone_cnt = Counter()
    for e in eqs:
        z = e.get("location", "") or e.get("zone", "")
        if z:
            zone_cnt[z.split("-")[0]] += 1
    zone_dist = [{"zone": z, "count": c} for z, c in zone_cnt.most_common()]

    # 故障率: alarm + maintenance / total
    fault = sum(1 for e in eqs if (e.get("status", "") or "").strip() in ("alarm", "maintenance", "offline", "故障"))
    fault_rate = round(fault / total, 4) if total else 0

    # 产线 join 设备
    line_stats = []
    if line_path and os.path.exists(line_path):
        lines = load_csv(line_path)
        # 按 line_id 分组设备
        by_line = defaultdict(list)
        for e in eqs:
            lid = e.get("line_id", "") or e.get("LineId", "")
            if lid:
                by_line[lid].append(e)
        for ln in lines:
            lid = ln.get("line_id", "")
            devs = by_line.get(lid, [])
            running = sum(1 for d in devs if (d.get("status", "") or "").strip() == "running")
            alarm = sum(1 for d in devs if (d.get("status", "") or "").strip() in ("alarm", "maintenance", "offline"))
            total_power = round(sum(float(d.get("power_kw", 0) or 0) for d in devs), 1)
            line_stats.append({
                "line": lid, "name": ln.get("line_name", ""), "area": ln.get("area", ""),
                "supervisor": ln.get("supervisor", ""), "device_count": len(devs),
                "running": running, "alarm": alarm, "total_power_kw": total_power,
            })

    result = {
        "total_devices": total,
        "device_type_dist": device_type_dist,
        "status_dist": status_dist,
        "zone_dist": zone_dist,
        "line_stats": line_stats,
        "fault_rate": fault_rate,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
