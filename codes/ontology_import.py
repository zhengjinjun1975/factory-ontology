#!/usr/bin/env python3
"""ontology_import.py — 本体导入层（L4）：外部标准本体 → 内部模型 + 对齐报告。

定位与 ontology_export.py 对称：**单向、只读、离线**。运行时永不 import 本模块。
纯标准库手写 Turtle / JSON-LD 解析，不引 rdflib、不引推理机、不联网。

用途（两条，都是"为未来准备"的能力）：
  1) **往返自检（round-trip）**：把 ontology_export 的产物读回来，核对 label/definition/
     subClassOf/domain/range 是否无损 —— 这是导出物质量的可重跑门。
  2) **外部对齐**：读入外部 OWL/SHACL（国标词表、行业标准、schema.org 子集等），
     与本库 schema 做对齐报告（同名类/属性覆盖率、IRI 映射、可供对齐的空位），
     从而让"对标国标"从口头变成可核对的清单。

用法:
  python ontology_import.py --in export/ontology.ttl                       # 概览
  python ontology_import.py --in export/ontology.ttl --roundtrip           # 与 schema 往返核对
  python ontology_import.py --in external/gb_wordlist.ttl --schema config/ontology_schema.json --align
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDFS_SUBCLASS = "http://www.w3.org/2000/01/rdf-schema#subClassOf"
RDFS_DOMAIN = "http://www.w3.org/2000/01/rdf-schema#domain"
RDFS_RANGE = "http://www.w3.org/2000/01/rdf-schema#range"
SKOS_DEF = "http://www.w3.org/2004/02/skos/core#definition"
OWL_CLASS = "http://www.w3.org/2002/07/owl#Class"
OWL_DATAPROP = "http://www.w3.org/2002/07/owl#DatatypeProperty"
OWL_OBJPROP = "http://www.w3.org/2002/07/owl#ObjectProperty"
OWL_ONTOLOGY = "http://www.w3.org/2002/07/owl#Ontology"
OWL_VERSIONIRI = "http://www.w3.org/2002/07/owl#versionIRI"

_IRI_RE = re.compile(r"^<([^>]*)>$")


# ═══════════ Turtle 解析（纯标准库，够用且从严）═══════════
def _split_top(s: str, seps=(";", ",", ".")):
    """按顶层分隔符切分（跳过 <...> / "..." / '...' / 括号内）。"""
    out, buf, i, n = [], [], 0, len(s)
    in_iri = in_str = None
    depth = 0
    while i < n:
        c = s[i]
        if in_iri:
            buf.append(c)
            if c == ">":
                in_iri = False
        elif in_str:
            if c == "\\" and i + 1 < n:
                buf.append(s[i:i + 2])      # 转义序列整体保留，避免把 \" 当成字符串结束
                i += 2
                continue
            if s.startswith(in_str, i):
                end = i + len(in_str)
                buf.append(s[i:end])
                i = end
                in_str = None
                continue
            buf.append(c)
        elif c == "<":
            in_iri = True
            buf.append(c)
        elif c in ('"', "'"):
            in_str = c * 3 if s.startswith(c * 3, i) else c
            buf.append(s[i:i + len(in_str)])
            i += len(in_str)
            continue
        elif c in "([":
            depth += 1
            buf.append(c)
        elif c in ")]":
            depth -= 1
            buf.append(c)
        elif c in seps and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return [x for x in out if x]


def _strip_comments(text: str) -> str:
    """去 Turtle 注释。必须跳过 <IRI> 与字符串 —— 否则 IRI 里的 # 会被当注释切掉(曾致前缀表污染)。"""
    out, i, n = [], 0, len(text)
    in_iri = in_str = None
    while i < n:
        c = text[i]
        if in_iri:
            out.append(c)
            if c == ">":
                in_iri = False
            i += 1
            continue
        if in_str:
            if c == "\\":
                out.append(text[i:i + 2])
                i += 2
                continue
            if text.startswith(in_str, i):
                out.append(text[i:i + len(in_str)])
                i += len(in_str)
                in_str = None
                continue
            out.append(c)
            i += 1
            continue
        if c == "<":
            in_iri = True
            out.append(c)
            i += 1
            continue
        if c in ('"', "'"):
            in_str = c * 3 if text.startswith(c * 3, i) else c
            out.append(text[i:i + len(in_str)])
            i += len(in_str)
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def parse_turtle(text: str):
    """极简 Turtle → (prefixes, triples[(s,p,o)])。支持 @prefix/PREFIX、a、;,、字符串@lang/^^类型。"""
    prefixes = {}
    body = _strip_comments(text)
    # @prefix / @base / PREFIX
    for m in re.finditer(r"(?:@prefix|PREFIX)\s+([\w\-]*):?\s*<([^>]*)>\s*\.?", body, re.I):
        prefixes[m.group(1)] = m.group(2)
    body = re.sub(r"(?:@prefix|PREFIX)\s+[\w\-]*:?\s*<[^>]*>\s*\.?", "", body, flags=re.I)

    def expand(term: str) -> str:
        term = term.strip()
        m = _IRI_RE.match(term)
        if m:
            return m.group(1)
        if ":" in term:
            pfx, local = term.split(":", 1)
            base = prefixes.get(pfx)
            if base is not None:
                return base + local
            return term  # 未知前缀: 原样(如 xsd:string 这类保留)
        return term

    def literal(term: str):
        term = term.strip()
        m = re.match(r'^"(.*)"(?:@([\w\-]+)|\^\^(<[^>]*>|[\w\-]+:[\w\-]+))?$', term, re.S) or \
            re.match(r"^'(.*)'(?:@([\w\-]+)|\^\^(<[^>]*>|[\w\-]+:[\w\-]+))?$", term, re.S)
        if m:
            return m.group(1), (m.group(2) or None)
        return None

    triples = []
    # 按 . 切语句（顶层）
    for stmt in _split_top(body, seps=(".")):
        parts = _split_top(stmt, seps=(";",))
        if not parts:
            continue
        subj = expand(parts[0].split()[0]) if parts[0].split() else None
        if not subj:
            continue
        # subject 后可能紧跟谓词（`subj p o`）或分号分隔
        head = parts[0]
        rest = parts[1:]
        toks = head.split(None, 1)
        if len(toks) > 1:
            rest = [toks[1]] + rest
        for chunk in rest:
            if not chunk.strip():
                continue
            objects = _split_top(chunk, seps=(",",))
            if not objects:
                continue
            pred_tok = objects[0].split(None, 1)
            if not pred_tok:
                continue
            p = RDF_TYPE if pred_tok[0] == "a" else expand(pred_tok[0])
            objs = ([pred_tok[1]] if len(pred_tok) > 1 else []) + objects[1:]
            for o in objs:
                lit = literal(o)
                if lit:
                    triples.append((subj, p, lit))          # (s, p, (value, lang))
                else:
                    triples.append((subj, p, expand(o)))
    return prefixes, triples


def parse_input(path: str):
    """读 .ttl / .jsonld / .json → (fmt, data)。"""
    text = open(path, encoding="utf-8").read()
    if path.lower().endswith((".jsonld", ".json")):
        return "jsonld", json.loads(text)
    return "turtle", parse_turtle(text)


def graph_from_jsonld(doc: dict):
    """JSON-LD → 同构三元组（供与 Turtle 一致的后续处理）。"""
    ctx = doc.get("@context") or {}
    vocab = ctx.get("@vocab", "") if isinstance(ctx, dict) else ""
    short = {k: v for k, v in (ctx.items() if isinstance(ctx, dict) else []) if isinstance(v, str) and ":" in v}
    term_map = {}
    for k, v in short.items():
        if v.startswith("http"):
            term_map[k] = v
        else:
            pfx, local = v.split(":", 1)
            base = (ctx.get(pfx) or "") if isinstance(ctx, dict) else ""
            if base:
                term_map[k] = base + local   # 键必须是 k(简写名), 不是 v
    # 前缀表(用于展开 @type 里的 owl:Class / rdfs:Class 这类简写)
    prefixes = {k: v for k, v in (ctx.items() if isinstance(ctx, dict) else [])
                if isinstance(v, str) and v.startswith("http")}

    def expand_term(t: str) -> str:
        if str(t).startswith("http"):
            return t
        if ":" in str(t):
            pfx, local = str(t).split(":", 1)
            if pfx in prefixes:
                return prefixes[pfx] + local
        return (vocab + str(t)) if vocab else str(t)
    triples = []
    for node in (doc.get("@graph") or []):
        s = node.get("@id")
        if not s:
            continue
        for k, v in node.items():
            if k.startswith("@"):
                # @type → rdf:type 三元组；其余 @ 关键字(@id 等)一律跳过
                if k == "@type":
                    for tv in (v if isinstance(v, list) else [v]):
                        triples.append((s, RDF_TYPE, expand_term(tv)))
                continue
            p = expand_term(term_map.get(k, k))
            vals = v if isinstance(v, list) else [v]
            for val in vals:
                if isinstance(val, dict):
                    o = val.get("@id") or val.get("@value")
                    lang = val.get("@language")
                    if o is None:
                        continue
                    triples.append((s, p, (str(o), lang) if lang else str(o)))
                else:
                    triples.append((s, p, str(val)))
    return triples, (doc.get("@context") or {})


# ═══════════ 图 → 模型 ═══════════
def graph_to_model(triples):
    """三元组 → {ontology, classes, datatype_props, object_props, untyped_props}。"""
    types = defaultdict(set)
    labels, defs, subclass, dom, rng = {}, {}, {}, {}, {}
    for s, p, o in triples:
        if p == RDF_TYPE and isinstance(o, str):
            types[s].add(o)
        elif p == RDFS_LABEL:
            labels[s] = o[0] if isinstance(o, tuple) else o
        elif p == SKOS_DEF:
            defs[s] = o[0] if isinstance(o, tuple) else o
        elif p == RDFS_SUBCLASS:
            subclass.setdefault(s, set()).add(o)
        elif p == RDFS_DOMAIN:
            dom.setdefault(s, set()).add(o)
        elif p == RDFS_RANGE:
            rng.setdefault(s, set()).add(o)
    model = {"ontology": {}, "classes": {}, "datatype_props": {}, "object_props": {}, "untyped_props": {}}
    for s, ts in types.items():
        if OWL_ONTOLOGY in ts:
            model["ontology"] = {"iri": s, "label": labels.get(s, ""), "version_iri": None}
    for s, p, o in triples:
        if p == OWL_VERSIONIRI and model["ontology"] and model["ontology"]["iri"] == s:
            model["ontology"]["version_iri"] = o
    for s, ts in types.items():
        cls = {"uri": s, "label": labels.get(s, ""), "definition": defs.get(s, ""),
               "subClassOf": sorted(subclass.get(s, set()))}
        if OWL_CLASS in ts:
            model["classes"][s] = cls
        elif OWL_DATAPROP in ts:
            model["datatype_props"][s] = {**cls, "domain": sorted(dom.get(s, set())),
                                          "range": sorted(rng.get(s, set()))}
        elif OWL_OBJPROP in ts:
            model["object_props"][s] = {**cls, "domain": sorted(dom.get(s, set())),
                                        "range": sorted(rng.get(s, set()))}
    # 有 domain/range 但没声明类型 → untyped
    for s in set(dom) | set(rng):
        if s not in model["datatype_props"] and s not in model["object_props"]:
            model["untyped_props"][s] = {"uri": s, "label": labels.get(s, ""),
                                         "domain": sorted(dom.get(s, set())),
                                         "range": sorted(rng.get(s, set()))}
    return model


# ═══════════ 往返核对 / 对齐报告 ═══════════
def roundtrip(model, schema_path):
    """把导入模型与源 schema 对照：类/属性/描述项是否无损往返。"""
    if not schema_path or not os.path.exists(schema_path):
        return {"error": "需要 --schema 做往返核对"}
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import schema_ontology as so
    sch = so.fill_iris(so.load_schema(schema_path))
    m = sch.get("_ns") or so.resolve_namespace(sch)
    ns = m["ns"]
    miss = {"class_label": [], "class_def": [], "subClassOf": [], "prop_domain_range": [], "prop_label": []}
    n_cls = got_cls = 0
    for e in sch.get("entities", []):
        uri = e.get("iri") or ns + so._pascal(e["id"])
        got = model["classes"].get(uri) or model["classes"].get(uri.rstrip("#")) or {}
        n_cls += 1
        if not got:
            miss["class_label"].append(f"类未导入: {uri}")
            continue
        got_cls += 1
        if (e.get("label") or "") != (got.get("label") or ""):
            miss["class_label"].append(f"{uri}: schema='{e.get('label')}' import='{got.get('label')}'")
        if (e.get("definition") or "") != (got.get("definition") or ""):
            miss["class_def"].append(uri)
        if e.get("parent"):
            want = ns + so._pascal(e["parent"])
            if want not in (got.get("subClassOf") or []):
                miss["subClassOf"].append(f"{uri} 缺 subClassOf {want}")
    # 属性（与导出器同规则：跳过主键列与外键列 —— 外键在导出侧表达为对象属性）
    n_prop = 0
    for e in sch.get("entities", []):
        key = e.get("key")
        rel_cols = {r["fk"].split(".")[1] for r in sch.get("relations", [])
                    if (r.get("fk") or "").startswith(str(e.get("table", "")) + ".")}
        for a in e.get("attributes", []):
            if a["name"] == key or a["name"] in rel_cols:
                continue
            uri = a.get("iri") or (ns + so._local_name(a["name"]))
            n_prop += 1
            got = model["datatype_props"].get(uri)
            if not got:
                miss["prop_domain_range"].append(f"属性未导入: {uri}")
                continue
            if (a.get("label") or a["name"]) != (got.get("label") or ""):
                miss["prop_label"].append(uri)
            want_dom = e.get("iri") or ns + so._pascal(e["id"])
            if want_dom not in (got.get("domain") or []):
                miss["prop_domain_range"].append(f"{uri} domain 不符: {got.get('domain')}")
    n_obj = len(model["object_props"])
    lost = sum(len(v) for v in miss.values())
    return {"classes_expected": n_cls, "props_expected": n_prop,
            "classes_imported": got_cls, "dataprops_imported": n_prop - len([x for x in miss["prop_domain_range"] if x.startswith("属性未导入")]),
            "objectprops_imported": len(model["object_props"]), "loss_count": lost,
            "losses": {k: v for k, v in miss.items() if v}, "ok": lost == 0}


def align_report(model, schema_path):
    """外部本体 ↔ 本库 schema 的对齐报告（同名/IRI 覆盖、可对齐空位）。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import schema_ontology as so
    sch = so.fill_iris(so.load_schema(schema_path))
    m = sch.get("_ns") or so.resolve_namespace(sch)
    ns = m["ns"]

    def local(uri):
        return uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]

    ext_by_local = {local(u): u for u in model["classes"]}
    rows, matched, unmatched_ext = [], 0, []
    for e in sch.get("entities", []):
        ours = e.get("iri") or ns + so._pascal(e["id"])
        hit = ext_by_local.get(so._pascal(e["id"])) or ext_by_local.get(e["id"])
        if hit:
            matched += 1
            rows.append({"ours": ours, "theirs": hit,
                         "same_label": (model["classes"][hit].get("label") or "") == (e.get("label") or ""),
                         "theirs_def": model["classes"][hit].get("definition", "")})
        else:
            rows.append({"ours": ours, "theirs": None, "note": "外部无同名类"})
    used = {r["theirs"] for r in rows if r["theirs"]}
    unmatched_ext = [u for u in model["classes"] if u not in used]
    return {"our_classes": len(sch.get("entities", [])), "external_classes": len(model["classes"]),
            "matched": matched, "external_only": len(unmatched_ext),
            "external_only_sample": sorted(unmatched_ext)[:10],
            "rows": rows[:40]}


def main():
    ap = argparse.ArgumentParser(description="本体导入（Turtle/JSON-LD → 模型 + 往返/对齐报告，纯标准库）")
    ap.add_argument("--in", dest="infile", required=True, help="输入的 .ttl / .jsonld")
    ap.add_argument("--schema", help="本库 schema（往返核对 / 对齐用）")
    ap.add_argument("--roundtrip", action="store_true", help="与 schema 做往返无损核对")
    ap.add_argument("--align", action="store_true", help="与 schema 做对齐报告")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    a = ap.parse_args()

    fmt, data = parse_input(a.infile)
    triples = graph_from_jsonld(data)[0] if fmt == "jsonld" else data[1]
    model = graph_to_model(triples)
    print(f"源格式: {fmt} | 三元组 {len(triples)} | 类 {len(model['classes'])} | "
          f"数据属性 {len(model['datatype_props'])} | 对象属性 {len(model['object_props'])}")
    if model["ontology"]:
        o = model["ontology"]
        print(f"本体头: {o['iri']} label='{o['label']}' versionIRI={o['version_iri']}")

    out = {"format": fmt, "triples": len(triples),
           "classes": len(model["classes"]), "datatype_props": len(model["datatype_props"]),
           "object_props": len(model["object_props"]), "ontology": model["ontology"]}
    if a.roundtrip:
        rt = roundtrip(model, a.schema)
        out["roundtrip"] = rt
        print(f"\n往返核对: {'✅ 无损' if rt.get('ok') else '❌ 有丢失'} "
              f"(类 {rt.get('classes_imported')}/{rt.get('classes_expected')}, "
              f"数据属性 {rt.get('dataprops_imported')}/{rt.get('props_expected')}, 丢失 {rt.get('loss_count')})")
        for k, v in (rt.get("losses") or {}).items():
            print(f"  {k}: {len(v)} 例  e.g. {v[:2]}")
    if a.align:
        al = align_report(model, a.schema)
        out["align"] = al
        print(f"\n对齐: 本库类 {al['our_classes']} / 外部类 {al['external_classes']} / "
              f"同名匹配 {al['matched']} / 外部独有 {al['external_only']}")
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
