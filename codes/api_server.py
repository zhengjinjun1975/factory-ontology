#!/usr/bin/env python3
"""api_server.py — 食品企业知识库 REST API

在已验证的 factory-ontology 之上提供 REST 接口，是 APP / 语音 / Web 的统一入口：
- 规则问答（ontology_qa_v3，确定性）
- 正/反向溯源（graph_rag，食品安全核心）
- 扫码溯源（code -> 产品批次 -> 原料）
- 统计

用法:
  python api_server.py               # 启动 http://localhost:8000
  uvicorn api_server:app --port 8000

需: pip install fastapi uvicorn   （Python 3.9+）
"""
import os
import re
import sys
import json
import time
import shutil
import hashlib
import logging
import threading
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# ── 结构化日志 ──
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("food-api")

from fastapi import FastAPI, HTTPException, Query, Header, Request, Depends, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse
from pydantic import BaseModel

import graph_rag as gr
import ontology_qa_v3 as v3
import multi_table as mt

# ── 食品知识库配置 ──
NS = "http://factory.example/ontology#"   # 与 multi_table 建本体一致
# 多租户隔离(T-D1): 从 kbs.json 注册表选知识库, 每企业隔离数据目录/词典
KB_NAME = os.environ.get("FOOD_KB", "food")
KBS_FILE = os.path.join(ROOT, "config", "kbs.json")


def _load_kbs():
    try:
        return json.load(open(KBS_FILE, encoding="utf-8")).get("kbs", {})
    except Exception:
        return {}


KBS = _load_kbs()
_kb = KBS.get(KB_NAME, {})
DATA = os.environ.get("FOOD_DATA_DIR", os.path.join(ROOT, _kb.get("data_dir", "data")))
FOOD_NT = os.environ.get("FOOD_NT", os.path.join(ROOT, "output", f"{KB_NAME}.nt"))
FOOD_LEX = os.environ.get("FOOD_LEX", os.path.join(ROOT, "config", _kb.get("lexicon", "lexicon_food_products.json")))


def _find(tail_name):
    """按尾部名找图内实体 URI（跨命名空间）。"""
    for k in graph:
        if gr.tail(k) == tail_name:
            return k
    return None


def _data_hash():
    """数据文件集合的哈希(用于增量重建检测)。"""
    h = hashlib.md5()
    for f in sorted(os.listdir(DATA)):
        if f.startswith("food_") and f.endswith(".csv"):
            h.update(open(os.path.join(DATA, f), "rb").read())
    return h.hexdigest()


def _has_required_relations(nt_file):
    """校验缓存本体含跨表对象属性(produces/belongsToBatch/usesRawMaterial)。缺失=被污染/损坏, 需重建。"""
    if not os.path.exists(nt_file):
        return False
    try:
        txt = open(nt_file, encoding="utf-8").read()
        return all(r in txt for r in ("#produces", "#belongsToBatch", "#usesRawMaterial"))
    except Exception:
        return False


def _ensure_food_ontology():
    """若本体已存在且数据未变且关系完整则复用(增量); 否则重建。"""
    cur = _data_hash()
    state = os.path.join(os.path.dirname(FOOD_NT), "food_data_hash.txt")
    prev = open(state).read().strip() if os.path.exists(state) else ""
    if os.path.exists(FOOD_NT) and prev == cur and _has_required_relations(FOOD_NT):
        logger.info("本体已是最新(数据未变+关系完整), 复用缓存, 增量模式")
        return
    logger.info("检测到数据变化/首次构建/关系缺失, 重建本体...")
    def load(t):
        return mt.load_table(os.path.join(DATA, f"{t}.csv"))
    tables = {}
    for t, idc in [("food_products","id"),("food_raw_materials","id"),("food_batches","id"),
                   ("food_batch_ingredient","batch_id"),("food_qc","id"),("food_equipment","id")]:
        n, h, rows = load(t)
        tables[n] = {"headers": h, "rows": rows, "id_col": idc}
    rels = {
        "food_batches": {"product_id": {"target_class":"Food_products","rel":"http://food.example/ontology#produces","label":"生产产品"}},
        "food_batch_ingredient": {
            "batch_id": {"target_class":"Food_batches","rel":"http://food.example/ontology#belongsToBatch","label":"属于批次"},
            "raw_id": {"target_class":"Food_raw_materials","rel":"http://food.example/ontology#usesRawMaterial","label":"使用原料"}},
    }
    os.makedirs(os.path.dirname(FOOD_NT), exist_ok=True)
    mt.build_nt(tables, rels, FOOD_NT)
    open(state, "w").write(cur)  # 记录当前数据hash, 下次比对


def _load():
    _ensure_food_ontology()
    graph, labels, vi, rev = gr.build_graph(FOOD_NT)
    return graph, labels, vi, rev


graph, labels, vi, rev = _load()
D = v3.load_dict(FOOD_LEX)
QDATA = v3.build_data(v3.parse_nt(FOOD_NT), D)
_KB_INDEX_CACHE = {}  # 多租户: {kb} 独立的 BM25/向量索引缓存


def _warm_embedding():
    """后台预热 embedding 模型(nomic-embed-text), 避免首次查询冷加载卡住。

    冷启动时 Ollama 首次拉 274MB 模型进显存耗时, 首查会慢(记忆经验: 超15s)。
    启动即后台预加载, 用户首查时模型已就绪。失败静默(不阻塞启动)。
    """
    try:
        from vector_retrieval import embed_text, EMBED_MODEL
        embed_text("预热 embedding 模型", model=EMBED_MODEL)
        logger.info(f"embedding 模型预热完成: {EMBED_MODEL}")
    except Exception:
        pass  # 预热失败静默, 不阻塞服务启动

app = FastAPI(title="食品企业知识库 API", version="0.1.4",
              description="本体驱动的食品企业问答 + 溯源检索（中小型食品企业场景）")

# ── 托管移动端食品溯源 APP（与 API 同源，一套部署） ──
FOOD_APP_HTML = os.path.join(os.path.dirname(ROOT), "web", "food_app", "index.html")
ADMIN_HTML = os.path.join(os.path.dirname(ROOT), "web", "admin.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    """管理后台页(需要 admin Key 调 /api/admin/*, 页面本身静态)。"""
    if os.path.exists(ADMIN_HTML):
        return HTMLResponse(open(ADMIN_HTML, encoding="utf-8").read())


@app.get("/api/ontology/structure")
def ontology_structure():
    """本体建模视图数据：类 + Is-A 类别层级(subClassOf) + 对象属性关系 + 实例数。
    优先读深化本体(含 subClassOf 类别层级), 回退当前活动本体。"""
    from ontology_qa_v3 import parse_nt
    deep_nt = os.path.join(ROOT, "output", "food_deep.nt")
    nt_file = deep_nt if os.path.exists(deep_nt) else FOOD_NT
    triples = parse_nt(nt_file)
    classes, subcls, objprops = [], [], []
    seen = set()
    OWL_CLASS = "http://www.w3.org/2002/07/owl#Class"
    for s, p, o in triples:
        oo = str(o).strip("<>")
        if oo == OWL_CLASS:
            nm = s.split("#")[-1].strip("<>")
            if nm and nm not in seen:
                classes.append(nm); seen.add(nm)
        elif "subClassOf" in p:
            subcls.append((s.split("#")[-1].strip("<>"), o.split("#")[-1].strip("<>")))
        elif "ObjectProperty" in str(o):
            nm = s.split("#")[-1].strip("<>")
            if nm not in objprops: objprops.append(nm)
    return {"ok": True, "classes": sorted(classes), "subclass_of": sorted(subcls),
            "object_properties": sorted(objprops), "instance_total": len(graph),
            "nt_file": os.path.basename(nt_file)}


@app.get("/api/ontology/graph-svg", include_in_schema=False)
def ontology_graph_svg():
    """返回企业本体大图 SVG(企业与客户关系 + 本体层次 Is-A)。"""
    svg = os.path.join(ROOT, "..", "docs", "diagrams", "ontology-大图.svg")
    if os.path.exists(svg):
        return HTMLResponse(open(svg, encoding="utf-8").read())
    return HTMLResponse("<div>大图未生成</div>")


@app.get("/api/ontology/graph")
def ontology_graph():
    """本体完整图(节点+边)，供前端 ECharts 动态大图渲染(仿 sme-decision-ontology /graph/full)。"""
    nodes, edges = [], []
    seen_edges = set()
    RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
    # 实体类(从URI前缀推断: NS + <类名>_<实例id>); 仅保留实例节点(带 _ 的)
    for uri, props in graph.items():
        if uri == RDF_TYPE:
            continue
        nm = labels.get(uri, uri.split("#")[-1].strip("<>"))
        local = uri.split("#")[-1].strip("<>")
        if "_" not in local:
            continue  # 跳过属性/关系/类声明节点(无实例id)
        entity = local.rsplit("_", 1)[0]
        nodes.append({"id": uri, "name": nm, "entity": entity})
    # 边(对象属性: 目标是实体URI)
    node_ids = {n["id"] for n in nodes}
    for uri, props in graph.items():
        if uri not in node_ids:
            continue
        for rel, vals in props.items():
            if "type" in rel or "label" in rel or "domain" in rel or "range" in rel:
                continue
            for v in vals:
                vv = str(v).strip("<>")
                if vv in node_ids and (uri, vv) not in seen_edges:
                    edges.append({"from": uri, "to": vv, "rel": rel})
                    seen_edges.add((uri, vv))
    return {"ok": True, "nodes": nodes, "edges": edges,
            "counts": {"nodes": len(nodes), "edges": len(edges)}}


@app.get("/", include_in_schema=False)
def app_home():
    """食品溯源移动端 APP 首页。"""
    if os.path.exists(FOOD_APP_HTML):
        return FileResponse(FOOD_APP_HTML, media_type="text/html")
    return HTMLResponse("食品溯源 APP 未找到（web/food_app/index.html）", status_code=404)


class AskReq(BaseModel):
    question: str
    kb: str = ""  # 多租户: 指定知识库; 缺省用 FOOD_KB(默认 food), 兼容旧调用


# ── 多租户惰性加载(T-D1 彻底化): 按 kb 加载本体/词典, 缓存多库, 根治串台 ──
_kb_ctx_cache = {}


def _get_kb_ctx(kb=None):
    """按 kb 惰性加载并缓存该知识库的本体图+词典+问答数据。

    返回 {graph, labels, vi, rev, D, QDATA, nt_file, lex_file, kb}。
    首次访问某 kb 才构建并缓存；kb 无效或数据缺失返回 None。
    """
    kb = (kb or KB_NAME or "food").strip()
    if kb in _kb_ctx_cache:
        return _kb_ctx_cache[kb]
    kbc = KBS.get(kb)
    if not kbc:
        return None
    nt_file = os.path.join(ROOT, kbc.get("nt", f"output/{kb}.nt"))
    lex_file = os.path.join(ROOT, "config", kbc.get("lexicon", f"lexicon_{kb}.json"))
    if not os.path.exists(nt_file) or not os.path.exists(lex_file):
        return None
    try:
        g, lb, v, rv = gr.build_graph(nt_file)
        D = v3.load_dict(lex_file)
        QD = v3.build_data(v3.parse_nt(nt_file), D)
    except Exception as e:
        logger.warning(f"kb '{kb}' 加载失败: {e}")
        return None
    ctx = {"graph": g, "labels": lb, "vi": v, "rev": rv,
           "D": D, "QDATA": QD, "nt_file": nt_file, "lex_file": lex_file, "kb": kb}
    _kb_ctx_cache[kb] = ctx
    return ctx


# ── 角色化鉴权(M1.2): FOOD_ADMIN_KEY 管理 / FOOD_READ_KEY 只读 ──
# 安全加固(2026-08-12, 架构师审计 P0-1): fail-closed 默认拒绝, 不再无 key 开放。
ADMIN_KEY = os.environ.get("FOOD_ADMIN_KEY", "").strip()
READ_KEY = os.environ.get("FOOD_READ_KEY", "").strip()


def _valid(key, target):
    """key 是否匹配目标(或已配置的角色 key)。用常量时间比较防时序侧信道。"""
    if not target:
        return False
    try:
        import hmac
        return hmac.compare_digest(key, target)
    except Exception:
        return key == target


def require_key(x_api_key: str = Header(default="")):
    """只读端点鉴权(fail-closed): 需匹配 read 或 admin key; 未配置或未匹配一律 401。"""
    if not _valid(x_api_key, ADMIN_KEY) and not _valid(x_api_key, READ_KEY):
        raise HTTPException(401, "无效或缺失 API Key (需 X-API-Key 头)")


def require_admin(x_api_key: str = Header(default="")):
    """管理端点鉴权(fail-closed): 需匹配 admin key。"""
    if not _valid(x_api_key, ADMIN_KEY):
        raise HTTPException(401, "需要管理权限 (admin API Key)")

# ── 请求计数(M1.3 metrics) + 审计日志(T3.1) ──
from collections import Counter as _Counter
REQ_COUNT = _Counter()
AUDIT_FILE = os.path.join(ROOT, "output", "audit.log")


def _audit(record):
    """追加审计日志(JSONL)。"""
    try:
        os.makedirs(os.path.dirname(AUDIT_FILE), exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


@app.middleware("http")
async def audit_and_count(request: Request, call_next):
    REQ_COUNT[request.url.path] += 1
    start = time.time()
    response = await call_next(request)
    _audit({
        "ts": datetime.now().isoformat(),
        "method": request.method,
        "path": request.url.path,
        "status": response.status_code,
        "client": request.client.host if request.client else "",
        "ms": int((time.time() - start) * 1000),
    })
    return response


@app.get("/api/export/reverse", dependencies=[Depends(require_key)])
def export_reverse(raw: str = Query(..., description="原料编号，如 RM008"), fmt: str = Query("csv", pattern="^(csv|txt)$")):
    """溯源报告导出: 原料 → 受影响批次 → 产品(食品召回/合规)。"""
    data = _reverse_trace(raw)
    raw_name = _resolve_readable(data["raw_material"])
    lines = [["原料", raw_name], [], ["受影响批次", "产品", "生产日期"]]
    for ab in data["affected_batches"]:
        prod = _resolve_readable(ab["product"]) if ab.get("product") else ""
        lines.append([ab["batch"], prod, ab.get("produce_date", "")])
    if fmt == "txt":
        body = "\n".join("\t".join(map(str, r)) for r in lines)
        return PlainTextResponse(body, media_type="text/plain",
                                 headers={"Content-Disposition": f"attachment; filename=trace_{raw}.txt"})
    import io, csv as _csv
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerows(lines)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=trace_{raw}.csv"})


@app.post("/api/admin/upload", dependencies=[Depends(require_admin)])
async def admin_upload(file: UploadFile = File(...), table: str = Query("products", description="目标表,如 products/raw_materials/batches/ingredient/qc/equipment")):
    """管理操作: 上传 CSV 到指定表 + 重建本体。"""
    fname = file.filename or "upload.csv"
    if not fname.endswith(".csv"):
        raise HTTPException(400, "仅支持 CSV")
    target = table if table.startswith("food_") else f"food_{table}"
    dest = os.path.join(DATA, f"{target}.csv")
    os.makedirs(DATA, exist_ok=True)
    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)
    # 重建(强制, 让新数据生效)
    for fn in ["food.nt", "food_data_hash.txt"]:
        p = os.path.join(os.path.dirname(FOOD_NT), fn)
        if os.path.exists(p):
            os.remove(p)
    global graph, labels, vi, rev, QDATA
    graph, labels, vi, rev = _load()
    QDATA = v3.build_data(v3.parse_nt(FOOD_NT), D)
    logger.info("上传 %s -> %s, 本体已重建", fname, dest)
    return {"ok": True, "file": fname, "table": target, "nodes": len(graph)}


@app.get("/api/admin/kbs", dependencies=[Depends(require_admin)])
def admin_kbs():
    """管理操作: 列出所有已注册知识库 + 当前激活的。"""
    return {"ok": True, "active": KB_NAME, "kbs": list(KBS.keys())}


@app.get("/api/admin/audit", dependencies=[Depends(require_admin)])
def admin_audit(limit: int = Query(50, le=500)):
    """管理操作: 读取最近审计日志。"""
    lines = []
    if os.path.exists(AUDIT_FILE):
        with open(AUDIT_FILE, encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()][-limit:]
    return {"ok": True, "count": len(lines), "audit": lines}


def _label(uri):
    return labels.get(uri, gr.tail(uri))


def _resolve_readable(tail_name):
    """按实体 ID(tail) 找 URI 并返回可读名。"""
    if not tail_name:
        return ""
    uri = _find(tail_name)
    return _readable_name(uri) if uri else tail_name


def _readable_name(uri):
    """从图的数据属性解析实体可读名(产品名/原料名/设备名), 供导出/展示。"""
    if not uri:
        return ""
    props = graph.get(uri, {})
    for rel in ("productName", "rawName", "deviceName", "name"):
        if props.get(rel):
            return str(props[rel][0])
    return _label(uri)


def _forward_trace(batch_id):
    """正向溯源: 批次 -> 产品 + 原料。"""
    b = _find(f"Food_batches_{batch_id}")
    if not b:
        raise HTTPException(404, f"批次不存在: {batch_id}")
    product = [t for r, ts in graph[b].items() if r == "produces" for t in ts]
    ingredients = []
    # 经 batch_ingredient 找原料
    for bi, rels in graph.items():
        if gr.tail(bi).startswith("Food_batch_ingredient_") and any(t == b for t in rels.get("belongsToBatch", [])):
            for raw in rels.get("usesRawMaterial", []):
                ingredients.append(raw)
    return {
        "batch": batch_id,
        "product": [_label(p) for p in product],
        "raw_materials": sorted({_label(r) for r in ingredients}),
        "produce_date": next((t for r, ts in graph[b].items() if r == "produceDate" for t in ts), ""),
    }


def _reverse_trace(raw_id):
    """反向溯源: 原料 -> 批次 -> 产品（食品安全核心）。"""
    r = _find(f"Food_raw_materials_{raw_id}")
    if not r:
        raise HTTPException(404, f"原料不存在: {raw_id}")
    affected = []
    for bi, rels in graph.items():
        if gr.tail(bi).startswith("Food_batch_ingredient_") and r in rels.get("usesRawMaterial", []):
            for b in rels.get("belongsToBatch", []):
                product = [gr.tail(p) for rel, ts in graph[b].items() if rel == "produces" for p in ts]
                affected.append({
                    "batch": gr.tail(b),
                    "product": product[0] if product else "",
                    "produce_date": next((t for rel, ts in graph[b].items() if rel == "produceDate" for t in ts), ""),
                })
    return {"raw_material": _label(r), "affected_batches": affected}


@app.post("/api/admin/rebuild", dependencies=[Depends(require_admin)])
def admin_rebuild():
    """管理操作: 强制重建本体(接新数据后调用)。"""
    global graph, labels, vi, rev, QDATA
    for f in ["food.nt", "food_data_hash.txt"]:
        p = os.path.join(os.path.dirname(FOOD_NT), f)
        if os.path.exists(p):
            os.remove(p)
    graph, labels, vi, rev = _load()
    QDATA = v3.build_data(v3.parse_nt(FOOD_NT), D)
    logger.info("本体已重建, 节点=%d", len(graph))
    return {"ok": True, "message": "本体已重建", "nodes": len(graph)}


@app.post("/api/admin/sync", dependencies=[Depends(require_admin)])
def admin_sync():
    """管理操作: 实时数据同步 — 重读知识库数据目录(若 KB 配置了外部源则先 data_import), 再重建本体。"""
    global graph, labels, vi, rev, QDATA
    src = _kb.get("source")
    imported = None
    if src:
        try:
            from data_import import import_source
            imported = import_source(os.path.join(ROOT, src))
        except Exception as e:
            logger.warning("data_import 失败(用现有数据): %s", e)
    for f in ["food.nt", "food_data_hash.txt"]:
        p = os.path.join(os.path.dirname(FOOD_NT), f)
        if os.path.exists(p):
            os.remove(p)
    graph, labels, vi, rev = _load()
    QDATA = v3.build_data(v3.parse_nt(FOOD_NT), D)
    return {"ok": True, "kb": KB_NAME, "imported": imported, "nodes": len(graph), "message": "已实时同步"}


@app.get("/metrics", include_in_schema=False)
def metrics():
    """轻量指标: 各端点请求计数(供监控/排障)。"""
    return {"ok": True, "requests": dict(REQ_COUNT), "total": sum(REQ_COUNT.values())}


@app.get("/health")
def health():
    return {"status": "ok", "version": "2.1.0"}


@app.get("/api/app-config", include_in_schema=False)
def app_config():
    """APP 动态配置: 返回当前知识库的品牌/图标/示例问题(去硬编码)。"""
    return {
        "ok": True, "kb": KB_NAME,
        "name": _kb.get("name", "知识库助手"),
        "icon": _kb.get("icon", "🏭"),
        "examples": _kb.get("examples", []),
    }


@app.post("/api/ask", dependencies=[Depends(require_key)])
def ask(req: AskReq):
    """自然语言问答：规则引擎 → 逻辑推理桥(确定性) → GraphRAG → 引导。返回答案+证据(可解释)。

    多租户: 按 req.kb 惰性加载对应知识库(根治串台); kb 缺省用 FOOD_KB 兼容旧调用。
    """
    ctx = _get_kb_ctx(req.kb)
    if ctx is None:
        kb = (req.kb or KB_NAME).strip()
        return {"ok": False, "error": {"code": 4001,
                "message": f"知识库 '{kb}' 无效或数据缺失(未注册/本体或词典不存在)"}}
    D, QDATA, graph = ctx["D"], ctx["QDATA"], ctx["graph"]
    FOOD_NT, FOOD_LEX = ctx["nt_file"], ctx["lex_file"]
    # 1. 规则引擎(确定性, 结构化查询)
    ans = v3.answer(req.question, QDATA, D)
    if ans != "暂不支持该问题":
        try:
            import evidence
            ev = evidence.extract_evidence(req.question, QDATA, D, ans)
        except Exception:
            ev = {}
        return {"ok": True, "mode": "rule", "answer": ans, "evidence": ev, "kb": ctx["kb"]}
    # 2. 逻辑推理桥(LLM转逻辑查询→确定性执行, 借鉴KAG; 覆盖更多开放式问题而不失确定性)
    try:
        import logical_qa
        lres = logical_qa.answer(req.question, QDATA, D)
        if lres:
            lans, lmode = lres
            try:
                import evidence
                ev = evidence.extract_evidence(req.question, QDATA, D, lans)
            except Exception:
                ev = {}
            return {"ok": True, "mode": "logical", "answer": lans, "evidence": ev, "kb": ctx["kb"]}
    except Exception:
        pass  # 逻辑桥不可用则跳过
    # 3. GraphRAG(LLM 兜底)
    gans, gctx = gr.answer_graph(req.question, FOOD_NT, depth=2, max_nodes=40, lexicon=D)
    if not gans.startswith("[图检索]"):
        return {"ok": True, "mode": "graphrag", "answer": gans, "context": gctx[:2000], "kb": ctx["kb"]}
    # 3.5 混合检索(BM25 稀疏 + 向量语义, 提升模糊/同义/口语化查询召回)
    try:
        from bm25_retrieval import BM25Index
        from vector_retrieval import VectorIndex
        # 多租户: 每个 kb 独立索引缓存
        bm_key, vec_key = f"bm25_{ctx['kb']}", f"vec_{ctx['kb']}"
        if bm_key not in _KB_INDEX_CACHE:
            _KB_INDEX_CACHE[bm_key] = BM25Index.from_graph(graph)
        if vec_key not in _KB_INDEX_CACHE:
            _KB_INDEX_CACHE[vec_key] = VectorIndex.from_graph(graph, lexicon=D)
        bm_hits = _KB_INDEX_CACHE[bm_key].search(req.question, top_k=3, min_score=4.0)
        vec_hits = _KB_INDEX_CACHE[vec_key].search(req.question, top_k=5, min_score=0.60)
        seen, fused = set(), []
        for h in (bm_hits + vec_hits):
            ent = h["entity"]
            if ent not in seen:
                seen.add(ent)
                fused.append(h)
        if fused:
            ents = "、".join(h["entity"] for h in fused)
            return {"ok": True, "mode": "hybrid",
                    "answer": f"（混合检索）找到相关实体: {ents}",
                    "hits": fused[:5], "bm25_hits": bm_hits[:3], "vector_hits": vec_hits[:5],
                    "kb": ctx["kb"]}
    except Exception:
        pass
    # 3.75 文档知识库 RAG 兜底: 本体/图/混合检索都答不上时, 检索该 kb 已入库的
    #      说明书/规范/PDF 文档知识, 返回带溯源的答案(让知识库不再是"孤岛")。
    #      全程 try/except, 检索失败/无有效答案则降级到原 miss, 绝不抛异常。
    try:
        from knowledge.rag import answer as _rag_answer
        from knowledge.store import KnowledgeStore
        _kbdir = _kb_dir(ctx["kb"])
        if _kbdir is not None:
            _store = KnowledgeStore(_kbdir)
            _res = _rag_answer(None, req.question, _store, top_k=5)
            _ans = (_res or {}).get("answer", "") or ""
            _ev = (_res or {}).get("evidence", []) or []
            # 判定"有效答案": 非空、非错误占位符、且文档片段确实覆盖了问题
            _invalid = not _ans.strip() or _ans.startswith("[") or "片段未覆盖" in _ans
            if not _invalid and _ev:
                return {"ok": True, "mode": "kb_rag", "answer": _ans,
                        "evidence": _ev, "kb": ctx["kb"]}
    except Exception:
        pass
    # 4. 答不上: 从 KB 配置读示例引导(去硬编码)
    examples = KBS.get(ctx["kb"], {}).get("examples", ["乳制品的数量", "原味酸奶是什么"])
    guide = "\n".join(f"· {e}" for e in examples[:5])
    return {"ok": True, "mode": "miss", "answer": f"抱歉，暂未理解该问题。\n可试试问：\n{guide}",
            "kb": ctx["kb"]}


@app.get("/api/trace/forward", dependencies=[Depends(require_key)])
def trace_forward(batch: str = Query(..., description="生产批次号，如 B001")):
    return {"ok": True, "direction": "forward", **_forward_trace(batch)}


@app.get("/api/trace/reverse", dependencies=[Depends(require_key)])
def trace_reverse(raw: str = Query(..., description="原料编号，如 RM008")):
    return {"ok": True, "direction": "reverse", **_reverse_trace(raw)}


@app.get("/api/scan", dependencies=[Depends(require_key)])
def scan(code: str = Query(..., description="溯源码，如 P003-B005 或 B001")):
    """扫码溯源：识别产品批次或批次号。"""
    parts = code.split("-")
    batch_id = parts[-1] if parts and parts[-1].startswith("B") else code
    return {"ok": True, "code": code, **_forward_trace(batch_id)}


@app.get("/api/stats", dependencies=[Depends(require_key)])
def stats():
    """知识库统计。"""
    n_products = sum(1 for k in graph if gr.tail(k).startswith("Food_products_P"))
    n_batches = sum(1 for k in graph if gr.tail(k).startswith("Food_batches_B"))
    n_raw = sum(1 for k in graph if gr.tail(k).startswith("Food_raw_materials_RM"))
    return {"ok": True, "products": n_products, "batches": n_batches,
            "raw_materials": n_raw, "nodes": len(graph), "edges": sum(len(v) for v in graph.values())}


# ════════════════════════════════════════════════════════════════════════
# 契约端点 v1.0 — knowledge / eval / assets / version（增量，不影响既有接口）
# 统一响应信封: {ok, data?, error?, elapsed_s}
# 错误码: 4001参数 4041不存在 4091冲突 5001引擎 5031模型
# 多租户兼容: kb 参数隔离存储目录; 写操作幂等
# ════════════════════════════════════════════════════════════════════════

CONTRACT_VERSION = "1.0"
FEATURES = ["knowledge", "eval", "assets", "version", "trace", "qa", "ontology", "stats"]

# 文档知识库存储根 + 临时上传目录(每 kb 一个隔离子目录)
_KB_ROOT = os.path.join(ROOT, "output", "kb_store")
_TMP_UPLOAD = os.path.join(ROOT, "output", "_tmp_uploads")
_ASSET_DIR = os.path.join(ROOT, "output", "asset_versions")
_ASSET_MANIFEST = os.path.join(_ASSET_DIR, "manifest.json")


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


def _safe_doc_id(doc_id):
    """doc_id 白名单化(字母数字下划线连字符), 避免向量文件名路径穿越。"""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", doc_id or "")


def _ok_env(data, start):
    """成功信封。"""
    return {"ok": True, "data": data, "elapsed_s": round(time.time() - start, 3)}


def _err_env(code, msg, start):
    """失败信封。"""
    return {"ok": False, "error": {"code": code, "message": str(msg)},
            "elapsed_s": round(time.time() - start, 3)}


# ── 语义资产快照/回滚（lexicon + ontology + knowledge store）──

def _asset_dir(kb):
    """按 kb 隔离的资产版本目录(多租户)。"""
    kb = _safe_doc_id(kb or "food")
    return os.path.join(_ASSET_DIR, kb)


def _asset_manifest(kb="food"):
    kb_dir = _asset_dir(kb)
    p = os.path.join(kb_dir, "manifest.json")
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                m = json.load(f)
                return m if isinstance(m, dict) else {}
    except Exception:
        pass
    return {}


def _asset_save_manifest(man, kb="food"):
    kb_dir = _asset_dir(kb)
    try:
        os.makedirs(kb_dir, exist_ok=True)
        with open(os.path.join(kb_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _asset_active(kb="food"):
    p = os.path.join(_asset_dir(kb), "active.txt")
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return f.read().strip() or None
    except Exception:
        pass
    return None


def _asset_set_active(v, kb="food"):
    try:
        os.makedirs(_asset_dir(kb), exist_ok=True)
        with open(os.path.join(_asset_dir(kb), "active.txt"), "w", encoding="utf-8") as f:
            f.write(v)
    except Exception:
        pass


def _hash_dir(d):
    """目录内容哈希(相对路径 + 字节), 供资产版本指纹。"""
    h = hashlib.sha256()
    items = []
    for root, _, files in os.walk(d):
        for fn in sorted(files):
            p = os.path.join(root, fn)
            items.append((os.path.relpath(p, d).replace("\\", "/"), p))
    for rel, p in sorted(items):
        try:
            with open(p, "rb") as f:
                h.update(rel.encode("utf-8"))
                h.update(f.read())
        except Exception:
            pass
    return h.hexdigest()[:16]


def _asset_snapshot(kb="food"):
    """快照当前激活语义资产(词典/本体/文档知识库), 返回 (version, hash)。多租户: 按 kb 隔离。"""
    ctx = _get_kb_ctx(kb)
    if ctx is None:
        return None, None
    nt_file, lex_file = ctx["nt_file"], ctx["lex_file"]
    version = datetime.now().strftime("v%Y%m%d_%H%M%S")
    vdir = os.path.join(_asset_dir(kb), version)
    os.makedirs(vdir, exist_ok=True)
    copied = {"lexicon": False, "ontology": False, "knowledge": False}
    if os.path.exists(lex_file):
        try:
            shutil.copy2(lex_file, os.path.join(vdir, "lexicon.json")); copied["lexicon"] = True
        except Exception:
            pass
    if os.path.exists(nt_file):
        try:
            shutil.copy2(nt_file, os.path.join(vdir, "ontology.nt")); copied["ontology"] = True
        except Exception:
            pass
    try:
        os.makedirs(os.path.join(vdir, "knowledge"), exist_ok=True)
        kbd = _kb_dir(kb)
        if kbd and os.path.isdir(kbd):
            shutil.copytree(kbd, os.path.join(vdir, "knowledge"), dirs_exist_ok=True)
        copied["knowledge"] = True
    except Exception:
        copied["knowledge"] = False
    h = _hash_dir(vdir)
    man = _asset_manifest(kb)
    man[version] = {"hash": h, "created": datetime.now().isoformat(), "assets": copied}
    _asset_save_manifest(man, kb)
    _asset_set_active(version, kb)
    return version, h


def _asset_rollback(version, kb="food"):
    """按版本回滚语义资产到磁盘。返回 version 或 None(版本不存在)。多租户: 按 kb 隔离。"""
    ctx = _get_kb_ctx(kb)
    if ctx is None:
        return None
    nt_file, lex_file = ctx["nt_file"], ctx["lex_file"]
    man = _asset_manifest(kb)
    if version not in man:
        return None
    vdir = os.path.join(_asset_dir(kb), version)
    if not os.path.isdir(vdir):
        return None
    sp = os.path.join(vdir, "lexicon.json")
    if os.path.exists(sp):
        os.makedirs(os.path.dirname(lex_file) or ROOT, exist_ok=True)
        shutil.copy2(sp, lex_file)
    sp = os.path.join(vdir, "ontology.nt")
    if os.path.exists(sp):
        os.makedirs(os.path.dirname(nt_file) or ROOT, exist_ok=True)
        shutil.copy2(sp, nt_file)
    sp = os.path.join(vdir, "knowledge")
    kbd = _kb_dir(kb)
    if os.path.isdir(sp) and kbd:
        shutil.rmtree(kbd, ignore_errors=True)
        os.makedirs(kbd, exist_ok=True)
        shutil.copytree(sp, kbd, dirs_exist_ok=True)
    _asset_set_active(version, kb)
    # 失效缓存, 下次 /api/ask 加载回滚后的本体
    _kb_ctx_cache.pop(kb, None)
    _KB_INDEX_CACHE.pop(f"bm25_{kb}", None)
    _KB_INDEX_CACHE.pop(f"vec_{kb}", None)
    return version


# ── 请求模型 ──

class KnowledgeQueryReq(BaseModel):
    kb: str = "food"
    q: str
    top_k: int = 5


class KnowledgeDeleteReq(BaseModel):
    kb: str = "food"
    doc_id: str


class AssetSnapshotReq(BaseModel):
    kb: str = "food"


class AssetRollbackReq(BaseModel):
    version: str
    kb: str = "food"


# ── 1. knowledge ──

@app.post("/api/knowledge/ingest", dependencies=[Depends(require_key)])
async def knowledge_ingest(file: UploadFile = File(...),
                           kb: str = Form("food"),
                           doc_id: str = Form("")):
    """上传文档(PDF/Word/TXT) → 解析+切块+向量化+入库。同 doc_id 幂等覆盖。"""
    start = time.time()
    try:
        from knowledge.ingest import extract_text
        from knowledge.chunk import chunk_text
        from knowledge.embed import embed_chunks
        from knowledge.store import KnowledgeStore
    except Exception as e:
        logger.warning(f"API内部错误[知识引擎不可用]: {e}")
        return _err_env(5001, "知识引擎不可用(内部错误已记录)", start)
    fname = file.filename or "upload.txt"
    ext = os.path.splitext(fname)[1].lower()
    if ext not in (".pdf", ".doc", ".docx", ".txt"):
        return _err_env(4001, f"仅支持 PDF/Word/TXT, 收到: {ext or '未知扩展名'}", start)
    # 安全加固(架构师审计 P1-5): 上传大小上限 50MB, 防 DoS。
    MAX_UPLOAD = 50 * 1024 * 1024
    try:
        _size = file.size if hasattr(file, "size") else None
        if _size is not None and _size > MAX_UPLOAD:
            return _err_env(4001, f"文件过大: >50MB", start)
    except Exception:
        pass
    kbdir = _kb_dir(kb)
    if kbdir is None:
        return _err_env(4001, "非法 kb 名", start)
    tmp = os.path.join(_TMP_UPLOAD, f"{time.time_ns()}{ext}")
    try:
        try:
            os.makedirs(_TMP_UPLOAD, exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(await file.read())
            doc = extract_text(tmp)
            if not doc:
                return _err_env(4001, "文档解析失败(缺解析库或内容为空), 未入库", start)
            chunks = chunk_text(doc["raw_text"])
            if not chunks:
                return _err_env(4001, "文档切块为空, 未入库", start)
            vectors = embed_chunks(chunks)
            if not vectors:
                return _err_env(5031, "embedding 服务不可用(未产出向量), 文档未入库", start)
            did = _safe_doc_id(doc_id.strip()) or "%s_%s" % (
                doc["title"], hashlib.md5(doc["raw_text"].encode("utf-8")).hexdigest()[:8])
            store = KnowledgeStore(kbdir)
            if not store.add_doc(did, doc["title"], chunks, vectors):
                return _err_env(5001, "文档入库失败", start)
            return _ok_env({"kb": kb, "doc_id": did, "title": doc["title"],
                            "chunks": len(chunks), "status": "stored"}, start)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"API内部错误[文档接入失败]: {e}")
        return _err_env(5001, "文档接入失败(内部错误已记录)", start)


@app.post("/api/knowledge/query", dependencies=[Depends(require_key)])
def knowledge_query(req: KnowledgeQueryReq):
    """文档 RAG 检索。body {kb, q, top_k?} → {answer, evidence}。"""
    start = time.time()
    try:
        from knowledge.rag import answer as rag_answer
        from knowledge.store import KnowledgeStore
    except Exception as e:
        logger.warning(f"API内部错误[知识引擎不可用]: {e}")
        return _err_env(5001, "知识引擎不可用(内部错误已记录)", start)
    if not (req.q or "").strip():
        return _err_env(4001, "缺少 q", start)
    kbdir = _kb_dir(req.kb)
    if kbdir is None:
        return _err_env(4001, "非法 kb 名", start)
    try:
        store = KnowledgeStore(kbdir)
        res = rag_answer(None, req.q, store, top_k=max(1, min(req.top_k, 20)))
    except Exception as e:
        logger.warning(f"API内部错误[检索失败]: {e}")
        return _err_env(5001, "检索失败(内部错误已记录)", start)
    ans = res.get("answer", "")
    if ans.startswith("[模型未配置]"):
        return _err_env(5031, "模型未配置", start)
    return _ok_env({"kb": req.kb, "answer": ans, "evidence": res.get("evidence", [])}, start)


@app.get("/api/knowledge/list", dependencies=[Depends(require_key)])
def knowledge_list(kb: str = Query("food")):
    """列出某 kb 的已入库文档。"""
    start = time.time()
    try:
        from knowledge.store import KnowledgeStore
    except Exception as e:
        logger.warning(f"API内部错误[知识引擎不可用]: {e}")
        return _err_env(5001, "知识引擎不可用(内部错误已记录)", start)
    kbdir = _kb_dir(kb)
    if kbdir is None:
        return _err_env(4001, "非法 kb 名", start)
    try:
        docs = KnowledgeStore(kbdir).list_docs()
    except Exception as e:
        logger.warning(f"API内部错误[读取失败]: {e}")
        return _err_env(5001, "读取失败(内部错误已记录)", start)
    return _ok_env({"kb": kb, "docs": docs}, start)


@app.post("/api/knowledge/delete", dependencies=[Depends(require_key)])
def knowledge_delete(req: KnowledgeDeleteReq):
    """删除某 kb 下的一篇文档。幂等: 重复删除已不存在文档返回 4041。"""
    start = time.time()
    try:
        from knowledge.store import KnowledgeStore
    except Exception as e:
        logger.warning(f"API内部错误[知识引擎不可用]: {e}")
        return _err_env(5001, "知识引擎不可用(内部错误已记录)", start)
    kbdir = _kb_dir(req.kb)
    if kbdir is None:
        return _err_env(4001, "非法 kb 名", start)
    try:
        ok = KnowledgeStore(kbdir).delete(req.doc_id)
    except Exception as e:
        logger.warning(f"API内部错误[删除失败]: {e}")
        return _err_env(5001, "删除失败(内部错误已记录)", start)
    if not ok:
        return _err_env(4041, f"文档不存在: {req.doc_id}", start)
    return _ok_env({"kb": req.kb, "deleted": req.doc_id}, start)


# ── 2. eval ──

@app.get("/api/eval/benchmark", dependencies=[Depends(require_key)])
def eval_benchmark(kb: str = Query("food")):
    """评测基线: 用 kb 配置的示例题目跑 EvalAgent baseline, 返回命中率。"""
    start = time.time()
    kbc = KBS.get(kb, {})
    questions = kbc.get("examples") or _kb.get("examples", [])
    if not questions:
        return _err_env(4001, f"kb '{kb}' 无评测题目(未配置 examples)", start)
    ctx = _get_kb_ctx(kb)  # 多租户: 按 kb 取本体/词典, 与 /api/ask 对齐
    if ctx is None:
        return _err_env(4001, f"知识库 '{kb}' 无效或数据缺失", start)
    try:
        from agents.eval_agent import EvalAgent
        r = EvalAgent().run({"questions": questions, "nt_file": ctx["nt_file"],
                             "lexicon": ctx["lex_file"], "mode": "baseline"})
    except Exception as e:
        logger.warning(f"API内部错误[评测引擎不可用]: {e}")
        return _err_env(5001, "评测引擎不可用(内部错误已记录)", start)
    if not r.ok:
        return _err_env(5001, r.error, start)
    data = r.data or {}
    per = data.get("per_question", [])
    hits = sum(1 for p in per if p.get("hit"))
    return _ok_env({"kb": kb, "questions_n": data.get("questions_n", len(per)),
                    "hits": hits, "score": data.get("score")}, start)


@app.post("/api/eval/isolate", dependencies=[Depends(require_key)])
async def eval_isolate(request: Request):
    """评测隔离: 只问答不打分。字段白名单 {kb, questions}; 出现 gold/rubric/score 返回 4001。"""
    start = time.time()
    try:
        body = await request.json()
    except Exception:
        return _err_env(4001, "请求体不是合法 JSON", start)
    if not isinstance(body, dict):
        return _err_env(4001, "请求体应为 JSON 对象", start)
    allowed = {"kb", "questions"}
    extra = set(body.keys()) - allowed
    if extra:
        return _err_env(4001, f"isolate 模式禁止字段: {sorted(extra)} (白名单: {sorted(allowed)})", start)
    questions = body.get("questions")
    if not isinstance(questions, list) or not questions:
        return _err_env(4001, "缺少非空 questions 列表", start)
    if any(not isinstance(q, str) or not q.strip() for q in questions):
        return _err_env(4001, "questions 必须全为非空字符串", start)
    kb = body.get("kb", "food")
    ctx = _get_kb_ctx(kb)  # 多租户: 按 kb 取本体/词典, 与 /api/ask 对齐
    if ctx is None:
        return _err_env(4001, f"知识库 '{kb}' 无效或数据缺失", start)
    try:
        from agents.eval_agent import EvalAgent
        r = EvalAgent().run({"questions": questions, "nt_file": ctx["nt_file"],
                             "lexicon": ctx["lex_file"], "mode": "isolate"})
    except Exception as e:
        logger.warning(f"API内部错误[评测引擎不可用]: {e}")
        return _err_env(5001, "评测引擎不可用(内部错误已记录)", start)
    if not r.ok:
        return _err_env(5001, r.error, start)
    data = r.data or {}
    per = data.get("per_question", [])
    answers = [{"q": p.get("q"), "answer": p.get("answer"), "hit": p.get("hit")} for p in per]
    return _ok_env({"kb": kb, "questions_n": len(answers), "answers": answers}, start)


# ── 3. assets ──

@app.post("/api/assets/snapshot", dependencies=[Depends(require_key)])
def assets_snapshot(req: AssetSnapshotReq):
    """快照语义资产(lexicon + ontology + knowledge) → {version, hash}。多租户按 kb 隔离。"""
    start = time.time()
    kb = req.kb or KB_NAME
    try:
        version, h = _asset_snapshot(kb)
    except Exception as e:
        logger.warning(f"API内部错误[快照失败]: {e}")
        return _err_env(5001, "快照失败(内部错误已记录)", start)
    if version is None:
        return _err_env(4001, f"知识库 '{kb}' 无效或数据缺失, 无法快照", start)
    return _ok_env({"version": version, "hash": h, "kb": kb}, start)


@app.post("/api/assets/rollback", dependencies=[Depends(require_key)])
def assets_rollback(req: AssetRollbackReq):
    """按版本回滚语义资产并重载内存本体 → {active_version}。多租户按 kb 隔离。"""
    start = time.time()
    kb = req.kb or KB_NAME
    try:
        v = _asset_rollback(req.version, kb)
    except Exception as e:
        logger.warning(f"API内部错误[回滚失败]: {e}")
        return _err_env(5001, "回滚失败(内部错误已记录)", start)
    if v is None:
        return _err_env(4041, f"版本不存在: {req.version} (kb={kb})", start)
    return _ok_env({"active_version": v, "kb": kb}, start)


@app.get("/api/assets/list", dependencies=[Depends(require_key)])
def assets_list(kb: str = Query("food")):
    """列出某 kb 的语义资产版本。多租户按 kb 隔离。"""
    start = time.time()
    man = _asset_manifest(kb)
    versions = [{"version": v, "hash": e.get("hash"), "created": e.get("created"),
                 "assets": e.get("assets", {})} for v, e in sorted(man.items())]
    return _ok_env({"kb": kb, "versions": versions, "active_version": _asset_active(kb)}, start)


# ── 4. version ──

@app.get("/api/version", dependencies=[Depends(require_key)])
def api_version():
    """服务 + 契约版本与能力特性。"""
    start = time.time()
    return _ok_env({
        "version": getattr(app, "version", "0.1.4"),
        "contract_version": CONTRACT_VERSION,
        "features": FEATURES,
        "kb": KB_NAME,
    }, start)


# ── 5. ontology/build（多租户: 闭源 REST 化前置——建本体端点）──

class OntologyBuildReq(BaseModel):
    kb: str
    csv_path: str = ""   # 单表: 数据文件路径(相对 codes/ 或绝对)
    data_dir: str = ""   # 多表: 数据目录路径(可选, 走 schema 建模)
    use_llm: bool = True


@app.post("/api/ontology/build", dependencies=[Depends(require_key)])
def ontology_build(req: OntologyBuildReq):
    """按 kb 建本体: 复用 run.setup 建模, 产出 nt+lex, 更新 kbs.json, 失效缓存。

    支持单表(csv_path)或多表(data_dir)。建模成功后该 kb 立即可被 /api/ask 问答。
    """
    start = time.time()
    kb = (req.kb or "").strip()
    if not kb or kb.startswith(".") or any(c in kb for c in ("/", "\\", "..")):
        return _err_env(4001, "非法 kb 名", start)
    # 数据源分流：data_dir(多文件目录) 优先，其次 csv_path(单文件)。
    # 修复: 此前用 `src = req.csv_path or req.data_dir` 把目录当单文件传 run.setup → 报"不支持的数据格式"。
    if req.data_dir:
        src = req.data_dir
    elif req.csv_path:
        src = req.csv_path
    else:
        return _err_env(4001, "需提供 csv_path(单表) 或 data_dir(多表)", start)
    # 安全加固(架构师审计 P0-2): 数据源必须限定在 codes/data 或 codes/output 白名单内,
    # 拒绝绝对路径和 .. 穿越, 防止任意文件读取(/etc/passwd/.env/.ssh 等)。
    data_root = os.path.realpath(os.path.join(ROOT, "data"))
    out_root = os.path.realpath(os.path.join(ROOT, "output"))
    src_abs = src if os.path.isabs(src) else os.path.realpath(os.path.join(ROOT, src))
    if not (os.path.realpath(src_abs).startswith(data_root + os.sep) or
            os.path.realpath(src_abs).startswith(out_root + os.sep)):
        return _err_env(4001, f"数据源必须在 data/ 或 output/ 内(防路径穿越): {src}", start)
    if not os.path.exists(src_abs):
        return _err_env(4001, f"数据源不存在: {src}", start)
    try:
        import run as run_mod
        if req.data_dir:
            # 多文件目录: 复用 multi_model 统一多表建模(schema-free), 同时产出 nt + lex。
            # setup_schema 只返回 nt 不产出词典, 故用 multi_model.build 等效多文件建模。
            import multi_model as mm_mod
            mm_mod.build(src_abs, table=kb)
            nt = os.path.join(ROOT, "output", f"{kb}.nt")
            lex = os.path.join(ROOT, "config", f"lexicon_{kb}.json")
        else:
            # 单文件: 复用 run.setup 单表建模, 产出 nt + lex
            nt, lex = run_mod.setup(src_abs, table=kb, use_llm=req.use_llm)
    except Exception as e:
        logger.warning(f"API内部错误[建本体失败]: {e}")
        return _err_env(5001, "建本体失败(内部错误已记录)", start)
    if not nt or not lex:
        return _err_env(5001, "建本体失败: 未产出 nt 或 lexicon", start)
    # 更新 kbs.json: 注册该 kb 的 nt/lexicon(相对路径), 使 /api/ask 可感知
    nt_rel = os.path.relpath(nt, ROOT).replace("\\", "/")
    lex_rel = os.path.relpath(lex, ROOT).replace("\\", "/")
    try:
        _update_kbs(kb, nt_rel, lex_rel)
    except Exception as e:
        logger.warning(f"kbs.json 更新失败: {e}")
    # 失效该 kb 缓存, 下次 /api/ask 重新加载新本体
    _kb_ctx_cache.pop(kb, None)
    _KB_INDEX_CACHE.pop(f"bm25_{kb}", None)
    _KB_INDEX_CACHE.pop(f"vec_{kb}", None)
    return _ok_env({"kb": kb, "nt": nt_rel, "lexicon": lex_rel,
                    "status": "built", "ask_ready": True}, start)


def _update_kbs(kb, nt_rel, lex_rel):
    """把 kb 的 nt/lexicon 写回 kbs.json(幂等)。"""
    data = json.load(open(KBS_FILE, encoding="utf-8"))
    kbs = data.setdefault("kbs", {})
    entry = kbs.get(kb, {})
    entry["nt"] = nt_rel
    entry["lexicon"] = os.path.basename(lex_rel)
    entry.setdefault("data_dir", "data")
    kbs[kb] = entry
    with open(KBS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    global KBS
    KBS = _load_kbs()


if __name__ == "__main__":
    import uvicorn
    # 安全加固(架构师审计 P0-1): fail-closed 鉴权 + 默认仅本机可访问。
    # 未配置 ADMIN/READ key 时, 自动生成随机 admin key 打印到 stdout(方便单机首次使用),
    # 避免无 key 开放或启动即拒绝。对外部署需显式配 key + --host 0.0.0.0 + 反代 TLS。
    if not (ADMIN_KEY or READ_KEY):
        import secrets
        _gen = "FOOD_ADMIN_KEY=" + secrets.token_hex(16)
        os.environ["FOOD_ADMIN_KEY"] = _gen.split("=", 1)[1]
        ADMIN_KEY = _gen.split("=", 1)[1]
        print("=" * 50)
        print("未配置 API Key, 已自动生成(请复制保存):")
        print(f"  {_gen}")
        print("对外开放前请务必设置 FOOD_ADMIN_KEY / FOOD_READ_KEY 环境变量!")
        print("=" * 50)
    host = sys.argv[sys.argv.index("--host") + 1] if "--host" in sys.argv else "127.0.0.1"
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8000
    # 后台预热 embedding 模型(不阻塞服务启动)
    threading.Thread(target=_warm_embedding, daemon=True).start()
    uvicorn.run(app, host=host, port=port)
