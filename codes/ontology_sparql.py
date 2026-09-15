#!/usr/bin/env python3
"""ontology_sparql.py — 只读 SPARQL 子集查询（零依赖、纯标准库、离线）。

产品硬约束：数据不出厂、离线可用、零第三方依赖 —— 因此本模块**不引 rdflib**，
自研一个"够用即可"的 SPARQL 子集解析 + 求值引擎，直接跑在 .nt / .ttl 文件上。

────────────────────────────────────────────────────────────────────
唯一对外接口（签名固定）:
    query(nt_path: str, sparql: str) -> dict
        {'vars': [...], 'rows': [{var: value, ...}, ...], 'error': None}
        失败 → {'vars': [], 'rows': [], 'error': '<中文错误原因>'}

支持的 SPARQL 子集（刻意只做这些）:
  · SELECT ?a ?b WHERE { ... }     （SELECT * → 取模式中出现的全部变量）
  · 三元组模式       ?s ?p ?o / ?s <谓词> ?o / <主> <谓> <宾>
                    （主/谓/宾任一位置可以是变量、<完整IRI> 或 前缀名 如 rdfs:label）
                    `a` 作为谓词等价于 rdf:type
  · PREFIX fac: <...>              （也可不声明前缀，直接用完整 IRI）
  · 多个三元组模式 = 逻辑与（join，共享变量必须一致）
  · FILTER(?x = "字面量")  /  FILTER(?x != "字面量")
  · 字面量匹配忽略 @lang：模式写 "阀门" 能匹配 "阀门"@zh；写 "阀门"@zh 则只匹配 zh

只读保证:
  · 任何写操作（INSERT/DELETE/DROP/CREATE/LOAD/CLEAR/ADD/MOVE/COPY/WITH）
    一律直接返回 error='只读端点，不支持写操作'，绝不触碰文件。

值的输出形式（rows 内）:
  · IRI      → 完整 IRI 字符串
  · 字面量   → 纯文本值（去掉引号与语言标签，便于前端直接展示中文标签）

自测: python ontology_sparql.py --in output/valve.nt --sparql "SELECT ?s ?p ?o WHERE {?s ?p ?o}"
"""
import os
import re
import sys

# ── 常量 ────────────────────────────────────────────────────────────────
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

# 写操作关键字（出现在查询首关键字位置即判为写操作）
_WRITE_KEYWORDS = ("INSERT", "DELETE", "DROP", "CREATE", "LOAD",
                   "CLEAR", "ADD", "MOVE", "COPY", "WITH", "UPDATE", "MODIFY")


class SparqlError(Exception):
    """SPARQL 子集解析/执行错误。"""


# ══════════════════════════════════════════════════════════════════════
# 词法：字符串 / 字面量扫描
# ══════════════════════════════════════════════════════════════════════
def _scan_literal(s: str, i: int):
    """从 s[i] 处的引号开始扫一个字面量（含 @lang / ^^datatype 后缀）。

    返回 (end_index, token_text)。
    """
    q = s[i]
    if s.startswith(q * 3, i):                    # 三引号长字符串
        end = s.find(q * 3, i + 3)
        if end < 0:
            raise SparqlError("字符串字面量未闭合")
        j = end + 3
    else:                                          # 单引号 / 双引号（支持 \\ 转义）
        j = i + 1
        while j < len(s):
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == q:
                break
            j += 1
        if j >= len(s):
            raise SparqlError("字符串字面量未闭合")
        j += 1
    k = j
    if k < len(s) and s[k] == "@":                 # @zh
        k += 1
        while k < len(s) and (s[k].isalnum() or s[k] == "-"):
            k += 1
    elif s.startswith("^^", k):                    # ^^xsd:string / ^^<...>
        k += 2
        if k < len(s) and s[k] == "<":
            e = s.find(">", k)
            if e < 0:
                raise SparqlError("数据类型 IRI 未闭合")
            k = e + 1
        else:
            while k < len(s) and (s[k].isalnum() or s[k] in ":#-_"):
                k += 1
    return k, s[i:k]


def _parse_literal(tok: str):
    """字面量 token → (value, lang, datatype)。"""
    if tok[:3] in ('"""', "'''"):
        q, start = tok[:3], 3
    else:
        q, start = tok[0], 1
    j = start
    while j < len(tok):
        if tok[j] == "\\":
            j += 2
            continue
        if tok.startswith(q, j):
            break
        j += 1
    value = tok[start:j]
    suffix = tok[j + len(q):]
    lang = dtype = None
    if suffix.startswith("@"):
        lang = suffix[1:]
    elif suffix.startswith("^^"):
        dtype = suffix[2:]
    # 反转义常用序列，便于与 NT 里读进来的值对齐比较
    value = value.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\'", "'")
    return value, lang, dtype


def _tokenize(s: str):
    """把一段三元组模式切成 token（空格分隔，但 <IRI> / "字面量" 整体保留）。"""
    toks, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
            continue
        if c == "<":
            j = s.find(">", i)
            if j < 0:
                raise SparqlError("IRI 未闭合: 缺少 '>'")
            toks.append(s[i:j + 1])
            i = j + 1
            continue
        if c in ('"', "'"):
            j, tok = _scan_literal(s, i)
            toks.append(tok)
            i = j
            continue
        if c in "?$":                             # 变量
            j = i + 1
            while j < n and (s[j].isalnum() or s[j] == "_"):
                j += 1
            if j == i + 1:
                raise SparqlError("变量名缺失：'?' 后需跟变量名")
            toks.append(s[i:j])
            i = j
            continue
        if c in "{}":
            raise SparqlError("不支持 GRAPH / 嵌套花括号")
        j = i
        while j < n and not s[j].isspace() and s[j] not in "{}":
            j += 1
        toks.append(s[i:j])
        i = j
    return toks


# ══════════════════════════════════════════════════════════════════════
# 语法：SPARQL 子集 → 中间结构
# ══════════════════════════════════════════════════════════════════════
def _mask_iri_and_string(text: str) -> str:
    """把 <IRI> 与字符串字面量替换成等长空格，用于安全地做关键字扫描（避免 IRI 里的词误判）。"""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "<":
            j = text.find(">", i)
            if j < 0:
                raise SparqlError("IRI 未闭合: 缺少 '>'")
            out.append(" " * (j - i + 1))
            i = j + 1
            continue
        if c in ('"', "'"):
            j, _ = _scan_literal(text, i)
            out.append(" " * (j - i))
            i = j
            continue
        if c == "#":                              # 注释行
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_prefixes(text: str):
    """剥掉开头的 PREFIX/@prefix 声明，返回 (prefixes, rest)。"""
    prefixes = {}
    rest = text
    while True:
        m = re.match(r"(?is)^\s*(?:PREFIX|@prefix)\s+([\w\-]*)\s*:\s*<([^>]*)>\s*", rest)
        if not m:
            break
        prefixes[m.group(1)] = m.group(2)
        rest = rest[m.end():]
    return prefixes, rest


def _find_matching_brace(text: str, start: int) -> int:
    """text[start] == '{'，返回匹配的 '}' 下标。"""
    depth = 0
    i, n = start, len(text)
    while i < n:
        c = text[i]
        if c == "<":
            j = text.find(">", i)
            if j < 0:
                raise SparqlError("IRI 未闭合: 缺少 '>'")
            i = j + 1
            continue
        if c in ('"', "'"):
            i, _ = _scan_literal(text, i)
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise SparqlError("WHERE 子图的 '{' 未闭合")


def _split_clauses(inner: str):
    """把 WHERE { ... } 内部切成 [('triples', text) | ('filter', expr)]。"""
    clauses, buf = [], []
    i, n = 0, len(inner)
    while i < n:
        c = inner[i]
        if c == "#":
            while i < n and inner[i] != "\n":
                i += 1
            continue
        if c == "<":
            j = inner.find(">", i)
            if j < 0:
                raise SparqlError("IRI 未闭合: 缺少 '>'")
            buf.append(inner[i:j + 1])
            i = j + 1
            continue
        if c in ('"', "'"):
            j, tok = _scan_literal(inner, i)
            buf.append(tok)
            i = j
            continue
        if c == "{":
            raise SparqlError("不支持嵌套花括号 / GRAPH / OPTIONAL 等写法")
        # FILTER( ... ) 顶层识别
        if inner[i:i + 6].upper() == "FILTER" and (i + 6 >= n or not (inner[i + 6].isalnum() or inner[i + 6] == "_")):
            k = i + 6
            while k < n and inner[k].isspace():
                k += 1
            if k >= n or inner[k] != "(":
                raise SparqlError("FILTER 语法错误：关键字后缺少 '('")
            depth, s = 0, k
            while k < n:
                if inner[k] == "(":
                    depth += 1
                elif inner[k] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            if k >= n:
                raise SparqlError("FILTER 表达式括号未闭合")
            if "".join(buf).strip():
                clauses.append(("triples", "".join(buf).strip()))
                buf = []
            clauses.append(("filter", inner[s + 1:k].strip()))
            i = k + 1
            continue
        if c == ".":
            if "".join(buf).strip():
                clauses.append(("triples", "".join(buf).strip()))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    if "".join(buf).strip():
        clauses.append(("triples", "".join(buf).strip()))
    return clauses


def _resolve_term(tok: str, prefixes: dict):
    """term token → ('var', name) | ('node', iri) | ('lit', value, lang)。"""
    if tok[0] in "?$":
        return ("var", tok[1:])
    if tok.startswith("<") and tok.endswith(">"):
        return ("node", tok[1:-1])
    if tok[0] in ('"', "'"):
        v, lang, _dt = _parse_literal(tok)
        return ("lit", v, lang)
    if tok == "a":
        return ("node", RDF_TYPE)
    if ":" in tok:
        pfx, local = tok.split(":", 1)
        if pfx in prefixes:
            return ("node", prefixes[pfx] + local)
        if pfx in ("http", "https"):
            return ("node", tok)
        raise SparqlError(f"未知前缀 '{pfx}:'（请用 PREFIX 声明，或改用完整 IRI）")
    raise SparqlError(f"无法识别的三元组项: '{tok}'")


def _parse_pattern(text: str, prefixes: dict):
    toks = _tokenize(text)
    if len(toks) != 3:
        raise SparqlError(f"只支持三元素三元组模式（主 谓 宾），实际收到 {len(toks)} 项: {text!r}"
                          + ("；不支持 ';' / ',' 简写" if any(t in ";," for t in toks) else ""))
    return (_resolve_term(toks[0], prefixes),
            _resolve_term(toks[1], prefixes),
            _resolve_term(toks[2], prefixes))


def _parse_filter(expr: str):
    """FILTER(?x = "字面量") / FILTER(?x != "字面量") → (var, op, term)。"""
    m = re.match(r"^\s*([?$][\w]*)\s*(=|!=)\s*(.+?)\s*$", expr)
    if not m:
        raise SparqlError('不支持的 FILTER 表达式（仅支持 ?x = "字面量" 与 ?x != "字面量"）: ' + expr)
    var, op, rhs = m.group(1)[1:], m.group(2), m.group(3)
    return var, op, rhs


# ══════════════════════════════════════════════════════════════════════
# 求值
# ══════════════════════════════════════════════════════════════════════
def _norm_object(o):
    """NT 解析产出的 object → 内部值 ('node', iri) | ('lit', value, lang)。"""
    if isinstance(o, tuple):
        val = o[0] if o else ""
        lang = o[1] if len(o) > 1 else None
        return ("lit", val, lang)
    return ("node", o)


def _bind(term, val, binding: dict) -> bool:
    """把 term 与内部值 val 匹配；成功则写回 binding。"""
    kind = term[0]
    if kind == "var":
        name = term[1]
        if name in binding:
            return binding[name] == val
        binding[name] = val
        return True
    if kind == "node":
        return val[0] == "node" and val[1] == term[1]
    # kind == 'lit'：值相等；模式未标语言 → 任意语言都算命中（"阀门" 匹配 "阀门"@zh）
    if val[0] != "lit":
        return False
    if val[1] != term[1]:
        return False
    return term[2] is None or term[2] == val[2]


def _filter_ok(var, op, rhs_tok, binding: dict, prefixes: dict) -> bool:
    if var not in binding:
        return False                                   # 未绑定 → FILTER 视为假
    left = binding[var]
    term = _resolve_term(rhs_tok, prefixes)
    if term[0] == "var":
        right = binding.get(term[1])
        eq = right is not None and left == right
    elif term[0] == "node":
        eq = left[0] == "node" and left[1] == term[1]
    else:
        eq = (left[0] == "lit" and left[1] == term[1]
              and (term[2] is None or term[2] == left[2]))
    return eq if op == "=" else (not eq)


def _to_output(val):
    """内部值 → 对外输出值。"""
    if val[0] == "node":
        return val[1]
    return val[1]                                       # 字面量取纯文本


def _load_triples(path: str):
    """复用 ontology_import.parse_input 读 NT/TTL/JSON-LD → 三元组列表。"""
    if not os.path.exists(path):
        raise SparqlError(f"文件不存在: {path}")
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import ontology_import as oi
    except Exception as e:                              # pragma: no cover
        raise SparqlError(f"无法导入 ontology_import（同目录需存在该模块）: {e}")
    fmt, data = oi.parse_input(path)
    if fmt == "jsonld":
        triples, _ctx = oi.graph_from_jsonld(data)
        return triples
    # turtle / nt：data = (prefixes, triples)
    if isinstance(data, tuple) and len(data) == 2 and isinstance(data[1], list):
        return data[1]
    if isinstance(data, list):
        return data
    raise SparqlError("ontology_import.parse_input 返回了无法识别的结构")


# ══════════════════════════════════════════════════════════════════════
# 对外接口
# ══════════════════════════════════════════════════════════════════════
def query(nt_path: str, sparql: str) -> dict:
    """对 NT 文件执行只读 SPARQL 子集查询。

    返回 {'vars': [...], 'rows': [ {var: value, ...}, ... ], 'error': None}
    解析/执行失败 → {'vars': [], 'rows': [], 'error': '<中文错误原因>'}
    """
    try:
        return _query(nt_path, sparql)
    except SparqlError as e:
        return {"vars": [], "rows": [], "error": str(e)}
    except Exception as e:                              # 兜底：绝不向调用方抛异常
        return {"vars": [], "rows": [], "error": f"查询失败: {type(e).__name__}: {e}"}


def _query(nt_path: str, sparql: str) -> dict:
    if not isinstance(sparql, str) or not sparql.strip():
        raise SparqlError("SPARQL 语句为空")

    # 前缀声明必须在**原文**上剥（掩码会把 <IRI> 打成空格，前缀声明就认不出了）
    prefixes, q = _strip_prefixes(sparql)
    masked = _mask_iri_and_string(q)
    if not masked.strip():
        raise SparqlError("SPARQL 语句为空（仅注释或仅前缀声明）")
    head_word = re.match(r"\s*([A-Za-z]+)", masked)
    first = (head_word.group(1) if head_word else "").upper()

    if first in _WRITE_KEYWORDS:
        return {"vars": [], "rows": [], "error": "只读端点，不支持写操作"}
    if first != "SELECT":
        raise SparqlError(f"只支持 SELECT 查询，收到: {first or '空语句'}")

    # ── 定位 WHERE { ... }（在原文上做，保证三元组内容原样）──
    brace = q.find("{")
    if brace < 0:
        raise SparqlError("缺少 WHERE { ... } 子图")
    close = _find_matching_brace(q, brace)
    prologue = q[:brace]
    inner = q[brace + 1:close]
    if q[close + 1:].strip():
        raise SparqlError("WHERE 子图之后不支持其他子句（如 ORDER BY / LIMIT / GROUP BY）")

    # ── SELECT 变量表 ──
    pm = re.search(r"(?is)\bSELECT\b(.*?)(\bWHERE\b|$)", prologue)
    if not pm:
        raise SparqlError("SELECT 子句解析失败")
    sel_txt = pm.group(1).strip()
    sel_txt = re.sub(r"(?i)^DISTINCT\s+", "", sel_txt)
    if sel_txt == "*":
        select_all = True
        select_vars = []
    else:
        if not sel_txt:
            raise SparqlError("SELECT 后缺少变量（或使用 SELECT *）")
        select_vars = [t[1:] for t in re.findall(r"[?$][\w]+", sel_txt)]
        if not select_vars:
            raise SparqlError("SELECT 子句未解析出变量")
        select_all = False

    # ── 切分 WHERE 子图：三元组片段 + FILTER 表达式（全部基于原文 inner）──
    clauses = _split_clauses(inner)

    patterns, filters = [], []
    for kind, text in clauses:
        if kind == "triples":
            patterns.append(_parse_pattern(text, prefixes))
        else:
            filters.append(_parse_filter(text))
    if not patterns:
        raise SparqlError("WHERE 子图内没有任何三元组模式")

    # ── 收集变量 ──
    pattern_vars = []
    for pat in patterns:
        for term in pat:
            if term[0] == "var" and term[1] not in pattern_vars:
                pattern_vars.append(term[1])
    if select_all:
        vars_out = list(pattern_vars)
    else:
        unknown = [v for v in select_vars if v not in pattern_vars]
        if unknown:
            raise SparqlError("变量未在 WHERE 中出现: " + ", ".join("?" + v for v in unknown))
        vars_out = list(select_vars)

    # ── 加载数据 ──
    triples = _load_triples(nt_path)

    # ── 逐模式 join ──
    bindings = [{}]
    for pat in patterns:
        nxt = []
        for b in bindings:
            for (s, p, o) in triples:
                trial = dict(b)
                if (_bind(pat[0], ("node", s), trial)
                        and _bind(pat[1], ("node", p), trial)
                        and _bind(pat[2], _norm_object(o), trial)):
                    nxt.append(trial)
        bindings = nxt
        if not bindings:
            break

    # ── FILTER ──
    for (var, op, rhs) in filters:
        bindings = [b for b in bindings if _filter_ok(var, op, rhs, b, prefixes)]

    rows = [{v: _to_output(b[v]) for v in vars_out if v in b} for b in bindings]
    return {"vars": vars_out, "rows": rows, "error": None}


# ══════════════════════════════════════════════════════════════════════
# 最小 CLI（便于人工自测，不参与产品运行时）
# ══════════════════════════════════════════════════════════════════════
def main():
    import argparse
    import json
    ap = argparse.ArgumentParser(description="只读 SPARQL 子集查询（零依赖）")
    ap.add_argument("--in", dest="infile", required=True, help=".nt / .ttl / .jsonld")
    ap.add_argument("--sparql", required=True, help="SPARQL 子集查询串")
    ap.add_argument("--limit", type=int, default=20, help="打印行数上限（默认 20）")
    a = ap.parse_args()
    res = query(a.infile, a.sparql)
    if res["error"]:
        print(f"[error] {res['error']}")
        return 1
    print(f"vars={res['vars']}  rows={len(res['rows'])}")
    for r in res["rows"][:a.limit]:
        print("  " + json.dumps(r, ensure_ascii=False))
    if len(res["rows"]) > a.limit:
        print(f"  ... 另 {len(res['rows']) - a.limit} 行省略")
    return 0


if __name__ == "__main__":
    sys.exit(main())
