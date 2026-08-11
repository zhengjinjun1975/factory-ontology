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


# ── 检索容错：值同义词扩展（真实工业数据噪声容错）──
# 材质/单位/类型的别名→规范组，检索时把查询词展开成同义组扩大匹配
_SYNONYM_GROUPS = {
    "不锈钢": ["不锈钢", "304", "316", "cf8", "cf8m", "cf3", "1cr18ni9ti"],
    "碳钢": ["碳钢", "wcb", "a105", "20钢", "20"],
    "合金钢": ["合金钢", "wc6", "wc9", "15crmo", "10cr2mo1"],
    "球墨铸铁": ["球墨铸铁", "qt450", "qt400"],
    "灰铸铁": ["灰铸铁", "ht200", "ht250"],
    "铜": ["铜", "铜合金", "h62", "h59"],
    "法兰": ["法兰", "flange"],
    "电动": ["电动", "电装", "z9"],
    "气动": ["气动", "q6"],
    "不锈钢304": ["不锈钢304", "304", "cf8"],
    "不锈钢316": ["不锈钢316", "316", "cf8m"],
}
# 单位归一：同一物理量多单位（psi/MPa/bar），统一到 MPa 再检索
_UNIT_ALIASES = {
    "mpa": ["mpa", "兆帕"], "bar": ["bar", "巴"],
    "psi": ["psi", "磅"],
}


def _expand_synonyms(text: str) -> str:
    """把查询里的同义词展开成匹配模式：'不锈钢' → '(不锈钢|304|316|cf8|cf8m|1cr18ni9ti)'。

    极简：用正则从同义词组生成捕获组，加到 value_index 子串匹配。失败静默返回原文本。
    """
    import re as _re
    low = text.lower()
    for cn, group in _SYNONYM_GROUPS.items():
        if cn in low:
            # 把同义词组拼成正则替代，优先匹配原始词再补同义词
            escaped = [_re.escape(g) for g in group]
            text += "|" + "|".join(escaped)
    return text


def _cn_segments(text: str) -> list:
    """提取文本里的连续中文片段(≥2字)，用于 value_index 部分匹配。
    如 '球阀的信息' → ['球阀']。纯中文连续序列切割，忽略标点/英文/数字。"""
    segs = []
    cur = ""
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff':
            cur += ch
        else:
            if len(cur) >= 2:
                segs.append(cur)
            cur = ""
    if len(cur) >= 2:
        segs.append(cur)
    return segs


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
    # 检索容错：材质/单位同义词扩展（真实工业数据噪声容错）
    # 把查询里的"不锈钢"展开，让 value_index 里 CF8/304/1Cr18Ni9Ti 也能命中
    syn_variants = [q]
    for cn, group in _SYNONYM_GROUPS.items():
        if cn in q:
            syn_variants += [g for g in group if len(g) >= 2]
    # 词典 LLM 语义聚类同义词组并入（别名命中：乳制品→奶制品 / 机加设备→机加工设备）
    if lexicon:
        _smap = lexicon.get("synonym_map", {}) or {}
        for _canon, _group in _smap.items():
            _members = [_canon] + list(_group)
            if any(_m and _m in q for _m in _members if _m):
                syn_variants += [_g for _g in _members if len(_g) >= 2]
    # 1. 值/标签/ID 子串匹配: value_index 的键若出现在问题里 (单字中文也允许, 如"盐")
    for key, ents in value_index.items():
        if not key:
            continue
        # 容错匹配：value_index 键 == 查询词 或 同义词组任一成员
        matched = any(k in q for k in (key,)) or key in syn_variants
        # 部分匹配增强：value_index 键的中文片段(≥2字, 如"球阀 q41f-16p"→"球阀")
        # 若该片段出现在问题里也命中。修复"球阀的信息"→键"球阀 q41f-16p"
        if not matched:
            for kseg in _cn_segments(key):
                if len(kseg) >= 2 and kseg in q:
                    matched = True
                    break
        if not matched:
            continue
        if len(key) < 2 and not (len(key) == 1 and '\u4e00' <= key <= '\u9fff'):
            continue
        for e in ents:
            scored[e] += (1.0 if any(k in q for k in (key,)) else 0.6)
    # 容错补充：同义词组内任一成员出现在 value_index 键里也命中（子串匹配，
    # 兼容 CF8(304)/CF8M(316) 这类带括号格式）
    for cn, group in _SYNONYM_GROUPS.items():
        if cn not in q:
            continue
        for g in group:
            if len(g) < 2:
                continue
            for vk, ents in value_index.items():
                if g in vk.lower():
                    for e in ents:
                        scored[e] += 0.8
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


_REWRITE_PROMPT = (
    "你是工厂知识图谱检索助手。请把用户的中文问题改写成适合图谱检索的关键词串："
    "提取关键实体词、属性词、查询意图（极值/比较/列举/计数等），用空格分隔。"
    "只输出改写后的关键词，不要解释、不要加引号。\n"
    "示例：\n"
    "问题：保质期最长的产品\n"
    "改写：产品 保质期 最长\n"
    "问题：振动最大的设备\n"
    "改写：设备 振动 最大\n"
    "问题：哪些客户买了价格最高的阀门\n"
    "改写：客户 价格 最高\n\n"
    "问题：{q}\n"
    "改写："
)


def _rewrite_query(question, model_key=None):
    """CoTKR 查询改写：用 LLM 把复杂/口语化问题改写为适合 find_seeds 检索的关键词串。

    返回改写后的查询串；改写失败 / LLM 不可用 / 输出非法时返回 None（调用方回落原问题，不阻塞）。
    """
    try:
        from model_llm import llm_generate
        out = llm_generate(_REWRITE_PROMPT.format(q=question),
                           temperature=0.2, max_tokens=60, model_key=model_key)
        if not out or out.startswith("[模型"):
            return None
        rq = out.strip().strip("\"'").strip("改写：").strip("改写:").strip()
        if not rq or len(rq) > 80:
            return None
        return rq
    except Exception:
        return None


# ── 动态调温：按查询类型选 LLM 温度（替代固定 0.6）──
# 精确数值/极值/计数查询需确定性(低温)，开放解释查询需多样性(高温)，其余取中间值。
# 依据调研：Selective Sampling + Tetrate 分场景温度(结构化0.0-0.2/对话0.6-0.8)。
_LOW_TEMP_WORDS = ("多少", "几", "最", "数量", "最大值", "最小值", "平均", "汇总",
                   "合计", "台", "个", "条", "总计", "共", "总数")
_HIGH_TEMP_WORDS = ("分析", "怎么样", "如何", "状况", "整体", "健康", "为什么",
                    "建议", "说明", "趋势", "评价", "原因", "解读")


def _pick_temperature(question):
    """按查询类型动态选温度：精确/极值/计数查询低温 0.2，开放/解释查询高温 0.7，其余 0.4。"""
    q = question or ""
    if any(w in q for w in _LOW_TEMP_WORDS):
        return 0.2
    if any(w in q for w in _HIGH_TEMP_WORDS):
        return 0.7
    return 0.4


# ── schema→prompt：把本体结构(schema)注入 LLM 上下文，提升未见行业查询准确率 ──
# 依据调研：ShEx-augmented prompting(未见KG F1 0.00→0.28) + OntoSCPrompt(schema引导prompt)。
# 极简：解析一次 nt，提取 owl:Class 的中文 label(实体类型) + 各类型 Datatype/ObjectProperty
# 的中文 label(属性名) 归组；失败回落用 lexicon 的 attr_cn2en/type_cn2en；再失败返回空串(不阻塞)。


def _schema_context(nt_file, lexicon=None):
    """从本体(.nt)提取 schema 上下文：实体类型(中文名)+各类型属性(中文名)，供 LLM 理解问题。

    返回形如: "知识图谱包含以下实体/属性: 设备(设备名称,设备类型,功率...), 产品(产品名称,...) 等，请据此理解问题回答。"
    解析失败 / 无信息时回落 lexicon 词典；仍无则返回空串（不阻塞 LLM 生成）。
    """
    from collections import defaultdict as _dd
    # 英文属性名->中文名：lexicon 为 snake_case(produce_date)，nt 尾为 camelCase(produceDate)。
    # 归一化(去下划线+小写)后做不敏感匹配，缺中文label时翻译。
    try:
        en2cn = {}
        for _en, _cn in ((lexicon or {}).get("attr_en2cn") or {}).items():
            en2cn[str(_en).replace("_", "").lower()] = _cn
    except Exception:
        en2cn = {}
    try:
        from ontology_qa_v3 import parse_nt as _parse
        triples = _parse(nt_file)
        res_label = {}     # 资源URI -> 中文label
        prop_domain = {}   # 属性URI -> domain类URI
        for s, p, o in triples:
            pn = tail(p)
            if pn == "label":
                res_label[s] = str(o).strip('"')
            elif pn == "domain":
                prop_domain[s] = o
        cls_attrs = _dd(list)
        for prop, dom in prop_domain.items():
            cname = res_label.get(dom) or tail(dom)
            aname = res_label.get(prop) or tail(prop)
            if not res_label.get(prop) and aname.replace("_", "").lower() in en2cn:  # 缺中文label时用词典翻译
                aname = en2cn[aname.replace("_", "").lower()]
            if aname and aname not in cls_attrs[cname]:
                cls_attrs[cname].append(aname)
        if cls_attrs:
            parts = [f"{c}({', '.join(a)})" for c, a in cls_attrs.items()]
            return "知识图谱包含以下实体/属性: " + ", ".join(parts) + " 等，请据此理解问题回答。"
    except Exception:
        pass
    # 回落：lexicon 词典（attr_cn2en 属性中文名 + type_cn2en 实体类型中文名）
    if lexicon:
        try:
            seg = []
            types = [str(t) for t in (lexicon.get("type_cn2en") or {}).keys() if str(t).strip()]
            if types:
                seg.append("实体类型: " + ", ".join(types))
            attrs = [str(a) for a in (lexicon.get("attr_cn2en") or {}).keys() if str(a).strip()]
            if attrs:
                seg.append("属性: " + ", ".join(attrs))
            if seg:
                return "知识图谱包含以下实体/属性: " + "; ".join(seg) + " 等，请据此理解问题回答。"
        except Exception:
            pass
    return ""


def answer_graph(question, nt_file, depth=1, max_nodes=40, model_key=None, lexicon=None):
    """GraphRAG 主入口：查询改写(CoTKR)->种子(词典引导)->子图->LLM生成。返回 (答案, 子图文本)。"""
    graph, labels, value_index, reverse = build_graph(nt_file)
    seeds = find_seeds(question, graph, labels, value_index, lexicon=lexicon)
    # CoTKR 查询改写：复杂/口语化问题先用 LLM 改写为检索关键词，提高种子/子图命中。
    # 改写后种子命中比原问题更多则用改写，否则回落原问题；LLM 失败不阻塞。
    rq = _rewrite_query(question, model_key=model_key)
    if rq and rq != question:
        try:
            r_seeds = find_seeds(rq, graph, labels, value_index, lexicon=lexicon)
            if len(r_seeds) > len(seeds):
                seeds = r_seeds
        except Exception:
            pass
    if not seeds:
        return "[图检索] 未定位到相关实体，请换种问法", ""
    sub = extract_subgraph(graph, reverse, seeds, depth=depth, max_nodes=max_nodes)
    context = serialize_subgraph(sub, labels)
    from model_llm import llm_generate
    # schema→prompt：把本体结构(实体类型+属性中文名)注入上下文，提升未见行业查询准确率(ShEx思路)
    schema_ctx = _schema_context(nt_file, lexicon)
    head = ("你是数据问答助手。\n" + schema_ctx + "\n\n") if schema_ctx else "你是数据问答助手。\n"
    prompt = (
        head +
        "下面是从知识图谱中检索到的相关子图(实体+关系+属性值):\n"
        f"{context}\n\n"
        f"请只依据上图信息回答问题: {question}\n"
        "只依据提供的子图事实回答，不编造不在图中的关系、实体或属性值。"
        "如果图中信息不足，如实说明。不要编造。"
    )
    temp = _pick_temperature(question)  # 动态调温：精确/极值/计数→0.2，开放解释→0.7，其余→0.4
    ans = llm_generate(prompt, temperature=temp, max_tokens=400, model_key=model_key)
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
