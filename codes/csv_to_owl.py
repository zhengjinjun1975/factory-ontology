#!/usr/bin/env python3
"""csv_to_owl.py — 零依赖：把 CSV 结构映射成 OWL/N-Triples 本体。

核心思想（数据驱动本体构建）：
1. 表名 -> 顶层类 (owl:Class)
2. 每条记录 -> 一个实例 (owl:NamedIndividual)
3. 每列 -> 数据属性 (owl:DatatypeProperty)，类型由值推断
4. 可选：--relations 声明的列为对象属性 (owl:ObjectProperty)，指向目标实体
5. 输出标准 N-Triples 单行格式

用法:
  python csv_to_owl.py <input.csv> <output.nt>               # 纯数据属性（兼容原用法）
  python csv_to_owl.py <input.csv> <output.nt> --relations <relations.json>  # 带对象属性
"""
import sys
import json
from datetime import datetime

from data_loader import load_table

NS = "http://factory.example/ontology#"
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
OWL_CLASS = "<http://www.w3.org/2002/07/owl#Class>"
OWL_OBJPROP = "<http://www.w3.org/2002/07/owl#ObjectProperty>"
OWL_DATAPROP = "<http://www.w3.org/2002/07/owl#DatatypeProperty>"
OWL_INDIV = "<http://www.w3.org/2002/07/owl#NamedIndividual>"
RDFS_DOMAIN = "<http://www.w3.org/2000/01/rdf-schema#domain>"
RDFS_RANGE = "<http://www.w3.org/2000/01/rdf-schema#range>"
RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"


def guess_type(value):
    """从实际值推断 xsd 类型（数据驱动）。"""
    v = value.strip()
    if v == "":
        return "xsd:string"
    try:
        int(v)
        return "xsd:integer"
    except ValueError:
        pass
    try:
        float(v)
        return "xsd:decimal"
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
    """列名 -> 局部名（去下划线，首词小写后续驼峰）。"""
    parts = [p for p in col.replace("-", "_").split("_") if p]
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def q(v):
    """转义并加引号。"""
    return '"%s"' % v.replace("\\", "\\\\").replace('"', '\\"')


def _uri(x):
    """包装成 <...> URI，若已是 <...> 则原样。"""
    x = x.strip()
    return x if x.startswith("<") and x.endswith(">") else f"<{x}>"


def load_relations(relations_path):
    with open(relations_path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_nt(inpath, outpath, relations=None):
    """核心转换逻辑。relations: {表名:{object_properties:{列:{rel,target_class,label}}}}"""
    table, headers, rows = load_table(inpath)

    if not rows:
        print("空表，无本体可建")
        return
    cls = table.capitalize()
    cls_uri = NS + cls

    # 对象属性配置（当前表）
    obj_props = {}
    if relations and table in relations:
        obj_props = relations[table].get("object_properties", {})

    # 推断每列类型（跳过对象属性列）
    prop_types = {}
    for h in headers:
        if h in obj_props:
            continue
        vals = [r[h] for r in rows if h in r and r[h].strip()]
        prop_types[h] = guess_type(vals[0]) if vals else "xsd:string"

    L = []

    # 类声明
    L.append(f"<{cls_uri}> {RDF_TYPE} {OWL_CLASS} .")
    L.append(f"<{cls_uri}> {RDFS_LABEL} {q(cls)} .")

    # 属性声明（数据 + 对象）
    for h in headers:
        if h.lower() == "id":
            continue
        if h in obj_props:
            cfg = obj_props[h]
            rel = cfg.get("rel", NS + local_name(h))
            p_uri = _uri(rel)
            t_cls = cfg["target_class"]
            L.append(f"{p_uri} {RDF_TYPE} {OWL_OBJPROP} .")
            L.append(f"{p_uri} {RDFS_DOMAIN} <{cls_uri}> .")
            L.append(f"{p_uri} {RDFS_RANGE} <{NS}{t_cls}> .")
            L.append(f"{p_uri} {RDFS_LABEL} {q(cfg.get('label', h))} .")
            # 目标类声明
            L.append(f"<{NS}{t_cls}> {RDF_TYPE} {OWL_CLASS} .")
            L.append(f"<{NS}{t_cls}> {RDFS_LABEL} {q(t_cls)} .")
        else:
            p = local_name(h)
            p_uri = NS + p
            L.append(f"<{p_uri}> {RDF_TYPE} {OWL_DATAPROP} .")
            L.append(f"<{p_uri}> {RDFS_DOMAIN} <{cls_uri}> .")
            L.append(f"<{p_uri}> {RDFS_RANGE} <http://www.w3.org/2001/XMLSchema#{prop_types[h].split(':')[1]}> .")

    # 实例声明
    for i, row in enumerate(rows):
        inst_id = row.get("id") or f"{i+1}"
        inst_uri = f"{cls_uri}_{inst_id}"
        L.append(f"<{inst_uri}> {RDF_TYPE} <{cls_uri}> .")
        for h in headers:
            if h.lower() == "id" or h not in row or not row[h].strip():
                continue
            if h in obj_props:
                cfg = obj_props[h]
                rel = _uri(cfg.get("rel", NS + local_name(h)))
                t_cls = cfg["target_class"]
                val = row[h].strip()
                target_uri = f"{NS}{t_cls}_{val}"
                # 对象属性三元组 + 目标实体实例声明（去重）
                L.append(f"<{inst_uri}> {rel} <{target_uri}> .")
                L.append(f"<{target_uri}> {RDF_TYPE} {OWL_INDIV} .")
                L.append(f"<{target_uri}> {RDF_TYPE} <{NS}{t_cls}> .")
            else:
                p = local_name(h)
                val = row[h].strip()
                t = prop_types[h]
                L.append(f"<{inst_uri}> <{NS}{p}> {q(val)}^^<http://www.w3.org/2001/XMLSchema#{t.split(':')[1]}> .")

    with open(outpath, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")

    print(f"✅ 本体已生成: {outpath} ({len(L)} 行 N-Triples)")
    print(f"   类: {cls} ({len(rows)} 实例)")
    print(f"   数据属性: {len([h for h in headers if h.lower() != 'id' and h not in obj_props])} 个")
    print(f"   对象属性: {len(obj_props)} 个")


def main():
    args = sys.argv[1:]
    # 解析 --relations
    relations_path = None
    if "--relations" in args:
        i = args.index("--relations")
        relations_path = args[i + 1]
        args = args[:i] + args[i + 2:]
    if len(args) != 2:
        print("用法: python csv_to_owl.py <input.csv> <output.nt> [--relations <relations.json>]")
        sys.exit(1)
    inpath, outpath = args[0], args[1]
    relations = load_relations(relations_path) if relations_path else None
    build_nt(inpath, outpath, relations)


if __name__ == "__main__":
    main()
