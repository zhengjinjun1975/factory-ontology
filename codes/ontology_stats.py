#!/usr/bin/env python3
"""ontology_stats.py — 从本体 .nt 文件聚合设备统计，供 Web 看板可视化。

替换已被删除的 aggregate.py（精炼化时移除），无第三方依赖。
复用 ontology_qa_v3.parse_nt 解析 N-Triples，聚合设备分布。

用法: python ontology_stats.py <nt文件>
输出: JSON {"total_devices", "device_type_dist", "status_dist", "line_stats", "fault_rate"}

字段兼容: 属性名可能是驼峰(deviceType/status)或下划线(device_type)，
          且不同数据集的字段名各异(Type/MachineFailure/workshop/...)，按别名表匹配。
"""
import sys
import json
from collections import Counter
from ontology_qa_v3 import parse_nt


# ------------------------------------------------------------------ 字段别名
# canonical 标准字段 -> 候选属性名(局部名)。覆盖常见命名与 ai4i/valve/energy 等示例
FIELD_ALIASES = {
    "deviceType": ["Type", "deviceType", "device_type", "类型", "设备类型"],
    "status": ["status", "Status", "MachineFailure", "machine_failure", "state", "State", "状态"],
    "location": ["workshop", "location", "line", "line_id", "lineId", "车间", "区域", "zone"],
}

# 故障状态词(英文/中文)。fault_rate = 命中这些状态占比
FAULT_STATUS = {
    "alarm", "maintenance", "offline", "fault", "fail", "failed", "error",
    "报警", "维护", "离线", "故障", "停机", "异常", "1",
}


def _local(uri):
    """URI 局部名(尾部)。"""
    return uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]


def _is_device_class(name):
    """设备类：实体类型名含 equipment/设备，或表名风格(Valve_equipment 等)。"""
    n = name.lower()
    return "equipment" in n or "设备" in n


def _field(rec, canonical):
    """按别名取标准字段值(含驼峰/下划线兼容)。"""
    for alias in FIELD_ALIASES.get(canonical, [canonical]):
        if alias in rec:
            return rec[alias]
    # 驼峰兜底: device_type -> deviceType, Machine_failure -> MachineFailure
    for alias in FIELD_ALIASES.get(canonical, [canonical]):
        parts = [p for p in alias.replace("-", "_").split("_") if p]
        camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
        if camel in rec:
            return rec[camel]
    return ""


def compute_stats(nt_file):
    """解析 .nt 并计算设备统计。返回 dict；无设备数据时返回 None。"""
    try:
        triples = parse_nt(nt_file)
    except (OSError, ValueError):
        return None
    if not triples:
        return None

    # 收集所有类(owl:Class 声明)
    classes = set()
    for s, p, o in triples:
        if _local(p) == "type" and _local(o) == "Class":
            classes.add(_local(s))
    if not classes:
        return None

    # 实例按类分组
    inst_by_cls = {}
    for s, p, o in triples:
        if _local(p) == "type" and _local(o) in classes:
            inst_by_cls.setdefault(_local(o), []).append(s)

    # 选设备类: 优先名字含 equipment/设备；否则取实例最多的类(主表)
    device_cls = next((c for c in classes if _is_device_class(c)), None)
    if not device_cls and inst_by_cls:
        device_cls = max(inst_by_cls, key=lambda c: len(inst_by_cls[c]))
    if not device_cls:
        return None

    dev_ids = inst_by_cls.get(device_cls, [])
    if not dev_ids:
        return None
    dev_set = set(dev_ids)

    # 提取设备实例属性(排除 rdf:type 本身)
    recs = {}
    for s, p, o in triples:
        if s in dev_set and _local(p) != "type":
            recs.setdefault(s, {})[_local(p)] = o

    # 聚合
    types = Counter()
    statuses = Counter()
    lines = Counter()
    for r in recs.values():
        t = _field(r, "deviceType")
        if t:
            types[t] += 1
        st = _field(r, "status")
        if st:
            statuses[st] += 1
        loc = _field(r, "location")
        if loc:
            lines[loc] += 1

    total = len(recs)
    fault = sum(statuses.get(k, 0) for k in statuses if k in FAULT_STATUS)

    return {
        "total_devices": total,
        "device_type_dist": [{"type": k, "count": v} for k, v in types.items()],
        "status_dist": [{"status": k, "count": v} for k, v in statuses.items()],
        "line_stats": [{"line": k, "device_count": v} for k, v in lines.items()],
        "fault_rate": round(fault / total, 4) if total else 0.0,
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"total_devices": 0, "device_type_dist": [],
                          "status_dist": [], "line_stats": [], "fault_rate": 0.0},
                         ensure_ascii=False))
        return
    stats = compute_stats(sys.argv[1])
    if stats is None:
        stats = {"total_devices": 0, "device_type_dist": [],
                 "status_dist": [], "line_stats": [], "fault_rate": 0.0}
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
