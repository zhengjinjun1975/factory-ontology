#!/usr/bin/env python3
"""graph_rag.py — GraphRAG-lite：本体知识图谱 + 图遍历检索 + LLM 生成

在已建好的本体(N-Triples)之上建内存图，做图检索增强生成：
1. 建图：实体->{关系:[目标]}，实体标签 + 字面属性值可检索
2. 种子定位：问题关键词 -> 匹配实体标签/属性值
3. 子图提取：BFS 邻域遍历，把子图序列化成上下文
4. LLM 生成：问题 + 子图上下文 -> 自然语言答案

对比：本体规则引擎(ontology_qa_v3)确定性回答结构化问题；
GraphRAG 层回答开放式/关系/模糊问题——两者互补。

用法:
  python graph_rag.py <nt文件> "<问题>" [lexicon.json]
  python graph_rag.py <nt文件> "<问题>" --depth 2 --max-nodes 40
"""
import sys
import os
from collections import defaultdict, deque


def parse_nt(nt_file):
    """解析 N-Triples，返回 (s,p,o) 三元组列表。"""
    from ontology_qa_v3 import parse_nt as _p
    return _p(nt_file)


def tail(uri):
    return uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]


def _is_entity(v):
    """parse_nt 剥掉 <>，实体 URI 以 http 开头，字面值不是。"""
    return isinstance(v, str) and v.startswith(("http://", "https://"))


def build_graph(nt_file):
    """建内存图。
    返回:
      graph: {实体URI: {关系局部名: [目标URI或字面值]}}
      labels: {实体URI: 显示名}
      value_index: {小写值: [实体URI]}  # 字面属性值 -> 实体(用于种子定位)
    """
    triples = parse_nt(nt_file)
    graph = defaultdict(lambda: defaultdict(list))
    reverse = defaultdict(lambda: defaultdict(list))  # 反向边: 目标<-源
    labels = {}
    value_index = defaultdict(list)

    for s, p, o in triples:
        pn = tail(p)
        if pn == "type":
            if tail(o) == "Class":
                labels[s] = tail(s)
            continue
        if pn == "label" and str(o).startswith('"'):
            labels[s] = str(o).strip('"')
            continue
        if _is_entity(o):
            # 对象属性：实体 -> 实体 (同时记录反向)
            graph[s][pn].append(o)
            reverse[o][f"~{pn}"].append(s)
        else:
            # 数据属性：实体 -> 字面值
            val = str(o)
            graph[s][pn].append(val)
            if len(val) <= 40:
                value_index[val.lower()].append(s)

    # 实体索引(局部名 + 标签 + 实例ID)
    for ent in graph:
        if ent not in labels:
            labels[ent] = tail(ent)
        ln = tail(ent).lower()
        value_index.setdefault(ln, []).append(ent)
        # 实例ID: Equipment_E1 -> 索引 'e1'
        parts = tail(ent).split("_")
        if len(parts) >= 2:
            value_index.setdefault(parts[-1].lower(), []).append(ent)
    return graph, labels, value_index, reverse


def find_seeds(question, graph, labels, value_index, top=8, lexicon=None, ontology=None):
    """从问题关键词定位种子实体(子串匹配 + 词典引导 + 本体关系路径引导)。
    lexicon 提供 attr_cn2en/type_cn2en, 问题提到属性/类型时加权有该属性/类型的实体。
    ontology 提供关系列表 [{'id':'locatedIn','label':'位于','from':'Equipment','to':'Location'},...],
    问题含关系 label/id 时, 沿该关系在图中找到连接的目标实体并入种子(基于2025 OG-RAG 本体引导检索)。"""
    q = question.lower()
    scored = defaultdict(float)
    # 1. 值/标签/ID 子串匹配: value_index 的键若出现在问题里 (单字中文也允许, 如"盐")
    for key, ents in value_index.items():
        if not key or key not in q:
            continue
        if len(key) < 2 and not (len(key) == 1 and '\u4e00' <= key <= '\u9fff'):
            continue
        for e in ents:
            scored[e] += 1.0
    # 2. 实体标签匹配
    for ent, lbl in labels.items():
        lb = lbl.lower()
        if len(lb) >= 2 and (lb in q or tail(ent).lower() in q):
            scored[ent] += 0.8
    # 3. 词典引导(实体链接增强): 问题含属性/类型中文名时, 加权有该字段的实体
    if lexicon:
        def _norm(s):
            return str(s).lower().replace("_", "")
        try:
            attrs = lexicon.get("attr_cn2en", {})
            types = lexicon.get("type_cn2en", {})
            for cn, en in attrs.items():
                if len(cn) >= 2 and cn in q:
                    for ent, props in graph.items():
                        if any(_norm(en) == _norm(rel) or _norm(en) in _norm(rel) for rel in props):
                            scored[ent] += 0.5
            for cn, en in types.items():
                if len(cn) >= 2 and cn in q:
                    for ent, props in graph.items():
                        if any(_norm(en) in _norm(str(v)) for v in props.get("deviceType", [])) or \
                           any(_norm(en) in _norm(str(v)) for v in props.get("category", [])):
                            scored[ent] += 0.5
        except Exception:
            pass
    # 4. 本体关系路径引导(OG-RAG): 问题含某关系 label/id 时, 沿该关系把连接的目标实体并入种子
    if ontology:
        try:
            for rel in ontology:
                rid = str(rel.get("id", "")).lower()
                rlabel = str(rel.get("label", "")).lower()
                if not ((rid and rid in q) or (rlabel and len(rlabel) >= 2 and rlabel in q)):
                    continue
                for ent, props in graph.items():
                    for relname, targets in props.items():
                        rn = relname.lower()
                        if rn == rid or (rid and rid in rn) or (rlabel and rlabel in rn):
                            scored[ent] += 1.0
                            for t in targets:
                                if _is_entity(t):
                                    scored[t] += 1.0
        except Exception:
            pass
    ranked = sorted(scored.items(), key=lambda x: -x[1])
    return [e for e, _ in ranked[:top]]


def extract_subgraph(graph, reverse, seeds, depth=1, max_nodes=40):
    """BFS 提取种子邻域子图(合并正反向边)。返回 {实体URI: {关系: [目标]}}。"""
    sub = {}
    visited = set()
    dq = deque((s, 0) for s in seeds)
    while dq and len(sub) < max_nodes:
        node, d = dq.popleft()
        if node in visited:
            continue
        visited.add(node)
        merged = defaultdict(list)
        for k, v in graph.get(node, {}).items():
            merged[k] = list(v)
        for k, v in reverse.get(node, {}).items():
            merged[k] = list(v)
        if merged:
            sub[node] = dict(merged)
            if d < depth:
                for targets in merged.values():
                    for t in targets:
                        if _is_entity(t) and t not in visited and len(sub) < max_nodes:
                            dq.append((t, d + 1))
    return sub


def serialize_subgraph(sub, labels):
    """把子图序列化成文本(节点+关系+字面值)。"""
    lines = []
    for ent, props in sub.items():
        name = labels.get(ent, tail(ent))
        lines.append(f"[{name}]")
        for rel, vals in props.items():
            parts = []
            for v in vals:
                parts.append(labels.get(v, tail(v)) if _is_entity(v) else str(v))
            lines.append(f"  {rel}: {', '.join(parts)}")
    return "\n".join(lines)


def answer_graph(question, nt_file, depth=1, max_nodes=40, model_key=None, lexicon=None):
    """GraphRAG 主入口：种子(词典引导)->子图->LLM生成。返回 (答案, 子图文本)。"""
    graph, labels, value_index, reverse = build_graph(nt_file)
    seeds = find_seeds(question, graph, labels, value_index, lexicon=lexicon)
    if not seeds:
        return "[图检索] 未定位到相关实体，请换种问法", ""
    sub = extract_subgraph(graph, reverse, seeds, depth=depth, max_nodes=max_nodes)
    context = serialize_subgraph(sub, labels)
    from model_llm import llm_generate
    prompt = (
        "你是数据问答助手。下面是从知识图谱中检索到的相关子图(实体+关系+属性值):\n"
        f"{context}\n\n"
        f"请只依据上图信息回答问题: {question}\n"
        "只依据提供的子图事实回答，不编造不在图中的关系、实体或属性值。"
        "如果图中信息不足，如实说明。不要编造。"
    )
    ans = llm_generate(prompt, temperature=0.2, max_tokens=300, model_key=model_key)
    return ans, context


def main():
    args = sys.argv[1:]
    lex = None
    depth, max_nodes = 1, 40
    i = 0
    while i < len(args):
        if args[i] == "--depth":
            depth = int(args[i + 1]); i += 2
        elif args[i] == "--max-nodes":
            max_nodes = int(args[i + 1]); i += 2
        elif args[i] in ("--lexicon",):
            lex = args[i + 1]; i += 2
        else:
            i += 1
    if len(args) < 2:
        print("用法: python graph_rag.py <nt文件> '<问题>' [--depth N] [--max-nodes N]")
        sys.exit(1)
    nt, q = args[0], args[1]
    ans, ctx = answer_graph(q, nt, depth=depth, max_nodes=max_nodes)
    print(ans)
    if os.environ.get("GRAPH_DEBUG"):
        print("\n--- 检索到的子图 ---\n" + ctx)


if __name__ == "__main__":
    main()
