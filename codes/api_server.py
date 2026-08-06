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

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

import graph_rag as gr
import ontology_qa_v3 as v3
import multi_table as mt

# ── 食品知识库配置 ──
NS = "http://factory.example/ontology#"   # 与 multi_table 建本体一致
DATA = os.path.join(ROOT, "data")
FOOD_NT = os.path.join(ROOT, "output", "food.nt")
FOOD_LEX = os.path.join(ROOT, "config", "lexicon_food_products.json")


def _find(tail_name):
    """按尾部名找图内实体 URI（跨命名空间）。"""
    for k in graph:
        if gr.tail(k) == tail_name:
            return k
    return None


def _ensure_food_ontology():
    """若 food.nt 不存在则现场构建（多表+溯源关系）。"""
    if os.path.exists(FOOD_NT):
        return
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


def _load():
    _ensure_food_ontology()
    graph, labels, vi, rev = gr.build_graph(FOOD_NT)
    return graph, labels, vi, rev


graph, labels, vi, rev = _load()
D = v3.load_dict(FOOD_LEX)
QDATA = v3.build_data(v3.parse_nt(FOOD_NT), D)

app = FastAPI(title="食品企业知识库 API", version="2.1.0",
              description="本体驱动的食品企业问答 + 溯源检索（中小型食品企业场景）")


class AskReq(BaseModel):
    question: str


def _label(uri):
    return labels.get(uri, gr.tail(uri))


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


@app.get("/health")
def health():
    return {"status": "ok", "version": "2.1.0"}


@app.post("/api/ask")
def ask(req: AskReq):
    """自然语言问答：先规则引擎，命中不了走 GraphRAG。"""
    ans = v3.answer(req.question, QDATA, D)
    if ans != "暂不支持该问题":
        return {"ok": True, "mode": "rule", "answer": ans}
    gans, ctx = gr.answer_graph(req.question, FOOD_NT, FOOD_LEX, depth=2, max_nodes=40)
    return {"ok": True, "mode": "graphrag", "answer": gans, "context": ctx[:2000]}


@app.get("/api/trace/forward")
def trace_forward(batch: str = Query(..., description="生产批次号，如 B001")):
    return {"ok": True, "direction": "forward", **_forward_trace(batch)}


@app.get("/api/trace/reverse")
def trace_reverse(raw: str = Query(..., description="原料编号，如 RM008")):
    return {"ok": True, "direction": "reverse", **_reverse_trace(raw)}


@app.get("/api/scan")
def scan(code: str = Query(..., description="溯源码，如 P003-B005 或 B001")):
    """扫码溯源：识别产品批次或批次号。"""
    parts = code.split("-")
    batch_id = parts[-1] if parts and parts[-1].startswith("B") else code
    return {"ok": True, "code": code, **_forward_trace(batch_id)}


@app.get("/api/stats")
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
