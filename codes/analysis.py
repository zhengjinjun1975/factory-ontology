#!/usr/bin/env python3
"""analysis.py — 智能分析引擎：统计摘要 + LLM 洞察。

针对分析类问题（分析/比较/关注/趋势/整体/状况/产能/健康等），
先算出结构化统计（复用 aggregate 逻辑），再让 LLM 基于统计做**分析洞察**，
而不是让 LLM 从原始数据里硬数。

用法: python analysis.py <equipment.csv> <line.csv> "<问题>"
输出: LLM 生成的结构化分析报告
"""
import sys
import os
import csv
import json
import re
from collections import Counter, defaultdict

# 统一模型调用（从 model_config.json 读取，可切 ornith/deepseek）
from model_llm import llm_generate


def load_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def cn_type(t):
    m = {"machine_tool": "机床", "injection_molding": "注塑机", "robot_welder": "焊接机器人",
         "conveyor": "输送带", "compressor": "空压机", "agv": "AGV小车"}
    return m.get(t, t)


def compute_stats(eqs, lines):
    """计算结构化统计（与 aggregate.py 一致，供分析）。"""
    total = len(eqs)
    type_cnt = Counter(cn_type(e.get("device_type", "") or e.get("Type", "?")) for e in eqs)
    status_cnt = Counter((e.get("status", "?").strip() or "?") for e in eqs)
    zone_cnt = Counter()
    for e in eqs:
        z = e.get("location", "") or e.get("zone", "")
        if z:
            zone_cnt[z.split("-")[0]] += 1
    fault = sum(1 for e in eqs if (e.get("status", "") or "").strip() in ("alarm", "maintenance", "offline", "故障"))
    fault_rate = round(fault / total, 3) if total else 0

    # 产线统计（含负责人/区域）
    line_stats = []
    if lines:
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
            line_stats.append({"line": lid, "name": ln.get("line_name", ""), "area": ln.get("area", ""),
                               "supervisor": ln.get("supervisor", ""), "device_count": len(devs),
                               "running": running, "alarm": alarm, "total_power_kw": total_power})
    return {
        "total_devices": total,
        "device_type_dist": dict(type_cnt.most_common()),
        "status_dist": dict(status_cnt.most_common()),
        "zone_dist": dict(zone_cnt.most_common()),
        "line_stats": line_stats,
        "fault_rate": fault_rate,
    }


def compute_trends(obss):
    """计算时序观测趋势：每个 设备+指标 的 首值/末值/变化率/趋势方向。"""
    # 按 (sensor, metric) 分组
    series = defaultdict(list)
    for ob in obss:
        sid = ob.get("sensor_id", "").strip()
        metric = ob.get("metric", "").strip()
        try:
            val = float(ob.get("value", ""))
        except ValueError:
            continue
        series[(sid, metric)].append(val)
    trends = []
    for (sid, metric), vals in sorted(series.items()):
        if len(vals) < 2:
            continue
        first, last = vals[0], vals[-1]
        change = round(last - first, 2)
        pct = round((change / first) * 100, 1) if first else 0
        # 趋势方向
        if pct > 5:
            direction = "上升"
        elif pct < -5:
            direction = "下降"
        else:
            direction = "平稳"
        trends.append({
            "sensor": sid, "metric": metric,
            "start": first, "end": last, "change": change,
            "change_pct": pct, "direction": direction,
        })
    return trends


def analyze(question, eqs, lines, model_key=None):
    """基于统计做分析洞察。返回 (report_text, stats)。model_key 可选 local/cloud。
    包含时序观测趋势分析（若存在 observation.csv）。"""
    stats = compute_stats(eqs, lines)
    # 时序观测趋势
    obs_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "observation.csv")
    if os.path.exists(obs_path):
        obss = load_csv(obs_path)
        if obss:
            stats["observations"] = compute_trends(obss)
    ctx = json.dumps(stats, ensure_ascii=False, indent=1)
    prompt = (
        "你是工业数据分析师。下面是一份设备数据的统计摘要（JSON），含设备/产线/传感器/维护分布，以及时序观测趋势（observations）。\n"
        f"【统计】\n{ctx}\n\n"
        f"用户问题：{question}\n\n"
        "请基于这些统计做**系统性分析**，输出结构化中文报告，包含：\n"
        "1. 核心结论（一段话概括整体状况）\n"
        "2. 关键发现（分点，引用具体数字：分布/占比/异常/故障率/产线对比）\n"
        "3. 风险点（异常设备/高故障区域/需关注项）\n"
        "4. 时序趋势（若有观测数据：哪些指标在上升/下降，是否异常）\n"
        "5. 建议（可操作的改进方向）\n"
        "只依据给出的统计，不编造数字。用 markdown 加粗标题。"
    )
    ans = llm_generate(prompt, temperature=0.3, max_tokens=900, model_key=model_key)
    if not ans or ans.startswith("[模型"):
        return ans or "[分析失败] 模型未生成内容", stats
    return ans, stats


def main():
    if len(sys.argv) < 4:
        print("用法: python analysis.py <equipment.csv> <line.csv> '<问题>'")
        sys.exit(1)
    eq_path, line_path, question = sys.argv[1], sys.argv[2], sys.argv[3]
    eqs = load_csv(eq_path)
    lines = load_csv(line_path) if os.path.exists(line_path) else []
    report, stats = analyze(question, eqs, lines)
    # 输出结构化 JSON：report 文本 + stats 统计（供前端画图）
    print(json.dumps({"report": report, "stats": stats}, ensure_ascii=False))


if __name__ == "__main__":
    main()
