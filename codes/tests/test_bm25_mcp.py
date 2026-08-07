#!/usr/bin/env python3
"""test_bm25_mcp.py — BM25 混合检索 + MCP server 测试"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _tmp_module(name, content):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".py")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_bm25_retrieves_entity():
    from bm25_retrieval import BM25Index
    g = {
        "u#Food_products_P001": {"productName": ["原味酸奶"], "保质期": ["15天"]},
        "u#Food_products_P003": {"productName": ["全麦面包"], "保质期": ["7天"]},
    }
    bm = BM25Index.from_graph(g)
    hits = bm.search("面包", top_k=2, min_score=1)
    assert any("P003" in h["entity"] for h in hits)


def test_bm25_ranks_relevant_higher():
    from bm25_retrieval import BM25Index
    g = {"u#Food_products_P003": {"productName": ["全麦面包"]}}
    bm = BM25Index.from_graph(g)
    # 相关查询分数应高于无关查询(相对排序), 无关词分数很低
    s_relevant = bm.search("面包", top_k=1)
    s_noise = bm.search("完全无关xyz", top_k=1)
    rel = s_relevant[0]["score"] if s_relevant else 0
    noi = s_noise[0]["score"] if s_noise else 0
    assert rel > noi
    assert noi < 0.4  # 无关词分数低(噪音)


def test_mcp_handshake_and_tools():
    import mcp_server
    resp = mcp_server._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert "factory-ontology-mcp" in resp["result"]["serverInfo"]["name"]
    tools = mcp_server._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in tools["result"]["tools"]]
    assert {"ask", "trace_forward", "trace_reverse", "stats"} <= set(names)


def test_mcp_ask_and_stats():
    import mcp_server
    r = mcp_server._handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "stats", "arguments": {}}})
    text = r["result"]["content"][0]["text"]
    assert "products" in text
    r2 = mcp_server._handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                             "params": {"name": "ask", "arguments": {"question": "乳制品的数量"}}})
    out = json.loads(r2["result"]["content"][0]["text"])
    assert out["mode"] in ("rule", "logical", "bm25")


def test_mcp_reverse_trace():
    import mcp_server
    r = mcp_server._handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                            "params": {"name": "trace_reverse", "arguments": {"raw": "RM008"}}})
    out = json.loads(r["result"]["content"][0]["text"])
    assert out["batches"]  # RM008 应能找到受影响批次
