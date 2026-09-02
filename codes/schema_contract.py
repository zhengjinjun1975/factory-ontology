# -*- coding: utf-8 -*-
"""schema_contract.py — factory-ontology 的版本化语义契约（schema 演化破坏性检测）。

对齐 factory 的 config/ontology_schema.json 结构(entities/relations/constraints/version)：
把 schema 当代码管——改动 schema 前跑 diff，判断"这一版相对上一版"是破坏性还是兼容性变更，
甩出语义化影响清单 + exit code(有破坏=1)，供发布门/CI 拦截，避免删类型/关系时牵一发动全身。

契约规则(向后兼容语义)：
  破坏(breaking)   — 删实体 / 删关系 / 删字段(entity 的 attribute) /
                      改关系的 from/to/cardinality / 收紧约束(改 on 目标或类型)
  兼容(compat)     — 加实体 / 加关系 / 加字段 / 加约束 / version 变化
  去噪: 删一个实体只报一条(不连带报它的 N 个字段——字段随实体消亡非独立删除)

用法:
  python schema_contract.py config/ontology_schema.json <上一版.json>   # 前者为当前、后者为旧(prev)
  from schema_contract import diff_schema, format_report
  breaks, compats = diff_schema(prev, curr)        # 参数 (旧, 新)
  print(format_report(breaks, compats, curr_version, prev_version))
"""

import sys
import json


def _id_set(items):
    """entities/relations 是 list of {id,...} -> {id}。"""
    return {x.get("id") for x in items if isinstance(x, dict) and x.get("id")}


def _by_id(items):
    return {x.get("id"): x for x in items if isinstance(x, dict)}


def diff_schema(prev, curr):
    """比较 (prev旧, curr新)。返回 (breaks:list[str语义化], compats:list[str])。"""
    breaks, compats = [], []
    pv = (prev or {}).get("version")
    cv = (curr or {}).get("version")

    pe = _by_id((prev or {}).get("entities", []))
    ce = _by_id((curr or {}).get("entities", []))
    pr = _by_id((prev or {}).get("relations", []))
    cr = _by_id((curr or {}).get("relations", []))

    # ---- 删除: 破坏 ----
    for eid in sorted(_id_set((prev or {}).get("entities", [])) - set(ce)):
        tbl = pe[eid].get("table", "")
        breaks.append(f"删除实体: {eid}" + (f" (含表 {tbl})" if tbl else ""))
    for rid in sorted(_id_set((prev or {}).get("relations", [])) - set(cr)):
        r = pr[rid]
        breaks.append(f"删除关系: {rid} ({r.get('from')}→{r.get('to')})")
    # 删字段: 仅当实体仍在 curr 里(实体删除则字段随消亡, 不重复报=去噪)
    for eid in sorted(set(pe) & set(ce)):
        pa = {a.get("name") for a in pe[eid].get("attributes", [])}
        ca = {a.get("name") for a in ce[eid].get("attributes", [])}
        for an in sorted(pa - ca):
            breaks.append(f"删除字段: {eid}.{an}")
    # 改关系端点/基数/FK
    for rid in sorted(set(pr) & set(cr)):
        a, b = pr[rid], cr[rid]
        for field in ("from", "to", "cardinality"):
            if a.get(field) != b.get(field):
                breaks.append(f"关系变更: {rid} 的 {field} 由 {a.get(field)!r} → {b.get(field)!r}")
        # 收紧必填
    # 字段 required 收紧(可选→必填) = 破坏
    for eid in sorted(set(pe) & set(ce)):
        pb = {a.get("name"): a for a in pe[eid].get("attributes", [])}
        cb = {a.get("name"): a for a in ce[eid].get("attributes", [])}
        for an in sorted(set(pb) & set(cb)):
            if pb[an].get("required") and not cb[an].get("required"):
                pass  # 放宽必填 = 兼容
            elif not pb[an].get("required") and cb[an].get("required"):
                breaks.append(f"字段收紧: {eid}.{an} 由可选改为必填")

    # ---- 新增: 兼容 ----
    for eid in sorted(set(ce) - set(pe)):
        compats.append(f"新增实体: {eid}")
    for rid in sorted(set(cr) - set(pr)):
        r = cr[rid]
        compats.append(f"新增关系: {rid} ({r.get('from')}→{r.get('to')})")
    for eid in sorted(set(pe) & set(ce)):
        pa = {a.get("name") for a in pe[eid].get("attributes", [])}
        ca = {a.get("name") for a in ce[eid].get("attributes", [])}
        for an in sorted(ca - pa):
            compats.append(f"新增字段: {eid}.{an}")
    # 约束: 加约束 = 兼容提示; 若同 on 约束类型或 on 目标被删则算破坏(改 on)
    pc = {(c.get("type"), c.get("on")) for c in (prev or {}).get("constraints", []) if isinstance(c, dict)}
    cc = {(c.get("type"), c.get("on")) for c in (curr or {}).get("constraints", []) if isinstance(c, dict)}
    for (t, o) in sorted(cc - pc):
        compats.append(f"新增约束: {t} on {o}")
    for (t, o) in sorted(pc - cc):
        # 约束被删算破坏(约束通常代表数据完整性保障被撤)
        breaks.append(f"删除约束: {t} on {o}")
    if pv != cv:
        compats.append(f"版本号: {pv} → {cv}")
    return breaks, compats


def format_report(breaks, compats, curr_version=None, prev_version=None):
    lines = []
    if prev_version or curr_version:
        lines.append(f"schema 版本: {prev_version or '?'} → {curr_version or '?'}")
    lines.append(f"破坏性变更: {len(breaks)} 条" + ("" if breaks else " (无)"))
    for b in breaks:
        lines.append(f"  [破坏] {b}")
    lines.append(f"兼容性变更: {len(compats)} 条")
    for c in compats:
        lines.append(f"  [兼容] {c}")
    return "\n".join(lines)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    curr_path, prev_path = sys.argv[1], sys.argv[2]
    with open(curr_path, encoding="utf-8") as f:
        curr = json.load(f)
    with open(prev_path, encoding="utf-8") as f:
        prev = json.load(f)
    breaks, compats = diff_schema(prev, curr)
    print(format_report(breaks, compats, curr.get("version"), prev.get("version")))
    print("\n结论:", "阻断——存在破坏性变更, 需人工确认" if breaks else "通过——兼容变更, 可放行")
    return 1 if breaks else 0


if __name__ == "__main__":
    sys.exit(main())
