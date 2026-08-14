#!/usr/bin/env python3
"""ontology_health.py — 本体健康评估(把"稀疏/断链"量化为数字)

对某 kb 的本体文件做拓扑质量评估，输出:
- 节点/边数
- 关系覆盖率: 有边实体 / 总实体 (低=孤立多)
- 连通分量数 & 最大分量占比 (大=紧凑, 多个小分量=断链)
- 关系丰富度: 对象属性数 / (对象属性+数据属性) (低=扁平本体, 缺实体关系)
- 平均度

用法: python ontology_health.py <kb> [data_dir]
例:   python ontology_health.py seismic
"""
import os
import sys
import re
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")


def _tail(uri):
    return uri.split("#")[-1].strip("<>").split("/")[-1]


def analyze(nt_file):
    """分析 N-Triples 本体, 返回健康指标 dict。"""
    if not os.path.exists(nt_file):
        return {"error": f"本体文件不存在: {nt_file}"}
    lines = open(nt_file, encoding="utf-8").read().splitlines()

    # 解析三元组: (s, p, o) — 用贪婪匹配到行尾(避免非贪婪在 object 的 http:// 点处截断)
    triples = []
    for l in lines:
        l = l.strip()
        if not l or l.startswith("#"):
            continue
        m = re.match(r"<([^>]+)>\s+<([^>]+)>\s+(.+?)\s*\.\s*$", l)
        if m:
            s, p, o = m.group(1), m.group(2), m.group(3).strip()
            triples.append((s, p, o))

    XSD = "http://www.w3.org/2001/XMLSchema#"
    RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

    # 实体(实例, 尾名含 _): subject 带 _ 的
    entities = set()
    obj_props = set()   # 对象属性(domain/range 都非 xsd)
    data_props = set()  # 数据属性(range 是 xsd)
    # domain/range 声明
    dom_rng = defaultdict(list)
    for s, p, o in triples:
        if "_" in _tail(s):
            entities.add(s)
        if "domain" in p:
            dom_rng[s].append(("d", o.strip("<>")))
        if "range" in p:
            dom_rng[s].append(("r", o.strip("<>")))
    # 分类属性
    for uri, pairs in dom_rng.items():
        ds = [o for t, o in pairs if t == "d"]
        rs = [o for t, o in pairs if t == "r"]
        has_class_d = any(not o.startswith(XSD) for o in ds)
        has_class_r = any(not o.startswith(XSD) for o in rs)
        if has_class_d and has_class_r:
            obj_props.add(uri)
        elif has_class_r and any(o.startswith(XSD) for o in rs):
            data_props.add(uri)

    # 关系边(对象属性实例关系): s->o 都是实体
    edges = set()
    adj = defaultdict(set)
    for s, p, o in triples:
        if p in obj_props or (p not in (RDF_TYPE,) and _tail(s) != _tail(o)):
            # 对象属性实例关系: 两个端点都是实体
            oo = o.strip("<>")
            if s in entities and oo in entities:
                edges.add((s, oo))
                adj[s].add(oo)
                adj[oo].add(s)

    # 连通分量(BFS)
    visited = set()
    components = []
    for ent in entities:
        if ent in visited:
            continue
        comp = []
        stack = [ent]
        visited.add(ent)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in adj[cur]:
                if nb not in visited:
                    visited.add(nb)
                    stack.append(nb)
        components.append(comp)

    total = len(entities)
    comps = len(components)
    max_comp = max((len(c) for c in components), default=0)
    # 关系覆盖率: 有边的实体 / 总实体
    has_edge = sum(1 for e in entities if adj[e])
    coverage = has_edge / total if total else 0
    # 关系丰富度: 对象属性 / (对象属性+数据属性)
    obj_n, data_n = len(obj_props), len(data_props)
    richness = obj_n / (obj_n + data_n) if (obj_n + data_n) else 0
    # 平均度
    avg_deg = (len(edges) * 2) / total if total else 0

    return {
        "entities": total, "edges": len(edges),
        "object_props": obj_n, "data_props": data_n,
        "coverage": round(coverage, 3),          # 关系覆盖率(低=孤立多)
        "components": comps, "max_component": max_comp,
        "component_ratio": round(max_comp / total, 3) if total else 0,
        "richness": round(richness, 3),          # 关系丰富度(低=扁平)
        "avg_degree": round(avg_deg, 2),
    }


def main():
    if len(sys.argv) < 2:
        print("用法: python ontology_health.py <kb>")
        return
    kb = sys.argv[1]
    nt_file = os.path.join(OUT, f"{kb}.nt")
    h = analyze(nt_file)
    if "error" in h:
        print(h["error"])
        return
    print(f"=== 本体健康评估: {kb} ===")
    print(f"实体数: {h['entities']}   关系边: {h['edges']}")
    print(f"对象属性: {h['object_props']}   数据属性: {h['data_props']}")
    print(f"关系覆盖率: {h['coverage']}   (有边实体占比, 低=孤立多)")
    print(f"连通分量: {h['components']}   最大分量占比: {h['component_ratio']}")
    print(f"关系丰富度: {h['richness']}   (对象属性占比, 低=扁平本体)")
    print(f"平均度: {h['avg_degree']}")
    # 健康判断
    flags = []
    if h["coverage"] < 0.5: flags.append("⚠ 覆盖率低(>50%实体孤立)")
    if h["component_ratio"] < 0.7: flags.append("⚠ 存在多个孤立分量")
    if h["richness"] < 0.1: flags.append("⚠ 关系丰富度低(近扁平, 缺实体关系)")
    if not flags: flags.append("✅ 本体健康")
    print("\n结论:", " | ".join(flags))


if __name__ == "__main__":
    main()
