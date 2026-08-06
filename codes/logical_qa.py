#!/usr/bin/env python3
"""logical_qa.py — 逻辑推理桥：LLM 转结构化逻辑查询 → 确定性执行器。

设计定位（见 docs/逻辑推理与可解释-设计.md）：
  规则引擎 miss
    → logical_qa.nl_to_query(question, D)    LLM 把自然语言转结构化逻辑查询(JSON)
    → logical_qa.execute_query(query, data, D)  确定性执行器(复用规则引擎字段解析/数值/枚举)
        可执行 → 返回(确定性零幻觉)   不可执行 → 返回 None 继续走 GraphRAG

与 ontology_qa_v3 的区别：
  - v3 是"规则/模板"命中（纯字符串匹配，零 token）
  - logical_qa 是"LLM 把开放式问题翻译成逻辑查询 JSON"，再由确定性执行器结算，
    兼具 LLM 的泛化理解与执行器的零幻觉确定性。

数据契约：
  data: dict{ 实体名: { 属性英文名: 值 } }   —— 与 ontology_qa_v3.build_data 输出一致
  D   : dict, 词典 lexicon.json（attr_cn2en / attr_en2cn / field_aliases ...）

用法:
  from logical_qa import answer
  res = answer(question, data, D)   # -> (answer_str, "logical") 或 None
"""
import os
import json
import re
import sys

# 复用规则引擎的字段/数值/显示名/格式化逻辑（不重复实现）
try:
    from ontology_qa_v3 import (
        _field, _num, _display_name, _fmt_names, load_dict,
    )
except Exception:  # pragma: no cover - 仅兜底，正常路径走上面
    _field = _num = _display_name = _fmt_names = None
    def load_dict(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

try:
    from model_llm import llm_generate
except Exception:  # pragma: no cover
    llm_generate = None


VALID_INTENTS = ("count", "extreme", "filter", "topn", "total")
VALID_EXTREME = ("max", "min")


# ------------------------------------------------------------------ LLM 翻译


_NL_TO_QUERY_PROMPT = """你是一个严谨的"自然语言 → 结构化逻辑查询"翻译器。只允许输出一个合法 JSON，禁止任何多余文字、注释或 Markdown。

任务：把用户问的数据库问题，翻译成如下结构的 JSON：
{{
  "intent": "count|extreme|filter|topn|total",
  "attr": "<数值/可比较属性英文名，没有则为 null>",
  "filter_cn": "<中文过滤词/枚举值，如 '乳制品'、'运行中'，没有则为 null>",
  "rel": null,
  "n": <topn 的个数，其他意图为 null>
}}

intent 含义：
  - "count"   : 问"数量/多少个"，带过滤条件 → 过滤后计数
  - "extreme" : 问"最……的"，如保质期最长 / 价格最便宜 / 功率最大
  - "filter"  : 按某个属性值过滤出满足条件的记录
  - "topn"    : 问"前 N 个 / 最……的 N 个"，如保质期最长的前3个
  - "total"   : 问"总共有多少条记录 / 一共有多少"

可选属性（attr 只能从中选，未命中填 null）：
{attrs}

extreme 意图还需一个 "extreme_dir" 字段："max" 或 "min"。

示例：
  问: 一共有多少条记录
  出: {{"intent": "total", "attr": null, "filter_cn": null, "rel": null, "n": null}}

  问: 保质期最长的产品是什么
  出: {{"intent": "extreme", "attr": "expiry_days", "extreme_dir": "max", "filter_cn": null, "rel": null, "n": null}}

  问: 乳制品有几款
  出: {{"intent": "count", "attr": null, "filter_cn": "乳制品", "rel": null, "n": null}}

  问: 保质期最长的前3个乳制品
  出: {{"intent": "topn", "attr": "expiry_days", "filter_cn": "乳制品", "rel": null, "n": 3}}

用户问题：{question}
请只输出 JSON。
"""


def _extract_json(text):
    """从 LLM 输出中宽容地提取第一个 JSON 对象。"""
    if not text:
        return None
    # 去掉可能的 Markdown 代码块围栏
    text = re.sub(r"```(?:json)?", "", text).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _attrs_prompt(D):
    """把词典的属性映射拼成 prompt 里的候选属性列表。"""
    en2cn = D.get("attr_en2cn", {})
    if not en2cn:
        return "(词典未提供属性映射)"
    return "，".join("%s(%s)" % (en, cn) for en, cn in en2cn.items())


def nl_to_query(question, D):
    """用 LLM 把自然语言翻译成结构化逻辑查询 JSON。

    返回 dict 或 None（LLM 失败 / 返回非 JSON / intent 非法时）。
    """
    if llm_generate is None:
        return None
    prompt = _NL_TO_QUERY_PROMPT.format(
        attrs=_attrs_prompt(D) or "(无属性)", question=question)
    try:
        out = llm_generate(prompt, temperature=0.0, max_tokens=400)
    except Exception:
        return None
    q = _extract_json(out)
    if not isinstance(q, dict):
        return None
    intent = q.get("intent")
    if intent not in VALID_INTENTS:
        return None
    # 规范化字段
    norm = {
        "intent": intent,
        "attr": q.get("attr") if isinstance(q.get("attr"), str) else None,
        "filter_cn": q.get("filter_cn") if isinstance(q.get("filter_cn"), str) else None,
        "rel": q.get("rel") if isinstance(q.get("rel"), str) else None,
        "n": q.get("n"),
    }
    if intent == "extreme":
        norm["extreme_dir"] = q.get("extreme_dir") if q.get("extreme_dir") in VALID_EXTREME else "max"
    # 属性名若命中词典中文，回译为英文字段
    cn2en = D.get("attr_cn2en", {})
    if norm["attr"] in cn2en:
        norm["attr"] = cn2en[norm["attr"]]
    return norm


# ------------------------------------------------------------------ 确定性执行器


def _resolve_attr(query, D):
    """把查询里的 attr（中文或英文）解析为标准字段名，返回 (字段名, 中文名) 或 (None,None)。"""
    attr = query.get("attr")
    if not attr:
        return None, None
    en2cn = D.get("attr_en2cn", {})
    cn2en = D.get("attr_cn2en", {})
    if attr in en2cn:                      # 已是英文标准字段
        return attr, en2cn[attr]
    if attr in cn2en:                      # 中文 → 英文
        return cn2en[attr], attr
    # 尝试别名/驼峰兜底（复用 _field 的 aliases 逻辑）
    aliases = D.get("field_aliases", {})
    for key, lst in aliases.items():
        if attr in lst or attr == key:
            return key, en2cn.get(key, key)
    return None, None


def _matches_filter(rec, filter_cn, D):
    """判断一条记录是否命中过滤词（在任意字段的值里包含该词）。"""
    if not filter_cn:
        return True
    for v in rec.values():
        if v is None:
            continue
        if str(v).strip() and filter_cn in str(v):
            return True
    return False


def execute_query(query, data, D):
    """确定性执行逻辑查询。

    data: dict{实体名: {属性:值}}。query: nl_to_query 产出的 dict。
    返回答案字符串，无法执行时返回 None。
    """
    if not isinstance(query, dict) or not data:
        return None
    intent = query.get("intent")
    if intent not in VALID_INTENTS:
        return None

    filter_cn = query.get("filter_cn")
    filtered = {n: d for n, d in data.items() if _matches_filter(d, filter_cn, D)}

    aliases = D.get("field_aliases", {})
    en2cn = D.get("attr_en2cn", {})

    # ---- total: 总记录数 ----
    if intent == "total":
        return "一共有 %d 条记录" % len(data)

    # ---- count: 过滤后计数 ----
    if intent == "count":
        if filter_cn:
            return "符合条件的记录有 %d 条" % len(filtered)
        return "一共有 %d 条记录" % len(data)

    # 下面都需要解析属性
    attr_en, attr_cn = _resolve_attr(query, D)
    if not attr_en:
        return None

    # 提取数值化的 (实体, 值) 列表
    def num_items(recs):
        return [(n, _num(_field(d, attr_en, aliases))) for n, d in recs.items()]

    # ---- extreme: 求最值 ----
    if intent == "extreme":
        items = [(n, v) for n, v in num_items(filtered) if v is not None]
        if not items:
            return None
        is_max = query.get("extreme_dir", "max") != "min"
        best = max(items, key=lambda x: x[1]) if is_max else min(items, key=lambda x: x[1])
        label = "%s（%s）" % (attr_cn or attr_en, "最高" if is_max else "最低")
        return "%s的记录: %s (%s=%s)" % (label, best[0], attr_cn or attr_en, best[1])

    # ---- topn: 前 N 个 ----
    if intent == "topn":
        items = [(n, v) for n, v in num_items(filtered) if v is not None]
        if not items:
            return None
        n = int(query["n"]) if isinstance(query.get("n"), (int, float)) else 3
        n = max(1, min(n, 100))
        is_max = query.get("extreme_dir", "max") != "min"
        items.sort(key=lambda x: x[1], reverse=is_max)
        cname = attr_cn or attr_en
        rows = ["  - %s (%s=%s)" % (n, cname, v) for n, v in items[:n]]
        return "%s%s的%d个:\n%s" % ("最高" if is_max else "最低", cname, n, "\n".join(rows))

    # ---- filter: 属性过滤 ----
    if intent == "filter":
        matched = {n: d for n, d in filtered.items()
                   if _field(d, attr_en, aliases) not in ("", None)}
        if not matched:
            return None
        cname = attr_cn or attr_en
        return "%s信息(%d):\n%s" % (cname, len(matched),
                                    _fmt_names([_display_name(d, aliases, default=n)
                                                for n, d in matched.items()]))

    return None


# ------------------------------------------------------------------ 组合入口


def answer(question, data, D):
    """组合 nl_to_query + execute_query。

    任一失败返回 None（上游规则引擎可继续走 GraphRAG）。
    成功返回 (answer_str, "logical")。
    """
    if not question or not data or not D:
        return None
    query = nl_to_query(question, D)
    if query is None:
        return None
    ans = execute_query(query, data, D)
    if ans is None:
        return None
    return ans, "logical"


# ------------------------------------------------------------------ main


def main():
    """命令行用法: python logical_qa.py <nt文件> '<问题>' [lexicon.json]

    注意: nl_to_query 需要 LLM 在线；若 LLM 不可用，可先手工构造 query 调 execute_query。
    """
    if len(sys.argv) < 3:
        print("用法: python logical_qa.py <nt文件> '<问题>' [lexicon.json]")
        sys.exit(1)
    nt_file = sys.argv[1]
    question = sys.argv[2]
    lex = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config", "lexicon.json")
    D = load_dict(lex)
    from ontology_qa_v3 import parse_nt, build_data
    data = build_data(parse_nt(nt_file), D)
    if not data:
        print("本体解析失败或无实例")
        sys.exit(1)
    res = answer(question, data, D)
    if res is None:
        print("[logical_qa] 无法转成可执行的逻辑查询，请走 GraphRAG")
        sys.exit(2)
    print(res[0])


if __name__ == "__main__":
    main()
