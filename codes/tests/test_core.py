#!/usr/bin/env python3
"""核心单测（pytest）— 自包含，无需外部模型，CI 可跑。

覆盖：
- 数据加载（CSV/JSON/SQLite 三格式）
- 本体生成（csv_to_owl 建本体、multi_table 跨表）
- 规则问答引擎（ontology_qa_v3 确定性模板）
- GraphRAG 图检索（种子定位 + 子图提取）

运行：cd codes && python -m pytest tests/ -v
"""
import os
import sys
import json
import sqlite3
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # codes/
sys.path.insert(0, ROOT)

DATA = os.path.join(ROOT, "data")


def _make_db(path):
    """建一个临时 SQLite 库。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE equipment(id TEXT, device_name TEXT, power_kw REAL, status TEXT)")
    conn.executemany("INSERT INTO equipment VALUES(?,?,?,?)",
                     [("E1", "CNC铣床-1", 22.5, "running"), ("E2", "焊机-1", 15.0, "stopped")])
    conn.commit()
    conn.close()


# ── 数据加载 ──
def test_data_loader_csv_json_sqlite():
    import data_loader as dl
    # CSV
    _, h, rows = dl.load_table(os.path.join(DATA, "equipment.csv"))
    assert len(rows) >= 3 and "device_name" in h
    # JSON
    jp = os.path.join(DATA, "_tmp_test.json")
    json.dump([{"id": "X1", "name": "a", "val": "1"}], open(jp, "w", encoding="utf-8"))
    _, hj, rj = dl.load_table(jp)
    assert len(rj) == 1 and rj[0]["id"] == "X1"
    os.remove(jp)
    # SQLite
    dbp = os.path.join(DATA, "_tmp_test.db")
    _make_db(dbp)
    _, hd, rd = dl.load_table(dbp)
    assert len(rd) == 2 and rd[0]["id"] == "E1"
    os.remove(dbp)


# ── 本体生成 ──
def test_csv_to_owl_builds_ontology(tmp_path):
    import csv_to_owl as c2o
    out = str(tmp_path / "eq.nt")
    c2o.build_nt(os.path.join(DATA, "equipment.csv"), out)
    txt = open(out, encoding="utf-8").read()
    assert "Equipment" in txt and "deviceName" in txt


def test_multi_table_cross_relation(tmp_path):
    import multi_table as mt
    out = str(tmp_path / "multi.nt")
    mt.build_nt({"equipment": {"headers": ["id", "line_id"], "rows": [{"id": "E1", "line_id": "L1"}], "id_col": "id"},
                 "line": {"headers": ["id"], "rows": [{"id": "L1"}], "id_col": "id"}},
                mt.detect_relations({
                    "equipment": {"headers": ["id", "line_id"], "rows": [{"id": "E1", "line_id": "L1"}], "id_col": "id"},
                    "line": {"headers": ["id"], "rows": [{"id": "L1"}], "id_col": "id"}}), out)
    txt = open(out, encoding="utf-8").read()
    assert "hasLine" in txt


# ── 规则问答引擎 (ontology_qa_v3) ──
def test_qa_rule_templates(tmp_path):
    import csv_to_owl as c2o
    import ontology_qa_v3 as v3
    # 用合成小数据建本体 + 词典
    csvp = os.path.join(DATA, "energy_station.csv")
    ntp = str(tmp_path / "es.nt")
    c2o.build_nt(csvp, ntp)
    lex = os.path.join(ROOT, "config", "lexicon_energy_station.json")
    D = v3.load_dict(lex)
    data = v3.build_data(v3.parse_nt(ntp), D)
    assert "10" in v3.answer("一共有多少条记录", data, D)          # 总数
    assert v3.answer("功率的最大值", data, D) != "暂不支持该问题"    # 极值
    assert v3.answer("功率的平均值", data, D) != "暂不支持该问题"    # 平均


# ── GraphRAG 图检索 ──
def test_graph_rag_retrieval(tmp_path):
    import csv_to_owl as c2o
    import multi_table as mt
    import graph_rag as gr
    # 建多表本体(有 hasLine 跨表关系)
    ntp = str(tmp_path / "mt.nt")
    mt.build_nt({"equipment": {"headers": ["id", "device_name", "line_id"], "rows": [
                    {"id": "E1", "device_name": "CNC铣床-1", "line_id": "L1"},
                    {"id": "E2", "device_name": "焊机-1", "line_id": "L2"}], "id_col": "id"},
                 "line": {"headers": ["id", "line_name"], "rows": [
                    {"id": "L1", "line_name": "产线A"}, {"id": "L2", "line_name": "产线B"}], "id_col": "id"}},
                {"equipment": {"line_id": {"target_class": "Line",
                                           "rel": "http://factory.example/ontology#hasLine",
                                           "label": "属于产线"}}},
                ntp)
    graph, labels, value_index, reverse = gr.build_graph(ntp)
    seeds = gr.find_seeds("CNC铣床-1 属于哪条产线", graph, labels, value_index)
    assert seeds and "Equipment_E1" in [gr.tail(s) for s in seeds]
    sub = gr.extract_subgraph(graph, reverse, seeds, depth=1, max_nodes=20)
    ctx = gr.serialize_subgraph(sub, labels)
    assert "产线A" in ctx or "Line_L1" in ctx  # 反向遍历能找到产线
