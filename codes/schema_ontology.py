#!/usr/bin/env python3
"""schema_ontology.py — 工厂本体 schema 驱动建模（移植 sme-decision-ontology 精髓）

在现有 N-Triples 自动建模之外，新增一条**企业级 schema 驱动**建模路径：
显式声明实体/属性/关系/约束 → 跨表建统一实例图 → 约束校验 → 跨域图遍历。

融合 sme-decision-ontology `core/ontology.py` 的本体重构精髓：
1. schema 驱动（ontology.json 显式声明，非纯 CSV 推断）
2. 属性语义角色（Palantir Property: identifier/reference/measure/category/timestamp/text）
3. 类型体系（Enterprise → BusinessObject → 业务域类 → 实体）
4. 跨表 join 建统一实例图（build_graph，双向 FK）
5. 约束校验（validate: unique/required/positive）
6. 跨域图遍历（traverse）

与现有 N-Triples 兼容并存：本模块产出 Python dict 图（内存），不替代 csv_to_owl/multi_table 的 RDF 输出。
可独立使用，也可作为 run.py 的可选增强建模层。

用法:
  from schema_ontology import load_schema, build_graph, validate, traverse, build_ontology_model
  schema = load_schema("config/ontology_schema.json")
  data = load_all(data_dir)               # {表名: [行...]}
  graph = build_graph(data, schema)
  issues = validate(data, schema)
  model = build_ontology_model(data, schema)
"""
import os
import json

# ═══════════ 数据加载（复用 factory data_loader 接口）═══════════
def load_all(data_dir: str) -> dict:
    """加载数据目录下所有 CSV/JSON/SQLite/Excel 表 → {表名: [行...]}（动态发现）。"""
    from data_loader import load_table
    data = {}
    for f in sorted(os.listdir(data_dir)):
        if not f.startswith(".") and os.path.splitext(f)[1].lower() in (".csv", ".json", ".db", ".sqlite", ".sqlite3", ".xlsx", ".xls"):
            name, _headers, rows = load_table(os.path.join(data_dir, f))
            if rows:
                data[name] = rows
    return data


# ═══════════ schema 加载与校验 ═══════════
def load_schema(path: str) -> dict:
    """加载 + 校验本体 schema（实体/关系/约束合法性）。"""
    schema = json.load(open(path, encoding="utf-8"))
    entities = {e["id"]: e for e in schema.get("entities", [])}
    # 实体 id 唯一
    assert len(entities) == len(schema.get("entities", [])), f"实体 id 重复: {path}"
    # 关系 from/to 必须存在
    for r in schema.get("relations", []):
        assert r["from"] in entities, f"关系 {r['id']} 的 from={r['from']} 不存在"
        assert r["to"] in entities, f"关系 {r['id']} 的 to={r['to']} 不存在"
    schema["_entities"] = entities
    return schema


# ═══════════ 属性语义角色（Palantir Property）═══════════
_REF_HINTS = ("_id", "_code", "_no", "id")
_MEASURE_HINTS = ("qty", "amount", "price", "cost", "stock", "pct", "days", "months", "age", "limit", "rank", "rate", "num", "weight", "size", "power", "kw")
_DATE_HINTS = ("date", "time", "day", "install", "create", "timestamp")
_CATEGORY_HINTS = ("category", "type", "status", "state", "kind", "flag", "grade", "level")


def _infer_prop_role(col: str, ptype: str) -> str:
    """属性语义角色分类(identifier/reference/measure/category/timestamp/text)。

    顺序关键: reference(*_id/*_code) 优先于 identifier(恰好是 id)——
    product_id 是引用列不是主键, 只有严格等于 'id' 才是 identifier。
    """
    low = col.lower()
    if col.endswith("_id") or col.endswith("_code"):
        return "reference"
    if low == "id":
        return "identifier"
    if ptype == "number" or any(h in low for h in _MEASURE_HINTS):
        return "measure"
    if ptype == "date" or any(h in low for h in _CATEGORY_HINTS):
        return "category"
    if any(h in low for h in _DATE_HINTS):
        return "timestamp"
    return "text"


def classify_properties(schema: dict) -> dict:
    """为每个实体的属性标注语义角色。"""
    for e in schema.get("entities", []):
        for a in e.get("attributes", []):
            a["role"] = _infer_prop_role(a["name"], a.get("type", "string"))
        for a in e.get("attributes", []):
            if a["name"] == e.get("key"):
                a["role"] = "identifier"
    return schema


# ═══════════ 类型体系（Type hierarchy）═══════════
def build_class_hierarchy(schema: dict) -> list:
    """类型体系：Enterprise → BusinessObject → 业务域类 → 实体（Is-A）。"""
    hierarchy = [
        {"name": "Enterprise", "super": None, "label": "企业"},
        {"name": "BusinessObject", "super": "Enterprise", "label": "业务对象"},
    ]
    domains = {}
    for e in schema.get("entities", []):
        d = e.get("domain", "其他域")
        domains.setdefault(d, {"name": d, "super": "BusinessObject", "label": d, "children": []})
        domains[d]["children"].append(e["id"])
    for d in domains.values():
        hierarchy.append({"name": d["name"], "super": "BusinessObject", "label": d["label"], "entities": d["children"]})
    for e in schema.get("entities", []):
        if "category" in [a["name"] for a in e.get("attributes", [])]:
            hierarchy.append({"name": f"{e['id']}Category", "super": e["id"], "label": f"{e['label']}类别", "kind": "is_a"})
    return hierarchy


def enrich_links(schema: dict) -> dict:
    """链接类型：关系补 inverse 反向标签 + kind(data/derived)。"""
    for r in schema.get("relations", []):
        r["kind"] = "derived" if r.get("abstract") else "data"
        r["inverse"] = {"Product→Supplier": "供应商提供产品", "Product→Inventory": "产品库存",
                        "Product→Sale": "产品销售", "Sale→Customer": "客户购买",
                        "Purchase→Supplier": "供应商供货", "Purchase→Product": "产品被采购",
                        "Production→Product": "产品被生产", "Production→Equipment": "设备被使用",
                        "Payment→Customer": "客户付款"}.get(f"{r['from']}→{r['to']}", "关联")
    return schema


def build_ontology_model(data: dict, schema: dict) -> dict:
    """构建 Palantir 风格企业本体模型：对象类型 + 链接类型 + 类型体系 + 语义域。"""
    schema = classify_properties(schema)
    schema = enrich_links(schema)
    return {
        "object_types": schema["entities"],
        "link_types": schema["relations"],
        "type_hierarchy": build_class_hierarchy(schema),
        "semantic_domains": sorted({e.get("domain", "其他域") for e in schema["entities"]}),
        "instance_counts": {e["id"]: len(data.get(e.get("table", ""), [])) for e in schema["entities"]},
    }


# ═══════════ 外键推断 + 跨表建图 ═══════════
def _infer_relations(data: dict) -> list:
    """外键推断（自动）：`*_id`/`*_code` 列指向另一实体主键 → 关系。"""
    inferred = []
    for table, rows in data.items():
        if not rows:
            continue
        sample = rows[0]
        for col in sample:
            if col.endswith("_id") or col.endswith("_code") or col.endswith("_key"):
                target = col.replace("_id", "").replace("_code", "").replace("_key", "")
                for tname in data:
                    if target.lower() in tname.lower():
                        inferred.append({"id": f"auto_{table}_{col}", "from": _cap(table),
                                         "to": _cap(tname), "fk": f"{table}.{col}", "cardinality": "N:1", "label": "关联", "auto": True})
    return inferred


def _cap(name: str) -> str:
    return name[0].upper() + name[1:] if name else name


def build_graph(data: dict, schema: dict) -> dict:
    """跨表跨域建统一实例图：实体实例 + 关系边（FK join）。"""
    graph = {"nodes": {}, "edges": []}
    entities = schema.get("_entities", {})
    declared_fks = {r.get("fk") for r in schema.get("relations", []) if r.get("fk")}
    inferred = [r for r in _infer_relations(data) if r.get("fk") not in declared_fks]
    relations = list(schema.get("relations", [])) + inferred
    node_ids = {}
    for eid, ent in entities.items():
        table = ent["table"]
        if table not in data:
            continue
        key = ent["key"]
        detail = ent.get("detail", False)
        for i, row in enumerate(data[table]):
            kid = row.get(key)
            node_id = f"{eid}:{kid}@{i}" if detail else f"{eid}:{kid}"
            graph["nodes"][node_id] = {"entity": eid, "id": kid, "idx": i, "data": row}
            node_ids.setdefault(eid, []).append(node_id)
    # 类别类层级（实体含 category → 类别节点 + isA 边）
    for eid, ent in entities.items():
        table = ent["table"]
        if table not in data or not data[table] or "category" not in data[table][0]:
            continue
        key = ent["key"]
        detail = ent.get("detail", False)
        for i, row in enumerate(data[table]):
            cat = row.get("category")
            if not cat:
                continue
            cat_id = f"Category:{eid}:{cat}"
            if cat_id not in graph["nodes"]:
                graph["nodes"][cat_id] = {"entity": "Category", "id": cat, "data": {"name": cat, "of": eid}}
            src = f"{eid}:{row.get(key)}@{i}" if detail else f"{eid}:{row.get(key)}"
            graph["edges"].append({"from": src, "to": cat_id, "rel": "isA", "label": "属于类别"})
    # 边：FK join（支持 FK 在 from 侧或 to 侧）
    for r in relations:
        if r.get("abstract") or not r.get("fk"):
            continue
        ftable, fcol = r["fk"].split(".")
        if ftable not in data:
            continue
        from_e = entities.get(r["from"], {})
        to_e = entities.get(r["to"], {})
        fk_val_to_nodes = {}
        for nd in node_ids.get(r["to"], []):
            info = graph["nodes"][nd]
            fk_val_to_nodes.setdefault(f"{info['id']}", []).append(nd)
        for i, row in enumerate(data[ftable]):
            val = row.get(fcol)
            if not val:
                continue
            # FK 值 = 对侧实体(非 fk 表所在实体)的主键引用
            # fk 在 from 表: 每行 from 实体通过自己的 fk 列连到 to 实体
            # fk 在 to 表: 每行 to 实体通过自己的 fk 列连到 from 实体
            if ftable == from_e.get("table"):
                # fk 在 from 表: src=当前 from 行, dst=to 实体(按 fk 值匹配主键)
                src = f"{r['from']}:{row.get(from_e['key'])}@{i}" if from_e.get("detail") else f"{r['from']}:{row.get(from_e['key'])}"
                for dst in fk_val_to_nodes.get(str(val), []):
                    if src in graph["nodes"]:
                        graph["edges"].append({"from": src, "to": dst, "rel": r["id"], "label": r.get("label", r["id"])})
            elif ftable == to_e.get("table"):
                # fk 在 to 表: src=from 实体(按 fk 值匹配其主键), dst=当前 to 行(用 to 表自身主键)
                src = f"{r['from']}:{val}"
                dst = f"{r['to']}:{row.get(to_e['key'])}@{i}" if to_e.get("detail") else f"{r['to']}:{row.get(to_e['key'])}"
                if src in graph["nodes"] and dst in graph["nodes"]:
                    graph["edges"].append({"from": src, "to": dst, "rel": r["id"], "label": r.get("label", r["id"])})
            else:
                # fk 表既非 from 也非 to: 退化为按 fk 值跨表匹配
                src = f"{r['from']}:{val}"
                for nd in fk_val_to_nodes.get(str(val), []):
                    if src in graph["nodes"]:
                        graph["edges"].append({"from": src, "to": nd, "rel": r["id"], "label": r.get("label", r["id"])})
    return graph


# ═══════════ 约束校验 ═══════════
def validate(data: dict, schema: dict) -> list:
    """约束校验：unique/required/positive + 基数。返回问题清单。"""
    issues = []
    entities = schema.get("_entities", {})
    for ent in entities.values():
        table, key = ent["table"], ent["key"]
        if table not in data:
            continue
        seen = set()
        for row in data[table]:
            kid = row.get(key)
            if not ent.get("detail") and kid in seen:
                issues.append({"severity": "error", "type": "unique", "msg": f"实体 {ent['id']} 主键重复: {kid}"})
            seen.add(kid)
            for attr in ent.get("attributes", []):
                v = row.get(attr["name"])
                if attr.get("required") and (v is None or str(v).strip() == ""):
                    issues.append({"severity": "error", "type": "required", "msg": f"{ent['label']}.{attr['label']} 必填缺失"})
                if attr.get("type") == "number" and v is not None:
                    try:
                        if float(v) < 0 and attr["name"] in ("stock", "price", "cost"):
                            issues.append({"severity": "warn", "type": "positive", "msg": f"{ent['label']}.{attr['label']} 为负: {v}"})
                    except (TypeError, ValueError):
                        pass
    for c in schema.get("constraints", []):
        if c.get("type") == "required":
            ent, attr = c["on"].split(".")
            if ent in entities and entities[ent]["table"] in data:
                for row in data[entities[ent]["table"]]:
                    if not row.get(attr):
                        issues.append({"severity": "error", "type": "required", "msg": c.get("msg", "必填缺失")})
                        break
    return issues


# ═══════════ 跨域图遍历 ═══════════
def traverse(graph: dict, entity: str, eid) -> list:
    """图遍历（跨域）：从某实体实例出发，经关系到达的所有相关实例。"""
    start = f"{entity}:{eid}"
    if start not in graph["nodes"]:
        return []
    result = []
    for e in graph["edges"]:
        if e["from"] == start:
            result.append({"rel": e["rel"], "label": e["label"], "to": e["to"]})
        elif e["to"] == start:
            result.append({"rel": e["rel"], "label": e["label"], "from": e["from"]})
    return result


# ═══════════ 统一 N-Triples 输出（替代 csv_to_owl / multi_table 的建本体职责）═══════════
NS = "http://factory.example/ontology#"
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
OWL_CLASS = "<http://www.w3.org/2002/07/owl#Class>"
OWL_OBJPROP = "<http://www.w3.org/2002/07/owl#ObjectProperty>"
OWL_DATAPROP = "<http://www.w3.org/2002/07/owl#DatatypeProperty>"
RDFS_DOMAIN = "<http://www.w3.org/2000/01/rdf-schema#domain>"
RDFS_RANGE = "<http://www.w3.org/2000/01/rdf-schema#range>"
RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"
RDFS_SUBCLASS = "<http://www.w3.org/2000/01/rdf-schema#subClassOf>"
_NS_URI = "http://www.w3.org/2001/XMLSchema#"

def _guess_type(value) -> str:
    """从实际值推断 xsd 类型（数据驱动）。"""
    v = str(value).strip()
    if v == "":
        return "xsd:string"
    try:
        int(v); return "xsd:integer"
    except ValueError:
        pass
    try:
        float(v); return "xsd:decimal"
    except ValueError:
        pass
    if v.lower() in ("true", "false"):
        return "xsd:boolean"
    return "xsd:string"

def _local_name(col: str) -> str:
    """列名 -> 局部名（去下划线，首词小写后续驼峰）。"""
    parts = [p for p in str(col).replace("-", "_").split("_") if p]
    return parts[0] + "".join(p.capitalize() for p in parts[1:]) if parts else "col"

def _q(v) -> str:
    return '"%s"' % str(v).replace("\\", "\\\\").replace('"', '\\"')

def _nt_class_decls(entities, eid_to_cls, L):
    """类声明：每实体一个 owl:Class + 中文 label。返回 eid->类名映射已由调用方维护。"""
    for eid, ent in entities.items():
        cls_uri = NS + eid_to_cls[eid]
        L.append(f"<{cls_uri}> {RDF_TYPE} {OWL_CLASS} .")
        L.append(f"<{cls_uri}> {RDFS_LABEL} {_q(ent.get('label') or eid_to_cls[eid])} .")


def _nt_property_decls(entities, relations, data, eid_to_cls, L):
    """数据属性 + 对象属性声明（跳过主键/外键列，对象属性用关系英文id）。"""
    for eid, ent in entities.items():
        cls_uri = NS + eid_to_cls[eid]
        table = ent.get("table")
        if table not in data or not data[table]:
            continue
        key = ent.get("key")
        rel_cols = {r["fk"].split(".")[1] for r in relations if r.get("fk", "").startswith(table + ".")}
        for attr in ent.get("attributes", []):
            aname = attr["name"]
            if aname == key or aname in rel_cols:
                continue
            p = _local_name(aname)
            vals = [r.get(aname) for r in data[table] if r.get(aname)]
            t = _guess_type(vals[0]) if vals else "xsd:string"
            L.append(f"<{NS}{p}> {RDF_TYPE} {OWL_DATAPROP} .")
            L.append(f"<{NS}{p}> {RDFS_DOMAIN} <{cls_uri}> .")
            L.append(f"<{NS}{p}> {RDFS_RANGE} <{_NS_URI}{t.split(':')[1]}> .")
        for r in relations:
            if r.get("fk", "").startswith(table + "."):
                rel = NS + r["id"]  # 对象属性 URI 用关系 id（下游消费一致）
                L.append(f"<{rel}> {RDF_TYPE} {OWL_OBJPROP} .")
                L.append(f"<{rel}> {RDFS_DOMAIN} <{cls_uri}> .")
                L.append(f"<{rel}> {RDFS_RANGE} <{NS}{eid_to_cls.get(r['to'], r['to'])}> .")
                L.append(f"<{rel}> {RDFS_LABEL} {_q(r.get('label', '关联'))} .")


def _nt_category_hierarchy(entities, data, eid_to_cls, L):
    """类别类层级 + 类型体系(subClassOf)：实体含 category 列 → Category 类 + isA。"""
    for eid, ent in entities.items():
        cls = eid_to_cls[eid]
        table = ent.get("table")
        if table not in data or not data[table] or "category" not in data[table][0]:
            continue
        cat_cls = f"{cls}Category"
        L.append(f"<{NS}{cat_cls}> {RDF_TYPE} {OWL_CLASS} .")
        L.append(f"<{NS}{cat_cls}> {RDFS_SUBCLASS} <{NS}{cls}> .")
        for row in data[table]:
            cat = row.get("category")
            if cat:
                cat_uri = f"{NS}{cat_cls}_{cat}"
                L.append(f"<{cat_uri}> {RDF_TYPE} {OWL_CLASS} .")
                L.append(f"<{cat_uri}> {RDFS_SUBCLASS} <{NS}{cat_cls}> .")


def _nt_instances(entities, relations, data, eid_to_cls, L):
    """实例 + 数据/对象属性（FK join，明细实体按行建实例）。"""
    for eid, ent in entities.items():
        cls_uri = NS + eid_to_cls[eid]
        table = ent.get("table")
        if table not in data or not data[table]:
            continue
        key = ent.get("key")
        detail = ent.get("detail", False)
        rels_of_table = [r for r in relations if r.get("fk", "").startswith(table + ".")]
        rel_cols = {r["fk"].split(".")[1] for r in rels_of_table}
        seen_ids = set()
        for i, row in enumerate(data[table]):
            kid = row.get(key) or f"{i+1}"
            if str(kid) in seen_ids:
                kid = f"{kid}_{i}"
            seen_ids.add(str(kid))
            inst_uri = f"{cls_uri}_{kid}" + (f"@{i}" if detail else "")
            L.append(f"<{inst_uri}> {RDF_TYPE} <{cls_uri}> .")
            if "category" in row and row.get("category"):
                L.append(f"<{inst_uri}> <{NS}hasCategory> <{NS}{eid_to_cls[eid]}Category_{row['category']}> .")
            for attr in ent.get("attributes", []):
                aname = attr["name"]
                if aname == key or aname in rel_cols:
                    continue
                if aname in row and row.get(aname) is not None and str(row.get(aname)).strip() != "":
                    p = _local_name(aname)
                    t = _guess_type(row[aname])
                    L.append(f"<{inst_uri}> <{NS}{p}> {_q(row[aname])}^^<{_NS_URI}{t.split(':')[1]}> .")
            for r in rels_of_table:
                fcol = r["fk"].split(".")[1]
                if fcol in row and row.get(fcol):
                    rel = NS + r["id"]
                    L.append(f"<{inst_uri}> <{rel}> <{NS}{eid_to_cls.get(r['to'], r['to'])}_{row[fcol]}> .")


def to_nt(data: dict, schema: dict, outpath: str = None) -> list:
    """把 schema 驱动建模结果输出成标准 N-Triples（统一替代 csv_to_owl/multi_table）。

    从 schema(实体/关系/约束) + data(多表) 生成：类声明、数据属性、对象属性、
    实例、类别类层级、类型体系。格式与 csv_to_owl/multi_table 对齐，下游
    ontology_qa_v3.parse_nt / graph_rag.parse_nt 无缝消费。

    outpath 非空则写文件，返回行列表。
    """
    L = []
    entities = schema.get("_entities", schema)
    declared_fks = {r.get("fk") for r in schema.get("relations", []) if r.get("fk")}
    inferred = [r for r in _infer_relations(data) if r.get("fk") not in declared_fks]
    relations = list(schema.get("relations", [])) + inferred
    # 实体 ID -> 类局部名（用表名风格，与 multi_table 下游兼容）
    eid_to_cls = {eid: (ent.get("table", eid).capitalize() if ent.get("table") else eid)
                  for eid, ent in entities.items()}
    _nt_class_decls(entities, eid_to_cls, L)
    _nt_property_decls(entities, relations, data, eid_to_cls, L)
    _nt_category_hierarchy(entities, data, eid_to_cls, L)
    _nt_instances(entities, relations, data, eid_to_cls, L)
    if outpath:
        with open(outpath, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
    return L


if __name__ == "__main__":
    import sys
    root = os.path.dirname(os.path.abspath(__file__))
    data_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "data_valve")
    schema_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(root, "config", "ontology_schema.json")
    data = load_all(data_dir)
    print(f"加载数据: {len(data)} 表 -> {list(data.keys())}")
    if os.path.exists(schema_path):
        schema = load_schema(schema_path)
        issues = validate(data, schema)
        print(f"约束校验: {len(issues)} 问题", [i["msg"] for i in issues[:5]])
        graph = build_graph(data, schema)
        print(f"本体图: {len(graph['nodes'])} 节点 / {len(graph['edges'])} 关系边")
        model = build_ontology_model(data, schema)
        print(f"类型体系: {[h['name'] for h in model['type_hierarchy']]}")
        print(f"语义域: {model['semantic_domains']}")
        # 跨域遍历示例：第一个实体类型的第一个实例
        if graph["nodes"]:
            first = next(iter(graph["nodes"].values()))
            rel = traverse(graph, first["entity"], first["id"])
            print(f"遍历 {first['entity']}:{first['id']} -> {[r['label'] for r in rel[:5]]}")
    else:
        print(f"无 schema({schema_path}), 仅加载数据。用 suggest_schema 可自动推断。")
