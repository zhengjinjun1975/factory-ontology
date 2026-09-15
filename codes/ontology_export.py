#!/usr/bin/env python3
"""ontology_export.py — 本体合规导出层（L3）：schema → 标准 OWL / SHACL / JSON-LD。

定位：**单向、只读、离线**。运行时永不 import 本模块——它只是"合规交付物生成器"，
不参与问答/决策。红线：纯标准库（手写 Turtle 序列化），不引 rdflib、不引推理机。

对标：GB/T 48000.3-2026 §5.1(e)「符合 OWL/SHACL」/§5.3-5.5 类与属性描述项/§9 命名空间扩展、
      GB/T 41472.2-2022（OWL 本体开发规则）、GB/T 42131-2022、ISO/IEC 21838、IEEE 知识图谱评估标准。

用法:
  python ontology_export.py --schema config/ontology_schema.json --outdir export
  python ontology_export.py --schema config/ontology_schema.json --outdir export --data data_onb_251
产物:
  ontology.ttl    owl:Ontology(+versionIRI) / owl:Class / rdfs:subClassOf / skos:definition
                  / owl:DatatypeProperty|ObjectProperty / rdfs:domain|range / rdfs:label
  shapes.ttl      SHACL：constraints → sh:NodeShape + sh:PropertyShape
  ontology.jsonld JSON-LD（@context 复用 RDF 词汇，供互操作/跨系统聚合）
  ontology-full.ttl  (需 --data) 术语层 + **实例层(A-Box)** —— 表头之外的真正属性值。
                     实例命名/属性规则直接复用 schema_ontology.to_nt，不重写第二套。

幂等：同一 schema 导出结果字节稳定（便于 diff 与版本管理）。
"""
import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_ontology as so  # noqa: E402

VERSION = "0.3.0"

PREFIXES = [
    ("owl", "http://www.w3.org/2002/07/owl#"),
    ("rdf", "http://www.w3.org/1999/02/22-rdf-syntax-ns#"),
    ("rdfs", "http://www.w3.org/2000/01/rdf-schema#"),
    ("xsd", "http://www.w3.org/2001/XMLSchema#"),
    ("skos", "http://www.w3.org/2004/02/skos/core#"),
    ("sh", "http://www.w3.org/ns/shacl#"),
]

# constraint.type → SHACL 映射（GB/T 48000.3 §5.1(e)；与运行时 validate 同源，避免双写漂移）
_SHACL_BY_TYPE = {
    "unique": ("sh:maxCount", "1"),
    "required": ("sh:minCount", "1"),
    "positive": ("sh:minInclusive", "0"),
}

# 业务域中文名 → 英文标识符（通用词表，非业务硬编码）。
# 为什么需要：Turtle 前缀名不允许中文，域类名必须转 ASCII；这张表覆盖的是
# "业务域"这一层通用概念集，不绑定任何具体行业。未收录的走 _domain_id 兜底。
_DOMAIN_EN = {
    "生产域": "Production", "制造域": "Manufacturing", "销售域": "Sales",
    "采购域": "Procurement", "供应域": "Supply", "质量域": "Quality",
    "库存域": "Inventory", "仓储域": "Warehouse", "设备域": "Equipment",
    "财务域": "Finance", "人力域": "Hr", "研发域": "Rnd",
    "物流域": "Logistics", "客户域": "Customer", "原料域": "Material",
    "工艺域": "Process", "运维域": "Maintenance", "安全域": "Safety",
    "合规域": "Compliance", "能源域": "Energy",
}


def _domain_id(name: str) -> str:
    """业务域名称 → 合法 Turtle 局部名（英文词表优先，兜底稳定哈希）。"""
    n = str(name or "").strip()
    if not n:
        return ""
    if n in _DOMAIN_EN:
        return _DOMAIN_EN[n]
    ascii_ = re.sub(r"[^A-Za-z0-9]", "", n)
    if ascii_ and ascii_[0].isalpha():
        return ascii_
    return "Domain" + hashlib.md5(n.encode("utf-8")).hexdigest()[:4]


def _esc(s) -> str:
    """Turtle 字符串转义。"""
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _xsd(t: str) -> str:
    name = so._XSD_BY_TYPE.get(t, "string")
    return f"xsd:{name}"


def _local(iri: str, ns: str) -> str:
    """IRI → 前缀化名称（本命名空间内用前缀，其余保留 <>）。"""
    if iri.startswith(ns):
        return iri[len(ns):]
    return f"<{iri}>"


def build_ontology_ttl(schema: dict) -> str:
    """生成 OWL Turtle（类 + 层次 + 定义 + 属性 + domain/range + 本体头）。"""
    schema = so.fill_iris(schema)
    m = schema.get("_ns") or so.resolve_namespace(schema)
    ns, prefix = m["ns"], m["prefix"]
    L = [f"# {m['name']} — OWL 导出（ontology_export.py v{VERSION}）",
         f"# 源: schema {schema.get('name', '')} v{schema.get('version', '')}",
         ""]
    for p, u in PREFIXES:
        L.append(f"@prefix {p}: <{u}> .")
    L.append(f"@prefix {prefix}: <{ns}> .")
    L.append("")
    # 本体头
    L += [f"{prefix}: a owl:Ontology ;",
          f'    rdfs:label "{_esc(m["label"])}"@zh ;',
          f"    owl:versionIRI <{m['version_iri']}> .",
          ""]
    # 类层次：根 → 域 → 实体（三层）
    # 实体自带 domain（生产域/销售域…）时就地生成域类，避免所有类平铺挂根
    ents = schema.get("entities", [])
    ent_ids = {e["id"] for e in ents}
    roots, domains = [], {}          # domains: 域名 → 域类局部名
    for e in ents:
        d = str(e.get("domain") or "").strip()
        if d:
            domains.setdefault(d, _domain_id(d))
        p = e.get("parent")
        if p and p not in ent_ids:
            roots.append(p)
    # 显式 parent 声明的外部根（历史兼容）
    for p in sorted(set(roots)):
        if p not in domains:
            L += [f"{prefix}:{so._pascal(p)} a owl:Class ;",
                  f'    rdfs:label "{_esc(p)}"@zh .', ""]
    # 域类：挂到各自 parent（缺省 BusinessObject）之下
    parent_by_domain = {}
    for e in ents:
        d = str(e.get("domain") or "").strip()
        if d and e.get("parent"):
            parent_by_domain.setdefault(d, e["parent"])
    for d, did in sorted(domains.items()):
        sup = so._pascal(parent_by_domain.get(d) or "BusinessObject")
        L += [f"{prefix}:{did} a owl:Class ;",
              f"    rdfs:subClassOf {prefix}:{sup} ;",
              f'    rdfs:label "{_esc(d)}"@zh .', ""]
    # 实体类
    for e in sorted(schema.get("entities", []), key=lambda x: x["id"]):
        local = _local(e["iri"], ns)
        body = [f"{prefix}:{local} a owl:Class"]
        # 有 domain 就挂域类（三层：根→域→实体）；无则退回显式 parent
        sup = domains.get(str(e.get("domain") or "").strip())
        if not sup and e.get("parent"):
            sup = so._pascal(e["parent"])
        if sup:
            body.append(f"    rdfs:subClassOf {prefix}:{sup}")
        body.append(f'    rdfs:label "{_esc(e.get("label") or e["id"])}"@zh')
        if e.get("definition"):
            body.append(f'    skos:definition "{_esc(e["definition"])}"@zh')
        L.append(" ;\n".join([body[0]] + [f"    {b.strip()}" if not b.strip().startswith("    ") else b for b in body[1:]]) + " .")
        L.append("")
    # 属性
    for e in sorted(schema.get("entities", []), key=lambda x: x["id"]):
        cls = _local(e["iri"], ns)
        key = e.get("key")
        rel_cols = {r["fk"].split(".")[1] for r in schema.get("relations", [])
                    if (r.get("fk") or "").startswith(str(e.get("table", "")) + ".")}
        for a in sorted(e.get("attributes", []), key=lambda x: x["name"]):
            # key 列也要声明为数据属性 —— owl:hasKey(见 _axioms)会引用它, 跳过就成悬空引用
            if a["name"] in rel_cols:
                continue
            p = so._local_name(a["name"])
            L.append(f"{prefix}:{p} a owl:DatatypeProperty ;")
            L.append(f"    rdfs:domain {prefix}:{cls} ;")
            rng = a.get("range")
            L.append(f"    rdfs:range {('<'+rng+'>') if (rng and str(rng).startswith('http')) else _xsd(a.get('type'))} ;")
            L.append(f'    rdfs:label "{_esc(a.get("label") or a["name"])}"@zh' +
                     (f' ;\n    skos:definition "{_esc(a["definition"])}"@zh' if a.get("definition") else "") + " .")
            L.append("")
    for r in sorted(schema.get("relations", []), key=lambda x: str(x.get("id"))):
        rel = _local(r.get("iri") or (ns + str(r["id"])), ns)
        L.append(f"{prefix}:{rel} a owl:ObjectProperty ;")
        L.append(f"    rdfs:domain {prefix}:{so._pascal(str(r['from']))} ;")
        L.append(f"    rdfs:range {prefix}:{so._pascal(str(r['to']))} ;")
        L.append(f'    rdfs:label "{_esc(r.get("label", "关联"))}"@zh' +
                 (f' ;\n    skos:definition "{_esc(r["definition"])}"@zh' if r.get("definition") else "") + " .")
        L.append("")
    L += _axioms(schema, ns, prefix)
    return "\n".join(L)


def _axioms(schema: dict, ns: str, prefix: str) -> list:
    """最小公理集（OWL TBox）：主键唯一 + 属性基数约束。

    为什么只做这三类（这是"轻量有限对齐"的关键取舍）：
      · 能从 schema 零人工自动派生，不破坏"表驱动"核心优势
      · 是**真公理**（可被标准推理机消费做蕴含推理），不是注释性的摆设
      · 不引推理机、不引第三方依赖 —— 公理只是导出物的属性，运行时依旧轻量

    与 SHACL 的分工（同源 constraints，双轨互补，非重复）：
      SHACL  → 数据校验（运行时挡脏数据，工程侧）
      OWL 公理 → 语义蕴含（外部推理机/对接方可推导，语义侧）

    覆盖 GB/T 48000.3 §5.1(e) 与 ISO/IEC 21838 对 TBox 形式化层的最低要求。
    """
    L = ["# ── 最小公理集（TBox）：主键唯一 + 属性基数 ──",
         "# 由 schema 自动派生；与 shapes.ttl 同源，前者做语义蕴含、后者做数据校验", ""]
    ent_by_id = {e["id"]: e for e in schema.get("entities", [])}
    # ① 主键唯一 → owl:hasKey（真公理：两实例主键相同即可推出是同一实例）
    for e in sorted(schema.get("entities", []), key=lambda x: x["id"]):
        key = e.get("key")
        if not key:
            continue
        L.append(f"{prefix}:{_local(e['iri'], ns)} owl:hasKey ( {prefix}:{so._local_name(key)} ) ;")
        L.append(f"    rdfs:label \"主键唯一：{_esc(key)}\"@zh .")
        L.append("")
    # ② 属性基数：required → minCardinality 1；unique → maxCardinality 1
    for c in schema.get("constraints", []):
        on = str(c.get("on") or "")
        if "." not in on:
            continue
        eid, attr = on.split(".", 1)
        e = ent_by_id.get(eid)
        if not e:
            continue
        ctype = c.get("type")
        # unique 若落在主键列上 → 跳过：owl:hasKey 已表达唯一性，再加 maxCardinality 是冗余公理
        if ctype == "unique" and attr == e.get("key"):
            continue
        card = {"required": ("minCardinality", "必填"), "unique": ("maxCardinality", "唯一")}.get(ctype)
        if not card:
            continue
        kind, cn = card
        L += [f"{prefix}:{_local(e['iri'], ns)} rdfs:subClassOf [",
              "    a owl:Restriction ;",
              f"    owl:onProperty {prefix}:{so._local_name(attr)} ;",
              f"    owl:{kind} \"1\"^^xsd:nonNegativeInteger ;",
              f"    rdfs:label \"属性{cn}：{_esc(attr)}\"@zh",
              "] .", ""]
    # ③ 枚举 → owl:oneOf（枚举类是 OWL 可表达的真公理；range/positive/pattern 类
    #    约束 OWL DL 表达不了，交给 SHACL 那一轨，这是双轨分工而非覆盖不全）
    for c in schema.get("constraints", []):
        if c.get("type") != "in" or not c.get("values"):
            continue
        on = str(c.get("on") or "")
        if "." not in on:
            continue
        eid, attr = on.split(".", 1)
        e = ent_by_id.get(eid)
        if not e:
            continue
        enum_cls = f"{so._pascal(attr)}Enum"
        vals = " ".join(f'"{_esc(v)}"' for v in c["values"])
        L += [f"{prefix}:{enum_cls} a owl:Class ;",
              f"    owl:oneOf ( {vals} ) ;",
              f"    rdfs:label \"{_esc(c.get('msg') or attr)}（枚举）\"@zh .",
              "",
              f"{prefix}:{so._local_name(attr)} rdfs:range {prefix}:{enum_cls} .",
              ""]
    return L


def build_shapes_ttl(schema: dict) -> str:
    """constraints → SHACL 形状（每条约束一个 sh:PropertyShape）。"""
    schema = so.fill_iris(schema)
    m = schema.get("_ns") or so.resolve_namespace(schema)
    ns, prefix = m["ns"], m["prefix"]
    ent = {e["id"]: e for e in schema.get("entities", [])}
    L = [f"# {m['name']} — SHACL 约束导出（ontology_export.py v{VERSION}）",
         "# constraints 与运行时 validate 同源派生（避免双写漂移）", ""]
    for p, u in PREFIXES:
        L.append(f"@prefix {p}: <{u}> .")
    L.append(f"@prefix {prefix}: <{ns}> .")
    L.append("")
    rows = []
    for c in schema.get("constraints", []):
        on = str(c.get("on") or "")
        if "." not in on:
            continue
        ent_id, attr = on.split(".", 1)
        if ent_id not in ent:
            continue
        prop = so._local_name(attr)
        ctype = c.get("type")
        pred, val = _SHACL_BY_TYPE.get(ctype, (None, None))
        extra = []
        if ctype == "range":
            if c.get("min") is not None:
                extra.append(f"sh:minInclusive {c['min']}")
            if c.get("max") is not None:
                extra.append(f"sh:maxInclusive {c['max']}")
        elif ctype == "in":
            vals = " ".join(f'"{_esc(v)}"' for v in (c.get("values") or []))
            extra.append(f"sh:in ( {vals} )")
        elif ctype == "pattern":
            extra.append(f'sh:pattern "{_esc(c.get("regex", ""))}"')
        elif ctype == "datatype":
            extra.append(f"sh:datatype {_xsd(c.get('value_type'))}")
        if not pred and not extra:
            continue
        shp = f"sh_{ent_id}_{prop}_{ctype}"
        # 注意: 行内不带分号, 由 join 统一补 —— 否则出现 "a sh:PropertyShape ; ;"
        lines = [f"{prefix}:{shp} a sh:PropertyShape",
                 f"    sh:path {prefix}:{prop}"]
        if pred:
            lines.append(f"    {pred} {val}")
        lines += [f"    {x}" for x in extra]
        if c.get("msg"):
            lines.append(f'    sh:message "{_esc(c["msg"])}"@zh')
        L.append(" ;\n".join(lines) + " .")
        L.append("")
        rows.append((ent_id, shp))
    # 节点形状：把属性形状挂到实体类上
    by_ent = {}
    for eid, shp in rows:
        by_ent.setdefault(eid, []).append(shp)
    for eid, shps in sorted(by_ent.items()):
        cls = _local(ent[eid]["iri"], ns)
        L.append(f"{prefix}:{eid}Shape a sh:NodeShape ;")
        L.append(f"    sh:targetClass {prefix}:{cls} ;")
        L.append("    sh:property " + " ,\n        ".join(f"{prefix}:{s}" for s in shps) + " .")
        L.append("")
    return "\n".join(L)


def build_jsonld(schema: dict) -> str:
    """JSON-LD（@context 复用 RDF 词汇，供互操作聚合）。"""
    schema = so.fill_iris(schema)
    m = schema.get("_ns") or so.resolve_namespace(schema)
    ns = m["ns"]
    ctx = {"@vocab": ns, "owl": "http://www.w3.org/2002/07/owl#",
           "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
           "skos": "http://www.w3.org/2004/02/skos/core#",
           "xsd": "http://www.w3.org/2001/XMLSchema#",
           "label": "rdfs:label", "definition": "skos:definition",
           "subClassOf": "rdfs:subClassOf", "domain": "rdfs:domain",
           "range": "rdfs:range", "owl:versionIRI": "http://www.w3.org/2002/07/owl#versionIRI"}
    graph = [{"@id": ns, "@type": "owl:Ontology",
              "label": m["label"], "owl:versionIRI": {"@id": m["version_iri"]}}]
    for e in schema.get("entities", []):
        node = {"@id": e["iri"], "@type": "owl:Class", "label": e.get("label") or e["id"]}
        if e.get("definition"):
            node["definition"] = e["definition"]
        if e.get("parent"):
            node["subClassOf"] = {"@id": ns + so._pascal(e["parent"])}
        graph.append(node)
    # 根类（被 parent 引用但自身不是实体）也须入图 —— 否则 JSON-LD 与 Turtle 不等价(少一个类)
    declared = {e.get("iri") for e in schema.get("entities", [])}
    for puri in sorted({ns + so._pascal(e["parent"]) for e in schema.get("entities", []) if e.get("parent")}):
        if puri not in declared:
            graph.append({"@id": puri, "@type": "owl:Class", "label": puri.rsplit("#", 1)[-1]})
    # 属性节点（数据属性 + 对象属性）—— JSON-LD 需与 Turtle 等价，否则往返会丢属性
    for e in schema.get("entities", []):
        key = e.get("key")
        rel_cols = {r["fk"].split(".")[1] for r in schema.get("relations", [])
                    if (r.get("fk") or "").startswith(str(e.get("table", "")) + ".")}
        for a in sorted(e.get("attributes", []), key=lambda x: x["name"]):
            if a["name"] == key or a["name"] in rel_cols:
                continue
            node = {"@id": a.get("iri") or (ns + so._local_name(a["name"])),
                    "@type": "owl:DatatypeProperty",
                    "label": a.get("label") or a["name"],
                    "domain": {"@id": e["iri"]},
                    "range": {"@id": a["range"] if (a.get("range") and str(a["range"]).startswith("http"))
                              else "http://www.w3.org/2001/XMLSchema#" + so._XSD_BY_TYPE.get(a.get("type"), "string")}}
            if a.get("definition"):
                node["definition"] = a["definition"]
            graph.append(node)
    for r in sorted(schema.get("relations", []), key=lambda x: str(x.get("id"))):
        node = {"@id": r.get("iri") or (ns + str(r["id"])),
                "@type": "owl:ObjectProperty",
                "label": r.get("label", "关联")}
        if r.get("from"):
            node["domain"] = {"@id": ns + so._pascal(str(r["from"]))}
        if r.get("to"):
            node["range"] = {"@id": ns + so._pascal(str(r["to"]))}
        if r.get("definition"):
            node["definition"] = r["definition"]
        graph.append(node)
    return json.dumps({"@context": ctx, "@graph": graph}, ensure_ascii=False, indent=2, sort_keys=True)


def _shorten(line: str, ns: str, prefix: str) -> str:
    """N-Triples 行 → 缩略 Turtle 行。

    N-Triples 行本身即合法 Turtle(`<s> <p> <o> .`)，只需把本命名空间的 `<ns内名>` 缩略成 `prefix:内名`
    （连闭合 `>` 一起去掉 —— 早期版本只替前缀，留下了 `fac:X>` 这种坏尾巴）。
    """
    return re.sub(rf"<{re.escape(ns)}([^>]*)>", rf"{prefix}:\1", line).rstrip()


def build_full_ttl(schema: dict, data: dict, prefix: str = "fac") -> str:
    """术语层 + 实例层(A-Box) 的完整 Turtle。

    实例部分直接调用 schema_ontology.to_nt —— 命名/属性/关系规则与运行时图谱同源，
    避免导出器另写一套导致两边漂移。
    """
    schema = so.fill_iris(schema)
    ns = (schema.get("_ns") or so.resolve_namespace(schema))["ns"]
    lines = so.to_nt(data, json.loads(json.dumps(schema)))
    head = [f"# {schema.get('name', 'ontology')} — 完整导出（术语层 + 实例层）",
            f"# 源: schema {schema.get('name', '')} v{schema.get('version', '')}"]
    head += [f"@prefix {p}: <{u}> ." for p, u in PREFIXES]
    head.append(f"@prefix {prefix}: <{ns}> .")
    body = [_shorten(x, ns, prefix) for x in lines]
    return "\n".join(head + [""] + body)


def export(schema_path: str, outdir: str, data_dir: str = None) -> dict:
    """执行导出，返回产物路径。data_dir 非空则额外产出含实例层的 ontology-full.ttl。"""
    schema = so.load_schema(schema_path)
    os.makedirs(outdir, exist_ok=True)
    outs = {}
    for fname, builder in (("ontology.ttl", build_ontology_ttl),
                           ("shapes.ttl", build_shapes_ttl),
                           ("ontology.jsonld", build_jsonld)):
        p = os.path.join(outdir, fname)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(builder(json.loads(json.dumps(schema))) + "\n")
        outs[fname] = p
    if data_dir:
        data = so.load_all(data_dir)
        p = os.path.join(outdir, "ontology-full.ttl")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(build_full_ttl(schema, data) + "\n")
        outs["ontology-full.ttl"] = p
    return outs


def main():
    ap = argparse.ArgumentParser(description="本体合规导出（OWL/SHACL/JSON-LD，纯标准库）")
    ap.add_argument("--schema", required=True, help="ontology schema JSON 路径")
    ap.add_argument("--outdir", default="export", help="输出目录")
    ap.add_argument("--data", default=None, help="数据目录(给出则额外产出含实例层的 ontology-full.ttl)")
    a = ap.parse_args()
    outs = export(a.schema, a.outdir, a.data)
    for k, v in outs.items():
        print(f"  {k:16s} {os.path.getsize(v):>7d} B  {v}")


if __name__ == "__main__":
    main()
