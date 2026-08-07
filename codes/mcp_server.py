#!/usr/bin/env python3
"""mcp_server.py — 轻量 MCP (Model Context Protocol) server（纯标准库零依赖）

暴露 factory-ontology 知识库给任意 MCP-native AI agent：问答/溯源/统计/导出。
AI 原生：任何支持 MCP 的 agent（Hermes/Claude/Cursor/自研）都能调用本工厂知识库。

协议：MCP over stdio，JSON-RPC 2.0（最小实现，无需 mcp SDK）。
支持：initialize, tools/list, tools/call。

用法：
    python mcp_server.py <KB环境变量>   # 或直接跑，默认 food
工具：
    ask           自然语言问答(复用规则→逻辑→GraphRAG→BM25→miss 链路)
    trace_forward 正向溯源(批次→原料)
    trace_reverse 反向溯源(原料→受影响批次/产品)
    stats         知识库统计
    export        溯源报告导出
"""
import sys
import json
import os
import importlib.util

# ── 复用核心逻辑：加载 KB 数据（与 api_server 一致）──
def _load_ontology():
    import ontology_qa_v3 as v3
    import multi_table
    # KB 环境变量（多租户，同 api_server）
    kb = os.environ.get("FOOD_KB", "food")
    import pathlib
    ROOT = pathlib.Path(__file__).parent
    NT = ROOT / "output" / f"{kb}.nt"
    NT.parent.mkdir(exist_ok=True)
    import graph_rag as gr
    if not NT.exists():
        # 用 demo 数据建本体
        DATA = ROOT / "data"
        multi_table.build_nt([str(DATA / "food_products.csv"),
                              str(DATA / "food_raw_materials.csv"),
                              str(DATA / "food_batches.csv"),
                              str(DATA / "food_batch_ingredient.csv"),
                              str(DATA / "food_qc.csv"),
                              str(DATA / "food_equipment.csv")], str(NT))
    graph, labels, vi, rev = gr.build_graph(str(NT))
    # 词典：优先 kbs.json 的 lexicon 字段，其次 FOOD_LEX 环境变量，最后按文件名约定
    import json as _json
    _lex = "lexicon_food_products.json"
    try:
        _kb = _json.load(open(ROOT / "config" / "kbs.json", encoding="utf-8"))
        _entry = _kb.get(kb) or (_kb.get("kbs") or {}).get(kb) or {}
        _lex = _entry.get("lexicon", _lex)
    except Exception:
        pass
    _lex = os.environ.get("FOOD_LEX", str(ROOT / "config" / _lex))
    D = v3.load_dict(_lex)
    QDATA = v3.build_data(v3.parse_nt(str(NT)), D)
    return gr, v3, graph, D, QDATA, str(NT)


gr, v3, GRAPH, DICT, QDATA, NT = _load_ontology()

# BM25 混合检索（惰性）
_BM = None


def _bm():
    global _BM
    if _BM is None:
        from bm25_retrieval import BM25Index
        try:
            _BM = BM25Index.from_graph(GRAPH)
        except Exception:
            _BM = None
    return _BM


def _ask(question: str) -> dict:
    """完整问答链路：规则 → 逻辑桥 → GraphRAG → BM25 混合 → miss。"""
    # 1 规则
    ans = v3.answer(question, QDATA, DICT)
    if ans != "暂不支持该问题":
        return {"mode": "rule", "answer": ans}
    # 2 逻辑桥
    try:
        import logical_qa
        lres = logical_qa.answer(question, QDATA, DICT)
        if lres:
            return {"mode": "logical", "answer": lres[0]}
    except Exception:
        pass
    # 3 GraphRAG
    gans, ctx = gr.answer_graph(question, NT, depth=2, max_nodes=40, lexicon=DICT)
    if not gans.startswith("[图检索]"):
        return {"mode": "graphrag", "answer": gans}
    # 4 BM25 混合检索（稀疏补充，提升召回）
    bm = _bm()
    if bm:
        hits = bm.search(question, top_k=3, min_score=4.0)
        if hits:
            ents = "、".join(h["entity"] for h in hits)
            return {"mode": "bm25", "answer": f"找到相关实体: {ents}", "hits": hits}
    return {"mode": "miss", "answer": "抱歉，暂未理解该问题"}


def _forward_trace(batch: str) -> dict:
    import graph_rag as _gr
    result = {"batch": batch, "products": [], "materials": []}
    for node, props in GRAPH.items():
        if _gr.tail(node).startswith("Food_batches_") and _gr.tail(node).endswith(batch):
            for rel, vals in props.items():
                r = _gr.tail(rel)
                for v in (vals if isinstance(vals, list) else [vals]):
                    if r == "usesRawMaterial":
                        result["materials"].append(_gr.tail(v))
                    elif r in ("produces", "productRef"):
                        result["products"].append(_gr.tail(v))
    return result


def _reverse_trace(raw: str) -> dict:
    import graph_rag as _gr
    result = {"raw": raw, "batches": [], "products": []}
    target = f"Food_raw_materials_{raw}"  # RM008 → Food_raw_materials_RM008
    for node, props in GRAPH.items():
        for rel, vals in props.items():
            for v in (vals if isinstance(vals, list) else [vals]):
                tv = _gr.tail(v)
                if tv == target and _gr.tail(rel) == "usesRawMaterial":
                    b = _gr.tail(node)
                    result["batches"].append(b)
                    for n2, p2 in GRAPH.items():
                        for r2, v2 in p2.items():
                            for vv in (v2 if isinstance(v2, list) else [v2]):
                                if _gr.tail(vv) == b and _gr.tail(r2) in ("produces", "productRef"):
                                    result["products"].append(_gr.tail(n2))
    # 去重
    result["batches"] = list(dict.fromkeys(result["batches"]))
    result["products"] = list(dict.fromkeys(result["products"]))
    return result


def _stats() -> dict:
    import graph_rag as _gr
    return {
        "entities": len(GRAPH),
        "products": sum(1 for k in GRAPH if _gr.tail(k).startswith("Food_products_")),
        "batches": sum(1 for k in GRAPH if _gr.tail(k).startswith("Food_batches_")),
        "raw": sum(1 for k in GRAPH if _gr.tail(k).startswith("Food_raw_materials_")),
    }


TOOLS = [
    {"name": "ask", "description": "自然语言问答工厂知识库", "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    {"name": "trace_forward", "description": "正向溯源: 批次号→原料", "inputSchema": {"type": "object", "properties": {"batch": {"type": "string"}}, "required": ["batch"]}},
    {"name": "trace_reverse", "description": "反向溯源: 原料编号→受影响批次/产品", "inputSchema": {"type": "object", "properties": {"raw": {"type": "string"}}, "required": ["raw"]}},
    {"name": "stats", "description": "知识库统计", "inputSchema": {"type": "object", "properties": {}}},
]


def _handle(req: dict) -> dict:
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "factory-ontology-mcp", "version": "0.1"}}}
    if method == "notifications/initialized":
        return None  # 无响应
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "ask":
                out = _ask(args.get("question", ""))
            elif name == "trace_forward":
                out = _forward_trace(args.get("batch", ""))
            elif name == "trace_reverse":
                out = _reverse_trace(args.get("raw", ""))
            elif name == "stats":
                out = _stats()
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"未知工具: {name}"}}
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"未知方法: {method}"}}


def main():
    """stdio JSON-RPC 循环。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
