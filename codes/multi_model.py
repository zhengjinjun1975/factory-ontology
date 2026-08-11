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
    极简：属性英文名→中文名用常见后缀映射 + 原样兜底。"""
    cn = {}  # 中文名 -> 英文名
    en = {}  # 英文名 -> 中文名
    _SUFFIX_CN = {"id": "编号", "name": "名称", "type": "类型", "status": "状态", "date": "日期",
                  "quantity": "数量", "amount": "金额", "price": "价格", "stock": "库存",
                  "material": "材质", "region": "区域", "supplier": "供应商", "result": "结果"}
    def _cn_for(attr):
        base = attr.replace("_", " ")
        # 取语义核心词（去掉 _id/_code 后缀）映射
        core = attr
        for suf in ("_id", "_code", "_key"):
            if core.endswith(suf):
                core = core[:-len(suf)]
                return _SUFFIX_CN.get("id", "编号") if core in ("",) and suf == "_id" else _SUFFIX_CN.get(core, core) + ("" if suf != "_id" else "编号")
        return _SUFFIX_CN.get(core, core)
    # 收集所有实体的属性
    for e in schema.get("entities", []):
        for a in e.get("attributes", []):
            name = a["name"]
            if name not in en:
                c = _cn_for(name)
                cn[c] = name
                en[name] = c
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
