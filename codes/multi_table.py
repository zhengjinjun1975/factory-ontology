#!/usr/bin/env python3
"""multi_table.py — 多表自动关联建本体（零依赖，纯标准库）

从多个关联的 CSV 表自动生成统一 N-Triples 本体，无需手写 relations.json：
- 每个表 -> 一个 owl:Class
- 每行 -> 一个实例
- 普通列 -> 数据属性 (DatatypeProperty)
- 外键列（引用其他表的 id）-> 对象属性 (ObjectProperty)，自动链接跨表实例

外键自动检测规则（任一命中即判为外键）：
1. 列名 = `<目标表名>_id` 或 `<目标表名>Id`（如 line_id -> line 表）
2. 列名去掉 _id 后 == 目标表的 id 列名（如 device_id 匹配 equipment 表的 device_id）
3. 目标表的 id 列与当前列名相同

用法:
  python multi_table.py out.nt equipment.csv line.csv sensor.csv
  python multi_table.py out.nt --main equipment.csv line.csv sensor.csv maintenance.csv
"""
import os
import sys
import argparse
from datetime import datetime

NS = "http://factory.example/ontology#"
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
OWL_CLASS = "<http://www.w3.org/2002/07/owl#Class>"
OWL_OBJPROP = "<http://www.w3.org/2002/07/owl#ObjectProperty>"
OWL_DATAPROP = "<http://www.w3.org/2002/07/owl#DatatypeProperty>"
RDFS_DOMAIN = "<http://www.w3.org/2000/01/rdf-schema#domain>"
RDFS_RANGE = "<http://www.w3.org/2000/01/rdf-schema#range>"
RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"
RDFS_SUBCLASS = "<http://www.w3.org/2000/01/rdf-schema#subClassOf>"


def guess_type(v):
    v = v.strip()
    if not v:
        return "xsd:string"
    for fn, t in ((int, "xsd:integer"), (float, "xsd:decimal")):
        try:
            fn(v)
            return t
        except ValueError:
            pass
    if v.lower() in ("true", "false"):
        return "xsd:boolean"
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return "xsd:date"
    except ValueError:
        pass
    return "xsd:string"


def local_name(col):
    parts = [p for p in col.replace("-", "_").split("_") if p]
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def q(v):
    return '"%s"' % v.replace("\\", "\\\\").replace('"', '\\"')


def _uri(x):
    x = x.strip()
    return x if x.startswith("<") and x.endswith(">") else f"<{x}>"


def load_table(path):
    """统一加载表(复用 data_loader, 支持 csv/json/sqlite/xlsx)。返回 (表名, 列名, 行)"""
    from data_loader import load_table as _load
    return _load(path)


def detect_id_column(headers, table):
    cands = ["id", f"{table}_id", f"{table}_id".lower(), f"{table}Id", f"{table}_ID"]
    for c in cands:
        if c in headers:
            return c
    # 兜底: 名为 <table> 或含 id 的列, 最后第一个
    for c in headers:
        if "id" in c.lower():
            return c
    return headers[0]


def detect_relations(tables):
    """tables: {name: {headers, rows, id_col}} -> {name: {col: {target_class, rel, id_col, label}}}"""
    rels = {}
    names = list(tables.keys())
    for tname, tinfo in tables.items():
        for col in tinfo["headers"]:
            if col == tinfo["id_col"]:
                continue
            base = col[:-3] if col.endswith("_id") else col
            base_l = base.lower()
            for oname in names:
                if oname == tname:
                    continue
                oinfo = tables[oname]
                # 目标表名去常见前缀(阀/food 等领域前缀)后匹配 FK 列名
                oname_base = oname
                for pre in ("valve_", "food_", "factory_", "demo_"):
                    if oname.lower().startswith(pre):
                        oname_base = oname[len(pre):]
                        break
                # 单数化(表名复数 vs FK列单数): products -> product
                oname_sing = oname_base[:-1] if oname_base.endswith("s") else oname_base
                if (base_l == oname.lower() or base_l == oname_base.lower()
                        or base_l == oname_sing.lower() or base_l == oinfo["id_col"].lower()):
                    rels.setdefault(tname, {})[col] = {
                        "target_class": oname.capitalize(),
                        "rel": NS + f"has{oname.capitalize()}",
                        "label": f"关联{oname}",
                        "id_col": oinfo["id_col"],
                    }
                    break
    return rels


def detect_categories(tables):
    """检测类别列(type/category/class) → {表名: {列名: [类别值...]}}，供本体层次(Is-A)深入。"""
    cats = {}
    CATEGORY_HINTS = ("type", "category", "class", "kind", "等级", "类型")
    for tname, tinfo in tables.items():
        for h in tinfo["headers"]:
            low = h.lower()
            if any(k in low for k in CATEGORY_HINTS) and low not in ("datatype",):
                vals = list(dict.fromkeys(r[h].strip() for r in tinfo["rows"] if h in r and r[h].strip()))
                if vals:
                    cats.setdefault(tname, {})[h] = vals
    return cats


def build_nt(tables, rels, outpath, categories=None):
    L = []
    # 类 + 属性声明
    categories = categories or {}
    cat_cols = {}  # (表名,列) -> [值]
    for tname, cinfo in categories.items():
        for h, vals in cinfo.items():
            cat_cols[(tname, h)] = vals
    for tname, tinfo in tables.items():
        cls = tname.capitalize()
        cls_uri = NS + cls
        L.append(f"<{cls_uri}> {RDF_TYPE} {OWL_CLASS} .")
        L.append(f"<{cls_uri}> {RDFS_LABEL} {q(cls)} .")
        # 类别类层级(Is-A): <表名>Category_值 subClassOf <表名>
        for (tn, h), vals in cat_cols.items():
            if tn != tname:
                continue
            cat_cls = f"{cls}Category"
            p_uri = NS + f"has{local_name(h).capitalize()}"
            L.append(f"<{NS}{cat_cls}> {RDF_TYPE} {OWL_CLASS} .")
            L.append(f"<{NS}{cat_cls}> {RDFS_LABEL} {q(f'{cls}类别')} .")
            L.append(f"{p_uri} {RDF_TYPE} {OWL_OBJPROP} .")
            L.append(f"{p_uri} {RDFS_DOMAIN} <{cls_uri}> .")
            L.append(f"{p_uri} {RDFS_RANGE} <{NS}{cat_cls}> .")
            L.append(f"{p_uri} {RDFS_LABEL} {q('所属类别')} .")
            for v in vals:
                cat_uri = f"{NS}{cat_cls}_{v}"
                L.append(f"<{cat_uri}> {RDF_TYPE} {OWL_CLASS} .")
                L.append(f"<{cat_uri}> {RDFS_LABEL} {q(v)} .")
                L.append(f"<{cat_uri}> {RDFS_SUBCLASS} <{cls_uri}> .")
        t_rels = rels.get(tname, {})
        for h in tinfo["headers"]:
            if h == tinfo["id_col"] or h in t_rels:
                continue
            p = local_name(h)
            L.append(f"<{NS}{p}> {RDF_TYPE} {OWL_DATAPROP} .")
            L.append(f"<{NS}{p}> {RDFS_DOMAIN} <{cls_uri}> .")
        for h, cfg in t_rels.items():
            p = _uri(cfg["rel"])
            L.append(f"{p} {RDF_TYPE} {OWL_OBJPROP} .")
            L.append(f"{p} {RDFS_DOMAIN} <{cls_uri}> .")
            L.append(f"{p} {RDFS_RANGE} <{NS}{cfg['target_class']}> .")
            L.append(f"{p} {RDFS_LABEL} {q(cfg['label'])} .")

    # 实例 + 数据/对象属性
    for tname, tinfo in tables.items():
        cls = tname.capitalize()
        cls_uri = NS + cls
        id_col = tinfo["id_col"]
        t_rels = rels.get(tname, {})
        prop_types = {}
        for h in tinfo["headers"]:
            if h == id_col or h in t_rels:
                continue
            vals = [r[h] for r in tinfo["rows"] if h in r and r[h].strip()]
            prop_types[h] = guess_type(vals[0]) if vals else "xsd:string"
        seen_ids = set()
        for i, row in enumerate(tinfo["rows"]):
            inst_id = row.get(id_col) or f"{i+1}"
            # join 表 id 列非唯一时去重(追加行号), 避免 URI 碰撞
            if inst_id in seen_ids:
                inst_id = f"{inst_id}_{i}"
            seen_ids.add(inst_id)
            inst_uri = f"{cls_uri}_{inst_id}"
            L.append(f"<{inst_uri}> {RDF_TYPE} <{cls_uri}> .")
            # 类别链接(Is-A 实例→类别类)
            for (tn, h), vals in cat_cols.items():
                if tn == tname and h in row and row[h].strip():
                    cat_cls = f"{cls}Category"
                    cat_uri = f"{NS}{cat_cls}_{row[h].strip()}"
                    p_uri = f"<{NS}has{local_name(h).capitalize()}>"
                    L.append(f"<{inst_uri}> {p_uri} <{cat_uri}> .")
            for h in tinfo["headers"]:
                # id 列仅作标识时跳过; 若 id 列同时在 relations 里则作为对象属性发出
                if (h == id_col and h not in t_rels) or h not in row or not row[h].strip():
                    continue
                if h in t_rels:
                    cfg = t_rels[h]
                    target_uri = f"<{NS}{cfg['target_class']}_{row[h].strip()}>"
                    L.append(f"<{inst_uri}> {_uri(cfg['rel'])} {target_uri} .")
                else:
                    p = local_name(h)
                    t = prop_types[h]
                    L.append(f"<{inst_uri}> <{NS}{p}> {q(row[h].strip())}^^<http://www.w3.org/2001/XMLSchema#{t.split(':')[1]}> .")

    with open(outpath, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

    n_rel = sum(len(v) for v in rels.values())
    print(f"✅ 多表本体已生成: {outpath} ({len(L)} 行)")
    print(f"   表: {', '.join(tables.keys())} | 总实例: {sum(len(t['rows']) for t in tables.values())}")
    print(f"   跨表对象属性(自动检测): {n_rel} 个")


def main():
    ap = argparse.ArgumentParser(description="多表自动关联建本体")
    ap.add_argument("outpath", help="输出 N-Triples 路径")
    ap.add_argument("csvs", nargs="+", help="多个 CSV 表(第一个为主表)")
    ap.add_argument("--main", action="store_true", help="csvs 中首个为其余表引用的主表")
    args = ap.parse_args()

    tables = {}
    for p in args.csvs:
        if not os.path.exists(p):
            print(f"❌ 表不存在: {p}")
            sys.exit(1)
        name, headers, rows = load_table(p)
        tables[name] = {"headers": headers, "rows": rows,
                        "id_col": detect_id_column(headers, name)}

    rels = detect_relations(tables)
    cats = detect_categories(tables)
    build_nt(tables, rels, args.outpath, categories=cats)


if __name__ == "__main__":
    main()
