#!/usr/bin/env python3
"""graph_store.py — 图持久化(SQLite)

把 graph_rag 的内存图持久化到 SQLite, 支持更大规模(100k-1M 实体)与跨重启复用:
- 比每次从 .nt 全量重建更快
- 超内存规模时作为图数据库的轻量过渡

用法:
  from graph_store import persist, load
  persist(graph, labels, value_index, reverse, "graph.db")
  graph, labels, value_index, reverse = load("graph.db")

>1M 实体建议上真实图数据库(Neo4j/LightGraph), 见 docs/规模化.md
"""
import os
import sys
import sqlite3
import json

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def persist(graph, labels, value_index, reverse, db_path):
    """把图写入 SQLite。表: nodes(uri,label), edges(src,rel,dst), reverse(src,rel,dst), vi(key,ent)。"""
    import graph_rag as gr
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.executescript("""
        DROP TABLE IF EXISTS nodes; DROP TABLE IF EXISTS edges;
        DROP TABLE IF EXISTS vi;
        CREATE TABLE nodes(uri TEXT PRIMARY KEY, label TEXT);
        CREATE TABLE edges(src TEXT, rel TEXT, dst TEXT);
        CREATE TABLE vi(key TEXT, ent TEXT);
    """)
    # nodes
    for uri, lbl in labels.items():
        cur.execute("INSERT OR IGNORE INTO nodes VALUES(?,?)", (uri, str(lbl)))
    # edges + reverse
    for src, props in graph.items():
        for rel, vals in props.items():
            for v in vals:
                dst = v if gr._is_entity(v) else f"'{v}"
                cur.execute("INSERT INTO edges VALUES(?,?,?)", (src, rel, str(dst)))
    for tgt, props in reverse.items():
        for rel, vals in props.items():
            for s in vals:
                cur.execute("INSERT INTO edges VALUES(?,?,?)", (tgt, rel, str(s)))
    # value_index
    for key, ents in value_index.items():
        for e in ents:
            cur.execute("INSERT INTO vi VALUES(?,?)", (key, e))
    conn.commit()
    n_edges = cur.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    conn.close()
    return {"nodes": len(labels), "edges": n_edges}


def load(db_path):
    """从 SQLite 载入图。返回 (graph, labels, value_index, reverse)。"""
    import graph_rag as gr
    from collections import defaultdict
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    graph = defaultdict(lambda: defaultdict(list))
    reverse = defaultdict(lambda: defaultdict(list))
    labels = {}
    value_index = defaultdict(list)
    # nodes
    for uri, lbl in cur.execute("SELECT uri, label FROM nodes"):
        labels[uri] = lbl
    # edges: 区分对象属性(指向存在节点)与数据属性(字面值)
    node_set = set(labels)
    for src, rel, dst in cur.execute("SELECT src, rel, dst FROM edges"):
        if dst.startswith("'"):
            graph[src][rel].append(dst[1:])  # 字面值
        elif dst in node_set:
            graph[src][rel].append(dst)
            reverse[dst][f"~{rel}"].append(src)
    # value_index
    for key, ent in cur.execute("SELECT key, ent FROM vi"):
        value_index[key].append(ent)
    conn.close()
    return graph, labels, value_index, reverse


def main():
    if len(sys.argv) < 3:
        print("用法: python graph_store.py <food.nt> <graph.db>")
        sys.exit(1)
    import graph_rag as gr
    g, labels, vi, rev = gr.build_graph(sys.argv[1])
    stat = persist(g, labels, vi, rev, sys.argv[2])
    print("图已持久化:", stat)
    # 回读校验
    g2, l2, v2, r2 = load(sys.argv[2])
    print("回读校验: 节点", len(l2), "| 边", sum(len(v) for p in g2.values() for v in p.values()), "| 一致:", len(l2) == len(labels))


if __name__ == "__main__":
    main()
