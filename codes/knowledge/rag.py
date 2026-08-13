#!/usr/bin/env python3
"""knowledge/rag.py — 检索增强生成（RAG）

问答闭环：问题向量化 → KnowledgeStore 检索 top_k 相关切块 → 拼上下文 →
llm_generate 生成答案 → 返回 {answer, evidence}。
全链路 try/except 包裹，缺模型/缺库/服务不可用返回空结果，绝不抛异常。

用法:
    from knowledge.rag import answer
    out = answer("doc1", "真空度是多少？", store, model)
    # {"answer": "...", "evidence":[{"doc_id","title","chunk","score"}]}
"""
try:
    from vector_retrieval import embed_text as _embed
except Exception:            # pragma: no cover
    _embed = None
try:
    from model_llm import llm_generate_auto as _generate_auto
except Exception:            # pragma: no cover
    _generate_auto = None


def _retrieve(store, question, base=None, model=None, top_k=5):
    """问题向量化 + 检索相关切块。返回 (hits, query_vec)。失败返回 ([], None)。"""
    try:
        if _embed is None:
            return [], None
        # 仅当显式传入 base/model 才覆盖 vector_retrieval 的默认值
        # (OLLAMA_BASE / EMBED_MODEL)；base/model 为 None 时不传该参数，
        # 避免把默认值覆写为 None 导致 base.rstrip() 崩溃、检索永远返回空。
        kw = {k: v for k, v in [("base", base), ("model", model)] if v}
        qv = _embed(question, **kw)
        if not qv:
            return [], None
        hits = store.search(qv, top_k=top_k)
        return hits, qv
    except Exception:
        return [], None


def answer(doc_id_or_kb, question, store, model=None, top_k=5, base=None,
           embed_model=None):
    """基于知识库检索生成答案。

    参数:
        doc_id_or_kb: 兼容两用——若为 str 且等于某文档 doc_id 则限定该文档检索；
                      否则视为忽略（检索整库）。传 None 检索整库。
        question: 用户问题
        store: KnowledgeStore 实例
        model: llm_generate 的 model_key（local/cloud），None 走配置默认
        top_k: 检索块数
        base/embed_model: embedding 服务参数，None 用默认
    返回:
        dict {"answer": str, "evidence": [{"doc_id","title","chunk","score"}]}
        缺模型/检索失败返回 {"answer": "[无法检索知识库]", "evidence": []}。
    """
    try:
        if _generate_auto is None:
            return {"answer": "[模型未配置]", "evidence": []}
        hits, qv = _retrieve(store, question, base=base, model=embed_model, top_k=top_k)
        if not hits:
            return {"answer": "[未能从知识库检索到相关内容]", "evidence": []}

        # 限定单文档时过滤
        if isinstance(doc_id_or_kb, str):
            filtered = [h for h in hits if h.get("doc_id") == doc_id_or_kb]
            hits = filtered or hits

        ctx_lines = []
        for h in hits:
            ctx_lines.append("[片段] %s" % h.get("chunk", ""))
        context = "\n".join(ctx_lines)
        prompt = (
            "请仅依据下面的知识库片段回答问题。若片段中没有答案，请明确说明\"片段未覆盖\"。\n"
            "---知识库片段---\n%s\n---问题---\n%s\n---\n请给出简洁、准确的中文回答。" % (context, question)
        )
        # 智能路由生成：model 显式给出则以其优先(force_key)，否则按问题复杂度路由
        # (简单→本地 ornith / 复杂→云端 DeepSeek / 云端不可用→降级本地)。
        ans, route = _generate_auto(prompt, question=question, force_key=model)
        evidence = [
            {"doc_id": h.get("doc_id"), "title": h.get("title"),
             "chunk": h.get("chunk", ""), "score": h.get("score")}
            for h in hits
        ]
        if not ans or ans.startswith("["):   # 模型错误/失败描述时照实返回，但附上路由
            return {"answer": ans or "[生成失败]", "evidence": evidence, "route": route}
        return {"answer": ans, "evidence": evidence, "route": route}
    except Exception:
        return {"answer": "[检索生成失败]", "evidence": []}
