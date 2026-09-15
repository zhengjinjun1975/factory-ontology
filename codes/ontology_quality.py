#!/usr/bin/env python3
"""ontology_quality.py — 本体建模质量门（L2.5）：建模后立刻体检，不达标就报红。

为什么需要它：schema 可以由 suggest_schema 自动生成，但"自动生成"不等于"可交付"。
FDE 现场赶工最容易出的问题——label 全是英文、定义空缺、外键没建关系、类建了却没有实例——
单看代码看不出，交付后客户一用就露。这个门把这些变成一条命令的退出码。

纯标准库、离线、只读。不参与问答/决策，运行时不被 import。

用法:
  python ontology_quality.py --schema config/ontology_schema.json --data data_valve
  python ontology_quality.py --schema config/ontology_schema.json --data data_valve --json
  python ontology_quality.py ... --strict     # 有任何警告也返回非零(用于 CI/交付闸门)

阈值（可用 --min-label 等覆盖）:
  中文标签覆盖 ≥ 80%   定义覆盖 ≥ 50%   外键关系完整 = 100%   无孤立类
退出码: 0=达标  1=有硬伤  2=仅警告(非 --strict 时为 0)
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schema_ontology as so  # noqa: E402

VERSION = "0.1.0"

DEFAULTS = {
    "min_label": 80.0,      # 中文标签覆盖率下限(%)
    "min_definition": 50.0,  # 定义覆盖率下限(%)
    "require_relations": True,  # 外键必须都有对象关系
    "require_instances": True,  # 每个类至少要有实例(否则是空壳)
}


def _has_cn(s):
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(s or ""))


def _pct(ok, total):
    return 100.0 if total == 0 else round(100.0 * ok / total, 1)


def inspect(schema: dict, data: dict = None) -> dict:
    """体检一个 schema(+可选数据)，返回结构化报告。"""
    schema = so.fill_iris(json.loads(json.dumps(schema)))
    entities = list((schema.get("_entities") or
                     {e["id"]: e for e in schema.get("entities", [])}).values())
    data = data or {}

    ent_label_ok = sum(1 for e in entities if _has_cn(e.get("label")))
    attrs = [(e, a) for e in entities for a in e.get("attributes", [])]
    attr_label_ok = sum(1 for _e, a in attrs if _has_cn(a.get("label")))
    ent_def_ok = sum(1 for e in entities if str(e.get("definition") or "").strip())
    attr_def_ok = sum(1 for _e, a in attrs if str(a.get("definition") or "").strip())

    # 外键 → 关系完整度
    relations = schema.get("relations", []) or []
    declared = {(r.get("fk") or "") for r in relations}
    tables = {str(e.get("table") or "").lower() for e in entities}
    stems = {t.split("_")[-1].rstrip("s") for t in tables}
    stems |= {str(e.get("id") or "").lower().rstrip("s") for e in entities}

    def _is_fk(col: str) -> bool:
        """是不是外键列。

        只认 *_id，或 *_code 且去掉后缀能对上某个已建模的表/实体 ——
        否则 model_code / credit_code 这类普通属性会被误判成"未建关系的外键"。
        """
        if col.endswith("_id"):
            return True
        if col.endswith("_code"):
            base = col[:-len("_code")].split("_")[-1].rstrip("s")
            return base in stems
        return False

    fk_cols = set()
    for e in entities:
        key = e.get("key")
        for a in e.get("attributes", []):
            n = a["name"]
            if n != key and _is_fk(n):
                fk_cols.add(f"{e.get('table')}.{n}")
    missing_rel = sorted(c for c in fk_cols if c not in declared)

    # 结构问题
    empty_classes = []
    if data:
        for e in entities:
            rows = data.get(e.get("table")) or []
            if not rows:
                empty_classes.append(e.get("id"))
    no_key = [e.get("id") for e in entities if not e.get("key")]

    label_total = len(entities) + len(attrs)
    label_ok = ent_label_ok + attr_label_ok
    def_total = label_total
    def_ok = ent_def_ok + attr_def_ok

    return {
        "version": VERSION,
        "name": schema.get("name", ""),
        "counts": {"entities": len(entities), "attributes": len(attrs), "relations": len(relations)},
        "label": {"ok": label_ok, "total": label_total, "rate": _pct(label_ok, label_total),
                  "entity_ok": ent_label_ok, "attribute_ok": attr_label_ok},
        "definition": {"ok": def_ok, "total": def_total, "rate": _pct(def_ok, def_total),
                       "entity_ok": ent_def_ok, "attribute_ok": attr_def_ok},
        "relations": {"declared": len(relations), "fk_cols": len(fk_cols),
                      "missing": missing_rel,
                      "rate": _pct(len(fk_cols) - len(missing_rel), len(fk_cols))},
        "structure": {"empty_classes": empty_classes, "no_key": no_key},
        # 中文标签全丢是最典型的"交付即露怯"，单独拎出来
        "english_labels": [e["id"] for e in entities if not _has_cn(e.get("label"))],
    }


def judge(report: dict, thresholds: dict = None) -> dict:
    """按阈值判定 → {passed, hard, warns}。"""
    t = dict(DEFAULTS)
    t.update(thresholds or {})
    hard, warns = [], []
    if report["label"]["rate"] < t["min_label"]:
        hard.append(f"中文标签覆盖 {report['label']['rate']}% < {t['min_label']}%"
                    + (f"（全英文: {', '.join(report['english_labels'][:5])}）"
                       if report["english_labels"] else ""))
    if report["definition"]["rate"] < t["min_definition"]:
        warns.append(f"定义覆盖 {report['definition']['rate']}% < {t['min_definition']}%（影响问答解释质量）")
    if t["require_relations"] and report["relations"]["missing"]:
        hard.append(f"外键未建关系: {', '.join(report['relations']['missing'][:5])}")
    if t["require_instances"] and report["structure"]["empty_classes"]:
        warns.append(f"无实例的类(空壳): {', '.join(report['structure']['empty_classes'][:5])}")
    if report["structure"]["no_key"]:
        hard.append(f"缺主键的实体: {', '.join(report['structure']['no_key'][:5])}")
    return {"passed": not hard, "hard": hard, "warns": warns}


def format_text(report: dict, verdict: dict) -> str:
    r = report
    mark = lambda ok, warn=False: "✅" if ok else ("⚠️ " if warn else "❌")  # noqa: E731
    lines = [f"本体质量门 · {r['name'] or '(未命名)'}  [v{r['version']}]",
             "─" * 52,
             f"  中文标签  {r['label']['ok']}/{r['label']['total']} = {r['label']['rate']}%   "
             f"(实体 {r['label']['entity_ok']}, 属性 {r['label']['attribute_ok']})",
             f"  定义覆盖  {r['definition']['ok']}/{r['definition']['total']} = {r['definition']['rate']}%",
             f"  外键关系  {r['relations']['fk_cols'] - len(r['relations']['missing'])}/"
             f"{r['relations']['fk_cols']} = {r['relations']['rate']}%",
             f"  结构      孤立类 {len(r['structure']['empty_classes'])} · "
             f"缺主键 {len(r['structure']['no_key'])} · "
             f"实体 {r['counts']['entities']} / 属性 {r['counts']['attributes']}",
             "─" * 52]
    for h in verdict["hard"]:
        lines.append(f"  ❌ {h}")
    for w in verdict["warns"]:
        lines.append(f"  ⚠️  {w}")
    lines.append(f"结论: {'PASS' if verdict['passed'] else 'FAIL'}"
                 + (f"（{len(verdict['warns'])} 项警告）" if verdict["warns"] else ""))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="本体建模质量门（纯标准库）")
    ap.add_argument("--schema", required=True)
    ap.add_argument("--data", default=None, help="数据目录(给出则检查空壳类)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict", action="store_true", help="有警告也算不通过")
    ap.add_argument("--min-label", type=float, default=DEFAULTS["min_label"])
    ap.add_argument("--min-definition", type=float, default=DEFAULTS["min_definition"])
    a = ap.parse_args()
    schema = so.load_schema(a.schema)
    data = so.load_all(a.data) if a.data else {}
    report = inspect(schema, data)
    verdict = judge(report, {"min_label": a.min_label, "min_definition": a.min_definition})
    print(json.dumps({"report": report, "verdict": verdict}, ensure_ascii=False, indent=2)
          if a.json else format_text(report, verdict))
    if not verdict["passed"]:
        sys.exit(1)
    if a.strict and verdict["warns"]:
        sys.exit(2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
