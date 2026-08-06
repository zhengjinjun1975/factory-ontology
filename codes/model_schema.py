#!/usr/bin/env python3
"""model_schema.py — 从本体 .nt 提取模型结构，供前端画本体结构图(SVG)。

输出 JSON:
{
  "class": "Equipment",               # 主类
  "instance_count": 10,               # 主类实例数
  "object_properties": [              # 对象属性（实体关系）
    {"rel": "locatedIn", "label": "位于", "target": "Location", "count": 10}, ...
  ],
  "data_properties": [                # 数据属性（主类属性）
    {"prop": "deviceId", "label": "设备ID"}, ...
  ],
  "target_classes": ["DeviceType", "Location", "Line", "Manufacturer"]
}

用法: python model_schema.py <ont.nt> [lexicon.json]
"""
import sys
import os
import re
import json


def parse_nt(path):
    triples = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("@"):
                continue
            m = re.match(r'^<([^>]*)>\s+<([^>]*)>\s+(.+?)\s*\.\s*$', line)
            if not m:
                continue
            triples.append((m.group(1), m.group(2), m.group(3).strip()))
    return triples


def tail(u):
    u = u.strip()
    if u.startswith("<"):
        u = u[1:-1]
    return u.split("#")[-1].split("/")[-1]


def main():
    if len(sys.argv) < 2:
        print("用法: python model_schema.py <ont.nt> [lexicon.json]")
        sys.exit(1)
    nt = sys.argv[1]
    lex_path = sys.argv[2] if len(sys.argv) > 2 else None

    triples = parse_nt(nt)
    # 词典（用于中文label）
    D = {}
    if lex_path and os.path.exists(lex_path):
        try:
            D = json.load(open(lex_path, encoding="utf-8"))
        except Exception:
            D = {}

    # 类：rdf:type owl:Class 且不是 owl#Class 本身
    classes = sorted(set(
        tail(s) for s, p, o in triples
        if tail(p) == "type" and tail(o) == "Class" and tail(s) != "Class"
    ))

    # 对象属性声明
    obj_rel = []
    for s, p, o in triples:
        if tail(p) == "type" and tail(o) == "ObjectProperty":
            rel = tail(s)
            # 找 range（target class）和 label
            target = ""
            label = rel
            for s2, p2, o2 in triples:
                if tail(s2) == rel and tail(p2) == "range":
                    target = tail(o2)
                if tail(s2) == rel and tail(p2) == "label":
                    label = o2.strip('"')
            obj_rel.append({"rel": rel, "label": label, "target": target})

    # 数据属性（domain = 主类的属性）
    # 主类 = 被最多 DatatypeProperty 作为 domain 的类
    domain_cnt = {}
    for s, p, o in triples:
        if tail(p) == "domain":
            domain_cnt.setdefault(tail(o), 0)
    # 统计每个类的数据属性
    cls_dp = {}
    for s, p, o in triples:
        if tail(p) == "type" and tail(o) == "DatatypeProperty":
            prop = tail(s)
            for s2, p2, o2 in triples:
                if tail(s2) == prop and tail(p2) == "domain":
                    cls_dp.setdefault(tail(o2), 0)
                    cls_dp[tail(o2)] += 1
    # 主类 = 有最多数据属性的类（排除 owl 命名空间）
    valid = {c: n for c, n in cls_dp.items() if c != "Class"}
    main_cls = max(valid, key=valid.get) if valid else (classes[0] if classes else "")

    data_props = []
    for s, p, o in triples:
        if tail(p) == "type" and tail(o) == "DatatypeProperty":
            prop = tail(s)
            is_main = any(tail(s2) == prop and tail(p2) == "domain" and tail(o2) == main_cls
                          for s2, p2, o2 in triples)
            if is_main:
                # 中文名：优先驼峰匹配词典 attr_en2cn
                cn = D.get("attr_en2cn", {}).get(prop)
                if not cn:
                    # 尝试驼峰→下划线匹配
                    snake = re.sub(r'([A-Z])', r'_\1', prop).lower().lstrip('_')
                    cn = D.get("attr_en2cn", {}).get(snake, prop)
                data_props.append({"prop": prop, "label": cn})

    # 主类实例数
    inst_count = sum(1 for s, p, o in triples
                     if tail(p) == "type" and tail(o) == main_cls and tail(o) != "Class")

    # 对象关系实例数
    for op in obj_rel:
        op["count"] = sum(1 for s, p, o in triples if tail(p) == op["rel"])

    # 目标类（对象属性的 target 集合）
    target_classes = sorted(set(op["target"] for op in obj_rel if op["target"]))

    # 类型父子类（subClassOf 层级）
    type_hierarchy = []
    for s, p, o in triples:
        if tail(p) == "subClassOf":
            child = tail(s)
            parent = tail(o)
            type_hierarchy.append({"child": child, "parent": parent})

    # 各关联实体的属性（属性本体：Line/Manufacturer/Sensor/Maintenance/Zone 实例的属性）
    # 对每个目标类，找其实例的属性（属性名按前缀分组）
    class_attributes = {}
    for cls in target_classes:
        # 找该类的实例
        insts = set(s for s, p, o in triples
                    if tail(p) == "type" and tail(o) == cls)
        attrs = set()
        for s, p, o in triples:
            if s in insts and tail(p) not in ("type", "label"):
                attrs.add(tail(p))
        if attrs:
            class_attributes[cls] = sorted(attrs)

    result = {
        "class": main_cls,
        "instance_count": inst_count,
        "object_properties": obj_rel,
        "data_properties": data_props,
        "target_classes": target_classes,
        "type_hierarchy": type_hierarchy,
        "class_attributes": class_attributes,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
