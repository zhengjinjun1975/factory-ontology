#!/usr/bin/env python3
"""multi_model.py — 多文件/多表统一建模桥接（Web 后端调用）

复用 schema_ontology 已验证的 schema-free 多表建模能力：
  load_all(data_dir) → suggest_schema(data) 自动推断 → to_nt 生成本体 .nt
数据本地处理，不出厂（本地局域网场景）。极简：只做桥接，不重写建模逻辑。

用法:
  python multi_model.py <data_dir> [table]
"""
import os
import sys
import json
import importlib.util

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
STATE = os.path.join(ROOT, "current.json")


def _load(mod, path):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def build(data_dir, table="factory_multi"):
    """load_all + suggest_schema + to_nt 统一建模。返回 (table, tables列表, nt行数)。"""
    so = _load("schema_ontology", os.path.join(ROOT, "schema_ontology.py"))
    data = so.load_all(data_dir)               # {表名: [行...]}
    schema = so.suggest_schema(data)           # 自动推断 schema（schema-free）
    os.makedirs(OUT, exist_ok=True)
    nt = os.path.join(OUT, f"{table}.nt")
    lines = so.to_nt(data, schema, outpath=nt)  # 写 N-Triples
    if not os.path.exists(nt):
        raise RuntimeError("to_nt 未生成本体文件")
    json.dump({"nt": os.path.relpath(nt, ROOT), "table": table,
               "data_dir": os.path.relpath(data_dir, ROOT)},
              open(STATE, "w", encoding="utf-8"))
    return table, list(data.keys()), len(lines)


def main():
    if len(sys.argv) < 2:
        print("用法: python multi_model.py <data_dir> [table]"); sys.exit(1)
    data_dir, table = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "factory_multi")
    try:
        table, tables, n = build(data_dir, table)
        print(f"✅ 多表建模完成: {len(tables)} 表 -> {table}.nt ({n} 行 N-Triples)")
        print(f"   表: {tables}")
    except Exception as e:
        print(f"❌ 多表建模失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
