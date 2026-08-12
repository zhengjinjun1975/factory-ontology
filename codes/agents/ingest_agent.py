#!/usr/bin/env python3
"""ingest_agent.py — 原子智能体：数据接入（甲方数据 → 本体 .nt 文件）。

核心：把甲方原始数据（CSV/JSON/SQLite/Excel 单文件，或数据目录）转成标准
N-Triples 本体文件，供下游问答/图检索消费。单文件走 csv_to_owl 自动建模；
数据目录 + schema 走 schema_ontology 做企业级 schema 驱动建模。

能力：
1. 单文件(CSV/JSON/...): csv_to_owl 数据驱动建模 → 一个 .nt
2. 数据目录 + schema: schema_ontology load_all→load_schema→to_nt → 跨表统一 .nt
3. 未传 schema 的目录: 自动推断 schema(suggest_schema) 兜底建本体

task 结构:
  {"source_path": "...", "out_nt": "...", "table_name": "...", "schema": "..."}
  - source_path: 数据文件(单文件建模) 或 数据目录(schema 驱动建模)
  - out_nt:      可选，本体输出路径，缺省放 output/<table_name>.nt
  - table_name:  可选，本体/类名；缺省取源文件名或 schema 名
  - schema:      可选，ontology_schema.json 路径；给则走 schema 驱动建模
"""

import os
import sys
import json

# 路径修正
_APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP)
sys.path.insert(0, os.path.join(_APP, ".."))
sys.path.insert(0, os.path.join(_APP, "..", "src"))

from core.base_agent import BaseAgent, AgentResult

_APP_ROOT = os.path.join(_APP, "..")


def _resolve(root, p):
    """相对路径基于包根解析（迁移后自动重定位）。"""
    return p if os.path.isabs(p) else os.path.join(root, p)


class IngestAgent(BaseAgent):
    name = "ingest"

    def run(self, task: dict) -> AgentResult:
        return self._timed(self._run, task)

    def _run(self, task):
        src = task.get("source_path")
        if not src or not os.path.exists(src):
            return self._err(f"数据源不存在: {src}")

        schema = task.get("schema")
        out_nt = task.get("out_nt")

        try:
            if schema:
                # ── schema 驱动建模（数据目录 + ontology_schema.json）──
                nt, instances, tables = self._ingest_schema(src, schema, out_nt, task)
            elif os.path.isdir(src):
                # ── 目录无 schema → 自动推断 schema 兜底建模 ──
                nt, instances, tables = self._ingest_dir_auto(src, out_nt, task)
            else:
                # ── 单文件 → csv_to_owl 数据驱动建模 ──
                nt, instances, tables = self._ingest_file(src, out_nt, task)
        except Exception as e:
            return self._err(f"数据接入失败: {e}")

        return self._ok({"nt_path": nt, "instances": instances,
                         "tables": tables, "schema_driven": bool(schema)}, "ingest")

    # ---------------- 单文件：csv_to_owl 数据驱动建模 ----------------

    def _ingest_file(self, src, out_nt, task):
        import csv_to_owl
        table = task.get("table_name") or self._base_name(src)
        nt = out_nt or os.path.join(_APP_ROOT, "output", f"{table}.nt")
        os.makedirs(os.path.dirname(nt), exist_ok=True)

        # 若有对象属性配置则传入 relations.json
        rel_path = os.path.join(_APP_ROOT, "config", "relations.json")
        relations = None
        if os.path.exists(rel_path):
            relations = csv_to_owl.load_relations(rel_path)
        csv_to_owl.build_nt(src, nt, relations)

        # 统计实例/表信息
        from data_loader import load_table
        tname, _headers, rows = load_table(src)
        return nt, len(rows), [tname]

    # ---------------- 数据目录 + schema：schema 驱动建模 ----------------

    def _ingest_schema(self, data_dir, schema_path, out_nt, task):
        import schema_ontology as so
        schema_path = _resolve(_APP_ROOT, schema_path)
        data = so.load_all(data_dir)                       # 多表数据
        schema = so.load_schema(schema_path)               # schema + 校验
        issues = so.validate(data, schema)
        if issues:
            print(f"⚠️ [ingest] 约束校验 {len(issues)} 问题: {[i['msg'] for i in issues[:3]]}")

        table = task.get("table_name") or schema.get("name", "factory")
        nt = out_nt or os.path.join(_APP_ROOT, "output", f"{table}.nt")
        os.makedirs(os.path.dirname(nt), exist_ok=True)
        lines = so.to_nt(data, schema, outpath=nt)

        tables = list(data.keys())
        instances = sum(len(rows) for rows in data.values())
        print(f"✅ [ingest] schema 驱动本体: {nt} ({len(lines)} 行 NT | {instances} 实例)")
        return nt, instances, tables

    # ---------------- 数据目录无 schema：自动推断兜底 ----------------

    def _ingest_dir_auto(self, data_dir, out_nt, task):
        import schema_ontology as so
        data = so.load_all(data_dir)
        schema = so.suggest_schema(data)                   # schema-free 自动推断
        table = task.get("table_name") or schema.get("name", "factory")
        nt = out_nt or os.path.join(_APP_ROOT, "output", f"{table}.nt")
        os.makedirs(os.path.dirname(nt), exist_ok=True)
        lines = so.to_nt(data, schema, outpath=nt)

        tables = list(data.keys())
        instances = sum(len(rows) for rows in data.values())
        print(f"✅ [ingest] 自动推断建模: {nt} ({len(lines)} 行 NT | {instances} 实例)")
        return nt, instances, tables

    # ---------------- 工具 ----------------

    @staticmethod
    def _base_name(p):
        return os.path.splitext(os.path.basename(p))[0]

    def main(self):
        task = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {
            "source_path": os.path.join(_APP_ROOT, "data", "equipment.csv"),
        }
        r = self.run(task)
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    IngestAgent().main()
