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


def _build_lexicon(schema, data):
    """从 suggest_schema 生成基础词典（attr_cn2en/attr_en2cn/status_cn2en 等），供 ask 问答使用。
    极简：直接用 suggest_schema 推断出的中文属性 label 生成自然词典
    （生产日期→produce_date / 批次编号→batch_id / 原料→raw_parts），缺失时用英文名兜底。"""
    cn = {}  # 中文名 -> 英文名
    en = {}  # 英文名 -> 中文名
    for e in schema.get("entities", []):
        for a in e.get("attributes", []):
            name = a["name"]
            if name in en:
                continue
            label = a.get("label") or name   # 中文 label（LLM/规则），缺失则英文兜底
            cn[label] = name
            en[name] = label
    return {
        "description": "自动生成词典（multi_model, suggest_schema 推断）",
        "attr_cn2en": cn, "attr_en2cn": en,
        "status_cn2en": {"运行中": "running", "待机": "idle", "报警": "alarm", "维护中": "maintenance", "离线": "offline", "合格": "pass", "不合格": "fail"},
        "field_aliases": {"status": ["status"], "deviceType": ["deviceType", "device_type", "type"], "deviceName": ["deviceName", "device_name", "name"]},
        "value_fields": [],
    }


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
    # 生成基础词典（供 ask 问答），写入 config/
    lexicon = _build_lexicon(schema, data)
    lex_path = os.path.join(ROOT, "config", f"lexicon_{table}.json")
    with open(lex_path, "w", encoding="utf-8") as f:
        json.dump(lexicon, f, ensure_ascii=False, indent=2)
    json.dump({"nt": os.path.relpath(nt, ROOT), "table": table,
               "data_dir": os.path.relpath(data_dir, ROOT),
               "lexicon": os.path.relpath(lex_path, ROOT)},
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
