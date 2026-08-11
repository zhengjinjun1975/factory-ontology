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
import sys
import json
import time
import hashlib
import logging
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# ── 结构化日志 ──
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("food-api")

from fastapi import FastAPI, HTTPException, Query, Header, Request, Depends, UploadFile, File
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
_BM25_INDEX = None  # BM25 混合检索索引(惰性构建)

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


# ── 角色化鉴权(M1.2): FOOD_ADMIN_KEY 管理 / FOOD_READ_KEY 只读 ──
ADMIN_KEY = os.environ.get("FOOD_ADMIN_KEY", "").strip()
READ_KEY = os.environ.get("FOOD_READ_KEY", "").strip()


def _valid(key, target):
    """key 是否匹配目标(或已配置的角色 key)。空=该角色未配置。"""
    return target != "" and key == target


def require_key(x_api_key: str = Header(default="")):
    """只读端点鉴权: 配置了任一 key 时, 需匹配 read 或 admin key; 均未配置则开放。"""
    if (ADMIN_KEY or READ_KEY) and not (_valid(x_api_key, ADMIN_KEY) or _valid(x_api_key, READ_KEY)):
        raise HTTPException(401, "无效或缺失 API Key (需 X-API-Key 头)")


def require_admin(x_api_key: str = Header(default="")):
    """管理端点鉴权: 需匹配 admin key(配置时)。"""
    if ADMIN_KEY and not _valid(x_api_key, ADMIN_KEY):
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
    """自然语言问答：规则引擎 → 逻辑推理桥(确定性) → GraphRAG → 引导。返回答案+证据(可解释)。"""
    # 1. 规则引擎(确定性, 结构化查询)
    ans = v3.answer(req.question, QDATA, D)
    if ans != "暂不支持该问题":
        try:
            import evidence
            ev = evidence.extract_evidence(req.question, QDATA, D, ans)
        except Exception:
            ev = {}
        return {"ok": True, "mode": "rule", "answer": ans, "evidence": ev}
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
            return {"ok": True, "mode": "logical", "answer": lans, "evidence": ev}
    except Exception:
        pass  # 逻辑桥不可用则跳过
    # 3. GraphRAG(LLM 兜底)
    gans, ctx = gr.answer_graph(req.question, FOOD_NT, depth=2, max_nodes=40, lexicon=D)
    if not gans.startswith("[图检索]"):
        return {"ok": True, "mode": "graphrag", "answer": gans, "context": ctx[:2000]}
    # 3.5 BM25 混合检索(轻量稀疏, 提升模糊查询召回, 零 token)
    try:
        from bm25_retrieval import BM25Index
        global _BM25_INDEX
        if _BM25_INDEX is None:
            _BM25_INDEX = BM25Index.from_graph(graph)
        hits = _BM25_INDEX.search(req.question, top_k=3, min_score=4.0)
        if hits:
            ents = "、".join(h["entity"] for h in hits)
            return {"ok": True, "mode": "bm25", "answer": f"（混合检索）找到相关实体: {ents}",
                    "hits": hits[:3]}
    except Exception:
        pass
    # 4. 答不上: 从 KB 配置读示例引导(去硬编码)
    examples = _kb.get("examples", ["乳制品的数量", "原味酸奶是什么"])
    guide = "\n".join(f"· {e}" for e in examples[:5])
    return {"ok": True, "mode": "miss", "answer": f"抱歉，暂未理解该问题。\n可试试问：\n{guide}"}


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
