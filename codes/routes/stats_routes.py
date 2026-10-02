# -*- coding: utf-8 -*-
"""stats 路由族 — 从 api_server.py 抽出的 APIRouter（代码审查 P1-1）。

函数体逐字搬运；对 api_server 模块级名字改写成 ``core.X``（6 个），
fastapi/pydantic 导入名在本模块显式重新导入；路由路径/鉴权依赖/返回结构不变。
回归门：codes/tests 单测 + codes/e2e_test.py + 路由清单逐条比对。
"""

from fastapi import Depends, Query

from fastapi import APIRouter

import api_server as core

router = APIRouter()


@router.get("/api/stats", dependencies=[Depends(core.require_key)])
def stats(kb: str = Query("", description="知识库名")):
    """知识库统计（按 kb 隔离，不串台）。"""
    if not kb:
        kb = core.KBS.get("_default", "") or "food"
    ctx = core._get_kb_ctx(kb)
    if not ctx:
        return {"ok": False, "error": f"kb '{kb}' 未建模或加载失败"}
    g = ctx["graph"]
    # 用 QDATA(实例字典)统计实例：key 形如 <Entity>_<field>_<ID>
    qd = ctx.get("QDATA") or {}
    inst_count = {}
    for k in qd:
        local = str(k).split("/")[-1]
        m = core.re.match(r"^([A-Za-z_]+?)_[A-Za-z0-9_]+$", local)
        if m:
            cls = m.group(1)
            inst_count[cls] = inst_count.get(cls, 0) + 1
    # ── 看板聚合(前端 DashboardPanel 契约): 设备类型/状态分布 + 产线(车间)统计 ──
    # 各行业设备表名不同(equipment / valve_equipment / ...)，由词典 entity_cn2en['设备'] 解析；
    # 无设备表的 kb(如纯产品库)返回空数组 → 前端显示空态而非报错。
    core.D = ctx.get("core.D") or {}
    aliases = core.D.get("field_aliases", {}) or {}
    dev_table = str((core.D.get("entity_cn2en", {}) or {}).get("设备", "") or "").strip()

    def _field(rec, en):
        for a in ([en] + list(aliases.get(en, []) or [])):
            v = rec.get(a)
            if v not in (None, ""):
                return str(v).strip()
        return ""

    def _num(rec, en):
        try:
            return float(_field(rec, en) or 0)
        except (TypeError, ValueError):
            return 0.0

    RUNNING = {"running", "run", "normal", "working", "active", "online",
               "运行中", "运行", "正常", "工作中", "在线", "生产中"}
    FAULT = {"alarm", "maintenance", "offline", "fault", "fail", "failed", "error",
             "报警", "维护", "离线", "故障", "停机", "异常", "检修"}
    # 用"包含"而非精确相等: 数据里是"维护中/运行中"这类带后缀的词, 精确匹配会漏。
    def _hit(val, words):
        t = (val or "").strip().lower()
        return any(w in t for w in words) if t else False
    devs = []
    if dev_table:
        pre = dev_table.lower() + "_"
        for k, rec in qd.items():
            local = str(k).split("/")[-1].lower()
            # 只取该表的一级实例(key 形如 <ent>_<table>_<id>，排除 _<n> 的关联实例)
            if local.startswith(pre) and len(local.split("_")) == len(dev_table.split("_")) + 1:
                if isinstance(rec, dict):
                    devs.append(rec)
    type_cnt, status_cnt, line_map = {}, {}, {}
    for rec in devs:
        t = _field(rec, "deviceType")
        if t:
            type_cnt[t] = type_cnt.get(t, 0) + 1
        s = _field(rec, "status")
        if s:
            status_cnt[s] = status_cnt.get(s, 0) + 1
        ln = _field(rec, "workshop") or _field(rec, "location") or _field(rec, "zone") or "未分组"
        e = line_map.setdefault(ln, {"device_count": 0, "running": 0, "alarm": 0, "total_power_kw": 0.0})
        e["device_count"] += 1
        if _hit(s, RUNNING):
            e["running"] += 1
        if _hit(s, FAULT):
            e["alarm"] += 1
        e["total_power_kw"] += _num(rec, "powerKw")
    line_stats = [{"line": ln, "name": ln, "area": ln, "supervisor": "",
                   "device_count": v["device_count"], "running": v["running"],
                   "alarm": v["alarm"], "total_power_kw": round(v["total_power_kw"], 2)}
                  for ln, v in sorted(line_map.items(), key=lambda x: -x[1]["device_count"])]
    fault_cnt = sum(v["alarm"] for v in line_map.values())
    total_dev = len(devs)
    return {
        "ok": True,
        "entities": inst_count, "entity_count": sum(inst_count.values()),
        "nodes": len(g), "edges": sum(len(v) for v in g.values()),
        "stats": {
            "total_devices": total_dev,
            "device_type_dist": [{"type": t, "count": c}
                                 for t, c in sorted(type_cnt.items(), key=lambda x: -x[1])],
            "status_dist": [{"status": s, "count": c}
                            for s, c in sorted(status_cnt.items(), key=lambda x: -x[1])],
            "line_stats": line_stats,
            "fault_rate": round(fault_cnt / total_dev, 4) if total_dev else 0.0,
        },
    }
