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

# 问答融合辅助层(P2 拆分): 证据归一化/LLM润色兜底/RAG+本体融合/文档目录等无状态逻辑
from ask_service import (  # noqa: E402
    _kb_dir,
    _norm_rule_evidence,
    _norm_doc_evidence,
    _polish_rule_answer,
    _llm_fallback_answer,
    _retrieve_doc_chunks,
    _fuse_doc_supplement,
    _doc_rag_fallback,
    _fuse_q_bigrams,
    _fuse_chunk_relevant,
)

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

app = FastAPI(title="食品企业知识库 API", version="0.2.1",
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
def ontology_structure(kb: str = Query("", description="知识库名")):
    """本体建模视图数据：类 + Is-A 类别层级(subClassOf) + 对象属性关系 + 实例数。
    按 kb 隔离（不串台）；优先读深化本体(含 subClassOf), 回退该 kb 本体。"""
    from ontology_qa_v3 import parse_nt
    if not kb:
        kb = "food"
    kbc = KBS.get(kb) or {}
    nt_path = kbc.get("nt", f"output/{kb}.nt")
    nt_file = os.path.join(ROOT, nt_path)
    # 深化本体优先(同 kb 的 _deep 变体)
    deep_nt = os.path.join(ROOT, "output", f"{kb}_deep.nt")
    if os.path.exists(deep_nt):
        nt_file = deep_nt
    if not os.path.exists(nt_file):
        return {"ok": False, "error": f"kb '{kb}' 本体不存在: {nt_file}"}
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
    return {"ok": True, "kb": kb, "classes": sorted(classes), "subclass_of": sorted(subcls),
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
def ontology_graph(kb: str = Query("")):
    """本体完整图(节点+边)，供前端 ECharts 动态大图渲染(仿 sme-decision-ontology /graph/full)。

    多租户: 按 kb 参数加载对应行业本体(_get_kb_ctx), 不再锁定 food。
    """
    # 多租户: 惰性加载所选行业本体; 无效 kb 返回空图而非抛错
    ctx = _get_kb_ctx(kb or None)
    if ctx is None:
        return {"ok": False, "error": "知识库无效", "nodes": [], "edges": []}
    g = ctx["graph"]      # 当前行业本体图(替代模块级 food graph)
    lb = ctx["labels"]    # 当前行业本体标签(替代模块级 food labels)
    nodes, edges = [], []
    seen_edges = set()
    RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
    # 实体类(从URI前缀推断: NS + <类名>_<实例id>); 仅保留实例节点(带 _ 的)
    for uri, props in g.items():
        if uri == RDF_TYPE:
            continue
        nm = lb.get(uri, uri.split("#")[-1].strip("<>"))
        local = uri.split("#")[-1].strip("<>")
        if "_" not in local:
            continue  # 跳过属性/关系/类声明节点(无实例id)
        entity = local.rsplit("_", 1)[0]
        nodes.append({"id": uri, "name": nm, "entity": entity})
    # 边(对象属性: 目标是实体URI)
    node_ids = {n["id"] for n in nodes}
    for uri, props in g.items():
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
    # ── 类级语义关系边(同属区域/生产产品等, 对象属性 domain→range 都是类, 让力导向图显示语义关系链) ──
    # 解析 nt 里的对象属性(domain/range 都指向非 xsd 类型的类 URI), 渲染为 类→类 边(带中文label)
    try:
        seen_crels = set()
        XSD = "http://www.w3.org/2001/XMLSchema#"
        for uri, props in g.items():
            doms = [str(x).strip("<>") for x in props.get("domain", [])]
            rngs = [str(x).strip("<>") for x in props.get("range", [])]
            # 过滤: domain/range 都指向非 xsd 类型的类 URI(排除数据属性)
            dom_class = [x for x in doms if not x.startswith(XSD)]
            rng_class = [x for x in rngs if not x.startswith(XSD)]
            if not dom_class or not rng_class:
                continue
            # 取第一个类 domain/range, 渲染类→类边
            dcl = dom_class[0]; rcl = rng_class[0]
            if dcl == rcl or (dcl, rcl) in seen_crels:
                continue
            # 类节点加入 nodes(力导向图边的端点需存在): 用 URI 尾名(去 #)
            for cn in (dcl, rcl):
                c_local = cn.split("#")[-1].strip("<>")
                if cn not in node_ids and cn not in {n["id"] for n in nodes}:
                    nodes.append({"id": cn, "name": lb.get(cn, c_local), "entity": c_local, "class_node": True})
            rel_name = lb.get(uri, uri.split("#")[-1].strip("<>"))
            edges.append({"from": dcl, "to": rcl, "rel": rel_name, "class_level": True})
            seen_crels.add((dcl, rcl))
    except Exception as _e:
        logger.warning(f"类级关系边解析跳过: {_e}")
    return {"ok": True, "nodes": nodes, "edges": edges,
            "counts": {"nodes": len(nodes), "edges": len(edges)}}


@app.get("/", include_in_schema=False)
def app_home():
    """统一前端入口：优先跳转到新 Web 前端（3001）；否则动态渲染当前激活 kb 的落地页。

    不再硬绑 food_app（BP-6）：前端应反映当前激活/建模的本体——建了哪个模检索哪个。
    """
    # 新 Web 前端地址（工厂本体问答 SPA，端口 3001）
    WEB_FRONT = os.environ.get("WEB_FRONT_URL", "http://localhost:3001/")
    # 动态渲染当前激活 kb 的品牌/示例（去硬编码）
    kb_name = KB_NAME
    name = _kb.get("name", "知识库助手")
    icon = _kb.get("icon", "🏭")
    examples = _kb.get("examples", [])
    ex_html = "".join(f"<li>{e}</li>" for e in examples[:6])
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{icon} {name} · 工厂智能体</title>
<style>body{{font-family:'Segoe UI','Microsoft YaHei',sans-serif;background:#eef1f5;color:#2d3436;margin:0;padding:0}}
.wrap{{max-width:680px;margin:60px auto;background:#fff;border:1px solid #d5dbe3;border-radius:8px;padding:32px;box-shadow:0 2px 10px rgba(0,0,0,.05)}}
h1{{font-size:20px}} .kb{{color:#2563eb;font-weight:700}}
.btn{{display:inline-block;margin-top:16px;background:#2563eb;color:#fff;padding:10px 22px;border-radius:6px;text-decoration:none;font-weight:600}}
li{{margin:6px 0}} .foot{{color:#94a3b8;font-size:12px;margin-top:24px}}</style>
</head><body><div class="wrap">
<h1>{icon} {name}</h1>
<p>当前激活知识库：<span class="kb">{kb_name}</span>（跟随激活本体，非固定 food）</p>
<p>示例问题：</p><ul>{ex_html}</ul>
<a class="btn" href="{WEB_FRONT}">打开新版 Web 前端（3001）→</a>
<p class="foot">统一前端入口 · 工厂本体问答套件</p>
</div></body></html>""")


class AskReq(BaseModel):
    question: str
    kb: str = ""  # 多租户: 指定知识库; 缺省用 FOOD_KB(默认 food), 兼容旧调用
    fuse_docs: bool = True  # RAG+本体融合: 结构化命中时是否并行检索文档补充细节/溯源(One Query 全答)


# ── 多租户惰性加载(T-D1 彻底化): 按 kb 加载本体/词典, 缓存多库, 根治串台 ──
# 统一缓存失效(T-D2 稳定化): 缓存记录文件指纹(mtime+size), 每次访问校验 nt/词典文件变更,
# 外部修改词典/本体后自动重载 —— 修"问答数字漂移"(词典改了缓存还用旧词典, 数字对不上)。
_kb_ctx_cache = {}


def _invalidate_kb(kb):
    """统一失效某 kb 的所有缓存(本体/词典 ctx + BM25/向量索引)。

    供回滚/重建/文件变更检测等场景调用; 幂等。同时失效词典(影响规则/向量)与本体
    (影响 BM25/图), 保证"词典/本体一变, 问答即用新数据"。
    """
    kb = (kb or KB_NAME or "food").strip()
    _kb_ctx_cache.pop(kb, None)
    _KB_INDEX_CACHE.pop(f"bm25_{kb}", None)
    _KB_INDEX_CACHE.pop(f"vec_{kb}", None)


def _file_fingerprint(path):
    """文件指纹 (mtime_ns, size)。文件缺失/不可读返回 None。"""
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _get_kb_ctx(kb=None):
    """按 kb 惰性加载并缓存该知识库的本体图+词典+问答数据。

    返回 {graph, labels, vi, rev, D, QDATA, nt_file, lex_file, kb, _fp_nt, _fp_lex}。
    首次访问某 kb 才构建并缓存；之后每次访问校验 nt/词典文件指纹, 文件被外部修改时
    自动失效重载(修"问答数字漂移": 词典变更后缓存仍用旧词典)。kb 无效返回 None。
    """
    kb = (kb or KB_NAME or "food").strip()
    kbc = KBS.get(kb)
    if not kbc:
        return None
    nt_file = os.path.join(ROOT, kbc.get("nt", f"output/{kb}.nt"))
    lex_file = os.path.join(ROOT, "config", kbc.get("lexicon", f"lexicon_{kb}.json"))
    if not os.path.exists(nt_file) or not os.path.exists(lex_file):
        return None
    # 文件变更检测: 缓存命中但 nt/词典指纹变化 → 统一失效, 走重载
    if kb in _kb_ctx_cache:
        ctx = _kb_ctx_cache[kb]
        if (ctx.get("_fp_nt") == _file_fingerprint(nt_file)
                and ctx.get("_fp_lex") == _file_fingerprint(lex_file)):
            return ctx
        _invalidate_kb(kb)  # 文件变了 → 失效(含 BM25/向量索引)
    try:
        g, lb, v, rv = gr.build_graph(nt_file)
        D = v3.load_dict(lex_file)
        QD = v3.build_data(v3.parse_nt(nt_file), D)
    except Exception as e:
        logger.warning(f"kb '{kb}' 加载失败: {e}")
        return None
    ctx = {"graph": g, "labels": lb, "vi": v, "rev": rv,
           "D": D, "QDATA": QD, "nt_file": nt_file, "lex_file": lex_file, "kb": kb,
           "_fp_nt": _file_fingerprint(nt_file), "_fp_lex": _file_fingerprint(lex_file)}
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
        _audit_event("login", role="deny", granted=False, reason="无效或缺失 API Key")
        raise HTTPException(401, "无效或缺失 API Key (需 X-API-Key 头)")
    role = "admin" if _valid(x_api_key, ADMIN_KEY) else "read"
    _audit_event("login", role=role, granted=True, scope="read")


def require_admin(x_api_key: str = Header(default="")):
    """管理端点鉴权(fail-closed): 需匹配 admin key。"""
    if not _valid(x_api_key, ADMIN_KEY):
        _audit_event("login", role="deny", granted=False, reason="需要 admin 权限")
        raise HTTPException(401, "需要管理权限 (admin API Key)")
    _audit_event("login", role="admin", granted=True, scope="admin")

# ── 请求计数(M1.3 metrics) + 审计日志(T3.1) ──
from collections import Counter as _Counter
import threading as _threading
REQ_COUNT = _Counter()
AUDIT_FILE = os.path.join(ROOT, "output", "audit.log")
_AUDIT_LOCK = _threading.Lock()
_AUDIT_MAX_BYTES = 10 * 1024 * 1024  # 单文件上限 10MB, 超出轮转归档(防无限增长)


def _audit(record):
    """追加审计日志(JSONL)。线程安全 + 大小轮转; 失败静默不影响业务。"""
    try:
        os.makedirs(os.path.dirname(AUDIT_FILE), exist_ok=True)
        with _AUDIT_LOCK:
            if os.path.exists(AUDIT_FILE) and os.path.getsize(AUDIT_FILE) > _AUDIT_MAX_BYTES:
                try:
                    os.replace(AUDIT_FILE, AUDIT_FILE + "." + datetime.now().strftime("%Y%m%d_%H%M%S"))
                except Exception:
                    pass
            with open(AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _audit_event(kind, **fields):
    """结构化审计事件(kind=access/login/qa/delivery...)。统一带时间戳, 便于检索/分类。"""
    rec = {"ts": datetime.now().isoformat(), "kind": kind}
    rec.update(fields)
    _audit(rec)


@app.middleware("http")
async def audit_and_count(request: Request, call_next):
    REQ_COUNT[request.url.path] += 1
    start = time.time()
    response = await call_next(request)
    # access 审计: 记录请求角色(admin/read/anon), 供登录/访问审计
    _audit_event("access", method=request.method, path=request.url.path,
                 status=response.status_code,
                 client=request.client.host if request.client else "",
                 ms=int((time.time() - start) * 1000))
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
    return {"status": "ok", "version": "0.2.1"}


@app.get("/api/app-config", include_in_schema=False)
def app_config():
    """APP 动态配置: 返回当前知识库的品牌/图标/示例问题(去硬编码)。"""
    return {
        "ok": True, "kb": KB_NAME,
        "name": _kb.get("name", "知识库助手"),
        "icon": _kb.get("icon", "🏭"),
        "examples": _kb.get("examples", []),
    }



# ── 问答融合辅助逻辑已拆分至 ask_service.py(P2) ─────────────────────────
def _ask_impl(req: AskReq):
    """问答引擎实现(供 ask 端点包装审计后调用)。"""
    ctx = _get_kb_ctx(req.kb)
    if ctx is None:
        kb = (req.kb or KB_NAME).strip()
        return {"ok": False, "error": {"code": 4001,
                "message": f"知识库 '{kb}' 无效或数据缺失(未注册/本体或词典不存在)"}}
    D, QDATA, graph = ctx["D"], ctx["QDATA"], ctx["graph"]
    FOOD_NT, FOOD_LEX = ctx["nt_file"], ctx["lex_file"]
    q = req.question
    # 1. 规则引擎(确定性, 结构化查询) + LLM 润色 + RAG+本体融合
    ans = v3.answer(q, QDATA, D)
    if ans != "暂不支持该问题":
        try:
            import evidence
            raw_ev = evidence.extract_evidence(q, QDATA, D, ans)
        except Exception:
            raw_ev = {}
        ev, structured = _norm_rule_evidence(raw_ev)
        polished = _polish_rule_answer(q, ans)
        if ev:
            # 结构化命中确定数据 → 融合文档补细节/溯源(One Query 全答)
            payload = {"ok": True, "mode": "rule", "answer": polished,
                       "evidence": ev, "engines": ["rule"], "structured": structured,
                       "no_basis": False, "kb": ctx["kb"]}
            if req.fuse_docs:
                payload = _fuse_doc_supplement(q, payload, ctx["kb"])
            return payload
        # 结构化"无记录"(no_basis) → 文档有而本体无时用文档答: 先让文档 RAG 兜底,
        # 文档也无有效依据时保留确定性"无记录"答案(不编造)。
        if req.fuse_docs:
            doc_payload = _doc_rag_fallback(q, ctx["kb"])
            if doc_payload:
                return doc_payload
        return {"ok": True, "mode": "rule", "answer": polished,
                "evidence": [], "engines": ["rule"], "structured": None,
                "no_basis": True, "kb": ctx["kb"]}
    # 1.5 通用跨域校验(P1, 取代原白名单词表): 规则引擎 miss 后, 若问题是一条明确的数据查询
    #     (计数/列表/极值/范围/统计), 且其中引用的实体概念不在该 kb 本体任何实体类/词典
    #     (kb_vocab: entity/type/status/zone/attr/numeric_fields), 则禁止逻辑桥/图检索/混合/
    #     LLM 兜底编造, 强制返回"无相关数据"。横向覆盖所有跨域问题(书/船/测线/冲床/图纸…),
    #     不靠具体词表。非数据查询(开放式/咨询/建议)不拦 —— 由下方 3.4 咨询拦截/LLM 兜底处理。
    try:
        from ontology_qa_v3 import is_cross_domain_data_query
        # 咨询/建议型开放问题即使含"哪些/多少"(如"有哪些需要注意的事项")也非数据查询,
        # 跳过跨域校验, 交给 3.4 咨询拦截生成建议。
        if is_cross_domain_data_query(q, D) and not re.search(
                r"需要注意|注意事项|建议|注意什么|注意哪些|应当注意|应该注意|风险|隐患|"
                r"怎么办|措施|方案|如何|怎么(才能|有效|避免|预防)|意义|作用|影响|经验", q):
            return {"ok": True, "mode": "miss", "answer": "无相关数据（该知识库不含该实体概念）",
                    "evidence": [], "engines": [], "structured": None,
                    "no_basis": True, "kb": ctx["kb"]}
    except Exception:
        pass
    # 2. 逻辑推理桥(LLM转逻辑查询→确定性执行, 借鉴KAG; 覆盖更多开放式问题而不失确定性)
    try:
        import logical_qa
        lres = logical_qa.answer(q, QDATA, D)
        if lres:
            lans, lmode = lres
            try:
                import evidence
                raw_ev = evidence.extract_evidence(q, QDATA, D, lans)
            except Exception:
                raw_ev = {}
            ev, structured = _norm_rule_evidence(raw_ev)
            polished = _polish_rule_answer(q, lans)
            if ev:
                payload = {"ok": True, "mode": "logical", "answer": polished,
                           "evidence": ev, "engines": ["rule"], "structured": structured,
                           "no_basis": False, "kb": ctx["kb"]}
                if req.fuse_docs:
                    payload = _fuse_doc_supplement(q, payload, ctx["kb"])
                return payload
            if req.fuse_docs:
                doc_payload = _doc_rag_fallback(q, ctx["kb"])
                if doc_payload:
                    return doc_payload
            return {"ok": True, "mode": "logical", "answer": polished,
                    "evidence": [], "engines": ["rule"], "structured": None,
                    "no_basis": True, "kb": ctx["kb"]}
    except Exception:
        pass  # 逻辑桥不可用则跳过
    # 3. GraphRAG(LLM 基于图子图作答)
    gans, gctx = gr.answer_graph(q, FOOD_NT, depth=2, max_nodes=40, lexicon=D)
    # P1: 图引擎命中(种子/子图有据)但 LLM 生成空串时, 不得当成"有据命中"(no_basis=False +
    # 空答案 = 假命中)。空串按未答处理, 继续走后续混合/文档/LLM 兜底(no_basis=True), 避免
    # "看似有据实则空答"绕过跨域拦截。
    if gans and not gans.startswith("[图检索]"):
        # 图检索有子图依据: evidence 记录图上下文溯源(来源=graph)
        g_ev = [{"entity": None, "attr": "context", "value": gctx[:1000],
                 "source": "graph", "score": 1.0}] if gctx.strip() else []
        return {"ok": True, "mode": "graphrag", "answer": gans, "context": gctx[:2000],
                "evidence": g_ev, "engines": ["graph"], "structured": None,
                "no_basis": not g_ev, "kb": ctx["kb"]}
    # 3.5 混合检索(BM25 稀疏 + 向量语义, RRF 融合): 先暂存命中, 继续走知识库 doc 配合
    #     权重/阈值统一见 bm25_retrieval.HYBRID_CFG(放宽召回 + 倒数排名融合, 提升复杂问题命中)
    # 3.4 咨询/建议型开放问题拦截: "有什么需要注意/建议/如何/风险"等是寻求建议, 不是列举实体。
    #     这类问题即使字面匹配到实体(如"化工"→Chem_*), 语义也是咨询, 实体列举是错答。
    #     直接走 LLM 兜底生成建议, 避免"（混合检索）找到相关实体: ..."误导。
    _ADVICE_RE = re.compile(
        r"有什么需要注意|注意事项|注意些什么|建议|应当注意|应该注意|需要警惕|"
        r"如何(才能|有效|更好|避免|预防|防范|降低|减少|提高|确保)|怎么(才能|有效|避免|预防)|"
        r"风险管理|安全事项|存在哪些风险|有哪些风险|风险有哪些|安全隐患|合规|规范要求|"
        r"需要注意|怎么办|意义|作用|影响|注意什么|流程是|做法是|标准是|原则|"
        r"风险(需要|应该|要)注意|注意(哪些|什么)", re.I)
    # 咨询/建议型问题命中特征词 → 拦截(即便含"哪些/什么"等, 咨询语义优先)
    _ADVICE_HIT = _ADVICE_RE.search(q)
    # 排除"明确列举实体"类问题: 含具体实体对象词(设备/产品/客户/批次等) + 多少/哪些, 走正常检索
    _ENTITY_LIST = re.search(
        r"(设备|产品|客户|批次|原料|机器|项目|订单|班组|测线|炮点|机组|装置|台账)\s*(有哪些|有多少|几个|多少|类型)", q)
    if _ADVICE_HIT and not _ENTITY_LIST:
        fallback = _llm_fallback_answer(q, KBS.get(ctx["kb"], {}).get("name", "知识库"))
        if fallback:
            return {"ok": True, "mode": "miss", "answer": fallback,
                    "evidence": [], "engines": [], "structured": None,
                    "no_basis": True, "kb": ctx["kb"]}
    hybrid_payload = None
    hit_engines = []
    try:
        from bm25_retrieval import BM25Index, HYBRID_CFG, rrf_fuse
        from vector_retrieval import VectorIndex
        bm_key, vec_key = f"bm25_{ctx['kb']}", f"vec_{ctx['kb']}"
        if bm_key not in _KB_INDEX_CACHE:
            _KB_INDEX_CACHE[bm_key] = BM25Index.from_graph(graph)
        if vec_key not in _KB_INDEX_CACHE:
            _KB_INDEX_CACHE[vec_key] = VectorIndex.from_graph(graph, lexicon=D)
        _b, _v = HYBRID_CFG["bm25"], HYBRID_CFG["vector"]
        bm_hits = _KB_INDEX_CACHE[bm_key].search(q, top_k=_b["top_k"], min_score=_b["min_score"])
        vec_hits = _KB_INDEX_CACHE[vec_key].search(q, top_k=_v["top_k"], min_score=_v["min_score"])
        fused = rrf_fuse(bm_hits, vec_hits, HYBRID_CFG)
        if fused:
            # 记录实际命中的引擎: bm25 / vector
            if bm_hits:
                hit_engines.append("bm25")
            if vec_hits:
                hit_engines.append("vector")
            ents = "、".join(f["entity"] for f in fused)
            ev = [{"entity": f["entity"], "attr": None,
                   "value": (f.get("hit") or {}).get("value")
                            or (f.get("hit") or {}).get("text") or f["entity"],
                   "source": "hybrid", "score": f["rrf"]}
                  for f in fused[:5]]
            hybrid_payload = {"ok": True, "mode": "hybrid",
                              "answer": f"（混合检索）找到相关实体: {ents}",
                              "evidence": ev, "engines": hit_engines,
                              "structured": None, "no_basis": not ev, "kb": ctx["kb"]}
    except Exception:
        pass
    # 3.75 文档知识库 RAG(doc 融合引擎): 本体/图答不上时, 检索该 kb 已入库文档
    #      说明书/规范/PDF 文档知识, 返回带溯源的答案。优先于 hybrid 占位。
    try:
        from knowledge.rag import answer as _rag_answer
        from knowledge.store import KnowledgeStore
        _kbdir = _kb_dir(ctx["kb"])
        if _kbdir is not None:
            _store = KnowledgeStore(_kbdir)
            _res = _rag_answer(None, q, _store, top_k=5)
            _ans = (_res or {}).get("answer", "") or ""
            _ev = (_res or {}).get("evidence", []) or []
            # 相关性闸门: 只保留与问题共享 >=2 个滑动二元组的切块, 过滤跨主题文档
            _ev = [e for e in _ev if _fuse_chunk_relevant(q, e.get("chunk") or "")]
            _invalid = not _ans.strip() or _ans.startswith("[") or "片段未覆盖" in _ans or not _ev
            if not _invalid:
                return {"ok": True, "mode": "kb_rag", "answer": _ans,
                        "evidence": _norm_doc_evidence(_ev), "engines": ["doc"],
                        "structured": None, "no_basis": False, "kb": ctx["kb"]}
    except Exception:
        pass
    # 知识库无有效答案: 若本体 hybrid 命中了实体, 不再直接输出"找到相关实体"占位(那是调试信息, 用户不可读)。
    # 把命中的实体作为线索喂给 LLM 兜底, 让它基于实体生成可读回答; 实体列表仅作为 evidence 溯源保留。
    # 这样"功率最大的设备"(数据无功率字段)会得到诚实的自然语言回答, 而非罗列 Chem_equipment_*。
    if hybrid_payload is not None:
        try:
            from model_llm import llm_generate
            _hint = "\n".join(f"- {f['entity']}" for f in (hybrid_payload.get("evidence") or [])[:8])
            _prompt = (
                f"知识库中可能相关的实体: {_hint or '(无)'}\n"
                f"用户问题: {q}\n"
                "请基于以上实体线索回答。若实体与问题无直接关系(如问题问极值/属性但实体是类名), "
                "如实说明知识库中没有该数据, 不要编造数字。回答简洁。"
            )
            _llm_ans = llm_generate(_prompt)
            if _llm_ans:
                return {"ok": True, "mode": "hybrid", "answer": _llm_ans,
                        "evidence": hybrid_payload.get("evidence", []),
                        "engines": hit_engines, "structured": None,
                        "no_basis": True, "kb": ctx["kb"]}
        except Exception:
            pass
        return hybrid_payload
    # 4. LLM 兜底: 全部检索答不上 → LLM 生成理解性回答, evidence 空数组(无依据)
    fallback = _llm_fallback_answer(q, KBS.get(ctx["kb"], {}).get("name", "知识库"))
    if fallback:
        return {"ok": True, "mode": "miss", "answer": fallback,
                "evidence": [], "engines": [], "structured": None,
                "no_basis": True, "kb": ctx["kb"]}
    # 4.5 兜底失败(LLM 不可用/问题无关): 从 KB 配置读示例引导(去硬编码)
    examples = KBS.get(ctx["kb"], {}).get("examples", ["乳制品的数量", "原味酸奶是什么"])
    guide = "\n".join(f"· {e}" for e in examples[:5])
    return {"ok": True, "mode": "miss", "answer": f"抱歉，暂未理解该问题。\n可试试问：\n{guide}",
            "evidence": [], "engines": [], "structured": None, "no_basis": True,
            "kb": ctx["kb"]}


@app.post("/api/ask", dependencies=[Depends(require_key)])
def ask(req: AskReq):
    """自然语言问答(对齐蓝图融合检索): 规则→逻辑→图谱→BM25→向量(RRF)→知识库doc→LLM兜底。

    返回(兼容旧字段 + 蓝图字段):
      {ok, mode, answer, evidence:[{entity,attr,value,source,score}],
       engines:[rule/graph/vector/bm25/doc], structured?, no_basis, kb}
    融合链每步命中即记录 engines; 全部答不上时 LLM 兜底, evidence 空数组 + no_basis=True。
    """
    start = time.time()
    result = _ask_impl(req)
    # P1 跨域拦截兜底: 任何引擎若产出"空答案", 一律判为无据(no_basis=True)——
    # no_basis=False + 空答案 是"看似有据实则空答"的假命中, 必须归为无据, 防绕过跨域拦截。
    try:
        if result and not str(result.get("answer") or "").strip():
            result["no_basis"] = True
    except Exception:
        pass
    # 问答审计: 记录问题/知识库/命中引擎/模式, 供追溯与质量分析
    try:
        _audit_event("qa", kb=req.kb or KB_NAME, question=req.question,
                     mode=result.get("mode"), engines=result.get("engines", []),
                     no_basis=result.get("no_basis"),
                     ans_len=len(str(result.get("answer", ""))),
                     ms=int((time.time() - start) * 1000))
    except Exception:
        pass
    return result


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
def stats(kb: str = Query("", description="知识库名")):
    """知识库统计（按 kb 隔离，不串台）。"""
    if not kb:
        kb = KBS.get("_default", "") or "food"
    ctx = _get_kb_ctx(kb)
    if not ctx:
        return {"ok": False, "error": f"kb '{kb}' 未建模或加载失败"}
    g = ctx["graph"]
    # 用 QDATA(实例字典)统计实例：key 形如 <Entity>_<field>_<ID>
    qd = ctx.get("QDATA") or {}
    inst_count = {}
    for k in qd:
        local = str(k).split("/")[-1]
        m = re.match(r"^([A-Za-z_]+?)_[A-Za-z0-9_]+$", local)
        if m:
            cls = m.group(1)
            inst_count[cls] = inst_count.get(cls, 0) + 1
    return {"ok": True, "entities": inst_count, "entity_count": sum(inst_count.values()),
            "nodes": len(g), "edges": sum(len(v) for v in g.values())}


# ════════════════════════════════════════════════════════════════════════
# 契约端点 v1.0 — knowledge / eval / assets / version（增量，不影响既有接口）
# 统一响应信封: {ok, data?, error?, elapsed_s}
# 错误码: 4001参数 4041不存在 4091冲突 5001引擎 5031模型
# 多租户兼容: kb 参数隔离存储目录; 写操作幂等
# ════════════════════════════════════════════════════════════════════════

CONTRACT_VERSION = "1.0"
FEATURES = ["knowledge", "eval", "assets", "version", "trace", "qa", "ontology", "stats"]

# 文档知识库存储根 + 临时上传目录(每 kb 一个隔离子目录)
_TMP_UPLOAD = os.path.join(ROOT, "output", "_tmp_uploads")
_ASSET_DIR = os.path.join(ROOT, "output", "asset_versions")
_ASSET_MANIFEST = os.path.join(_ASSET_DIR, "manifest.json")


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
    # 失效缓存, 下次 /api/ask 加载回滚后的本体(统一失效: ctx + BM25/向量索引)
    _invalidate_kb(kb)
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
    label: str = ""          # 版本标签(如 "交付基线"/"词典补词")
    changelog: str = ""      # 变更说明
    created_by: str = "human"  # 触发方: human/review/loop


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
            # 用用户上传的原始文件名(去扩展名)作为标题, 便于辨识/删除,
            # 避免 extract_text 默认用时间戳临时文件名(如 {time_ns()})做 title。
            doc["title"] = os.path.splitext(fname)[0]
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
    _audit_event("delivery", action="snapshot", kb=kb, version=version,
                 label=req.label, created_by=req.created_by)
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
    _audit_event("delivery", action="rollback", kb=kb, version=req.version, active=v)
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
        "version": getattr(app, "version", "0.2.1"),
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
    # 白名单: 接受 codes 下 data 开头的一级目录(data/data_valve/data_chem...)、data/ 内、output/ 内。
    # 拒绝绝对路径到 codes 外 / .. 穿越。
    _rp = os.path.realpath(src_abs)
    try:
        _rel = os.path.relpath(_rp, ROOT)
        _top = _rel.split(os.sep)[0]
        _ok = _rp.startswith(out_root + os.sep) or (not _rel.startswith("..") and (_top == "data" or _top.startswith("data") or _top == "output"))
    except Exception:
        _ok = False
    if not _ok:
        return _err_env(4001, f"数据源必须在 data*/ 或 output/ 内(防路径穿越): {src}", start)
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
    # 建模成功 → 把该 kb 设为当前激活(写 web_state.json, 前端 getCurrentKb 优先读取),
    # 让"建哪个激活哪个"真正通(直接调 API 建模也切激活, 不依赖前端显式 setCurrentKb)
    try:
        _set_active_kb(kb, nt_rel, lex_rel)
    except Exception as e:
        logger.warning(f"web_state.json 激活 kb 更新失败: {e}")
    # 失效该 kb 缓存, 下次 /api/ask 重新加载新本体(统一失效: ctx + BM25/向量索引)
    _invalidate_kb(kb)
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


class KbsExamplesReq(BaseModel):
    """更新某 kb 的评测示例问题(examples)。闭源/FDE 用 solo 起草的问题集写入。"""
    examples: list[str]


@app.post("/api/kbs/{kb}/examples", dependencies=[Depends(require_key)])
def kbs_update_examples(kb: str, req: KbsExamplesReq):
    """把 solo/闭源起草的评测问题写入 kb 的 examples，供 benchmark 基线使用。

    一企业一行业一数据：examples 跟随该 kb 的行业，替换为 FDE 校准后的问题集。
    空列表 = 清空该 kb 示例。
    """
    start = time.time()
    kb = _safe_doc_id(kb or "")
    if not kb:
        return _err_env(4001, "非法 kb 名", start)
    try:
        data = json.load(open(KBS_FILE, encoding="utf-8"))
        kbs = data.setdefault("kbs", {})
        entry = kbs.setdefault(kb, {})
        entry["examples"] = [str(q).strip() for q in req.examples if str(q).strip()]
        with open(KBS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        global KBS
        KBS = _load_kbs()
    except Exception as e:
        logger.warning(f"API内部错误[更新examples失败]: {e}")
        return _err_env(5001, "更新示例问题失败", start)
    return _ok_env({"kb": kb, "examples": entry.get("examples", [])}, start)


def _set_active_kb(kb, nt_rel, lex_rel):
    """把 kb 设为当前激活, 持久化到 web/web_state.json(前端 getCurrentKb 优先读取该文件)。

    '建哪个激活哪个': 建模成功后把激活 kb 同步到前端状态, 使界面/查询/看板跟随新本体。
    保留原 web_state 其他字段(table/nt/lexicon), 仅更新 kb 字段; 文件缺失则新建。
    """
    web_state_path = os.path.join(os.path.dirname(ROOT), "web", "web_state.json")
    state = {}
    if os.path.exists(web_state_path):
        try:
            state = json.load(open(web_state_path, encoding="utf-8")) or {}
        except Exception:
            state = {}
    state["kb"] = kb
    if nt_rel:
        state["nt"] = nt_rel
    if lex_rel:
        state["lexicon"] = lex_rel
    state.setdefault("table", kb)
    # 不再覆盖 state["kb"]：kb 的唯一真相是 users.json 的 user.kb（前端 index.js 每请求按 user.kb 设激活）。
    # 后端若写全局 kb，会在多用户/与前端并发时互相覆盖，导致 A 企业本体被 B 企业污染。
    # '建哪个激活哪个' 由前端按 user.kb 跟随实现（单企业收敛，user.kb 即刚建的 kb）。
    os.makedirs(os.path.dirname(web_state_path), exist_ok=True)
    with open(web_state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    logger.info("已持久化激活 kb 信息(nt/lexicon) -> %s (kb 由 user.kb 决定)", web_state_path)


@app.get("/api/industry/list", dependencies=[Depends(require_key)])
def industry_dict_list():
    """列出公共工业本体词典集(00基础+01泵阀+02化工+03地质)及各规模。"""
    try:
        from industrial_dict_loader import _load_public, _DICT_DIR
        items = []
        for fn in sorted(os.listdir(_DICT_DIR)):
            if not fn.endswith(".json") or fn == "index.json":
                continue
            fp = os.path.join(_DICT_DIR, fn)
            d = json.load(open(fp, encoding="utf-8"))
            items.append({
                "file": fn,
                "description": d.get("description", ""),
                "type": len(d.get("type_cn2en", {})),
                "status": len(d.get("status_cn2en", {})),
                "synonym": len(d.get("synonym_map", {})),
                "entity": len(d.get("entity_cn2en", {})),
            })
        return {"ok": True, "items": items}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/industry/absorb", dependencies=[Depends(require_key)])
async def industry_dict_absorb(req: Request):
    """吸收企业词典 → 行业词典。body: {lexicon: 企业词典路径, industry: 行业名}
    行业名: 泵阀/精细化工/地球物理/基础。返回吸收前后 type 数。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    lexicon = body.get("lexicon", "")
    industry = body.get("industry", "基础")
    if not lexicon or not os.path.exists(lexicon):
        return {"ok": False, "error": f"企业词典不存在: {lexicon}"}
    try:
        from absorb_public_dict import load_public, save_public, merge_into_public
        from collections import Counter
        d = json.load(open(lexicon, encoding="utf-8"))
        counter = Counter()
        for key in ("entity_cn2en", "type_cn2en", "fault_cn2en"):
            for cn in (d.get(key) or {}):
                if cn and len(cn) >= 2:
                    counter[cn] += 1
        # 复用 absorb_from_counter 提炼
        from absorb_public_dict import absorb_from_counter
        suggestions = absorb_from_counter(counter, threshold=1, verbose=False)
        pub = load_public(industry)
        before = len(pub.get("type_cn2en", {}))
        pub, changed = merge_into_public(pub, suggestions)
        if changed:
            save_public(pub, industry)
            after = len(pub.get("type_cn2en", {}))
            return {"ok": True, "industry": industry, "before": before, "after": after,
                    "added": after - before}
        return {"ok": True, "industry": industry, "before": before, "after": before, "added": 0}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/industry/export", dependencies=[Depends(require_key)])
def industry_dict_export(industry: str = Query("泵阀"), download: bool = Query(False)):
    """导出行业词典。industry: 泵阀/精细化工/地球物理/基础。
    download=true 返回文件下载, 否则返回 JSON。"""
    try:
        from absorb_public_dict import load_public, INDUSTRY_FILES
        fn = INDUSTRY_FILES.get(industry, "00_basis.json")
        pub = load_public(industry)
        export_dir = os.path.join(ROOT, "..", "dict_export")
        os.makedirs(export_dir, exist_ok=True)
        out = os.path.join(export_dir, fn)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(pub, f, ensure_ascii=False, indent=2)
        if download:
            return FileResponse(out, filename=fn, media_type="application/json")
        return {"ok": True, "file": out, "industry": industry, "type": len(pub.get("type_cn2en", {}))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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