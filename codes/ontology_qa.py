# 【已弃用 DEPRECATED】旧版问答引擎。请用 ontology_qa_v3.py（词典驱动的通用方法论核心）。本文件保留仅供历史引用/回退。
#!/usr/bin/env python3
"""ontology_qa.py — 基于本体的中文问答（零依赖，纯标准库）。

由 团队 Agent 首次生成（51分，review 发现5个问题），团队 Agent 按 review 反馈修复落盘。
修复点：①语法错误 ②Turtle解析健壮性 ③去掉错误的字符串替换链改用意图识别 ④group计数用Counter。

用法: python ontology_qa.py <ttl文件> "<问题>"
"""

import sys
import os
import re
from collections import Counter


# ---------------------------------------------------------------- 本体解析

def parse_turtle(ttl_file):
    """零依赖解析 N-Triples 格式（每行一个三元组），提取 (s, p, o) 列表。
    格式: <uri> <uri> "value"^^<type> . 或 <uri> <uri> <uri> ."""
    triples = []
    with open(ttl_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("@"):
                continue
            # 提取三个 token，跳过末尾的 " ."
            m = re.match(r'^<([^>]*)>\s+<([^>]*)>\s+(.+?)\s*\.\s*$', line)
            if not m:
                continue
            s, p = m.group(1), m.group(2)
            o_raw = m.group(3).strip()
            # 对象可能是 <uri> 或 "literal"^^<type>
            om = re.match(r'^<([^>]*)>$', o_raw)
            if om:
                o = om.group(1)
            else:
                lm = re.match(r'^"((?:[^"\\]|\\.)*)"(?:\^\^<([^>]*)>)?', o_raw)
                o = lm.group(1) if lm else o_raw
            triples.append((s, p, o))
    return triples


def build_data(triples, cls_name=None):
    """三元组 -> {实例名: {属性名: 值}}，忽略类/属性声明行。
    若未指定 cls_name，自动发现本体中的第一个 owl#Class（数据驱动）。"""
    NS = "http://factory.example/ontology#"
    OWL_CLASS = "http://www.w3.org/2002/07/owl#Class"
    def tail(uri):
        return uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]

    # 自动发现类名：找类型为 owl#Class 的 subject
    if cls_name is None:
        for s, p, o in triples:
            if tail(p) == "type" and tail(o) == "Class":
                cls_name = tail(s)
                break
    if cls_name is None:
        return {}

    data = {}
    individuals = set()
    for s, p, o in triples:
        if tail(p) == "type" and tail(o) == cls_name:
            individuals.add(s)
    for inst in individuals:
        data[tail(inst)] = {}
    for s, p, o in triples:
        sn = tail(s)
        if sn in data and tail(p) != "type":
            data[sn][tail(p)] = o
    return data


# ---------------------------------------------------------------- 关键词映射
# 词典外置：默认内嵌，可用 load_lexicon(path) 从 JSON 覆盖（泛化不同数据源）

STATUS_CN2EN = {
    "运行中": "running", "运行": "running", "正常": "running",
    "空闲": "idle", "闲置": "idle",
    "报警": "alarm", "故障": "alarm", "异常": "alarm",
    "维修": "maintenance", "保养": "maintenance",
    "离线": "offline", "停机": "offline",
}
TYPE_CN2EN = {
    "空压机": "air_compressor",
    "焊接机器人": "welding_robot", "机器人": "welding_robot",
    "注塑机": "injection_molding",
    "CNC": "cnc_machining", "铣床": "cnc_machining", "加工中心": "cnc_machining",
    "装配线": "assembly_line", "装配": "assembly_line",
    "视觉检测": "inspection_cam", "检测": "inspection_cam", "质检": "inspection_cam",
    "配电柜": "power_dist",
    "制冷机组": "cooling_unit", "制冷": "cooling_unit",
    "AGV": "agv", "叉车": "forklift", "电动叉车": "forklift",
    "输送线": "conveyor", "输送带": "conveyor",
}
ATTR_CN2EN = {
    "功率": "powerKw", "传感器": "sensorCount", "状态": "status",
    "位置": "location", "厂商": "manufacturer", "厂家": "manufacturer",
    "产线": "lineId", "安装日期": "installDate", "设备类型": "deviceType",
    "能耗": "energyKwhToday", "电耗": "energyKwhToday",
    "良品率": "yieldRatePct", "合格率": "yieldRatePct", "良率": "yieldRatePct",
    "运行小时": "runHours", "运行时长": "runHours",
    "负责人": "owner", "区域": "zone", "车间": "zone",
    "下次维护": "nextMaintenance", "维护周期": "maintenanceCycleDays",
    "优先级": "priority",
}
ATTR_EN2CN = {
    "powerKw": "功率", "sensorCount": "传感器数", "status": "状态",
    "location": "位置", "manufacturer": "厂商", "lineId": "产线",
    "installDate": "安装日期", "deviceType": "设备类型",
    "energyKwhToday": "今日能耗", "yieldRatePct": "良品率",
    "runHours": "运行小时", "owner": "负责人", "zone": "区域",
    "nextMaintenance": "下次维护", "maintenanceCycleDays": "维护周期",
    "priority": "优先级",
}

# 标准字段别名：让引擎跨数据源识别状态/类型/名称字段（泛化关键）
FIELD_ALIASES = {
    "status": ["status", "state", "condition", "运行状态"],
    "deviceType": ["deviceType", "device_type", "category", "type", "class"],
    "deviceName": ["deviceName", "device_name", "item_name", "name", "设备名称"],
    "location": ["location", "zone", "area", "region", "车间", "区域"],
}
ZONE_CN2EN = {
    "机加": "机加", "加工": "机加", "注塑": "注塑",
    "焊接": "焊接", "装配": "装配", "质检": "质检", "质量": "质检",
    "动力": "动力", "物流": "物流", "仓储": "物流",
}


def load_lexicon(path):
    """从 JSON 文件加载词典，覆盖全局。返回是否成功。"""
    import json as _json
    try:
        with open(path, encoding="utf-8") as f:
            lex = _json.load(f)
    except Exception as e:
        print(f"[词典] 加载失败 {path}: {e}")
        return False
    global STATUS_CN2EN, TYPE_CN2EN, ATTR_CN2EN, ATTR_EN2CN, FIELD_ALIASES, ZONE_CN2EN
    if lex.get("status_cn2en"):
        STATUS_CN2EN = lex["status_cn2en"]
    if lex.get("type_cn2en"):
        TYPE_CN2EN = lex["type_cn2en"]
    if lex.get("attr_cn2en"):
        ATTR_CN2EN = lex["attr_cn2en"]
    if lex.get("attr_en2cn"):
        ATTR_EN2CN = lex["attr_en2cn"]
    if lex.get("field_aliases"):
        FIELD_ALIASES = lex["field_aliases"]
    if lex.get("zone_cn2en"):
        ZONE_CN2EN = lex["zone_cn2en"]
    print(f"[词典] 已加载: {path}")
    return True


def _field(data, canonical):
    """从记录的字段中按别名取标准字段的值。data 是单条记录 dict。"""
    for alias in FIELD_ALIASES.get(canonical, [canonical]):
        if alias in data:
            return data[alias]
    return data.get(canonical, "")


def _display_name(data, default=""):
    """取设备/物品名称（优先 deviceName 别名，其次 id）。"""
    n = _field(data, "deviceName")
    return n if n else (data.get("deviceId") or default)


DEFAULT_LEXICON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "config", "lexicon.json")
# 若存在默认词典配置则加载
if os.path.exists(DEFAULT_LEXICON):
    load_lexicon(DEFAULT_LEXICON)


# ---------------------------------------------------------------- 问答引擎

def resolve_cn(q, mapping):
    """在问题中找中文关键词，返回 (属性/值英文, 替换后是否命中)。"""
    for cn, en in mapping.items():
        if cn in q:
            return en, True
    return None, False


def _to_num(v):
    """尝试转 float，失败返回 None。"""
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _num_field(data, canonical):
    """取字段并转数值，非数值返回 None。"""
    return _to_num(_field(data, canonical))


def _find_attr_en(question):
    """在问题中找出现的属性英文名。按中文词长度降序匹配，避免短词短路长词(如"运行"vs"运行小时")。"""
    for cn, en in sorted(ATTR_CN2EN.items(), key=lambda x: len(x[0]), reverse=True):
        if cn in question:
            return en
    return None


def _extract_number(q):
    """从问题提取数字，支持中文数字/阿拉伯数字。返回 float 或 None。"""
    m = re.search(r'(\d+(?:\.\d+)?)', q)
    if m:
        return float(m.group(1))
    cn_map = {"一":1,"二":2,"三":3,"四":4,"五":5,"十":10,"百":100}
    for cn, n in cn_map.items():
        if cn in q:
            return float(n)
    return None


def _list_names(matched, limit=20):
    """格式化设备名列表。matched 是 [(name, data)]，limit 截断。"""
    names = [m[0] for m in matched]
    shown = names[:limit]
    lines = "\n".join("  - " + x for x in shown)
    if len(names) > limit:
        lines += f"\n  ... 共 {len(names)} 台"
    return lines


def answer(question, data):
    """基于意图识别回答中文问题。"""
    q = question

    # G0. 区域关系查询 (优先, 避免"空压机区域"被类型拦截)
    zone_en, zone_hit = resolve_cn(q, ZONE_CN2EN)
    if zone_hit and any(k in q for k in ("区域的设备", "区域有", "区域的", "设备在")):
        matched = [(_display_name(d), d) for d in data.values() if _field(d, "zone") == zone_en]
        if "多少" in q:
            return "%s区域有 %d 台设备" % (zone_en, len(matched))
        return "%s区域的设备(%d台):\n%s" % (zone_en, len(matched), _list_names(matched))

    # 1. 统计按属性分组 (统计按[属性]分组 / 按[属性]统计)
    for cn, en in ATTR_CN2EN.items():
        if f"统计" in q and f"按{cn}" in q or (f"按{cn}" in q and "分组" in q):
            counter = Counter(d.get(en, "未知") for d in data.values())
            return "按%s统计:\n" % cn + "\n".join(f"  {k}: {v}台" for k, v in counter.items())

    # 2. 属性最大的设备
    for cn, en in ATTR_CN2EN.items():
        if cn in q and "最大" in q:
            items = [(d.get(en, ""), name) for name, d in data.items() if d.get(en)]
            if not items:
                return "暂不支持该问题"
            # 数值比较
            def num(x):
                try:
                    return float(x)
                except ValueError:
                    return 0.0
            val, name = max(items, key=lambda x: num(x[0]))
            return "功率最大的设备: %s (%.1f kW)" % (name, num(val)) if en == "powerKw" else \
                   "最大的设备: %s (%s=%s)" % (name, cn, val)

    # 2.5 组合查询: 状态+类型 (如"报警的焊接机器人" "运行中的空压机")
    st_en, st_hit = resolve_cn(q, STATUS_CN2EN)
    ty_en, ty_hit = resolve_cn(q, TYPE_CN2EN)
    if st_hit and ty_hit:
        cn_st = [k for k in STATUS_CN2EN if STATUS_CN2EN[k] == st_en][0]
        matched = [(_display_name(d, n), d) for n, d in data.items()
                   if _field(d, "status") == st_en and _field(d, "deviceType") == ty_en]
        names = [m[0] for m in matched]
        if "多少台" in q or "多少" in q:
            return "有 %d 台%s的%s" % (len(names), cn_st, ty_en)
        if "列出" in q:
            return "列出所有%s的%<仓库内路径>" % (cn_st, ty_en, "\n".join("  - " + x for x in names)) if names else "无%s的%s" % (cn_st, ty_en)
        return "%s的%s共 %d 台" % (cn_st, ty_en, len(names))

    # 3. 有多少台[状态]设备
    en, hit = resolve_cn(q, STATUS_CN2EN)
    if hit and "多少台" in q:
        n = sum(1 for d in data.values() if _field(d, "status") == en)
        return "有 %d 台%s的设备" % (n, [k for k in STATUS_CN2EN if STATUS_CN2EN[k] == en][0])

    # 3.5 有多少台[类型]设备 (如"有多少台伺服电机")
    en, hit = resolve_cn(q, TYPE_CN2EN)
    if hit and ("多少台" in q or "多少" in q):
        n = sum(1 for d in data.values() if _field(d, "deviceType") == en)
        cn = [k for k in TYPE_CN2EN if TYPE_CN2EN[k] == en][0]
        return "有 %d 台%s" % (n, cn)

    # 4. 列出所有[类型]设备
    en, hit = resolve_cn(q, TYPE_CN2EN)
    if hit and "列出" in q:
        names = [_display_name(d, n) for n, d in data.items() if _field(d, "deviceType") == en]
        return "列出所有%<仓库内路径>" % (en, "\n".join("  - " + x for x in names)) if names else "无此类设备"

    # 5. 列出所有[状态]设备
    en, hit = resolve_cn(q, STATUS_CN2EN)
    if hit and "列出" in q:
        names = [_display_name(d, n) for n, d in data.items() if _field(d, "status") == en]
        cn = [k for k in STATUS_CN2EN if STATUS_CN2EN[k] == en][0]
        return "列出所有%s设备:\n%s" % (cn, "\n".join("  - " + x for x in names)) if names else "无%s设备" % cn

    # 6. 按设备名/设备编号查属性
    for name, d in data.items():
        # 匹配设备名或设备编号 (DEV-xxx)
        if name in q or d.get("deviceName", "") in q or d.get("deviceId", "") in q:
            parts = []
            for cn, en in ATTR_CN2EN.items():
                if cn in q and en in d:
                    parts.append("%s=%s" % (cn, d[en]))
            if parts:
                display = d.get("deviceName", name)
                return "%s: %s" % (display, ", ".join(parts))

    # ---- D. 极值/排序 ----
    attr_en = _find_attr_en(q)
    # D2. Top N (如"能耗最高的3台") — 优先于单极值
    if attr_en and ("最高" in q or "最大" in q or "最低" in q or "最小" in q) and re.search(r'\d+\s*台', q):
        n = _extract_number(q) or 3
        items = [(d, _num_field(d, attr_en)) for d in data.values()]
        items = [(d, v) for d, v in items if v is not None]
        is_max = any(k in q for k in ("最高", "最大"))
        items.sort(key=lambda x: x[1], reverse=is_max)
        cn_attr = ATTR_EN2CN.get(attr_en, attr_en)
        top = [("  - %s (%s=%s)" % (_display_name(d), cn_attr, v)) for d, v in items[:int(n)]]
        return "%s%s的%d台设备:\n%s" % ("最高" if is_max else "最低", cn_attr, int(n), "\n".join(top))

    # D1. 单极值 (功率最大/最小/最高/最低)
    if attr_en and ("最大" in q or "最高" in q or "最多" in q or "最低" in q or "最小" in q or "最少" in q):
        items = [(d, _num_field(d, attr_en)) for d in data.values()]
        items = [(d, v) for d, v in items if v is not None]
        if items:
            is_max = any(k in q for k in ("最大", "最高", "最多"))
            best = max(items, key=lambda x: x[1]) if is_max else min(items, key=lambda x: x[1])
            d, v = best
            cn_attr = ATTR_EN2CN.get(attr_en, attr_en)
            return "%s%s的设备: %s (%s=%s)" % ("最" if is_max else "最", cn_attr, _display_name(d), cn_attr, v)

    # ---- E. 统计/聚合 ----
    if attr_en and ("平均" in q or "均值" in q):
        vals = [_num_field(d, attr_en) for d in data.values()]
        vals = [v for v in vals if v is not None]
        if vals:
            cn_attr = ATTR_EN2CN.get(attr_en, attr_en)
            avg = sum(vals) / len(vals)
            return "%s的平均值是 %.2f (%d台)" % (cn_attr, avg, len(vals))
    if attr_en and ("总" in q or "合计" in q or "总和" in q):
        vals = [_num_field(d, attr_en) for d in data.values()]
        vals = [v for v in vals if v is not None]
        if vals:
            cn_attr = ATTR_EN2CN.get(attr_en, attr_en)
            return "%s总和是 %.2f" % (cn_attr, sum(vals))

    # E4. 多级分组 (按X和Y统计)
    multi = [cn for cn, en in ATTR_CN2EN.items() if cn in q and f"按{cn}" in q or (cn in q and "按" in q)]
    # 已有单级分组在规则1，这里处理"按X和Y"
    if "和" in q and "按" in q:
        parts = [p.strip() for p in q.split("和") if p.strip()]
        grp_attrs = [ATTR_CN2EN.get(p.replace("按", ""), None) for p in parts if ATTR_CN2EN.get(p.replace("按", ""))]
        if len(grp_attrs) >= 2:
            from collections import defaultdict
            c = defaultdict(int)
            for d in data.values():
                key = tuple(_field(d, a) or "?" for a in grp_attrs[:2])
                c[key] += 1
            cn_a1, cn_a2 = ATTR_EN2CN.get(grp_attrs[0], grp_attrs[0]), ATTR_EN2CN.get(grp_attrs[1], grp_attrs[1])
            return "按%s和%s统计:\n%s" % (cn_a1, cn_a2, "\n".join(f"  {k[0]}/{k[1]}: {v}台" for k, v in c.items()))

    # ---- F. 数值范围 ----
    if attr_en:
        nums = re.findall(r'(\d+(?:\.\d+)?)', q)
        has_gt = any(k in q for k in ("大于", "高于", "超过", "以上"))
        has_lt = any(k in q for k in ("小于", "低于", "少于", "以下"))
        has_between = "到" in q and len(nums) >= 2 or "之间" in q
        if has_between and len(nums) >= 2:
            lo, hi = float(nums[0]), float(nums[1])
            matched = [(_display_name(d), _num_field(d, attr_en)) for d in data.values()]
            matched = [(nm, v) for nm, v in matched if v is not None and lo <= v <= hi]
            cn_attr = ATTR_EN2CN.get(attr_en, attr_en)
            return "%s在%.0f到%.0f之间的设备(%d台):\n%s" % (cn_attr, lo, hi, len(matched), _list_names([(nm, {}) for nm, _ in matched]))
        elif has_gt and nums:
            n = float(nums[0])
            matched = [(_display_name(d), _num_field(d, attr_en)) for d in data.values()]
            matched = [(n2, v) for n2, v in matched if v is not None and v > n]
            cn_attr = ATTR_EN2CN.get(attr_en, attr_en)
            return "%s大于%.0f的设备(%d台):\n%s" % (cn_attr, n, len(matched), _list_names([(n2, {}) for n2, _ in matched]))
        elif has_lt and nums:
            n = float(nums[0])
            matched = [(_display_name(d), _num_field(d, attr_en)) for d in data.values()]
            matched = [(n2, v) for n2, v in matched if v is not None and v < n]
            cn_attr = ATTR_EN2CN.get(attr_en, attr_en)
            return "%s小于%.0f的设备(%d台):\n%s" % (cn_attr, n, len(matched), _list_names([(n2, {}) for n2, _ in matched]))

    # ---- G. 关系/跨实体 ----
    zone_en, zone_hit = resolve_cn(q, ZONE_CN2EN)
    if zone_hit and ("区域的设备" in q or "的设备" in q or "列出" in q):
        matched = [(_display_name(d), d) for d in data.values() if _field(d, "zone") == zone_en]
        if "多少" in q:
            return "%s区域有 %d 台设备" % (zone_en, len(matched))
        return "%s区域的设备(%d台):\n%s" % (zone_en, len(matched), _list_names(matched))

    # C3. 反查: 某属性的值 -> 设备 (如"哪些设备在车间A")
    for cn, en in ATTR_CN2EN.items():
        if cn in q and ("哪些" in q or "是什么" in q):
            matched = [(d, _field(d, en)) for d in data.values() if _field(d, en)]
            cn_attr = ATTR_EN2CN.get(en, cn)
            return "%s信息:\n%s" % (cn_attr, "\n".join(f"  {_display_name(d)}: {v}" for d, v in matched[:20]))

    return "暂不支持该问题"


def main():
    if len(sys.argv) != 3:
        print("用法: python ontology_qa.py <ttl文件> '<问题>'")
        sys.exit(1)
    ttl_file, question = sys.argv[1], sys.argv[2]
    triples = parse_turtle(ttl_file)
    data = build_data(triples)
    if not data:
        print("本体解析失败或无实例")
        sys.exit(1)
    print(answer(question, data))


if __name__ == "__main__":
    main()
