#!/usr/bin/env python3
"""ontology_schema_info.py — 从本体 .nt 提取模型结构信息（替代已删的 model_schema.py）

供前端 ModelGraph 画结构图。纯标准库，解析 N-Triples 提取：
- 主类(class) + 实例数(instance_count)
- 数据属性(data_properties) + 对象属性(object_properties, 含 rel/target)
- 目标类(target_classes) + 类型层级(type_hierarchy) + 每类属性(class_attributes)

用法: python ontology_schema_info.py <nt文件> [lexicon.json]
"""
import sys, os, json
from collections import defaultdict


def parse_nt(nt_file):
    """解析 N-Triples → [(s,p,o)]。"""
    from ontology_qa_v3 import parse_nt as _p
    return _p(nt_file)


def extract(nt_file):
    """从 .nt 提取模型结构。返回 dict（前端 ModelGraph 消费的格式）。"""
    triples = parse_nt(nt_file)
    # 收集类、实例、属性
    classes = set()        # 类 URI
    obj_props = set()      # 对象属性 URI (rel)
    data_props = set()     # 数据属性 URI
    inst_by_cls = defaultdict(int)
    prop_domains = defaultdict(set)   # 属性 -> 主类
    obj_targets = {}       # 对象属性 -> 目标类
    cls_labels = {}        # 类 -> label
    for s, p, o in triples:
        s_ = str(s).strip("<>")
        o_ = str(o).strip("<>")
        p_ = str(p)
        tail_s = s_.split("#")[-1].split("/")[-1] if "#" in s_ else s_.split("/")[-1]
        tail_o = o_.split("#")[-1].split("/")[-1] if "#" in o_ else o_.split("/")[-1]
        # 类声明 (排除 DatatypeProperty/ObjectProperty/Class 等元类)
        if "owl#Class" in o_ and "type" in p_ and tail_o in ("Class",):
            # 只有当 o 是 owl#Class 本体声明(不是 DatatypeProperty 等)才记为主类
            if "ObjectProperty" not in s_ and "DatatypeProperty" not in s_:
                classes.add(s_)
        # label
        elif "rdf-schema#label" in p_ and s_ in classes:
            cls_labels[s_] = str(o).strip('"')
        # 实例 (s 是实体, o 是类; 排除 DatatypeProperty/ObjectProperty/Class 元类)
        elif "rdf-syntax-ns#type" in p_ and s_ not in classes and tail_o not in ("Class", "DatatypeProperty", "ObjectProperty"):
            inst_by_cls[o_] += 1
        # 数据属性声明 (domain/range)
        elif "owl#DatatypeProperty" in o_ and "type" in p_:
            data_props.add(s_)
        elif "rdf-schema#domain" in p_ and s_ in data_props:
            prop_domains[s_].add(tail_o)
        # 对象属性声明
        elif "owl#ObjectProperty" in o_ and "type" in p_:
            obj_props.add(s_)
        elif "rdf-schema#range" in p_ and s_ in obj_props:
            obj_targets[s_] = tail_o
        elif "rdf-schema#domain" in p_ and s_ in obj_props:
            prop_domains[s_].add(tail_o)
        # 类型层级 subClassOf
    # 主类 = 实例最多的类
    if not inst_by_cls:
        return {"class": list(classes)[0].split("#")[-1] if classes else "本体",
                "instance_count": 0, "data_properties": [], "object_properties": [],
                "target_classes": [], "type_hierarchy": [], "class_attributes": {}}
    main_cls = max(inst_by_cls, key=inst_by_cls.get)
    main_name = main_cls.split("#")[-1].split("/")[-1] if "#" in main_cls else main_cls.split("/")[-1]
    # 数据属性(主类的) + 对象属性(主类的)
    dprops = [p.split("#")[-1].split("/")[-1] for p in data_props if not prop_domains[p] or main_name in prop_domains[p] or not prop_domains]
    oprops = [{"rel": p.split("#")[-1].split("/")[-1], "target": obj_targets.get(p, "对象")}
              for p in obj_props if not prop_domains[p] or main_name in prop_domains[p] or not prop_domains]
    # 目标类(对象属性指向的类) + 各实例类
    targets = sorted({obj_targets.get(p, "") for p in obj_props} | set(inst_by_cls.keys()))
    target_names = [t.split("#")[-1].split("/")[-1] for t in targets if t]
    # 类型层级
    hierarchy = [{"name": main_name, "super": "BusinessObject", "label": cls_labels.get(main_cls, main_name)}]
    for t in target_names:
        if t and t != main_name:
            hierarchy.append({"name": t, "super": "BusinessObject", "label": t})
    # 每类属性
    class_attrs = {}
    for cls, cnt in sorted(inst_by_cls.items(), key=lambda x: -x[1]):
        cn = cls.split("#")[-1].split("/")[-1]
        class_attrs[cn] = {"instance_count": cnt, "label": cls_labels.get(cls, cn)}
    return {
        "class": main_name,
        "instance_count": inst_by_cls[main_cls],
        "data_properties": dprops,
        "object_properties": oprops,
        "target_classes": target_names,
        "type_hierarchy": hierarchy,
        "class_attributes": class_attrs,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python ontology_schema_info.py <nt文件>"); sys.exit(1)
    try:
        result = extract(sys.argv[1])
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
