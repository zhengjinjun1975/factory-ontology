#!/usr/bin/env python3
"""bm25_retrieval.py — 轻量 BM25 混合检索（纯标准库零依赖）

对知识图谱的文本内容建倒排索引，给定问题返回 top-K 相关实体/三元组。
作为"稀疏检索"层，补充图遍历检索，提升模糊/自然语言查询的召回。

中文处理：字符 unigram + 双字 bigram 混合分词（无需 jieba，纯标准库）。

用法（被 api_server 集成）：
    from bm25_retrieval import BM25Index
    bm = BM25Index.from_graph(graph)       # graph = build_graph(nt) 的 {node: {rel: [vals]}}
    hits = bm.search("保质期最长的产品是什么", top_k=5)
"""
import math
import re
from collections import defaultdict

# 中文分词：英文/数字保留，中文按 unigram+bigram
_RE_TOKEN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list:
    """混合分词：英文/数字词 + 中文单字，并对中文相邻字组 bigram。"""
    text = text.lower()
    toks = _RE_TOKEN.findall(text)
    out = []
    han = ""
    for t in toks:
        if re.fullmatch(r"[\u4e00-\u9fff]", t):
            han += t
        else:
            out.append(t)
    if len(han) >= 1:
        # unigram + bigram
        out.extend(han)
        for i in range(len(han) - 1):
            out.append(han[i:i + 2])
    return out


class BM25Index:
    """纯标准库 BM25。k1=1.5, b=0.75 为标准参数。"""

    def __init__(self):
        self.docs = []          # list of dict(文本, 实体)  实体=节点tail
        self.avgdl = 0.0
        self.df = defaultdict(int)   # 词 -> 含该词的文档数
        self.idf = {}
        self.doc_tokens = []    # list of Counter
        self._indexed = False

    @classmethod
    def from_graph(cls, graph, node_count=None):
        """从 build_graph 返回的 {node: {rel: [vals]}} 建索引。
        每个实体节点 = 一条文档(其名字+所有属性值)。"""
        bm = cls()
        for node, props in graph.items():
            from graph_rag import tail
            name = tail(node)
            if name.startswith("__") or name.startswith("//"):
                continue
            parts = [name]
            for rel, vals in props.items():
                for v in (vals if isinstance(vals, list) else [vals]):
                    parts.append(str(v))
            bm.add_document(" ".join(parts), name)
        bm.build()
        return bm

    def add_document(self, text: str, entity: str):
        self.docs.append({"text": text, "entity": entity})
        self.doc_tokens.append(defaultdict(int))
        for t in tokenize(text):
            self.doc_tokens[-1][t] += 1

    def build(self):
        n = len(self.docs)
        if n == 0:
            self._indexed = True
            return
        self.avgdl = sum(sum(d.values()) for d in self.doc_tokens) / n
        for d in self.doc_tokens:
            for t in d:
                self.df[t] += 1
        for t, df in self.df.items():
            # BM25 IDF，避免负值
            self.idf[t] = math.log(1 + (n - df + 0.5) / (df + 0.5))
        self._indexed = True

    def search(self, query: str, top_k: int = 5, min_score: float = 0.0) -> list:
        """返回 [{entity, score, text}] 按相关性降序，过滤低于 min_score 的噪音。"""
        if not self._indexed or not self.docs:
            return []
        q_terms = tokenize(query)
        scores = [0.0] * len(self.docs)
        for t in q_terms:
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, dt in enumerate(self.doc_tokens):
                tf = dt.get(t, 0)
                if tf == 0:
                    continue
                dl = sum(dt.values())
                scores[i] += idf * (tf * (1.5 + 1)) / (tf + 1.5 * (1 - 0.75 + 0.75 * dl / self.avgdl))
        ranked = sorted(zip(scores, self.docs), key=lambda x: -x[0])
        return [{"entity": d["entity"], "score": round(s, 4), "text": d["text"][:60]}
                for s, d in ranked[:top_k] if s >= min_score]


if __name__ == "__main__":
    # 自测
    g = {
        "http://factory.example/ontology#Food_products_P001": {
            "productName": ["原味酸奶"], "保质期": ["15天"]},
        "http://factory.example/ontology#Food_products_P003": {
            "productName": ["全麦面包"], "保质期": ["7天"]},
        "http://factory.example/ontology#Food_products_P005": {
            "productName": ["手工水饺"], "保质期": ["180天"]},
    }
    bm = BM25Index.from_graph(g)
    for q in ["保质期最长的", "面包", "酸奶"]:
        print(f"问 [{q}] →", [h["entity"] for h in bm.search(q)])
