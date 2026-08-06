#!/usr/bin/env python3
"""ontology_relation_qa.py — 关系问答模块（对象属性：设备→位于→车间 / 属于→产线 / 制造商 / 类型）。

配合 ontology_qa_v3 使用：v3 负责数据属性问答（数量/极值/平均等），本模块负责
对象属性反查（"车间A的设备有哪些" / "L1产线的设备" / "哪些设备在动力站"）。

词典 D 提供:
  relations_cn2en  关系词 -> 对象属性名 (如 {"车间":"locatedIn","产线":"belongsToLine","制造商":"manufacturedBy","类型":"hasType"})
  field_aliases    标准字段别名 (deviceName 等，用于显示名)

用法:
  import ontology_relation_qa as rel
  data = v3.build_data(v3.parse_nt(nt), D)   # 复用 v3 的数据构建
  ans = rel.relation_answer(q, data, D)
"""
import re


def relation_answer(q, data, D):
    """对象属性反查。返回字符串答案，无法匹配返回 None。"""
    relations_cn2en = D.get("relations_cn2en", {})
    aliases = D.get("field_aliases", {})

    # ---- 1. 提取关系类型词（车间/产线/制造商/类型）----
    rel_en = None
    rel_cn = None
    for cn, en in sorted(relations_cn2en.items(), key=lambda x: len(x[0]), reverse=True):
        if cn in q:
            rel_en, rel_cn = en, cn
            break
    if not rel_en:
        return None

    # ---- 2. 提取目标值（车间名/产线号/厂商/类型）----
    target = None
    # 关系词后紧跟的实体词（到分隔词为止）
    stop = '的有在哪些中里总共台个'
    cls = r'[^\s' + stop + r']'
    m = re.search(r'(?<=' + re.escape(rel_cn) + r')(' + cls + r'+)', q)
    if m:
        target = (rel_cn + m.group(1)).strip()
    if not target:
        # 前置模式: "L1产线的设备" -> 实体在关系词前
        m0 = re.search(r'(' + cls + r'+)' + re.escape(rel_cn) + r'[的有在]?', q)
        if m0:
            target = m0.group(1).strip()
    if not target:
        # 模式B: 在<实体>
        m2 = re.search(r'在(' + cls + r'+)', q)
        if m2:
            target = m2.group(1).strip()
    if not target:
        # 模式C: 关系词本身即完整实体（如"动力站的设备"），且关系词是地点/产线全名
        if re.search(re.escape(rel_cn) + r'[的]', q) and rel_cn not in ("位置", "地点", "区域"):
            target = rel_cn

    if not target:
        return None

    # ---- 3. 反查设备（对象属性值 == target）----
    def get_field(rec, canonical):
        for a in aliases.get(canonical, [canonical]):
            if a in rec:
                return rec[a]
        return rec.get(canonical, "")

    matched = []
    # target 可能带关系词前缀（如"制造商Fanuc"），也准备剥离版本
    target_candidates = [target]
    if target and rel_cn and target.startswith(rel_cn):
        target_candidates.append(target[len(rel_cn):])
    for key, d in data.items():
        raw = d.get(rel_en, "")
        # 对象属性值可能是完整URI: http://...#Location_车间A-01，取尾名再剥类前缀
        val = raw.split("#")[-1].split("/")[-1]
        if "_" in val:
            val = val.split("_", 1)[1]
        # 前缀匹配：target="车间A" 匹配 val="车间A-01"；产线"L1"匹配"L1"
        if any(val == t or (t and val.startswith(t)) for t in target_candidates):
            nm = get_field(d, "deviceName") or key
            matched.append(nm)
    if not matched:
        return None

    # 措辞: 目标是具体实体(如 车间A-01, L1)，输出"目标 + 的设备"
    return "%s的设备(%d):\n%s" % (target, len(matched), "\n".join("  - " + n for n in matched[:20]))


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ontology_qa_v3 as v3
    if len(sys.argv) < 3:
        print("用法: python ontology_relation_qa.py <nt文件> '<问题>' [lexicon.json]")
        sys.exit(1)
    nt = sys.argv[1]
    q = sys.argv[2]
    lex = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "config", "lexicon.json")
    D = v3.load_dict(lex)
    data = v3.build_data(v3.parse_nt(nt), D)
    ans = relation_answer(q, data, D)
    print(ans if ans else "暂不支持该问题")
