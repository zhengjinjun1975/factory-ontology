#!/usr/bin/env python3
"""ask_service.py — 问答融合辅助层(自 api_server.py 拆分, P2)

职责：只承载 /api/ask 六路融合中的"无状态"逻辑 ——
  证据归一化(_norm_*)、LLM 润色/兜底(_polish/_llm_fallback)、
  RAG+本体融合(_fuse_doc_supplement/_doc_rag_fallback)、文档切块检索(_retrieve_doc_chunks)、
  多租户文档目录(_kb_dir)。

设计约束(拆分安全边界)：
- 本模块**不持有可变更的全局状态**(如 api_server 里会被 `global KBS` 重绑定的注册表/缓存)，
  只做纯函数与常量。涉及知识库名的地方由调用方传入(如 kb_name), 避免与 api_server 的
  KBS/_KB_INDEX_CACHE 耦合。因此可从上帝模块安全抽出, 不破坏多租户注册/失效/重建。
- 文档目录 _KB_ROOT 为常量, 与 api_server 的 `output/kb_store` 一致。

api_server.py 保留: 路由 + 鉴权 + _ask_impl 编排 + 多租户 ctx 缓存 + 资产 + 建模。
"""
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
_KB_ROOT = os.path.join(ROOT, "output", "kb_store")


def _kb_dir(kb):
    """多租户隔离目录。kb 名非法(路径穿越/空)返回 None。"""
    kb = (kb or "food").strip()
    if not kb or kb.startswith(".") or any(c in kb for c in ("/", "\\", "..")):
        return None
    d = os.path.join(_KB_ROOT, kb)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return None
    return d


# ── 蓝图契约辅助: engines 记录 + evidence 归一化 + LLM 润色/兜底 ─────────────
# 对齐蓝图 L243: /api/ask 返回 {answer, structured?, evidence:[{entity,attr,value,source,score}],
#   engines:[rule/graph/vector/bm25/doc]}。evidence 空数组 = 纯 LLM 兜底(无依据)。


def _norm_rule_evidence(raw_ev):
    """把规则引擎证据(evidence.py 的 {rule, entities:[{name,prop,value}]})归一化为蓝图数组。

    返回 (evidence_list, structured):
      evidence_list: [{entity, attr, value, source:"rule", score:1.0}]
      structured:    原始结构化结果 {rule, entities}(若可提取)
    """
    if not isinstance(raw_ev, dict):
        return [], None
    ents = raw_ev.get("entities") or []
    ev = [{"entity": e.get("name"), "attr": e.get("prop"), "value": e.get("value"),
           "source": "rule", "score": 1.0} for e in ents if isinstance(e, dict)]
    structured = {"rule": raw_ev.get("rule"), "entities": ents} if ents else None
    return ev, structured


def _norm_doc_evidence(ev_list):
    """把知识库 RAG 的 evidence([{doc_id,title,chunk,score}])归一化为蓝图数组(引擎=doc)。"""
    out = []
    for e in (ev_list or []):
        if not isinstance(e, dict):
            continue
        out.append({"entity": e.get("title") or e.get("doc_id"),
                    "attr": "chunk", "value": e.get("chunk", ""),
                    "source": "doc", "score": e.get("score")})
    return out


def _polish_rule_answer(question, raw_answer):
    """规则查数 + LLM 润色: 把确定性查数结果(如"有 10 台设备")润色成带上下文的自然语言。

    保留确定性事实与数字(不改数), 只提升表达。LLM 失败时回退原答案, 绝不丢事实。
    """
    try:
        from model_llm import llm_generate
        prompt = (
            "你是严谨的数据问答助手。下面的答案来自确定性的知识库查询, 事实与数字必须原样保留。\n"
            "请把它改写为自然、带上下文的通顺中文回答(可补充'当前知识库中/根据设备台账等'衔接语), "
            "但绝不允许新增、删减或篡改任何数字与事实。\n"
            f"用户问题: {question}\n确定性查询结果: {raw_answer}\n"
            "请只输出润色后的回答, 不要解释。"
        )
        polished = llm_generate(prompt, temperature=0.2, max_tokens=300)
        if polished and not polished.startswith("["):
            return polished.strip()
    except Exception:
        pass
    return raw_answer


def _llm_fallback_answer(question, kb_name):
    """LLM 兜底: 全部检索答不上时, 生成理解性回答。evidence 空数组 = 无依据。

    问题与知识库无关/LLM 不可用时, 返回 None 交给上层走引导, 避免编造。
    kb_name: 该知识库显示名(由调用方从注册表解析后传入, 保持本模块无全局状态)。
    """
    try:
        from model_llm import llm_generate
        prompt = (
            f"你是'{kb_name}'的知识库问答助手。针对用户的问题, 若知识库无法检索到确凿依据, "
            "请诚实说明当前知识库中没有找到相关数据, 并给出基于常识的谨慎、不编造具体数字的回答; "
            "若问题本身与知识库领域完全无关, 请明确表示无法回答。\n"
            f"用户问题: {question}"
        )
        ans = llm_generate(prompt, temperature=0.4, max_tokens=300)
        if ans and not ans.startswith("["):
            return ans.strip()
    except Exception:
        pass
    return None


def _retrieve_doc_chunks(question, kb, top_k=5):
    """仅检索(不生成答案)某 kb 已入库文档, 返回归一化 doc 证据列表 [{doc_id,title,chunk,score}]。

    RAG+本体融合的"文档补细节/溯源"用: 只取向量命中的原文切块, 不调 LLM 生成答案,
    避免重复生成冗余 doc 回答、也保证溯源只引用原文不编造。无有效命中返回 []。
    """
    try:
        from knowledge.rag import _retrieve
        from knowledge.store import KnowledgeStore
        kbdir = _kb_dir(kb)
        if kbdir is None:
            return []
        hits, _qv = _retrieve(KnowledgeStore(kbdir), question, top_k=top_k)
        if not hits:
            return []
        return [{"doc_id": h.get("doc_id"), "title": h.get("title"),
                 "chunk": h.get("chunk", ""), "score": h.get("score")}
                for h in hits]
    except Exception:
        return []


_FUSION_STOP = {"的", "了", "是", "在", "有", "与", "和", "或", "及", "个", "只",
                "种", "类型", "哪些", "什么", "怎么", "如何", "为", "为了", "对",
                "从", "被", "把", "让", "要", "但", "并且", "哪", "些", "等",
                "关于", "请问", "一下", "信息", "相关", "数据"}

# 尾部疑问/请求助词(只做"去尾", 不作为内容词参与二元组匹配)
_FUSION_QUEST_TAIL = ("是什么", "有哪些", "多少个", "多少种", "多少", "怎么", "如何",
                      "什么", "哪些", "为什", "吗", "呢", "呀", "吧")


def _fuse_q_bigrams(q):
    """从问题提取"滑动二元组"(相邻词对), 用于文档融合的相关性闸门。

    去掉尾部疑问助词(是什么/有哪些/多少/怎么/如何等)后, 对剩余中文主体
    取每相邻两字组成一个词对(如"设备温度要求"→"设备/备温/温度/度要/要求"),
    得到一组可判断文档切块与问题是否同主题的滑动二元组。
    """
    out = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", q or ""):
        body = run
        for p in sorted(_FUSION_QUEST_TAIL, key=len, reverse=True):
            if body.endswith(p):
                body = body[:len(body) - len(p)]
                break
        if len(body) < 2:
            continue
        out.add(body)
        for i in range(len(body) - 1):
            out.add(body[i:i + 2])
    return out


def _fuse_chunk_relevant(question, chunk):
    """相关性闸门: 文档切块与问题共享 >=2 个滑动二元组(相邻词对)即判同主题。

    相比单一 2 字关键词(易被跨主题文档偶然命中, 如"类型/设备"在护肤/医药报告),
    滑动二元组要求问题与切块有多个相邻词对重合, 显著提升相关度判断精度,
    从而过滤跨主题的无关文档命中, 避免 RAG 融合/兜底时的主题污染。
    """
    bgs = _fuse_q_bigrams(question)
    if not bgs:
        return False
    return sum(1 for b in bgs if b in (chunk or "")) >= 2


def _fuse_doc_supplement(question, structured_payload, kb):
    """RAG+本体融合核心: 结构化答案优先, 文档补细节/溯源。

    当结构化查询命中确定数据(evidence 非空)时, 并行检索该 kb 已入库文档;
    若文档返回有效命中, 把文档切块并入 evidence(source=doc), 并在 answer 尾部
    追加一段带溯源的"文档补充", 返回融合 payload(mode=fused, engines 含 doc)。
    文档无有效命中、或命中切块与问题无关键词重合(跨主题干扰)时,
    原样返回结构化 payload(结构化仍优先, 不因文档缺失/无关而降级)。
    """
    if not kb or not structured_payload:
        return structured_payload
    doc_hits = _retrieve_doc_chunks(question, kb, top_k=5)
    if not doc_hits:
        return structured_payload
    # 相关性闸门: 只保留与问题共享 >=2 个滑动二元组(相邻词对)的切块,
    # 过滤跨主题的无关文档命中(如护肤/医药报告偶然命中"类型/设备"等单字)
    doc_hits = [h for h in doc_hits
                if _fuse_chunk_relevant(question, h.get("chunk") or "")]
    if not doc_hits:
        return structured_payload
    doc_ev = _norm_doc_evidence(doc_hits)
    payload = dict(structured_payload)
    payload["evidence"] = list(structured_payload.get("evidence", [])) + doc_ev
    payload["engines"] = list(dict.fromkeys(list(payload.get("engines", [])) + ["doc"]))
    payload["mode"] = "fused"
    payload["doc_evidence"] = doc_ev
    payload["no_basis"] = False
    # 文档补充段: 只引用原文切块 + 来源标题, 绝不自行编造细节
    top = doc_ev[0]
    chunk = str(top.get("value") or "")[:300].strip()
    if chunk:
        supp = "\n\n—— 📄 文档补充（溯源）——\n%s" % chunk
        if top.get("entity"):
            supp += "\n（来源：《%s》 相关度 %.2f）" % (top["entity"], top.get("score") or 0)
        payload["answer"] = str(payload.get("answer", "")).rstrip() + supp
    return payload


def _doc_rag_fallback(question, kb):
    """文档有而本体无时用文档答: 结构化查询"无记录"(no_basis)时, 让文档 RAG 兜底。

    返回文档 RAG 的完整答案 payload(kb_rag); 文档也无有效依据时返回 None,
    交由调用方保留原确定性"无记录"答案(不编造)。
    """
    try:
        from knowledge.rag import answer as _rag_answer
        from knowledge.store import KnowledgeStore
        kbdir = _kb_dir(kb)
        if kbdir is None:
            return None
        res = _rag_answer(None, question, KnowledgeStore(kbdir), top_k=5)
        ans = (res or {}).get("answer", "") or ""
        ev = (res or {}).get("evidence", []) or []
        # 相关性闸门: 只保留与问题共享 >=2 个滑动二元组的切块, 过滤跨主题文档
        ev = [e for e in ev if _fuse_chunk_relevant(question, e.get("chunk") or "")]
        if ans.strip() and not ans.startswith("[") and "片段未覆盖" not in ans and ev:
            return {"ok": True, "mode": "kb_rag", "answer": ans,
                    "evidence": _norm_doc_evidence(ev), "engines": ["doc"],
                    "structured": None, "no_basis": False, "kb": kb}
    except Exception:
        pass
    return None
