#!/usr/bin/env python3
"""knowledge/store.py — 知识库存储（纯 JSON 文件）

把 文档→切块→向量 落盘为 JSON 文件知识库：kb_dir/index.json 存元信息与切块文本，
向量单独存 kb_dir/vectors/<doc_id>.json，避免主索引过大。
支持文档增删查、向量余弦检索（纯标准库）。

用法:
    from knowledge.store import KnowledgeStore
    kb = KnowledgeStore("kb")                # kb/index.json + kb/vectors/
    kb.add_doc("doc1", "技术协议", chunks, vectors)
    hits = kb.search(query_vec, top_k=5)     # [{doc_id,title,chunk,score}]
    kb.list_docs(); kb.delete("doc1")
"""
import os
import json


def _cosine(a, b):
    """余弦相似度（纯标准库）。任一为空/长度不等返回 0.0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class KnowledgeStore:
    """基于 JSON 文件的知识库存储。线程不安全，单进程使用。"""

    def __init__(self, kb_dir):
        """kb_dir: 知识库目录（不存在则创建）。"""
        self.kb_dir = kb_dir
        self.index_path = os.path.join(kb_dir, "index.json")
        self.vec_dir = os.path.join(kb_dir, "vectors")
        try:
            os.makedirs(self.vec_dir, exist_ok=True)
        except Exception:
            pass
        self._index = {"docs": {}}   # doc_id -> {title, chunks:[{index,text,...}], count}
        self._load()

    def _load(self):
        """加载已有 index.json（异常则从空索引开始，不抛）。"""
        try:
            if os.path.exists(self.index_path):
                with open(self.index_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and isinstance(data.get("docs"), dict):
                        self._index = data
        except Exception:
            self._index = {"docs": {}}

    def _save(self):
        """写 index.json（异常静默，不阻塞主流程）。"""
        try:
            with open(self.index_path, "w", encoding="utf-8") as f:
                json.dump(self._index, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add_doc(self, doc_id, title, chunks, vectors):
        """入库一篇文档。

        参数:
            doc_id: 文档唯一 id
            title: 文档标题
            chunks: list[dict]（chunk 模块产物，含 text）
            vectors: list[list[float]] 与 chunks 一一对应
        返回:
            bool 是否成功；异常/参数非法返回 False。
        """
        try:
            if not doc_id or not chunks or not vectors or len(chunks) != len(vectors):
                return False
            chunks_safe = []
            for c in chunks:
                chunks_safe.append({
                    "index": c.get("index", 0),
                    "text": c.get("text", ""),
                    "char_start": c.get("char_start", 0),
                    "char_end": c.get("char_end", 0),
                } if isinstance(c, dict) else {"index": 0, "text": str(c)})
            # 向量单独落盘
            vpath = os.path.join(self.vec_dir, "%s.json" % doc_id)
            with open(vpath, "w", encoding="utf-8") as f:
                json.dump(vectors, f, ensure_ascii=False)
            self._index["docs"][doc_id] = {
                "title": title or doc_id,
                "count": len(chunks),
                "chunks": chunks_safe,
            }
            self._save()
            return True
        except Exception:
            return False

    def _load_vectors(self, doc_id):
        """读某文档向量文件。缺失/异常返回 []。"""
        try:
            vpath = os.path.join(self.vec_dir, "%s.json" % doc_id)
            if os.path.exists(vpath):
                with open(vpath, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def search(self, query_vec, top_k=5, min_score=0.0):
        """按问题向量检索 top_k 相关切块。

        返回:
            list[dict] 按分数降序，每项 {"doc_id","title","chunk","score"}；
            无查询向量/无文档返回 []。
        """
        try:
            if not query_vec or not self._index.get("docs"):
                return []
            scored = []
            for doc_id, meta in self._index["docs"].items():
                vectors = self._load_vectors(doc_id)
                chunks = meta.get("chunks", [])
                for i, vec in enumerate(vectors):
                    s = _cosine(query_vec, vec)
                    if s >= min_score:
                        chunk_text = ""
                        if i < len(chunks):
                            chunk_text = chunks[i].get("text", "")
                        scored.append((s, {
                            "doc_id": doc_id,
                            "title": meta.get("title", doc_id),
                            "chunk": chunk_text,
                            "score": round(s, 4),
                        }))
            scored.sort(key=lambda x: -x[0])
            return [item for _, item in scored[:top_k]]
        except Exception:
            return []

    def list_docs(self):
        """列出库内文档元信息。返回 list[dict] 或 []。"""
        try:
            return [
                {"doc_id": did, "title": meta.get("title", did),
                 "chunks": meta.get("count", len(meta.get("chunks", [])))}
                for did, meta in self._index.get("docs", {}).items()
            ]
        except Exception:
            return []

    def delete(self, doc_id):
        """删除一篇文档（索引 + 向量文件）。返回 bool。"""
        try:
            if doc_id not in self._index.get("docs", {}):
                return False
            del self._index["docs"][doc_id]
            vpath = os.path.join(self.vec_dir, "%s.json" % doc_id)
            if os.path.exists(vpath):
                try:
                    os.remove(vpath)
                except Exception:
                    pass
            self._save()
            return True
        except Exception:
            return False
