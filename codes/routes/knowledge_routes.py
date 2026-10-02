# -*- coding: utf-8 -*-
"""knowledge 路由族 — 从 api_server.py 抽出的 APIRouter（代码审查 P1-1）。

函数体逐字搬运；对 api_server 模块级名字改写成 ``core.X``（18 个），
fastapi/pydantic 导入名在本模块显式重新导入；路由路径/鉴权依赖/返回结构不变。
回归门：codes/tests 单测 + codes/e2e_test.py + 路由清单逐条比对。
"""

from fastapi import Depends, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse

from fastapi import APIRouter

import api_server as core

router = APIRouter()


@router.post("/api/knowledge/ingest", dependencies=[Depends(core.require_key)])
async def knowledge_ingest(file: UploadFile = File(...),
                           kb: str = Form("food"),
                           doc_id: str = Form("")):
    """上传文档(PDF/Word/TXT) → 解析+切块+向量化+入库。同 doc_id 幂等覆盖。"""
    start = core.time.time()
    core._kb_guard(kb)  # 多租户: 越权 KB → 403(拒绝)
    try:
        from knowledge.ingest import extract_text
        from knowledge.chunk import chunk_text
        from knowledge.embed import embed_chunks
        from knowledge.store import KnowledgeStore
    except Exception as e:
        core.logger.warning(f"API内部错误[知识引擎不可用]: {e}")
        return core._err_env(5001, "知识引擎不可用(内部错误已记录)", start)
    fname = file.filename or "upload.txt"
    ext = core.os.path.splitext(fname)[1].lower()
    if ext not in (".pdf", ".doc", ".docx", ".txt"):
        return core._err_env(4001, f"仅支持 PDF/Word/TXT, 收到: {ext or '未知扩展名'}", start)
    # 体积上限(第2轮, 可配 FOOD_MAX_UPLOAD_MB): 分块读入, 超限 413 且不落盘
    # (在创建临时文件之前就中止, 临时目录不留残留)。读入方式不得无界。
    try:
        _body = await core._read_upload_capped(file)
    except core.UploadTooLarge:
        return JSONResponse(core._err_env(4001, "文件过大: 超过上限 %.0fMB" % core.MAX_UPLOAD_MB, start),
                            status_code=413)
    kbdir = core._kb_dir(kb)
    if kbdir is None:
        return core._err_env(4001, "非法 kb 名", start)
    tmp = core.os.path.join(core._TMP_UPLOAD, f"{core.time.time_ns()}{ext}")
    try:
        try:
            core.os.makedirs(core._TMP_UPLOAD, exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(_body)
            doc = extract_text(tmp)
            if not doc:
                return core._err_env(4001, "文档解析失败(缺解析库或内容为空), 未入库", start)
            # 用用户上传的原始文件名(去扩展名)作为标题, 便于辨识/删除,
            # 避免 extract_text 默认用时间戳临时文件名(如 {time_ns()})做 title。
            doc["title"] = core.os.path.splitext(fname)[0]
            chunks = chunk_text(doc["raw_text"])
            if not chunks:
                return core._err_env(4001, "文档切块为空, 未入库", start)
            vectors = embed_chunks(chunks)
            if not vectors:
                return core._err_env(5031, "embedding 服务不可用(未产出向量), 文档未入库", start)
            did = core._safe_doc_id(doc_id.strip()) or "%s_%s" % (
                doc["title"], core.hashlib.md5(doc["raw_text"].encode("utf-8")).hexdigest()[:8])
            store = KnowledgeStore(kbdir)
            if not store.add_doc(did, doc["title"], chunks, vectors):
                return core._err_env(5001, "文档入库失败", start)
            return core._ok_env({"kb": kb, "doc_id": did, "title": doc["title"],
                            "chunks": len(chunks), "status": "stored"}, start)
        finally:
            if core.os.path.exists(tmp):
                try:
                    core.os.remove(tmp)
                except Exception as e:
                    core.note_swallow("knowledge_ingest", e)
                    pass
    except Exception as e:
        core.logger.warning(f"API内部错误[文档接入失败]: {e}")
        return core._err_env(5001, "文档接入失败(内部错误已记录)", start)


@router.post("/api/knowledge/query", dependencies=[Depends(core.require_key)])
def knowledge_query(req: core.KnowledgeQueryReq):
    """文档 RAG 检索。body {kb, q, top_k?} → {answer, evidence}。"""
    start = core.time.time()
    core._kb_guard(req.kb)  # 多租户: 越权 KB → 403(拒绝)
    try:
        from knowledge.rag import answer as rag_answer
        from knowledge.store import KnowledgeStore
    except Exception as e:
        core.logger.warning(f"API内部错误[知识引擎不可用]: {e}")
        return core._err_env(5001, "知识引擎不可用(内部错误已记录)", start)
    if not (req.q or "").strip():
        return core._err_env(4001, "缺少 q", start)
    kbdir = core._kb_dir(req.kb)
    if kbdir is None:
        return core._err_env(4001, "非法 kb 名", start)
    try:
        store = KnowledgeStore(kbdir)
        res = rag_answer(None, req.q, store, top_k=max(1, min(req.top_k, 20)))
    except Exception as e:
        core.logger.warning(f"API内部错误[检索失败]: {e}")
        return core._err_env(5001, "检索失败(内部错误已记录)", start)
    ans = res.get("answer", "")
    if ans.startswith("[模型未配置]"):
        return core._err_env(5031, "模型未配置", start)
    return core._ok_env({"kb": req.kb, "answer": ans, "evidence": res.get("evidence", [])}, start)


@router.get("/api/knowledge/list", dependencies=[Depends(core.require_key)])
def knowledge_list(kb: str = Query("food")):
    """列出某 kb 的已入库文档。"""
    start = core.time.time()
    core._kb_guard(kb)  # 多租户: 越权 KB → 403(拒绝)
    try:
        from knowledge.store import KnowledgeStore
    except Exception as e:
        core.logger.warning(f"API内部错误[知识引擎不可用]: {e}")
        return core._err_env(5001, "知识引擎不可用(内部错误已记录)", start)
    kbdir = core._kb_dir(kb)
    if kbdir is None:
        return core._err_env(4001, "非法 kb 名", start)
    try:
        docs = KnowledgeStore(kbdir).list_docs()
    except Exception as e:
        core.logger.warning(f"API内部错误[读取失败]: {e}")
        return core._err_env(5001, "读取失败(内部错误已记录)", start)
    return core._ok_env({"kb": kb, "docs": docs}, start)


@router.post("/api/knowledge/delete", dependencies=[Depends(core.require_key)])
def knowledge_delete(req: core.KnowledgeDeleteReq):
    """删除某 kb 下的一篇文档。幂等: 重复删除已不存在文档返回 4041。"""
    start = core.time.time()
    core._kb_guard(req.kb)  # 多租户: 越权 KB → 403(拒绝)
    try:
        from knowledge.store import KnowledgeStore
    except Exception as e:
        core.logger.warning(f"API内部错误[知识引擎不可用]: {e}")
        return core._err_env(5001, "知识引擎不可用(内部错误已记录)", start)
    kbdir = core._kb_dir(req.kb)
    if kbdir is None:
        return core._err_env(4001, "非法 kb 名", start)
    try:
        ok = KnowledgeStore(kbdir).delete(req.doc_id)
    except Exception as e:
        core.logger.warning(f"API内部错误[删除失败]: {e}")
        return core._err_env(5001, "删除失败(内部错误已记录)", start)
    if not ok:
        return core._err_env(4041, f"文档不存在: {req.doc_id}", start)
    return core._ok_env({"kb": req.kb, "deleted": req.doc_id}, start)
