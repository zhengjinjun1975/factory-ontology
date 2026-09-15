# -*- coding: utf-8 -*-
"""工厂本体问答 - 查询理解层。纯标准库。"""

try:
    import ontology_qa_v3 as qa
except Exception:
    qa = None

_ADVICE = ("注意事项", "注意", "建议", "如何", "怎么", "风险", "隐患", "怎么办",
           "措施", "意义", "作用", "影响", "合规", "规范要求")
_DATA = ("数量", "多少", "哪些", "几个", "几条", "最大", "最小", "最长",
         "最短", "分布", "统计", "平均", "合计", "列出", "排名")
_ENTITY_WORDS = ("产品", "设备", "客户", "批次", "原料", "订单",
                 "班组", "供应商", "库存")
_ANAPHORA = ("它", "该", "这个", "上述", "此", "其")


def _intent(q):
    for w in _ADVICE:
        if w in q:
            return "advice"
    for w in _DATA:
        if w in q:
            return "data_query"
    return "chitchat"


def _entities(q, D):
    out = []
    cn2en = {}
    if isinstance(D, dict):
        m = D.get("entity_cn2en")
        if isinstance(m, dict):
            cn2en = m
    for w in _ENTITY_WORDS:
        if w in q:
            out.append({"mention": w, "class": cn2en.get(w)})
    return out


def _resolve(q, ctx):
    if not isinstance(ctx, dict):
        return q
    ent = ctx.get("entities")
    if isinstance(ent, list) and ent:
        rep = str(ent[0])
    elif isinstance(ctx.get("entity"), str) and ctx["entity"]:
        rep = ctx["entity"]
    else:
        return q
    if not any(a in q for a in _ANAPHORA):
        return q
    # 取文本中出现位置最早的指代词(而非按 _ANAPHORA 元组声明顺序),
    # 修 "这个它的库存" 先替 "它" 的位置错乱(应按句中首现的指代词替换)。
    best_pos, best_a = -1, None
    for a in _ANAPHORA:
        p = q.find(a)
        if p != -1 and (best_pos == -1 or p < best_pos):
            best_pos, best_a = p, a
    if best_a is None:
        return q
    return q[:best_pos] + rep + q[best_pos + len(best_a):]


def understand(q, D, ctx=None):
    q = q if isinstance(q, str) else ""
    try:
        intent = _intent(q)
    except Exception:
        intent = "chitchat"
    try:
        entities = _entities(q, D)
    except Exception:
        entities = []
    try:
        resolved = _resolve(q, ctx)
    except Exception:
        resolved = q
    try:
        is_cross = bool(qa.is_cross_domain_data_query(q, D)) if qa else False
    except Exception:
        is_cross = False
    return {
        "intent": intent,
        "entities": entities,
        "resolved": resolved,
        "is_cross_domain": is_cross,
    }


if __name__ == "__main__":
    # advice
    r = understand("有什么需要注意的", {})
    assert r["intent"] == "advice", r
    # data_query
    r = understand("产品类型有哪些", {})
    assert r["intent"] == "data_query", r
    assert any(e["mention"] == "产品" for e in r["entities"]), r
    # 实体绑定
    r = understand("产品有哪些", {"entity_cn2en": {"产品": "Product"}})
    assert r["entities"][0]["class"] == "Product", r
    # 空 D / None 不崩
    assert understand("库存多少", None)["intent"] == "data_query"
    assert understand("", {})["intent"] == "chitchat"
    # cross_domain 可调用不崩
    r = understand("外部天气数据", {})
    assert isinstance(r["is_cross_domain"], bool), r
    # 指代消解
    r = understand("它的库存呢", {}, ctx={"entity": "P005"})
    assert "P005" in r["resolved"], r
    r = understand("它的库存呢", {}, ctx={"entities": ["P005"]})
    assert "P005" in r["resolved"], r
    # 无指代时原样
    assert understand("库存多少", {}, ctx={"entity": "P005"})["resolved"] == "库存多少"
    print("ok")