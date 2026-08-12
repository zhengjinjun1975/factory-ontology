#!/usr/bin/env python3
"""knowledge/store.py — 知识库存储（ChromaDB 专业向量库）

把 文档→切块→向量 落盘为 ChromaDB 持久化向量库：每个知识库对应一个
PersistentClient 目录下的同名 collection，用 HNSW 索引做余弦语义检索，
替代原 JSON 文件 + 暴力余弦扫描（O(N) 线性遍历）的实现。

本模块对外接口与原实现完全一致（add_doc/search/list_docs/delete），
不依赖内部存储形态，因此不影响 knowledge/rag.py 与 /api/knowledge/* 的调用。

用法:
    from knowledge.store import KnowledgeStore
    kb = KnowledgeStore("kb")            # kb/ 目录下的 chroma.sqlite3
    kb.add_doc("doc1", "技术协议", chunks, vectors)
    hits = kb.search(query_vec, top_k=5) # [{doc_id,title,chunk,score}]
    kb.list_docs(); kb.delete("doc1")

依赖: chromadb（若未安装则各方法安全返回空/False，不抛异常）。
"""
import os

try:
    import chromadb
    _CHROMA = True
except Exception:               # pragma: no cover - chromadb 缺失时优雅降级
    _CHROMA = False


def _sim(distance):
    """ChromaDB 余弦距离 → 相似度（cosine 空间距离 ∈ [0,2]，相似度 = 1 - 距离）。"""
    try:
        return 1.0 - float(distance)
    except Exception:
        return 0.0


class KnowledgeStore:
    """基于 ChromaDB 持久化向量库的知识库存储。线程安全（由 ChromaDB 内部保证）。"""

    def __init__(self, kb_dir):
        """kb_dir: 知识库目录（ChromaDB 持久化目录，不存在则创建）。

        collection 名取目录 basename（合法化后），索引用余弦距离（HNSW）。
        """
        self.kb_dir = kb_dir
        self._col = None
        self._ready = False
        try:
            os.makedirs(kb_dir, exist_ok=True)
        except Exception:
            pass
        if not _CHROMA:
            return
        try:
            name = os.path.basename(kb_dir.rstrip("/\\")) or "kb"
            # Chroma collection 名仅允许字母/数字/下划线/连字符
            name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
            client = chromadb.PersistentClient(path=kb_dir)
            self._col = client.get_or_create_collection(
                name, metadata={"hnsw:space": "cosine"})
            self._ready = True
        except Exception:
            self._col = None
            self._ready = False

    def add_doc(self, doc_id, title, chunks, vectors):
        """入库一篇文档（ChromaDB upsert）。

        参数:
            doc_id: 文档唯一 id
            title: 文档标题
            chunks: list[dict]（chunk 模块产物，含 text）
            vectors: list[list[float]] 与 chunks 一一对应
        返回:
            bool 是否成功；异常/参数非法/未就绪返回 False。
        """
        try:
            if not self._ready or not doc_id or not chunks or not vectors:
                return False
            if len(chunks) != len(vectors):
                return False
            # 幂等覆盖：先删该文档旧向量，再整体 upsert
            try:
                self._col.delete(where={"doc_id": doc_id})
            except Exception:
                pass
            ids = ["%s#%s" % (doc_id, i) for i in range(len(chunks))]
            docs = []
            metas = []
            embs = []
            for i, c in enumerate(chunks):
                text = (c.get("text") or "") if isinstance(c, dict) else str(c)
                docs.append(text)
                metas.append({
                    "doc_id": doc_id,
                    "title": title or doc_id,
                    "chunk": i,
                    "char_start": c.get("char_start", 0) if isinstance(c, dict) else 0,
                    "char_end": c.get("char_end", 0) if isinstance(c, dict) else 0,
                })
                embs.append(vectors[i])
            self._col.upsert(
                ids=ids, embeddings=embs, documents=docs, metadatas=metas)
            return True
        except Exception:
            return False

    def search(self, query_vec, top_k=5, min_score=0.0):
        """按问题向量检索 top_k 相关切块（HNSW 余弦语义检索）。

        返回:
            list[dict] 按相似度降序，每项 {"doc_id","title","chunk","score"}；
            无查询向量/未就绪返回 []。
        """
        try:
            if not self._ready or not query_vec:
                return []
            n = max(1, int(top_k))
            res = self._col.query(
                query_embeddings=[query_vec], n_results=n,
                include=["documents", "metadatas", "distances"])
            ids = (res.get("ids") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            out = []
            for i in range(len(ids)):
                s = _sim(dists[i])
                if s < min_score:
                    continue
                meta = metas[i] if isinstance(metas[i], dict) else {}
                out.append({
                    "doc_id": meta.get("doc_id") or ids[i].split("#")[0],
                    "title": meta.get("title") or ids[i].split("#")[0],
                    "chunk": docs[i] or meta.get("chunk") or "",
                    "score": round(s, 4),
                })
            return out
        except Exception:
            return []

    def list_docs(self):
        """列出库内文档元信息。返回 list[dict] 或 []。"""
        try:
            if not self._ready:
                return []
            data = self._col.get(include=["metadatas"])
            metas = data.get("metadatas") or []
            agg = {}
            for m in metas:
                if not isinstance(m, dict):
                    continue
                did = m.get("doc_id")
                if not did:
                    continue
                if did not in agg:
                    agg[did] = {"title": m.get("title", did), "chunks": 0}
                agg[did]["chunks"] += 1
            return [
                {"doc_id": did, "title": v["title"], "chunks": v["chunks"]}
                for did, v in agg.items()
            ]
        except Exception:
            return []

    def delete(self, doc_id):
        """删除一篇文档（按 doc_id 元数据过滤删除全部向量）。返回 bool。"""
        try:
            if not self._ready or not doc_id:
                return False
            self._col.delete(where={"doc_id": doc_id})
            return True
        except Exception:
            return False
