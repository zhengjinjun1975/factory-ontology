#!/usr/bin/env python3
"""db_setup.py — 数据库接入建模桥接（Web 后端调用）

复用 db_loader.load_db 读 MySQL/PostgreSQL 表 → 写 CSV 到临时目录 → 复用
multi_model.build 多表建模（load_all + suggest_schema + to_nt）。
数据本地处理、不出厂（本地局域网场景，连内网 MES/ERP 服务器）。
极简：只做桥接，不重写连接逻辑。

用法:
  python db_setup.py <data_dir> <table>
连接配置通过环境变量 DB_CFG(JSON) 传入，密码只在内存使用，不入库/不落盘。
"""
import os
import sys
import json
import csv
import importlib.util

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load(mod, path):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    if len(sys.argv) < 2:
        print("用法: python db_setup.py <data_dir> [table]（连接配置在环境变量 DB_CFG）")
        sys.exit(1)
    data_dir, table = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "factory_db")

    raw = os.environ.get("DB_CFG")
    if not raw:
        print("❌ 未提供连接配置（环境变量 DB_CFG 缺失）", file=sys.stderr); sys.exit(1)
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"❌ 连接配置不是合法 JSON: {e}", file=sys.stderr); sys.exit(1)

    db = _load("db_loader", os.path.join(ROOT, "db_loader.py"))
    tables = cfg.get("tables") or [cfg.get("table")]
    tables = [t for t in tables if t]
    if not tables:
        print("❌ 未指定表名 tables", file=sys.stderr); sys.exit(1)

    # 读取每张表 → 写 CSV 到临时数据目录
    os.makedirs(data_dir, exist_ok=True)
    loaded = []
    for t in tables:
        one = dict(cfg)
        one["table"] = t                      # 表名来自配置（db_loader 内部已做白名单校验）
        res = db.load_db(one)
        if isinstance(res, dict) and "error" in res:
            print(f"❌ 读取表 {t} 失败: {res['error']}", file=sys.stderr); sys.exit(1)
        name, headers, rows = res
        csv_path = os.path.join(data_dir, f"{name}.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(headers)
            for r in rows:
                w.writerow([r.get(h, "") for h in headers])
        loaded.append(f"{name}({len(rows)}行)")
        print(f"✅ 读表 {name}: {len(rows)} 行, 列 {headers}")

    # 多表统一建模（复用 multi_model.build）
    mm = _load("multi_model", os.path.join(ROOT, "multi_model.py"))
    try:
        table, tables_, n = mm.build(data_dir, table)
        print(f"✅ 数据库建模完成: {len(tables_)} 表 -> {table}.nt ({n} 行 N-Triples)")
        print(f"   表: {tables_}")
    except Exception as e:
        print(f"❌ 数据库建模失败: {e}", file=sys.stderr); sys.exit(1)


if __name__ == "__main__":
    main()
