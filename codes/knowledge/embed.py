#!/usr/bin/env python3
"""knowledge/embed.py — 切块向量化

复用 vector_retrieval.embed_text 调 Ollama nomic-embed-text（768 维）为每个切块
生成向量。embedding 服务不可用/失败时返回空向量列表（不阻塞），调用方据此跳过入库。

用法:
    from knowledge.embed import embed_chunks
    vecs = embed_chunks(chunks, base="http://127.0.0.1:11434", model="nomic-embed-text")
    # 成功: [[float,...], ...] 与 chunks 一一对应；失败: []
"""
try:
    from vector_retrieval import embed_text
except Exception:            # pragma: no cover - 复用失败时兜底
    embed_text = None


def embed_chunks(chunks, base=None, model=None):
    """为 chunks 逐块生成向量。

    参数:
        chunks: list[dict]，含 "text" 键（chunk 模块产物）
        base: Ollama base url，None 用 vector_retrieval 默认值
        model: embedding 模型名，None 用默认 nomic-embed-text
    返回:
        list[list[float]] 与 chunks 一一对应；任一块失败/无 embed_text 返回 []。
    """
    try:
        if embed_text is None or not chunks:
            return []
        vectors = []
        for c in chunks:
            text = (c.get("text") or "") if isinstance(c, dict) else str(c)
            vec = embed_text(text, base=base, model=model)
            if not vec:
                return []          # 服务不可用：整体回落，不产出半截结果
            vectors.append(vec)
        return vectors
    except Exception:
        return []
