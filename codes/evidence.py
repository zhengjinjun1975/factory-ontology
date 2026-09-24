#!/usr/bin/env python3
"""evidence.py — 答案溯源 / 可解释性：从规则引擎命中提取实体/属性/值证据。

设计：规则引擎 (ontology_qa_v3.answer) 是确定性的，命中哪些实体/属性/值，
就是证据。本模块不改 answer 签名，通过独立函数 extract_evidence 对同一份
(question, data, D) 重新做一次轻量"命中扫描"，把参与得出答案的实体 + 属性
+ 值整理成结构化证据，供溯源展示。

返回格式:
    {
      "rule": <规则名, str>,
      "entities": [ {"name": <实体显示名>, "prop": <属性英文>, "value": <属性值>}, ... ]
    }

规则名示例: count_by_type / count_by_status / extreme / top_n / average /
sum / range / list / total / group / lookup。

用法: from evidence import extract_evidence
      ev = extract_evidence(question, data, D, answer_str)
"""

import re

from ontology_qa_v3 import (
    _field,
    _display_name,
    _num,
    _find_attr,
    _find_enum,
    _is_max,
    _is_min,
    _EXTREME,
    _EXTREME_MIN,
    _entity_subset,
    _type_matches,
    _status_matches,
    _attr_val,
    _extract_nums,
    _value_filter_count,
)


def _mk_entity(d, name, prop, value):
    """构造一条实体证据。"""
    return {"name": name, "prop": prop, "value": value}


def _scan_attr_entities(data, aliases, attr_en, cn2cn):
    """扫描 data 中所有具有属性 attr_en 的实体，返回 (name, prop, value) 证据列表。"""
    cname = cn2cn.get(attr_en, attr_en)
    ev = []
    for name, d in data.items():
        val = _field(d, attr_en, aliases)
        if val not in (None, "", "?"):
            ev.append(_mk_entity(d, _display_name(d, aliases, default=name), cname, val))
    return ev


def extract_evidence(question, data, D, answer):
    """从规则引擎命中提取证据。返回 {"rule": ..., "entities": [...]}。

    参数:
      question 问题原文
      data     {实体名: {属性: 值}}  (与 ontology_qa_v3.answer 相同)
      D        词典 (attr_cn2en / attr_en2cn / *_cn2en / field_aliases)
      answer   规则引擎产出的答案字符串 (仅用于兜底/上下文, 不做解析)
    """
    aliases = D.get("field_aliases", {})
    cn2cn = D.get("attr_en2cn", {})

    # ---- 1) 按类型/状态/区域计数 (乳制品的数量) ----
    ty_en, ty_cn = _find_enum(D, question, "type")
    if ty_en and ("多少" in question or "数量" in question or "共" in question):
        matched = [(n, d) for n, d in data.items()
                   if _field(d, "deviceType", aliases) == ty_en]
        entities = [_mk_entity(d, _display_name(d, aliases, default=n), "category", ty_en)
                    for n, d in matched]
        return {"rule": "count_by_type", "entities": entities}

    st_en, st_cn = _find_enum(D, question, "status")
    if st_en and ("多少" in question or "数量" in question):
        matched = [(n, d) for n, d in data.items()
                   if _field(d, "status", aliases) == st_en]
        entities = [_mk_entity(d, _display_name(d, aliases, default=n), "status", st_en)
                    for n, d in matched]
        return {"rule": "count_by_status", "entities": entities}

    # ---- 2) 极值 (保质期最长的产品 / 功率的最大值) ----
    attr_en, attr_cn = _find_attr(D, question)
    if attr_en and _EXTREME.search(question):
        items = [(d, _num(_field(d, attr_en, aliases))) for d in data.values()]
        items = [(d, v) for d, v in items if v is not None]
        if items:
            best = max(items, key=lambda x: x[1]) if _is_max(question) else min(items, key=lambda x: x[1])
            cname = cn2cn.get(attr_en, attr_en)
            return {"rule": "extreme",
                    "entities": [_mk_entity(best[0],
                                            _display_name(best[0], aliases, default=""),
                                            cname, best[1])]}
        return {"rule": "extreme", "entities": []}

    # ---- 3) TopN (属性最高/最低的N个) ----
    if attr_en and re.search(r'\d+\s*[台个条]', question) and _EXTREME.search(question):
        n = int(re.findall(r'(\d+)', question)[0]) if re.findall(r'(\d+)', question) else 3
        items = [(d, _num(_field(d, attr_en, aliases))) for d in data.values()]
        items = [(d, v) for d, v in items if v is not None]
        items.sort(key=lambda x: x[1], reverse=_is_max(question))
        cname = cn2cn.get(attr_en, attr_en)
        entities = [_mk_entity(d, _display_name(d, aliases, default=""), cname, v)
                    for d, v in items[:n]]
        return {"rule": "top_n", "entities": entities}

    # ---- 4) 平均 / 总和 / 范围 / 反查 / 分组等通用属性命中 ----
    if attr_en:
        cname = cn2cn.get(attr_en, attr_en)
        entities = _scan_attr_entities(data, aliases, attr_en, cn2cn)
        if "平均" in question or "均值" in question:
            return {"rule": "average", "entities": entities}
        if any(k in question for k in ("总", "合计", "总和")):
            return {"rule": "sum", "entities": entities}
        nums = re.findall(r'(\d+(?:\.\d+)?)', question)
        if ("到" in question or "之间" in question) and len(nums) >= 2:
            return {"rule": "range", "entities": entities}
        if any(k in question for k in ("大于", "高于", "超过", "以上", "小于", "低于", "少于", "以下")):
            return {"rule": "range", "entities": entities}
        if "哪些" in question or "是什么" in question:
            return {"rule": "lookup", "entities": entities}
        if "统计" in question or "分组" in question:
            return {"rule": "group", "entities": entities}

    # ---- 5) 列类型/状态 ----
    if "列出" in question:
        ty_en2, _ = _find_enum(D, question, "type")
        st_en2, _ = _find_enum(D, question, "status")
        if ty_en2:
            matched = [(n, d) for n, d in data.items()
                       if _field(d, "deviceType", aliases) == ty_en2]
            entities = [_mk_entity(d, _display_name(d, aliases, default=n), "category", ty_en2)
                        for n, d in matched]
            return {"rule": "list", "entities": entities}
        if st_en2:
            matched = [(n, d) for n, d in data.items()
                       if _field(d, "status", aliases) == st_en2]
            entities = [_mk_entity(d, _display_name(d, aliases, default=n), "status", st_en2)
                        for n, d in matched]
            return {"rule": "list", "entities": entities}

    # ---- 6) 总数 ----
    if ("一共" in question or "总共有" in question or "总共" in question):
        return {"rule": "total",
                "entities": [_mk_entity(d, _display_name(d, aliases, default=n), "_count", 1)
                             for n, d in data.items()]}

    # ---- 兜底: 找不到明确规则, 返回空证据 ----
    return {"rule": "unknown", "entities": []}


# ------------------------------------------------------------------ 可回溯统一证据(批 2)
# 证据条数上限：避免 1 万行级 KB 把信封撑爆（计数类问题回该类实例，超出截断）。
TRACE_MAX = 50

def _mk_trace(d, name, prop, value, kind="entity", source="rule"):
    """构造一条统一证据 {kind, id, label, source}。kind ∈ entity|relation|dict|doc。"""
    return {"kind": kind, "id": name, "label": "%s=%s" % (prop, value), "source": source}


def build_trace(question, data, D, answer=None):
    """把问答命中整理成**可回溯**的统一证据列表 [{kind, id, label, source}]（批 2）。

    与 extract_evidence 的区别：extract_evidence 面向 api 老字段({name,prop,value})，
    本函数产出批 2 信封契约要求的 {kind,id,label,source}，并补齐词表(dict)证据与
    "值过滤计数"类兜底答案的命中记录（老规则表未覆盖，导致信封里 hit=true 却没有 evidence）。

    规则：先复用 extract_evidence 的规则证据；为空时按规则族重扫"参与得出答案的记录"；
    仍为空则返回 []（表示该答案无法回溯，由调用方决定是否据此降级）。
    """
    aliases = D.get("field_aliases", {})
    cn2cn = D.get("attr_en2cn", {})

    # 1) 词表证据：问句用到的 类型/状态/区域 词 → 值 的映射(可回溯到词典)
    dict_ev = []
    for which in ("type", "status", "zone"):
        en, cn = _find_enum(D, question, which)
        if cn:
            dict_ev.append({"kind": "dict", "id": str(en),
                            "label": "%s 词表：%s→%s" % (which, cn, en), "source": "lexicon"})

    # 2) 规则证据(复用既有 extract_evidence)
    raw = extract_evidence(question, data, D, answer or "")
    out = []
    for e in raw.get("entities", []) or []:
        out.append(_mk_trace(None, e.get("name"), e.get("prop"), e.get("value")))
    if out:
        return (out + dict_ev)[:TRACE_MAX]

    # 3) 规则表未覆盖 → 按规则族重扫参与记录
    ents = _trace_generic(question, data, D, aliases, cn2cn)
    return ents + dict_ev


def _trace_generic(question, data, D, aliases, cn2cn):
    """按规则族重扫"参与得出答案的记录"，产出记录级证据。无命中返回 []。"""
    sub = _entity_subset(question, D, data)

    def ent(name, d, prop, value):
        return _mk_trace(d, _display_name(d, aliases, default=name) or name, prop, value)

    attr_en, attr_cn = _find_attr(D, question)
    if attr_en:
        cname = attr_cn or cn2cn.get(attr_en, attr_en)
        pairs = [(n, d, _num(_field(d, attr_en, aliases))) for n, d in sub.items()]
        vals = [(n, d, v) for n, d, v in pairs if v is not None]
        # 极值：只回并列极值者
        if vals and _EXTREME.search(question):
            bestv = (max if _is_max(question) else min)(v for _, _, v in vals)
            return [ent(n, d, cname, bestv) for n, d, v in vals if v == bestv]
        # 平均/总和：回全部参与记录
        if vals and ("平均" in question or "均值" in question
                     or any(k in question for k in ("总", "合计", "总和"))):
            return [ent(n, d, cname, v) for n, d, v in vals]
        # 范围：回命中的记录
        nums = _extract_nums(question)
        if vals and nums:
            if any(k in question for k in ("大于", "高于", "超过", "以上")):
                hit = [(n, d, v) for n, d, v in vals if v > nums[0]]
                if hit:
                    return [ent(n, d, cname, v) for n, d, v in hit]
            if any(k in question for k in ("小于", "低于", "少于", "以下")):
                hit = [(n, d, v) for n, d, v in vals if v < nums[0]]
                if hit:
                    return [ent(n, d, cname, v) for n, d, v in hit]
            if ("到" in question or "之间" in question) and len(nums) >= 2:
                lo, hi = nums[0], nums[1]
                hit = [(n, d, v) for n, d, v in vals if lo <= v <= hi]
                if hit:
                    return [ent(n, d, cname, v) for n, d, v in hit]

    # 类型/状态 过滤（计数/列举）
    ty_en, ty_cn = _find_enum(D, question, "type")
    st_en, st_cn = _find_enum(D, question, "status")
    if ty_en and ("多少" in question or "数量" in question or "列出" in question
                  or "哪些" in question or "共" in question):
        matched = [(n, d) for n, d in data.items() if _type_matches(d, ty_en, aliases)]
        if matched:
            return [ent(n, d, "type", ty_en) for n, d in matched]
    if st_en and ("多少" in question or "数量" in question or "列出" in question
                  or "哪些" in question):
        matched = [(n, d) for n, d in data.items() if _status_matches(d, st_en, aliases, D)]
        if matched:
            return [ent(n, d, "status", st_en) for n, d in matched]

    # 值过滤计数兜底（"法兰的数量" 之类）——复用引擎同一算法，取命中实体名
    vf = _value_filter_count(question, data, with_names=True)
    if vf:
        names = vf[2] if len(vf) > 2 else []
        return [_mk_trace(data.get(n) if isinstance(data, dict) else None, n,
                          "匹配取值", vf[1]) for n in names][:TRACE_MAX]

    # 实体计数/总数 / 实体实例列举（有多少个产品 / 产品总数 / 产品有哪些）：
    # 回该类实例记录作为证据；类名取不到时回全库记录（引擎的计数口径即如此）。
    from ontology_qa_v3 import get_entity_cn2uri
    emap = dict(get_entity_cn2uri())
    emap.update(D.get("entity_cn2en") or {})
    for cn in sorted(emap, key=len, reverse=True):
        if cn and cn in question:
            uri = str(emap[cn]).lower()
            matched = [(n, d) for n, d in data.items() if uri in n.lower()]
            picked = matched or list(data.items())
            return [ent(n, d, "计数", 1) for n, d in picked][:TRACE_MAX]
    return []
