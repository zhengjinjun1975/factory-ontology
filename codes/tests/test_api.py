#!/usr/bin/env python3
"""API / 存储 / 数据接入 / 多租户 单测（pytest）。

运行: cd codes && python -m pytest tests/ -v
"""
import os
import sys
import json
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # codes/
sys.path.insert(0, ROOT)
DATA = os.path.join(ROOT, "data")

# fail-closed 鉴权(架构师审计 P0-1): 测试需配置 key + 传 X-API-Key
os.environ.setdefault("FOOD_ADMIN_KEY", "test-admin-key")
_API_HEADERS = {"X-API-Key": "test-admin-key"}


# ── API 端点 ──
def test_api_endpoints():
    from fastapi.testclient import TestClient
    import api_server as api
    c = TestClient(api.app)
    # 健康 + APP (无鉴权端点)
    assert c.get("/health").status_code == 200
    assert c.get("/").status_code == 200
    # 问答(规则引擎) — 带鉴权头
    r = c.post("/api/ask", json={"question": "乳制品的数量"}, headers=_API_HEADERS)
    assert r.status_code == 200 and "3" in r.json()["answer"]
    # 问答(答不上给引导)
    r = c.post("/api/ask", json={"question": "完全无关xyz"}, headers=_API_HEADERS)
    assert r.status_code == 200 and r.json()["mode"] == "miss"
    # 溯源
    r = c.get("/api/trace/forward?batch=B001", headers=_API_HEADERS)
    assert r.status_code == 200 and len(r.json()["raw_materials"]) == 2
    r = c.get("/api/trace/reverse?raw=RM008", headers=_API_HEADERS)
    assert r.status_code == 200 and any("B005" in x["batch"] for x in r.json()["affected_batches"])
    # 指标 + 审计
    assert c.get("/metrics").status_code == 200
    assert c.get("/api/admin/audit", headers=_API_HEADERS).status_code == 200
    # fail-closed 验证: 无 key 应 401
    assert c.post("/api/ask", json={"question": "乳制品的数量"}).status_code == 401


# ── 多租户 ──
def test_multi_tenant():
    import api_server as api
    from fastapi.testclient import TestClient
    c = TestClient(api.app)
    r = c.get("/api/admin/kbs", headers=_API_HEADERS)
    d = r.json()
    assert r.status_code == 200 and d["ok"] and "food" in d["kbs"] and d["active"] == "food"
    # 实时同步
    r = c.post("/api/admin/sync", headers=_API_HEADERS)
    assert r.status_code == 200 and r.json()["ok"] and r.json()["nodes"] >= 100


# ── 图持久化(graph_store) ──
def test_graph_store_roundtrip(tmp_path):
    import graph_rag as gr
    import graph_store as gs
    g, labels, vi, rev = gr.build_graph(os.path.join(ROOT, "output", "food.nt"))
    db = str(tmp_path / "g.db")
    gs.persist(g, labels, vi, rev, db)
    g2, l2, v2, r2 = gs.load(db)
    assert len(l2) == len(labels)  # 节点数一致
    # 持久化图问答可用
    seeds = gr.find_seeds("哪些批次用了盐", g2, l2, v2)
    assert any(gr.tail(s).startswith("Food_raw_materials_RM008") for s in seeds)


# ── 数据接入(data_import) ──
def test_data_import_mapping(tmp_path):
    import data_import as di
    src = str(tmp_path / "products.csv")
    open(src, "w", encoding="utf-8").write(
        "产品编号,产品名,品类,规格,保质期天,储存条件,售价\n"
        "Y1,测试酸奶,乳制品,200g,21,冷藏,4.0\n")
    r = di.import_source(src, dry_run=True)
    assert r["ok"] and "food_products" in str(r.get("dry_run"))
