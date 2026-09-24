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
            "硬性要求: 数字一律保持阿拉伯数字形式(如 3 不得写成'三个'), 不得新增/删减/篡改任何数字与事实, "
            "不得添加原文没有的修饰性叙述。\n"
            "请改写为简洁、自然的中文回答(可加'当前知识库中/根据设备台账'等轻量衔接), "
            "保持问答助手口吻, 不要文学化。\n"
            f"用户问题: {question}\n确定性查询结果: {raw_answer}\n"
            "请只输出润色后的回答, 不要解释。"
        )
        polished = llm_generate(prompt, temperature=0.1, max_tokens=300)
        if polished and not polished.startswith("["):
            # 事实保真校验(不依赖模型自觉): 原答案里的阿拉伯数字必须原样出现在润色结果中,
            # 否则视为改写事实(如 2→'两条'), 回退原答案。数字形式是用户的硬标准。
            import re as _re
            # 必须按"独立数字"比对, 不能用子串包含 ——
            # `"2" in "…2026-07-05…"` 会命中 2026 里的字符 2, 让 "有 2 条"→"有两条"
            # 这种改写悄悄通过校验。加数字边界 (?<!\d)…(?!\d) 才查得出。
            _nums = _re.findall(r"\d+(?:\.\d+)?", str(raw_answer))
            # 汉英之间不得加空格(用户的硬标准)。原文 "200L桶" 被润色成 "200L 桶" 就属违规,
            # 按"新增的汉英间空格数不得多于原文"来判, 而不是一律禁止(原文自带的允许保留)。
            _gap = r"[\u4e00-\u9fff]\s+[A-Za-z0-9]"
            _gap_ok = len(_re.findall(_gap, polished)) <= len(_re.findall(_gap, str(raw_answer)))
            # 单位也必须原样保留。数字管住了还不够: "0.6MPa" 被润色成 "0.6 兆帕",
            # 数字还在、意思还对, 但用户要看的是原始单位, 且判分/引用都会对不上。
            # 原文里的拉丁单位串(MPa/kW/℃/L…)必须原样出现在润色结果里。
            _units = [u for u in _re.findall(r"[A-Za-z][A-Za-z0-9%°/]{1,7}", str(raw_answer))
                      if not u.isascii() or len(u) >= 2]
            _unit_ok = all(u in polished for u in _units)
            if _gap_ok and _unit_ok and all(_re.search(r"(?<!\d)" + _re.escape(n) + r"(?!\d)", polished) for n in _nums):
                return polished.strip()
    except Exception:
        pass
    return raw_answer


def no_basis_reply(kb_name="知识库"):
    """无依据时的统一回复（确定性，不调 LLM）。

    历史上这里走过本地小模型"生成可读回答", 但实测小模型会把自己的推理想法
    当答案输出（"先确认问题：…看知识图谱…但子图里原料只列了 R001 到 R010…
    没有给任何库存数值"）。用户看到的是模型独白, 且每次说法都不同, 关键词拦不住。

    按"能确定性解决的不交给模型"改为固定话术：答不了就说答不了。
    """
    return f"当前知识库中未找到与这个问题直接对应的数据。可以换个问法，或确认该信息是否已录入{kb_name}。"


def _llm_fallback_answer(question, kb_name):
    """全部检索答不上时的兜底话术。evidence 空数组 = 无依据。

    ★ 这里**不再调 LLM**。原先走本地小模型让它"给个谨慎的回答", 实测小模型
    会把自己的推理想法当答案吐给用户（"先问一句，你问的是…可图上只有…而且原料的库存
    是 decimal 类型，但图上没给具体数值…"）。这是体验事故：用户看到的是模型的独白。
    靠关键词拦截也追不上 —— 换一种说法就绕过去了。

    按"能确定性解决的不交给模型"的原则，改为固定话术：答不了就说答不了，
    不编造、不推理、不给"常识性猜测"。调用方拿 None 时走引导分支。
    """
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


# ── 统一问答信封(批 2)：把既有返回扩成 {answer, hit, reason, evidence[], advisory?} ──
# 未命中/无据话术标记（与 ontology_qa_v3 口径一致）
_ENV_MISS_MARKERS = ("暂不支持", "无相关数据", "未找到", "没有找到", "不含所问", "无法回答",
                     "未收录", "无该", "不存在", "未找到与这个问题直接对应的数据")
_ENV_KINDS = ("entity", "relation", "dict", "doc")


def normalize_envelope_evidence(ev_list):
    """把既有证据(api 老格式 {entity,attr,value,source,score} 或批 2 统一格式)归一化为
    信封契约要求的列表: [{kind: entity|relation|dict|doc, id, label, source}]。

    - doc 来源的切块证据 → kind=doc；其余默认 kind=entity。
    - 已经是统一格式(kind+id+label+source 齐全)的条目原样保留。
    """
    out = []
    for e in (ev_list or []):
        if not isinstance(e, dict):
            continue
        kind = e.get("kind")
        if kind in _ENV_KINDS and "id" in e and "label" in e:
            out.append({"kind": kind, "id": e.get("id"), "label": e.get("label"),
                        "source": e.get("source") or "unknown"})
            continue
        src = e.get("source")
        if not src:
            # 无 source 时按形状判定：带 chunk/doc_id 的是文档切块证据 → doc
            src = "doc" if (e.get("chunk") is not None or e.get("doc_id")) else "rule"
        kind = "doc" if src == "doc" else "entity"
        ident = e.get("entity") or e.get("id") or e.get("doc_id") or ""
        attr = e.get("attr") or ""
        val = e.get("value")
        label = "%s=%s" % (attr, val) if attr else str(val)
        out.append({"kind": kind, "id": ident, "label": label, "source": src})
    return out


def envelope_from_result(result, kb_name="知识库"):
    """把 /api/ask 的既有返回包成统一信封，但**只新增字段、绝不删改老字段**（向后兼容）。

    新增字段：
      hit            bool  —— 所有分支都带（老字段 no_basis 取反，并与答案是否拒答对齐）
      reason         str   —— hit=false 时给未命中原因
      advisory       None  —— 批 3 模型建议层占位（本批恒为 None）
      evidence_trace list  —— 统一证据 [{kind,id,label,source}]（老 evidence 数组原样保留）

    前端现有读取（answer / evidence / no_basis / engines / mode / structured / kb ...）一个不动。
    接线只需在主链路出口加一行 `result = ask_service.envelope_from_result(result, 知识库名)`
    （批 2 因文件隔离未改 api_server.py，由主控/批 3 统一接线）。
    """
    out = dict(result or {})
    ans = str(out.get("answer") or "")
    refuse = (not ans.strip()) or any(m in ans for m in _ENV_MISS_MARKERS)
    hit = bool(ans.strip()) and not refuse and not bool(out.get("no_basis", False))
    if hit:
        out["hit"] = True
        out.setdefault("reason", "命中：检索有据")
    else:
        out["hit"] = False
        if not out.get("reason"):
            out["reason"] = "未命中：无可靠依据"
    out.setdefault("advisory", None)
    out["evidence_trace"] = normalize_envelope_evidence(out.get("evidence") or [])
    return out
