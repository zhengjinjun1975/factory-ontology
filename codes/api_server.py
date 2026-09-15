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


def _read_app_version():
    """版本单一事实源 = codes/run.py 的 __version__。

    前端 web/server/index.js 用同一套正则读同一个文件；这里保持一致，
    避免"前端显示 0.2.1、后端 /health 显示 0.2.2"的漂移（2026-09-15 实测）。
    """
    try:
        with open(os.path.join(ROOT, "run.py"), encoding="utf-8") as f:
            m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', f.read())
            return m.group(1) if m else "0.0.0"
    except Exception:
        return "0.0.0"


APP_VERSION = _read_app_version()

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
import schema_ontology as so
import fusion
import query_understand
import evidence_norm

# 问答融合辅助层(P2 拆分): 证据归一化/LLM润色兜底/RAG+本体融合/文档目录等无状态逻辑
from ask_service import (  # noqa: E402
    _kb_dir,
    _norm_rule_evidence,
    _norm_doc_evidence,
    _polish_rule_answer,
    _llm_fallback_answer,
    _fuse_doc_supplement,
    _doc_rag_fallback,
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


def _nt_path_for(kb):
    """kb → 本体文件路径(读 kbs.json 配置, 不假定任何行业)。"""
    kbc = KBS.get(kb) or {}
    return os.path.join(ROOT, kbc.get("nt", f"output/{kb}.nt"))


def _ensure_ontology(kb):
    """通用本体构建: 由 kb 注册表里配置的 schema 驱动。

    取代原先写死 food 表名/关系名的引导逻辑 —— 任何行业同一套代码:
      · kb 配了 schema → 按 schema + data_dir 重建 nt(schema 驱动, 域无关)
      · kb 没配 schema → 视为外部(闭源注册表)已生成, 原样复用, 不做任何假设
    这样新增行业只需在 kbs.json 里declare schema, 代码零改动。
    """
    kbc = KBS.get(kb) or {}
    schema_rel = kbc.get("schema")
    if not schema_rel:
        return
    nt_file = _nt_path_for(kb)
    data_dir = os.path.join(ROOT, kbc.get("data_dir", "data"))
    schema_path = os.path.join(ROOT, schema_rel)
    if not (os.path.exists(schema_path) and os.path.isdir(data_dir)):
        return
    try:
        data = so.load_all(data_dir)
        schema = so.load_schema(schema_path)
        schema = so.fill_iris(schema)
        lines = so.to_nt(data, schema)
        os.makedirs(os.path.dirname(nt_file), exist_ok=True)
        with open(nt_file, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        logger.info("本体已按 schema 重建: kb=%s nt=%s", kb, nt_file)
    except Exception as e:
        logger.warning("schema 驱动重建失败(沿用现有本体): kb=%s err=%s", kb, e)


def _load(kb=None):
    kb = (kb or KB_NAME).strip()
    _ensure_ontology(kb)
    nt_file = _nt_path_for(kb)
    if not os.path.exists(nt_file):
        raise FileNotFoundError(
            f"本体文件缺失: {nt_file}。请在 kbs.json 里为 kb={kb} 配置 schema(自动构建),"
            f"或先由建模流程生成该 .nt。")
    graph, labels, vi, rev = gr.build_graph(nt_file)
    return graph, labels, vi, rev


def _reload(kb=None):
    """通用重载: 失效该 kb 缓存后按配置重建/重载本体。

    取代原先三处写死 food.nt/food_data_hash.txt 的"删文件再重建"逻辑 ——
    任何 kb 同一套路径, 新增行业无需改代码。
    """
    kb = (kb or KB_NAME).strip()
    _invalidate_kb(kb)
    return _load(kb)


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

app = FastAPI(title="食品企业知识库 API", version=APP_VERSION,
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
    # 局部名 → 中文显示名(类名/关系名/属性名通用)
    lb_local = {}
    for _k, _v in lb.items():
        lb_local[_k.split("#")[-1].split("/")[-1]] = _v
    # 实体类(从URI前缀推断: NS + <类名>_<实例id>); 仅保留实例节点(带 _ 的)
    for uri, props in g.items():
        if uri == RDF_TYPE:
            continue
        nm = lb.get(uri, uri.split("#")[-1].strip("<>"))
        local = uri.split("#")[-1].strip("<>")
        if "_" not in local:
            continue  # 跳过属性/关系/类声明节点(无实例id)
        entity = local.rsplit("_", 1)[0]
        # 实例节点: 若没有中文 label，就用它的首个可读字符串属性值作显示名 ——
        # 否则图上只剩英文标头(productName 之类)而看不到"闸阀 Z41H-16C"这种具体值。
        disp = nm
        if nm == local:
            for k, vals in props.items():
                if k in ("type", "label") or not vals:
                    continue
                v = str(vals[0])
                if v and not v.startswith("http") and not v.replace(".", "").isdigit():
                    disp = v
                    break
            if disp == local:
                # 关联明细这类没有可读字符串属性的实例：显示「类名 + 实例号」，不露完整 URI 局部名。
                # 判据域无关：类名不含数字(实例键才含)，且是 local 的真前缀；取最长匹配。
                # 任意行业(阀门/食品/...)同一套逻辑，不针对某个域写死。
                best, cls_label = "", ""
                for _ck, _cv in lb_local.items():
                    if not _ck or any(ch.isdigit() for ch in _ck):
                        continue
                    if local == _ck or local.startswith(_ck + "_"):
                        if len(_ck) > len(best):
                            best, cls_label = _ck, (_cv if _cv != _ck else _ck)
                if best:
                    inst_id = local[len(best) + 1:] if local.startswith(best + "_") else ""
                    disp = f"{cls_label} {inst_id}".strip()
                else:
                    disp = local
        nodes.append({"id": uri, "name": disp, "entity": entity})
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
                    edges.append({"from": uri, "to": vv, "rel": lb_local.get(rel, rel)})
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
    context: dict = None  # 会话上下文(可选): 供指代消解/实体消歧, 形如 {"entity": "P005"}
    # 深度召回(默认关): 关=确定性命中即返回(秒回, 现状); 开=规则/逻辑命中后仍继续跑
    # graph/混合/文档, 由 fusion 统一融合(证据更全, 但每问都要等最慢一路, 响应变慢)。
    deep_recall: bool = False


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
    # 表前缀从 kb 配置读(不写死 food_)；未配置则按原名 —— 任何行业同一套代码
    _prefix = (KBS.get(KB_NAME) or {}).get("table_prefix", "")
    target = table if not _prefix or table.startswith(_prefix) else f"{_prefix}{table}"
    dest = os.path.join(DATA, f"{target}.csv")
    os.makedirs(DATA, exist_ok=True)
    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)
    # 重建(强制, 让新数据生效) —— 通用路径: 按 kb 配置重载, 不写死文件名
    global graph, labels, vi, rev, QDATA
    graph, labels, vi, rev = _reload()
    QDATA = v3.build_data(v3.parse_nt(_nt_path_for(KB_NAME)), D)
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


# ── 溯源审计链(可选, 零依赖) ──────────────────────────
_AUDIT = None


def _audit_chain():
    """惰性初始化审计链。默认存 temp, 可 AUDIT_DB=<path> 指定; 失败静默(审计不阻断主流程)。"""
    global _AUDIT
    if _AUDIT is None:
        try:
            from audit_chain import AuditChain
            _AUDIT = AuditChain(os.environ.get("AUDIT_DB"))
        except Exception as e:
            logger.warning(f"审计链不可用(降级跳过): {e}")
            _AUDIT = False
    return _AUDIT if _AUDIT else None


def _audit_trace(direction, query, result):
    """记录一次溯源到审计链。失败静默, 绝不影响主响应。"""
    ac = _audit_chain()
    if not ac:
        return
    try:
        summary = result.get("affected_batches") or result.get("product") or ""
        ac.record_trace(source=query, relation="trace_" + direction,
                        target=str(summary)[:500], detail="")
    except Exception:
        pass  # 审计失败不影响溯源主结果


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
    graph, labels, vi, rev = _reload()
    QDATA = v3.build_data(v3.parse_nt(_nt_path_for(KB_NAME)), D)
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
    graph, labels, vi, rev = _reload()
    QDATA = v3.build_data(v3.parse_nt(_nt_path_for(KB_NAME)), D)
    return {"ok": True, "kb": KB_NAME, "imported": imported, "nodes": len(graph), "message": "已实时同步"}


@app.get("/metrics", include_in_schema=False)
def metrics():
    """轻量指标: 各端点请求计数(供监控/排障)。"""
    return {"ok": True, "requests": dict(REQ_COUNT), "total": sum(REQ_COUNT.values())}


@app.get("/health")
def health():
    return {"status": "ok", "version": APP_VERSION}


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
    # 0. 查询理解(前置): 意图分类 + 实体消歧 + 指代消解 + 跨域判定
    rel = query_understand.understand(q, D, req.context or None)  # 传上下文以启用指代消解(P1-4)
    candidates = []  # 检索类候选(graph/hybrid/doc), 交 fusion 统一裁决
    _graph_context = ""  # 图检索上下文(原 graphrag 返回的 context 字段, 收口时补回)
    _schema = {"namespace": f"http://factory.example/{ctx['kb']}#"}  # 证据 IRI 补全(按 kb 命名空间)
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
            # 统一融合出口: 规则确定性答案经 fusion 收口(补 confidence=high + 证据归一),
            # 与 graph/hybrid/doc 走同一出口。单候选 → answer 不变。
            _cand = {
                "answer": payload["answer"],
                "evidence": evidence_norm.normalize_evidence(payload.get("evidence") or [], _schema),
                "source": "rule", "score": fusion.CAND_SCORE["rule"],
                "structured": payload.get("structured"),
            }
            if req.deep_recall:
                # 深度召回: 不即返, 入候选池继续跑 graph/混合/文档, 末尾统一融合
                candidates.append(_cand)
            else:
                try:
                    return fusion.fuse(q, [_cand], cross_domain=False, kb=ctx["kb"], schema=_schema)
                except Exception as e:  # P2-7: 收口异常不得 500, 回落原确定性 payload
                    logger.warning(f"fusion 收口异常(rule), 回落原 payload: {e}")
                    return payload
        else:
            # 结构化"无记录"(no_basis) → 文档有而本体无时用文档答: 先让文档 RAG 兜底,
            # 文档也无有效依据时保留确定性"无记录"答案(不编造)。
            # 注意: 必须用 else 隔离 —— 有据(ev)且 deep_recall 时不返回, 要继续走后续引擎到收口;
            # 若沿用裸 if, 会掉进本分支直接返回空证据"无记录"(deep 模式规则证据丢失的 bug)。
            if req.fuse_docs:
                doc_payload = _doc_rag_fallback(q, ctx["kb"])
                if doc_payload:
                    doc_payload.setdefault("confidence", "low")
                    return doc_payload
            return {"ok": True, "mode": "rule", "answer": polished,
                    "evidence": [], "engines": ["rule"], "structured": None,
                    "no_basis": True, "confidence": "none", "kb": ctx["kb"]}
    #     (计数/列表/极值/范围/统计), 且其中引用的实体概念不在该 kb 本体任何实体类/词典
    #     (kb_vocab: entity/type/status/zone/attr/numeric_fields), 则禁止逻辑桥/图检索/混合/
    #     LLM 兜底编造, 强制返回"无相关数据"。横向覆盖所有跨域问题(书/船/测线/冲床/图纸…),
    #     不靠具体词表。非数据查询(开放式/咨询/建议)不拦 —— 由下方 3.4 咨询拦截/LLM 兜底处理。
    try:
        # 咨询/建议型开放问题即使含"哪些/多少"(如"有哪些需要注意的事项")也非数据查询,
        # 跳过跨域校验, 交给 3.4 咨询拦截生成建议。
        # 只信 is_cross_domain(它本身即"是数据查询 + 引用库外概念"的判定)，
        # 不再叠加 intent=="data_query"——_intent 词表口径更窄(缺"几台/几本")会漏拦。
        # ponytail: 跨域判定单一来源 = ontology_qa_v3.is_cross_domain_data_query。
        if not candidates and rel.get("is_cross_domain") and not re.search(
                r"需要注意|注意事项|建议|注意什么|注意哪些|应当注意|应该注意|风险|隐患|"
                r"怎么办|措施|方案|如何|怎么(才能|有效|避免|预防)|意义|作用|影响|经验", q):
            # 走 fusion 的 cross_domain 拒答分支(文案单一来源: fusion.NO_DATA_ANSWER)
            return fusion.fuse(q, [], cross_domain=True, kb=ctx["kb"])
    except Exception:
        logger.warning("跨域判定异常, 放行至后续引擎(未拦截)", exc_info=True)
    # 2. 逻辑推理桥(LLM转逻辑查询→确定性执行, 借鉴KAG; 覆盖更多开放式问题而不失确定性)
    try:
        import logical_qa
        lres = logical_qa.answer(q, QDATA, D)
        if lres:
            lans, lmode = lres
            # 逻辑桥命中 = 确定性执行器产物(如"符合条件的记录有N条"), 答案本身即依据,
            # 不用规则专用 extract_evidence(只认 count/extreme/top_n 规则模式, 对逻辑查询答案
            # 提取常空→误判 no_basis)。直接以确定性答案落一条 logical 证据。
            try:
                import evidence
                raw_ev = evidence.extract_evidence(q, QDATA, D, lans)
            except Exception:
                raw_ev = {}
            ev, structured = _norm_rule_evidence(raw_ev)
            if not ev:
                ev = [{"entity": None, "attr": "logical_query", "value": lans[:500],
                       "source": "logical", "score": 1.0}]
            polished = _polish_rule_answer(q, lans)
            payload = {"ok": True, "mode": "logical", "answer": polished,
                       "evidence": ev, "engines": ["logical"], "structured": structured,
                       "no_basis": False, "kb": ctx["kb"]}
            if req.fuse_docs:
                payload = _fuse_doc_supplement(q, payload, ctx["kb"])
            # 统一融合出口: 逻辑桥确定性答案经 fusion 收口(同 rule 路径)
            _cand = {
                "answer": payload["answer"],
                "evidence": evidence_norm.normalize_evidence(payload.get("evidence") or [], _schema),
                "source": "logical", "score": fusion.CAND_SCORE["logical"],
                "structured": payload.get("structured"),
            }
            if req.deep_recall:
                candidates.append(_cand)
            else:
                try:
                    return fusion.fuse(q, [_cand], cross_domain=False, kb=ctx["kb"], schema=_schema)
                except Exception as e:
                    logger.warning(f"fusion 收口异常(logical), 回落原 payload: {e}")
                    return payload
    except Exception:
        logger.warning("逻辑桥异常, 跳过", exc_info=True)
    # 3. GraphRAG(LLM 基于图子图作答)
    gans, gctx = gr.answer_graph(q, FOOD_NT, depth=2, max_nodes=40, lexicon=D)
    # P1: 图引擎命中(种子/子图有据)但 LLM 生成空串时, 不得当成"有据命中"(no_basis=False +
    # 空答案 = 假命中)。空串按未答处理, 继续走后续混合/文档/LLM 兜底(no_basis=True), 避免
    # "看似有据实则空答"绕过跨域拦截。
    if gans and not gans.startswith("[图检索]"):
        # 图检索有子图依据: evidence 记录图上下文溯源(来源=graph)
        g_ev = [{"entity": None, "attr": "context", "value": gctx[:1000],
                 "source": "graph", "score": 1.0}] if gctx.strip() else []
        candidates.append({"answer": gans, "evidence": g_ev, "source": "graph",
                           "score": fusion.CAND_SCORE["graph"], "structured": None})
        _graph_context = gctx[:2000]
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
    _ADVICE_HIT = _ADVICE_RE.search(q) or rel.get("intent") == "advice"
    # 排除"明确列举实体"类问题: 含具体实体对象词(设备/产品/客户/批次等) + 多少/哪些, 走正常检索
    _ENTITY_LIST = re.search(
        r"(设备|产品|客户|批次|原料|机器|项目|订单|班组|测线|炮点|机组|装置|台账)\s*(有哪些|有多少|几个|多少|类型)", q)
    if not candidates and _ADVICE_HIT and not _ENTITY_LIST:
        fallback = _llm_fallback_answer(q, KBS.get(ctx["kb"], {}).get("name", "知识库"))
        if fallback:
            return {"ok": True, "mode": "miss", "answer": fallback,
                    "evidence": [], "engines": [], "structured": None,
                    "no_basis": True, "confidence": "none", "kb": ctx["kb"]}
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
            candidates.append({"answer": hybrid_payload["answer"], "evidence": ev,
                               "source": "hybrid", "score": fusion.CAND_SCORE["hybrid"],
                               "engines": hit_engines, "structured": None})
    except Exception:
        logger.warning("混合检索异常, 跳过 hybrid 候选", exc_info=True)
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
                candidates.append({"answer": _ans, "evidence": _norm_doc_evidence(_ev),
                                   "source": "doc", "score": fusion.CAND_SCORE["doc"], "structured": None})
    except Exception:
        logger.warning("文档 RAG 异常, 跳过 doc 候选", exc_info=True)
    # 3.9 融合决策收口(检索类 graph/hybrid/doc 产候选 → 统一裁决, 确定性优先)
    #     hybrid 命中不进收口: 保留原"喂 LLM 兜底生成可读回答"路径(下方 847 段),
    #     避免把"（混合检索）找到相关实体: ..."这类不可读占位直接返回给用户。
    if candidates:
        # 证据统一归一到标准 Evidence 结构(补 class/iri), 供融合去重与后续互操作
        # ponytail: 归一放在收口前一处, 而非各引擎各归一一次。
        for _c in candidates:
            _c["evidence"] = evidence_norm.normalize_evidence(_c.get("evidence") or [], _schema)
        try:
            _fused = fusion.fuse(q, candidates, cross_domain=False, kb=ctx["kb"], schema=_schema)
        except Exception as e:
            logger.warning(f"fusion 收口异常, 回落首个候选: {e}")
            _fused = None
        if _fused is None:
            _first = candidates[0]
            _fused = {"ok": True, "mode": _first.get("source") or "none",
                      "answer": _first.get("answer") or "",
                      "evidence": _first.get("evidence") or [],
                      "engines": [c.get("source") for c in candidates if c.get("source")],
                      "structured": None, "no_basis": not _first.get("evidence"),
                      "confidence": "low", "kb": ctx["kb"]}
        if _fused.get("mode") != "hybrid":
            if _fused.get("mode") == "graphrag" and _graph_context:
                _fused["context"] = _graph_context  # 恢复原 graphrag 的 context 字段
            return _fused
    # 知识库无有效答案: 若本体 hybrid 命中了实体, 不再直接输出"找到相关实体"占位(那是调试信息, 用户不可读)。
    # 把命中的实体作为线索喂给 LLM 兜底, 让它基于实体生成可读回答; 实体列表仅作为 evidence 溯源保留。
    # 这样"功率最大的设备"(数据无功率字段)会得到诚实的自然语言回答, 而非罗列 Chem_equipment_*。
    if hybrid_payload is not None:
        # 原来这里把命中实体喂给本地小模型"生成可读回答"。实测小模型会输出推理独白
        # （"先确认问题：…但子图里原料只列了 R001 到 R010…没有给任何库存数值"），
        # 用户看到的是模型的思考过程。改为确定性话术：没有就是没有，实体线索仍留在
        # evidence 里可溯源，但不当作答案文本抛给用户。
        from ask_service import no_basis_reply
        return {"ok": True, "mode": "hybrid",
                "answer": no_basis_reply(KBS.get(ctx["kb"], {}).get("name", "知识库")),
                "evidence": hybrid_payload.get("evidence", []),
                "engines": hit_engines, "structured": None,
                "no_basis": True, "confidence": "low", "kb": ctx["kb"]}
    # 4. LLM 兜底: 全部检索答不上 → LLM 生成理解性回答, evidence 空数组(无依据)
    fallback = _llm_fallback_answer(q, KBS.get(ctx["kb"], {}).get("name", "知识库"))
    if fallback:
        return {"ok": True, "mode": "miss", "answer": fallback,
                "evidence": [], "engines": [], "structured": None,
                "no_basis": True, "confidence": "none", "kb": ctx["kb"]}
    # 4.5 兜底失败(LLM 不可用/问题无关): 从 KB 配置读示例引导(去硬编码)
    examples = KBS.get(ctx["kb"], {}).get("examples", ["乳制品的数量", "原味酸奶是什么"])
    guide = "\n".join(f"· {e}" for e in examples[:5])
    return {"ok": True, "mode": "miss", "answer": f"抱歉，暂未理解该问题。\n可试试问：\n{guide}",
            "evidence": [], "engines": [], "structured": None, "no_basis": True,
            "confidence": "none", "kb": ctx["kb"]}


def _strip_reasoning_leak(ans):
    """拦截"模型独白"型答案 —— 面向用户的最后一道闸。

    多个引擎(hybrid/RAG/logical)的 LLM 出口都可能把内部推理当答案吐出来：
      "先确认问题：…看子图，原料的库存是 decimal 类型，但图中没有直接给出…"
    这些是模型的思考过程，用户看到会困惑，且每处都要单独堵、说法还总在变。

    所以不逐个堵引擎，在这个统一出口判一次：答案里出现**只可能来自内部实现**的词
    (子图/知识图谱/decimal/属性类型/"没有给任何"…)，或呈现"先…再…"的自述结构，
    就判定为泄漏，退化为确定性话术。
    """
    s = str(ans or "").strip()
    if not s:
        return s
    _INTERNAL = ("子图", "知识图谱", "decimal", "实体定义", "属性定义", "三元组",
                 "没有给任何", "没给具体数值", "先确认问题", "先问一句", "先数",
                 "让我先", "我先看", "第一步", "思考过程", "knowledge graph")
    if any(w in s for w in _INTERNAL):
        return None
    # "先…看…" 开头的自述句（模型独白的典型起手式）
    if re.match(r"^\s*(先|首先|让我|我需要|我来)", s) and len(s) > 40:
        return None
    return s


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
    # 统一出口净化: 任何引擎的 LLM 出口都可能把推理独白当答案(见 _strip_reasoning_leak)。
    # 放在这里一次判完, 不必逐个引擎去堵。
    try:
        _clean = _strip_reasoning_leak(result.get("answer") if result else None)
        if result is not None and _clean is None:
            from ask_service import no_basis_reply
            result["answer"] = no_basis_reply(KBS.get(req.kb or KB_NAME, {}).get("name", "知识库"))
            result["no_basis"] = True
            # mode 也要跟着降级。净化命中 = 该出口的 LLM 没给出可用答案（模型不可用、
            # 或把独白当答案），语义上就是"没答上来"。只改 answer 不改 mode 会让
            # 调用方看到 mode=hybrid 却拿到引导语——CI 上（无模型）实测即是此形态:
            # mode='hybrid' 而 answer 是"暂未理解"话术，断言 mode=='miss' 失败。
            result["mode"] = "miss"
        elif result is not None and _clean is not None:
            result["answer"] = _clean
    except Exception:
        pass
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
    res = _forward_trace(batch)
    _audit_trace("forward", batch, res)
    return {"ok": True, "direction": "forward", **res}


@app.get("/api/trace/reverse", dependencies=[Depends(require_key)])
def trace_reverse(raw: str = Query(..., description="原料编号，如 RM008")):
    res = _reverse_trace(raw)
    _audit_trace("reverse", raw, res)
    return {"ok": True, "direction": "reverse", **res}


@app.get("/api/scan", dependencies=[Depends(require_key)])
def scan(code: str = Query(..., description="溯源码，如 P003-B005 或 B001")):
    """扫码溯源：识别产品批次或批次号。"""
    parts = code.split("-")
    batch_id = parts[-1] if parts and parts[-1].startswith("B") else code
    res = _forward_trace(batch_id)
    _audit_trace("scan", code, res)
    return {"ok": True, "code": code, **res}


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
    # ── 看板聚合(前端 DashboardPanel 契约): 设备类型/状态分布 + 产线(车间)统计 ──
    # 各行业设备表名不同(equipment / valve_equipment / ...)，由词典 entity_cn2en['设备'] 解析；
    # 无设备表的 kb(如纯产品库)返回空数组 → 前端显示空态而非报错。
    D = ctx.get("D") or {}
    aliases = D.get("field_aliases", {}) or {}
    dev_table = str((D.get("entity_cn2en", {}) or {}).get("设备", "") or "").strip()

    def _field(rec, en):
        for a in ([en] + list(aliases.get(en, []) or [])):
            v = rec.get(a)
            if v not in (None, ""):
                return str(v).strip()
        return ""

    def _num(rec, en):
        try:
            return float(_field(rec, en) or 0)
        except (TypeError, ValueError):
            return 0.0

    RUNNING = {"running", "run", "normal", "working", "active", "online",
               "运行中", "运行", "正常", "工作中", "在线", "生产中"}
    FAULT = {"alarm", "maintenance", "offline", "fault", "fail", "failed", "error",
             "报警", "维护", "离线", "故障", "停机", "异常", "检修"}
    # 用"包含"而非精确相等: 数据里是"维护中/运行中"这类带后缀的词, 精确匹配会漏。
    def _hit(val, words):
        t = (val or "").strip().lower()
        return any(w in t for w in words) if t else False
    devs = []
    if dev_table:
        pre = dev_table.lower() + "_"
        for k, rec in qd.items():
            local = str(k).split("/")[-1].lower()
            # 只取该表的一级实例(key 形如 <ent>_<table>_<id>，排除 _<n> 的关联实例)
            if local.startswith(pre) and len(local.split("_")) == len(dev_table.split("_")) + 1:
                if isinstance(rec, dict):
                    devs.append(rec)
    type_cnt, status_cnt, line_map = {}, {}, {}
    for rec in devs:
        t = _field(rec, "deviceType")
        if t:
            type_cnt[t] = type_cnt.get(t, 0) + 1
        s = _field(rec, "status")
        if s:
            status_cnt[s] = status_cnt.get(s, 0) + 1
        ln = _field(rec, "workshop") or _field(rec, "location") or _field(rec, "zone") or "未分组"
        e = line_map.setdefault(ln, {"device_count": 0, "running": 0, "alarm": 0, "total_power_kw": 0.0})
        e["device_count"] += 1
        if _hit(s, RUNNING):
            e["running"] += 1
        if _hit(s, FAULT):
            e["alarm"] += 1
        e["total_power_kw"] += _num(rec, "powerKw")
    line_stats = [{"line": ln, "name": ln, "area": ln, "supervisor": "",
                   "device_count": v["device_count"], "running": v["running"],
                   "alarm": v["alarm"], "total_power_kw": round(v["total_power_kw"], 2)}
                  for ln, v in sorted(line_map.items(), key=lambda x: -x[1]["device_count"])]
    fault_cnt = sum(v["alarm"] for v in line_map.values())
    total_dev = len(devs)
    return {
        "ok": True,
        "entities": inst_count, "entity_count": sum(inst_count.values()),
        "nodes": len(g), "edges": sum(len(v) for v in g.values()),
        "stats": {
            "total_devices": total_dev,
            "device_type_dist": [{"type": t, "count": c}
                                 for t, c in sorted(type_cnt.items(), key=lambda x: -x[1])],
            "status_dist": [{"status": s, "count": c}
                            for s, c in sorted(status_cnt.items(), key=lambda x: -x[1])],
            "line_stats": line_stats,
            "fault_rate": round(fault_cnt / total_dev, 4) if total_dev else 0.0,
        },
    }


# ── 标准合规 API（GB/T 48000.3：合规度 + 标准导出物）──────────────
_EXPORT_ALLOW = {"ontology.ttl", "shapes.ttl", "ontology.jsonld"}


@app.get("/api/standard/compliance", dependencies=[Depends(require_key)])
def standard_compliance():
    """本体标准合规度（GB/T 48000.3 描述项齐备率 + 命名空间 + SHACL + 类层次 + 导出物）。

    与 ontology_check 的 F 类别同源（一处口径），供前端/验收展示。
    """
    try:
        import ontology_check as oc
        r = oc._check_standard(os.path.dirname(os.path.abspath(__file__)))
        st = r.get("state") or {}
        return {
            "ok": True,
            "standard_rate": st.get("standard_rate"),
            "ent_core_rate": st.get("ent_core_rate"),
            "ent_rate": st.get("ent_rate"),
            "attr_rate": st.get("attr_rate"),
            "ns_ok": st.get("ns_ok"),
            "subclass_count": st.get("subclass_count"),
            "has_export": st.get("has_export"),
            "issues": [{"severity": s, "message": m} for s, m in r.get("issues", [])],
            "standards": ["GB/T 48000.3-2026", "GB/T 42131-2022", "GB/T 41472.2-2022",
                          "ISO/IEC 21838", "IEEE 知识图谱评估标准"],
        }
    except Exception as e:
        return {"ok": False, "error": f"合规度计算失败: {e}"}


@app.post("/api/standard/export", dependencies=[Depends(require_key)])
def standard_export():
    """生成标准导出物（ontology.ttl / shapes.ttl / ontology.jsonld），返回文件名与大小。"""
    try:
        import ontology_export as ox
        root = os.path.dirname(os.path.abspath(__file__))
        outs = ox.export(os.path.join(root, "config", "ontology_schema.json"),
                         os.path.join(root, "export"))
        return {"ok": True, "files": [{"name": k, "size": os.path.getsize(v)}
                                      for k, v in sorted(outs.items())]}
    except Exception as e:
        return {"ok": False, "error": f"导出失败: {e}"}


@app.get("/api/standard/export/{fname}", dependencies=[Depends(require_key)])
def standard_export_download(fname: str):
    """下载标准导出物（白名单文件名，防路径穿越）。"""
    from fastapi.responses import FileResponse
    if fname not in _EXPORT_ALLOW:
        return {"ok": False, "error": "不允许的文件名"}
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "export", fname)
    if not os.path.exists(p):
        return {"ok": False, "error": "导出物不存在，请先生成"}
    return FileResponse(p, filename=fname,
                        media_type="text/turtle" if fname.endswith(".ttl") else "application/ld+json")


@app.get("/api/standard/quality", dependencies=[Depends(require_key)])
def standard_quality(kb: str = Query("")):
    """本体建模质量门：标签/定义/外键关系/结构 体检 + 阈值判定。

    kb 配了 schema 就用配置的；没配则从该 kb 数据自动推断(FDE 现场主场景：
    CSV 丢进来就能体检, 不用先手写 schema)。
    """
    try:
        import ontology_quality as oq
        kbc = KBS.get((kb or KB_NAME).strip()) or {}
        root = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(root, kbc.get("data_dir", "data"))
        data = so.load_all(data_dir) if os.path.isdir(data_dir) else {}
        schema_rel = kbc.get("schema")
        if schema_rel and os.path.exists(os.path.join(root, schema_rel)):
            schema, source = so.load_schema(os.path.join(root, schema_rel)), "configured"
        else:
            # 快速模式: 质量门只需结构体检, 不调 LLM(否则每次 19s)
            schema, source = so.suggest_schema(data, use_llm=False), "auto-inferred"
        rep = oq.inspect(schema, data)
        return {"ok": True, "source": source, **rep, "verdict": oq.judge(rep)}
    except Exception as e:
        return {"ok": False, "error": f"质量门执行失败: {e}"}



@app.get("/api/standard/roundtrip", dependencies=[Depends(require_key)])
def standard_roundtrip():
    """导入层自检：把导出的 ontology.ttl 读回来，与 schema 核对是否无损往返。

    这是导出物质量的可重跑门 —— 导出/导入任一侧退化都会立刻暴露。
    外部本体对齐（--align）走 CLI：python ontology_import.py --in <外部文件> --schema ... --align
    """
    try:
        import ontology_import as oim
        root = os.path.dirname(os.path.abspath(__file__))
        ttl = os.path.join(root, "export", "ontology.ttl")
        if not os.path.exists(ttl):
            return {"ok": False, "error": "导出物不存在，请先生成标准导出物"}
        fmt, data = oim.parse_input(ttl)
        model = oim.graph_to_model(data[1])
        rt = oim.roundtrip(model, os.path.join(root, "config", "ontology_schema.json"))
        return {"ok": bool(rt.get("ok")), **rt,
                "classes_note": f"{rt.get('classes_imported')}/{rt.get('classes_expected')}",
                "props_note": f"{rt.get('dataprops_imported')}/{rt.get('props_expected')}"}
    except Exception as e:
        return {"ok": False, "error": f"往返自检失败: {e}"}


@app.post("/api/standard/import-align", dependencies=[Depends(require_key)])
def standard_import_align(payload: dict | None = None):
    """外部本体对齐：入参 {"path": "外部 .ttl"} 或 {"content": "Turtle 文本"}，
    与 schema 做对齐报告（同名类匹配、外部独有类），供"对标国标"落到可核对清单。"""
    try:
        import ontology_import as oim
        import tempfile
        root = os.path.dirname(os.path.abspath(__file__))
        payload = payload or {}
        path = payload.get("path")
        tmp = None
        if not path and payload.get("content"):
            # 按内容探测后缀：JSON-LD 原文是 JSON，若写成 .ttl 会被按 Turtle 解析(曾静默出 0 类)
            content = payload["content"]
            suffix = ".jsonld" if content.lstrip().startswith(("{", "[")) else ".ttl"
            fd, tmp = tempfile.mkstemp(suffix=suffix, text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            path = tmp
        if not path or not os.path.exists(path):
            return {"ok": False, "error": "需提供存在的 path 或 content"}
        try:
            fmt, data = oim.parse_input(path)
            triples = oim.graph_from_jsonld(data)[0] if fmt == "jsonld" else data[1]
            model = oim.graph_to_model(triples)
            al = oim.align_report(model, os.path.join(root, "config", "ontology_schema.json"))
            out = {"ok": True, "format": fmt, "external_classes": len(model["classes"]), **al}
            # 无类可对齐时给出可操作提示（常见误操作：喂了 SHACL 约束文件而非本体文件）
            if not model["classes"]:
                n_shapes = sum(1 for t in triples if "shacl#" in str(t[1]))
                out["hint"] = ("该文件未含 owl:Class，无法对齐。"
                               + ("看起来是 SHACL 约束文件（shapes.ttl），请改喂本体文件（ontology.ttl / .jsonld）。"
                                  if n_shapes else "请确认是本体文件（含 owl:Class 的 .ttl / .jsonld）。"))
                out["ok"] = False
            return out
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
    except Exception as e:
        return {"ok": False, "error": f"对齐失败: {e}"}


# ── 溯源审计链 API(可选, 审核可追责) ──────────────
@app.get("/api/audit/chain", dependencies=[Depends(require_key)])
def audit_chain_status():
    """校验溯源审计链完整性(防篡改/防删行)。"""
    ac = _audit_chain()
    if not ac:
        return {"ok": False, "error": "审计链未启用(需 audit_chain.py 可用)"}
    try:
        ok, issues = ac.verify_chain()
        return {"ok": True, "chain_integrity": "PASS" if ok else "FAIL",
                "integrity_issues": issues, **ac.audit_report()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/audit/decisions", dependencies=[Depends(require_key)])
def audit_decisions(category: str = Query("", description="决策类别过滤")):
    """列出审计链中的决策记录(可选 category 过滤)。"""
    ac = _audit_chain()
    if not ac:
        return {"ok": False, "error": "审计链未启用"}
    try:
        decs = ac.decisions(category=category or None, limit=200)
        return {"ok": True, "count": len(decs), "decisions": decs}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/audit/export", dependencies=[Depends(require_key)])
def audit_export(fmt: str = Query("json", description="json/csv/prov-o")):
    """导出审计报告到 temp, 返回文件路径与校验状态。"""
    ac = _audit_chain()
    if not ac:
        return {"ok": False, "error": "审计链未启用"}
    try:
        import tempfile
        out = os.path.join(tempfile.gettempdir(), f"factory_audit.{fmt}"
                           if fmt != "prov-o" else "factory_audit.prov-o.json")
        r = ac.export_audit(out, fmt=fmt)
        return {"ok": True, **r}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
        "version": getattr(app, "version", APP_VERSION),
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


class OntologySuggestReq(BaseModel):
    """① AI 建议（预览）请求：指定数据源，拿回推断结果供人工确认。"""
    kb: str
    data_dir: str = None
    csv_path: str = None


def _resolve_src(kb: str, data_dir: str, csv_path: str):
    """校验 kb 名 + 数据源白名单（与 /api/ontology/build 同规则）。

    返回 (src_abs, err)：err 非空表示校验失败，调用方直接返回该错误。
    """
    if not kb or kb.startswith(".") or any(c in kb for c in ("/", "\\", "..")):
        return None, "非法 kb 名"
    src = data_dir or csv_path
    if not src:
        return None, "需提供 csv_path(单表) 或 data_dir(多表)"
    data_root = os.path.realpath(os.path.join(ROOT, "data"))
    out_root = os.path.realpath(os.path.join(ROOT, "output"))
    src_abs = src if os.path.isabs(src) else os.path.realpath(os.path.join(ROOT, src))
    _rp = os.path.realpath(src_abs)
    try:
        _rel = os.path.relpath(_rp, ROOT)
        _top = _rel.split(os.sep)[0]
        _ok = _rp.startswith(out_root + os.sep) or (
            not _rel.startswith("..") and (_top == "data" or _top.startswith("data") or _top == "output"))
    except Exception:
        _ok = False
    if not _ok:
        return None, f"数据源必须在 data*/ 或 output/ 内(防路径穿越): {src}"
    if not os.path.exists(src_abs):
        return None, f"数据源不存在: {src}"
    return src_abs, None


@app.post("/api/ontology/suggest", dependencies=[Depends(require_key)])
def ontology_suggest(req: OntologySuggestReq):
    """① AI 建议（预览，不落盘）：从数据推断实体/属性/关系/约束/业务域。

    这是"自助建模四步流程"的第②步 —— 系统先猜、人后拍板。
    只读、无副作用：不写 nt/lex，不注册 kb，可反复调用直到人工满意。
    """
    start = time.time()
    src_abs, err = _resolve_src((req.kb or "").strip(), req.data_dir, req.csv_path)
    if err:
        return _err_env(4001, err, start)
    try:
        import schema_ontology as so
        import data_loader as dl
        data = {}
        # data_loader.load_table 返回 (表名, 列名, 行列表) 三元组 —— 不是行列表
        if os.path.isdir(src_abs):
            for f in sorted(os.listdir(src_abs)):
                if f.lower().endswith((".csv", ".xlsx", ".json")):
                    try:
                        _name, _cols, rows = dl.load_table(os.path.join(src_abs, f))
                    except Exception:
                        continue
                    if rows:
                        data[_name] = rows
        else:
            _name, _cols, rows = dl.load_table(src_abs)
            if rows:
                data[_name] = rows
        if not data:
            return _err_env(4001, "数据源为空或未解析出任何表", start)
        schema = so.suggest_schema(data, use_llm=False, industry=req.kb.strip())
    except Exception as e:
        logger.warning(f"API内部错误[suggest 失败]: {e}")
        return _err_env(5001, "AI 建议失败(内部错误已记录)", start)
    # 转成前端友好的精简结构：实体(含域/主键/属性)、关系、约束、统计
    # 注意：属性必须带上 role/required —— 丢了 role，回传 confirm 后词典生成
    # 就认不出"类型列/状态列"，问答会答"没有相关数据"。
    ents = [{"id": e["id"], "label": e.get("label"), "table": e.get("table"),
             "key": e.get("key"), "domain": e.get("domain"),
             "definition": e.get("definition"),
             "attributes": [{"name": a["name"], "label": a.get("label"), "type": a.get("type"),
                             "role": a.get("role"), "required": a.get("required", False)}
                            for a in e.get("attributes", [])]}
            for e in schema.get("entities", [])]
    return _ok_env({"kb": req.kb.strip(), "source": "auto-inferred",
                    "entities": ents,
                    "relations": schema.get("relations", []),
                    "constraints": schema.get("constraints", []),
                    "stats": {"entities": len(ents),
                              "relations": len(schema.get("relations", [])),
                              "constraints": len(schema.get("constraints", [])),
                              "domains": sorted({e["domain"] for e in ents if e.get("domain")})},
                    "note": "预览结果未落盘；确认后调 /api/ontology/confirm 生效"}, start)


class OntologyConfirmReq(BaseModel):
    """③ 人拍板：把人工确认/修改后的 schema 提交生效。"""
    kb: str
    schema: dict
    data_dir: str = None      # 数据源目录（缺省从 kbs.json 该 kb 的 data_dir 取）


@app.post("/api/ontology/confirm", dependencies=[Depends(require_key)])
def ontology_confirm(req: OntologyConfirmReq):
    """③ 人拍板：保存人工确认后的 schema，按它产出 nt + lexicon 并注册 kb。

    与 /api/ontology/build 的区别：build 是纯自动推断直出，confirm 接受人工修正过的
    schema（改实体名/关系/域/约束），让"人拍板"真能落到产物上。
    """
    start = time.time()
    kb = (req.kb or "").strip()
    if not kb or kb.startswith(".") or any(c in kb for c in ("/", "\\", "..")):
        return _err_env(4001, "非法 kb 名", start)
    sch = req.schema or {}
    if not sch.get("entities"):
        return _err_env(4001, "schema 缺少 entities", start)
    sp = os.path.join(ROOT, "config", f"ontology_schema_{kb}.json")
    try:
        with open(sp, "w", encoding="utf-8") as f:
            json.dump(sch, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"API内部错误[schema 落盘失败]: {e}")
        return _err_env(5001, "schema 保存失败(内部错误已记录)", start)
    try:
        import run as run_mod
        # 注意：这里必须用 setup_schema(数据目录, schema路径) —— run.setup 会把
        # schema json 当数据源读，报"JSON 无数据行"。
        dd = req.data_dir
        if not dd:
            entry = _load_kbs().get(kb, {})
            dd = entry.get("data_dir") or f"data_{kb}"
        dd_abs = dd if os.path.isabs(dd) else os.path.join(ROOT, dd)
        if not os.path.isdir(dd_abs):
            _ok = False
            for cand in (f"data_{kb}", "data"):
                c = os.path.join(ROOT, cand)
                if os.path.isdir(c):
                    dd_abs, _ok = c, True
                    break
            if not _ok:
                return _err_env(4001, f"未找到数据目录: {dd}", start)
        nt, _lex_unused = run_mod.setup_schema(dd_abs, sp, table=kb)
        # setup_schema 只产 nt，不产词典 —— 词典另用 multi_model._build_lexicon 生成，
        # 否则 /api/ask 无词典可用（此前误判为"建本体失败"）。
        import multi_model as mm_mod
        import schema_ontology as so2
        _sch2 = so2.load_schema(sp)
        _data2 = so2.load_all(dd_abs)
        lex = os.path.join(ROOT, "config", f"lexicon_{kb}.json")
        with open(lex, "w", encoding="utf-8") as f:
            json.dump(mm_mod._build_lexicon(_sch2, _data2), f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"API内部错误[confirm 建模失败]: {e}")
        return _err_env(5001, "按确认 schema 建本体失败(内部错误已记录)", start)
    if not nt or not lex:
        return _err_env(5001, "建本体失败: 未产出 nt 或 lexicon", start)
    nt_rel = os.path.relpath(nt, ROOT).replace("\\", "/")
    lex_rel = os.path.relpath(lex, ROOT).replace("\\", "/")
    try:
        _update_kbs(kb, nt_rel, lex_rel)
        _set_active_kb(kb, nt_rel, lex_rel)
    except Exception as e:
        logger.warning(f"kbs.json/web_state 更新失败: {e}")
    _invalidate_kb(kb)
    return _ok_env({"kb": kb, "schema_path": os.path.relpath(sp, ROOT).replace("\\", "/"),
                    "nt": nt_rel, "lexicon": lex_rel, "status": "confirmed",
                    "ask_ready": True}, start)


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
        from industrial_dict_loader import _DICT_DIR
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