#!/usr/bin/env python3
"""ontology_qa_v3.py — 词典驱动的通用问答引擎（精简方法论核心）。

设计目标：换任何数据源/工厂，只换词典，引擎代码不动。
原则：模板逻辑通用，不硬编码任何具体中文词；属性名、枚举值全部从词典读。

词典 (lexicon.json) 提供：
  attr_cn2en   中文属性名 -> 字段英文 (如 功率->powerKw)
  attr_en2cn   字段英文 -> 中文属性名 (显示用)
  status_cn2en 状态词 -> 值
  type_cn2en   类型词 -> 值
  zone_cn2en   区域词 -> 值 (可选)
  field_aliases 标准字段别名 (status/deviceType/deviceName/location)

通用模板（与具体词无关）：
  数量: "多少[台/个/条][状态词/类型词/区域词]" 或 "[词]的[词]共几"
  列出: "列出所有[类型/状态/区域]"
  极值: "[属性]最[大/小/高/低]"
  TopN: "[属性]最[高/低]的N[台/个]"
  平均: "[属性]平均"
  总和: "[属性]总[和/计]"
  范围: "[属性][大于/小于/在A到B之间]"
  分组: "统计按[属性]" 或 "按[属性]和[属性]统计"
  反查: "哪些[设备/物品][属性]=值"
  组合: "[状态词]的[类型词]"

用法: python ontology_qa_v3.py <nt文件> "<问题>" [lexicon.json]
"""

import sys
import os
import re
import json
from collections import defaultdict


# ------------------------------------------------------------------ 词典加载

def load_dict(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ 解析(复用)

def parse_nt(nt_file):
    """解析 N-Triples。返回 (s,p,o) 列表。"""
    triples = []
    with open(nt_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("@"):
                continue
            m = re.match(r'^<([^>]*)>\s+<([^>]*)>\s+(.+?)\s*\.\s*$', line)
            if not m:
                continue
            s, p = m.group(1), m.group(2)
            o_raw = m.group(3).strip()
            om = re.match(r'^<([^>]*)>$', o_raw)
            if om:
                o = om.group(1)
            else:
                lm = re.match(r'^"((?:[^"\\]|\\.)*)"(?:\^\^<([^>]*)>)?', o_raw)
                o = lm.group(1) if lm else o_raw
            triples.append((s, p, o))
    return triples


def build_data(triples, dict_data):
    """三元组 -> {实例名: {属性: 值}}。字段名用尾部局部名。"""
    def tail(uri):
        return uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]

    # 自动发现类名
    cls_name = None
    for s, p, o in triples:
        if tail(p) == "type" and tail(o) == "Class":
            cls_name = tail(s)
            break
    if not cls_name:
        return {}
    individuals = set()
    for s, p, o in triples:
        if tail(p) == "type" and tail(o) == cls_name:
            individuals.add(s)
    data = {tail(i): {} for i in individuals}
    for s, p, o in triples:
        sn = tail(s)
        if sn in data and tail(p) != "type":
            data[sn][tail(p)] = o
    return data


# ------------------------------------------------------------------ 通用工具

def _field(rec, canonical, aliases):
    """按字段别名取标准字段值。canonical 如 status/deviceType/deviceName/location。
    别名找不到时，兜底匹配 csv_to_owl 驼峰化后的字段名(pump_status→pumpStatus)。"""
    for alias in aliases.get(canonical, [canonical]):
        if alias in rec:
            return rec[alias]
    # 驼峰兜底: 对每个别名尝试驼峰化
    for alias in aliases.get(canonical, [canonical]):
        parts = [p for p in alias.replace("-", "_").split("_") if p]
        camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
        if camel in rec:
            return rec[camel]
    return rec.get(canonical, "")


def _num(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _display_name(rec, aliases, default=""):
    """显示名：优先 deviceName；无名称字段时兜底 id/UDI/主键，最后用 key。"""
    n = _field(rec, "deviceName", aliases)
    if n:
        return n
    # 兜底: 尝试常见主键字段 (含驼峰)
    for key in ("id", "udi", "UDI", "device_id", "serial_no", "code", "name"):
        if key in rec:
            return rec[key]
    return default


def _find_attr(dict_data, q):
    """从词典找问题里出现的属性中文词 -> 字段英文。按长度降序避免短词短路。"""
    attr_cn2en = dict_data.get("attr_cn2en", {})
    for cn, en in sorted(attr_cn2en.items(), key=lambda x: len(x[0]), reverse=True):
        if cn in q:
            return en, cn
    return None, None


# 内置中文状态词 → 英文值兜底（当词典缺失中文映射时，覆盖高频运维词汇）
_COMMON_ZH_STATUS = {
    "运行中": "running", "运行": "running", "正常": "running", "工作中": "running",
    "停止": "stopped", "停机": "stopped", "空闲": "idle", "待机": "idle",
    "故障": "alarm", "报警": "alarm", "异常": "alarm",
    "维护": "maintenance", "保养": "maintenance", "维修": "maintenance",
    "离线": "offline",
}


def _find_enum(dict_data, q, which):
    """从词典找问题里出现的枚举词 -> 值。which in (status/type/zone)。
    增强：status 词在词典缺中文映射时，用内置中文→英文兜底。"""
    key = f"{which}_cn2en"
    for cn, en in sorted(dict_data.get(key, {}).items(), key=lambda x: len(x[0]), reverse=True):
        if cn in q:
            return en, cn
    # status 兜底：中文运维词 → 英文值（词典可能只有英文键，如 {running:running}）
    if which == "status":
        for cn, en in sorted(_COMMON_ZH_STATUS.items(), key=lambda x: len(x[0]), reverse=True):
            if cn in q:
                return en, cn
    return None, None


def _extract_nums(q):
    return [float(x) for x in re.findall(r'(\d+(?:\.\d+)?)', q)]


def _fmt_names(names, limit=20):
    shown = names[:limit]
    out = "\n".join("  - " + n for n in shown)
    if len(names) > limit:
        out += f"\n  ... 共 {len(names)} 条"
    return out


# ------------------------------------------------------------------ 问答主逻辑

_EXTREME = re.compile(r"最(大|高|多|低|小|少|长|短|贵|便宜|快|慢|久|重|轻|新|老|早|晚|近)")
_EXTREME_MAX = ("最大","最高","最多","最长","最贵","最快","最久","最重","最新","最老")
_EXTREME_MIN = ("最小","最低","最少","最短","最慢","最轻","最早","最便宜")


def _is_max(q):
    """问题是否求极值中的最大值。MIN 词(最便宜/最短/最慢等)优先按最小。"""
    if _is_min(q):
        return False
    for k in _EXTREME_MAX:
        if k in q:
            return True
    # 含"最"但未列出的, 按常识多数求最大
    return _EXTREME.search(q) is not None


def _is_min(q):
    return any(k in q for k in _EXTREME_MIN)


def answer(q, data, D):
    """词典 D 驱动的通用问答。"""
    aliases = D.get("field_aliases", {})
    cn2cn = D.get("attr_en2cn", {})

    # 显示名辅助
    def names(matched):
        return [_display_name(d, aliases, default=n) for n, d in matched]

    # ---- 组合: [状态]的[类型] (报警的焊接机器人) ----
    st_en, st_cn = _find_enum(D, q, "status")
    ty_en, ty_cn = _find_enum(D, q, "type")
    if st_en and ty_en:
        matched = [(n, d) for n, d in data.items()
                   if _field(d, "status", aliases) == st_en and _field(d, "deviceType", aliases) == ty_en]
        nm = names(matched)
        if "多少" in q:
            return "有 %d %s的%s" % (len(nm), st_cn, ty_cn)
        if "列出" in q:
            return "列出所有%s的%<仓库内路径>" % (st_cn, ty_cn, _fmt_names(nm)) if nm else "无%s的%s" % (st_cn, ty_cn)
        return "%s的%s共 %d" % (st_cn, ty_cn, len(nm))

    # ---- 区域关系 ----
    zo_en, zo_cn = _find_enum(D, q, "zone")
    if zo_en and any(k in q for k in ("区域", "区的", "在")):
        matched = [(n, d) for n, d in data.items() if _field(d, "location", aliases) == zo_en
                   or _field(d, "zone", aliases) == zo_en]
        nm = names(matched)
        if "多少" in q:
            return "%s区域有 %d" % (zo_cn, len(nm))
        return "%s区域的记录(%d):\n%s" % (zo_cn, len(nm), _fmt_names(nm))

    attr_en, attr_cn = _find_attr(D, q)
    # 通用属性识别到的模板
    if attr_en:
        # 数量: 多少[属性] (有多少台空压机 -> 类型词先; 这里处理属性值)
        pass

    # ---- 过滤计数: 属性=N 的数量 (机器故障标签=1 的数量) ----
    # 必须排在状态/类型词模板之前: 属性中文名可能含状态词(如"故障"),
    # 若被状态模板先命中会返回"有 N 故障的", 过滤计数永远不达。
    m_eq = re.search(r'[=＝]\s*(-?\d+(?:\.\d+)?)\s*的?\s*(数量|多少|共)', q)
    if attr_en and m_eq:
        target = m_eq.group(1)
        tv = float(target)
        n = sum(1 for d in data.values()
                if _num(_field(d, attr_en, aliases)) is not None
                and float(_num(_field(d, attr_en, aliases))) == tv)
        if n == 0:  # 可能以字符串存储
            n = sum(1 for d in data.values() if str(_field(d, attr_en, aliases)).strip() == target)
        cname = cn2cn.get(attr_en, attr_en)
        return "%s=%s 的数量是 %d" % (cname, target, n)

    # ---- 数量: 状态/类型/区域 ----
    st_en, st_cn = _find_enum(D, q, "status")
    if st_en and ("多少" in q or "数量" in q):
        n = sum(1 for d in data.values() if _field(d, "status", aliases) == st_en)
        return "有 %d %s的" % (n, st_cn)
    ty_en, ty_cn = _find_enum(D, q, "type")
    if ty_en and ("多少" in q or "数量" in q):
        n = sum(1 for d in data.values() if _field(d, "deviceType", aliases) == ty_en)
        return "有 %d %s" % (n, ty_cn)

    # ---- 列出 ----
    if "列出" in q:
        st_en, st_cn = _find_enum(D, q, "status")
        if st_en:
            matched = [(n, d) for n, d in data.items() if _field(d, "status", aliases) == st_en]
            return "列出所有%<仓库内路径>" % (st_cn, _fmt_names(names(matched))) if matched else "无%s" % st_cn
        ty_en, ty_cn = _find_enum(D, q, "type")
        if ty_en:
            matched = [(n, d) for n, d in data.items() if _field(d, "deviceType", aliases) == ty_en]
            return "列出所有%<仓库内路径>" % (ty_cn, _fmt_names(names(matched))) if matched else "无%s" % ty_cn

    # ---- TopN (属性最高/最低的N个) ----
    if attr_en and re.search(r'\d+\s*[台个条]', q) and _EXTREME.search(q):
        n = int(_extract_nums(q)[0]) if _extract_nums(q) else 3
        items = [(d, _num(_field(d, attr_en, aliases))) for d in data.values()]
        items = [(d, v) for d, v in items if v is not None]
        is_max = _is_max(q)
        items.sort(key=lambda x: x[1], reverse=is_max)
        cname = cn2cn.get(attr_en, attr_en)
        rows = ["  - %s (%s=%s)" % (_display_name(d, aliases, default=""), cname, v) for d, v in items[:n]]
        return "%s%s的%d个:\n%s" % ("最高" if is_max else "最低", cname, n, "\n".join(rows))

    # ---- 单极值 (属性最大/最小/最长/最贵等) ----
    if attr_en and _EXTREME.search(q):
        items = [(d, _num(_field(d, attr_en, aliases))) for d in data.values()]
        items = [(d, v) for d, v in items if v is not None]
        if items:
            is_max = _is_max(q)
            best = max(items, key=lambda x: x[1]) if is_max else min(items, key=lambda x: x[1])
            cname = cn2cn.get(attr_en, attr_en)
            return "%s的记录: %s (%s=%s)" % (("最大" if is_max else "最小") + cname, _display_name(best[0], aliases, default=""), cname, best[1])

    # ---- 平均 ----
    if attr_en and ("平均" in q or "均值" in q):
        vals = [_num(_field(d, attr_en, aliases)) for d in data.values()]
        vals = [v for v in vals if v is not None]
        if vals:
            cname = cn2cn.get(attr_en, attr_en)
            return "%s平均值 %.2f (%d条)" % (cname, sum(vals)/len(vals), len(vals))

    # ---- 总和 ----
    if attr_en and any(k in q for k in ("总", "合计", "总和")):
        vals = [_num(_field(d, attr_en, aliases)) for d in data.values()]
        vals = [v for v in vals if v is not None]
        if vals:
            cname = cn2cn.get(attr_en, attr_en)
            return "%s总和 %.2f" % (cname, sum(vals))

    # ---- 范围 (属性>N / <N / 在A到B) ----
    if attr_en:
        nums = _extract_nums(q)
        has_gt = any(k in q for k in ("大于", "高于", "超过", "以上"))
        has_lt = any(k in q for k in ("小于", "低于", "少于", "以下"))
        if ("到" in q or "之间" in q) and len(nums) >= 2:
            lo, hi = nums[0], nums[1]
            matched = [(d, _num(_field(d, attr_en, aliases))) for d in data.values()]
            matched = [(d, v) for d, v in matched if v is not None and lo <= v <= hi]
            cname = cn2cn.get(attr_en, attr_en)
            return "%s在%s到%s之间的(%d):\n%s" % (cname, lo, hi, len(matched), _fmt_names([_display_name(d, aliases, default="") for d, _ in matched]))
        elif has_gt and nums:
            n = nums[0]
            matched = [(d, _num(_field(d, attr_en, aliases))) for d in data.values()]
            matched = [(d, v) for d, v in matched if v is not None and v > n]
            cname = cn2cn.get(attr_en, attr_en)
            return "%s大于%s的(%d):\n%s" % (cname, n, len(matched), _fmt_names([_display_name(d, aliases, default="") for d, _ in matched]))
        elif has_lt and nums:
            n = nums[0]
            matched = [(d, _num(_field(d, attr_en, aliases))) for d in data.values()]
            matched = [(d, v) for d, v in matched if v is not None and v < n]
            cname = cn2cn.get(attr_en, attr_en)
            return "%s小于%s的(%d):\n%s" % (cname, n, len(matched), _fmt_names([_display_name(d, aliases, default="") for d, _ in matched]))

    # ---- 分组统计 ----
    if "统计" in q or "分组" in q or ("按" in q and "和" in q):
        grp_attrs = []
        parts = re.findall(r'按([^\s和]+)', q)
        for p in parts:
            en = D.get("attr_cn2en", {}).get(p.strip())
            if en:
                grp_attrs.append(en)
        # 处理 "按X和Y"
        if not grp_attrs and "和" in q and "按" in q:
            pieces = [x.strip() for x in q.split("和") if x.strip()]
            for p in pieces:
                p2 = p.replace("按", "")
                en = D.get("attr_cn2en", {}).get(p2)
                if en:
                    grp_attrs.append(en)
        if grp_attrs:
            cnt = defaultdict(int)
            for d in data.values():
                key = tuple(_field(d, a, aliases) or "?" for a in grp_attrs)
                cnt[key] += 1
            cn_grp = [cn2cn.get(a, a) for a in grp_attrs]
            return "按%s统计:\n%s" % ("和".join(cn_grp), "\n".join(f"  {'/'.join(k)}: {v}条" for k, v in cnt.items()))

    # ---- 反查: 哪些[属性]=值 ----
    if attr_en and ("哪些" in q or "是什么" in q):
        matched = [(d, _field(d, attr_en, aliases)) for d in data.values() if _field(d, attr_en, aliases)]
        cname = cn2cn.get(attr_en, attr_en)
        return "%s信息:\n%s" % (cname, "\n".join(f"  {_display_name(d, aliases, default='')}: {v}" for d, v in matched[:20]))

    # ---- 总数: 一共有多少条记录 ----
    if ("一共" in q or "总共有" in q or "总共" in q) and ("记录" in q or "多少" in q):
        return "一共有 %d 条记录" % len(data)

    return "暂不支持该问题"


# ------------------------------------------------------------------ main

def main():
    if len(sys.argv) < 3:
        print("用法: python ontology_qa_v3.py <nt文件> '<问题>' [lexicon.json]")
        sys.exit(1)
    nt_file = sys.argv[1]
    question = sys.argv[2]
    lex = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config", "lexicon.json")
    D = load_dict(lex)
    triples = parse_nt(nt_file)
    data = build_data(triples, D)
    if not data:
        print("本体解析失败或无实例")
        sys.exit(1)
    print(answer(question, data, D))


if __name__ == "__main__":
    main()
