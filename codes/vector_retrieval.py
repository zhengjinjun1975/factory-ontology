#!/usr/bin/env python3
"""vector_retrieval.py — 本地向量语义检索（纯标准库，零第三方向量库依赖）

对知识图谱的每个实例实体做 embedding（本地 Ollama nomic-embed-text, 768维），
查询时把问题 embedding 后与所有实体向量做余弦相似度，召回 top-k 语义相近实体。

作用：作为"语义检索"层，补充 BM25(稀疏) 无法处理的语义模糊/同义/口语化查询
（如"最贵的产品"→price、"油轮有几艘"→船型）。本地 embedding，零 API 成本，
embedding 失败/服务不可用时回落空结果，绝不阻塞下游。

用法（被 api_server 集成）：
    from vector_retrieval import VectorIndex
    vx = VectorIndex.from_graph(graph, lexicon)   # graph=build_graph(nt) 返回的图
    hits = vx.search("最贵的产品", top_k=5)        # [{entity, score, text}]

独立测试：
    python vector_retrieval.py <nt文件> <lexicon.json> "<问题>"
"""
import os
import json
import time
import random
import urllib.request
import urllib.error

# 从 model_config 读取向量模型配置（默认本地 nomic-embed-text）。读取失败回落默认，不阻塞。
try:
    from model_llm import get_embedding_config as _get_emb
    _emb = _get_emb()
except Exception:
    _emb = {}
OLLAMA_BASE = _emb.get("base_url") or os.environ.get("OLLAMA_BASE", "http://127.0.0.1:11434")
EMBED_MODEL = _emb.get("model") or os.environ.get("EMBED_MODEL", "nomic-embed-text")


def embed_text(text: str, base=OLLAMA_BASE, model=EMBED_MODEL):
    """调 Ollama /api/embeddings 生成向量。失败返回 None（不抛异常）。"""
    if not text or not text.strip():
        return None
    try:
        req = urllib.request.Request(
            base.rstrip("/") + "/api/embeddings",
            data=json.dumps({"model": model, "prompt": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        return data.get("embedding")
    except Exception:
        return None


def _cosine(a, b):
    """余弦相似度（纯标准库）。任一为空返回 0.0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class VectorIndex:
    """本地向量语义检索索引：{实体: (文本, 向量)}。"""

    def __init__(self):
        self.entries = []      # [{entity, text, vector}]
        self._built = False

    @classmethod
    def from_graph(cls, graph, lexicon=None, base=OLLAMA_BASE, model=EMBED_MODEL,
                   max_entries=2000, build_timeout=5.0):
        """从 build_graph 返回的 {node: {rel: [vals]}} 建向量索引。
        每个实例实体 = 一条记录(名字 + 各属性[中文label]:值)，做 embedding。
        lexicon 提供 attr_en2cn 把英文字段名转中文 label，提升中文语义匹配。
        类/属性声明节点(无下划线实例id)跳过。embedding 失败回落空索引(不阻塞)。

        规模防护（防止万级实例逐条 embedding 卡死）：
        - max_entries：实例实体超过该数量时随机抽样固定数量构建，其余不进向量索引
          （BM25 仍全量覆盖，语义召回降级但不阻塞）。
        - build_timeout：构建超时（秒）则放弃向量索引，降级纯 BM25（返回空索引），
          由混合检索路径自动回落，绝不阻塞下游。
        """
        vx = cls()
        en2cn = {}
        if lexicon:
            try:
                en2cn = lexicon.get("attr_en2cn", {}) or {}
            except AttributeError:
                pass
        # 先收集候选实例节点（文本已拼好），再决定抽样规模，避免构建前预嵌入
        candidates = []
        for node, props in graph.items():
            name = str(node).split("#")[-1] if "#" in str(node) else str(node).split("/")[-1]
            if name.startswith("__") or "domain" in name:
                continue
            if "_" not in name:           # 仅实例实体（含下划线 id）；跳过类/属性声明
                continue
            if set(props) == {"label"} or list(props) == ["label"]:
                continue  # 类声明节点（仅 rdfs:label），非实例，跳过
            parts = [name]
            for rel, vals in props.items():
                cn = en2cn.get(rel, rel) if isinstance(en2cn, dict) else rel
                for v in (vals if isinstance(vals, list) else [vals]):
                    parts.append("%s:%s" % (cn, v))
            candidates.append((name, " ".join(parts)))
        # 规模上限：超过则随机抽样，控制 embedding 调用次数
        if max_entries and max_entries > 0 and len(candidates) > max_entries:
            candidates = random.sample(candidates, max_entries)
        # 超时防护：构建超过 build_timeout 秒则放弃向量索引，降级纯 BM25
        deadline = time.monotonic() + (build_timeout or 0)
        for name, text in candidates:
            if build_timeout and build_timeout > 0 and time.monotonic() > deadline:
                return cls()  # 超时：放弃向量索引，返回空索引（BM25 兜底）
            vec = embed_text(text, base=base, model=model)
            if vec is None:
                continue
            vx.entries.append({"entity": name, "text": text, "vector": vec})
        vx._built = True
        return vx

    @classmethod
    def from_nt(cls, nt_file, lexicon=None, base=OLLAMA_BASE, model=EMBED_MODEL,
                max_entries=2000, build_timeout=5.0):
        """直接由 .nt 文件建向量索引（独立使用/测试入口）。"""
        import graph_rag as gr
        graph, _, _, _ = gr.build_graph(nt_file)
        return cls.from_graph(graph, lexicon=lexicon, base=base, model=model,
                              max_entries=max_entries, build_timeout=build_timeout)

    def search(self, query, top_k=5, min_score=0.0):
        """embedding 问题 → 余弦相似度召回 top-k 语义相近实体。
        返回 [{entity, score, text}] 降序。embedding 失败/无索引返回 []。"""
        if not self.entries:
            return []
        qv = embed_text(query)
        if qv is None:
            return []
        scored = [(_cosine(qv, e["vector"]), e) for e in self.entries]
        scored.sort(key=lambda x: -x[0])
        return [{"entity": e["entity"], "score": round(s, 4), "text": e["text"][:60]}
                for s, e in scored[:top_k] if s >= min_score]


def main():
    import sys as _sys
    if len(_sys.argv) < 4:
        print("用法: python vector_retrieval.py <nt文件> <lexicon.json> \"<问题>\" [top_k]")
        _sys.exit(1)
    nt_file, lex_file, question = _sys.argv[1], _sys.argv[2], _sys.argv[3]
    top_k = int(_sys.argv[4]) if len(_sys.argv) > 4 else 5
    lex = json.load(open(lex_file, encoding="utf-8")) if os.path.exists(lex_file) else None
    vx = VectorIndex.from_nt(nt_file, lexicon=lex)
    print("已建向量索引实体数:", len(vx.entries))
    for h in vx.search(question, top_k=top_k):
        print("  %.4f  %s  %s" % (h["score"], h["entity"], h["text"]))


if __name__ == "__main__":
    main()
