#!/usr/bin/env python3
"""db_to_ontology.py — 企业 ERP/MES 数据库一键建本体（DB → 本体，config 驱动）

把 SQLite / MySQL / PostgreSQL 里的表，经 config 指定"连接串 + 表清单"后，
一键建本体：读表 → 复用 multi_model.build_data（suggest_schema + to_nt + 词典 + state）。
数据本地处理、不出厂（本地局域网连内网 MES/ERP 服务器场景）。

连接写法（三选一，支持 DSN 与 dict）:
  - SQLite:     sqlite:///C:/path/factory.db
  - MySQL:      mysql+pymysql://user:pass@127.0.0.1:3306/erp
  - PostgreSQL: postgresql+psycopg2://user:pass@127.0.0.1:5432/erp

config 文件示例（config/db_ontology_config.json）:
  {
    "dsn": "sqlite:///C:/open-source/factory-ontology-kit/codes/data/erp_demo.db",
    "tables": ["equipment", "products", "orders"],
    "output": "factory_multi",
    "schema": "erp"
  }
  - tables: 要建模的表清单；缺省则自动列出库中所有表
  - output: 输出本体名(生成 output/<output>.nt + config/lexicon_<output>.json)

用法:
  python db_to_ontology.py config/db_ontology_config.json
"""
import os
import sys
import json
import importlib.util

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load(mod, path):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _read_config(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"[DB建模] config 文件不存在: {path}")
    try:
        cfg = json.load(open(path, encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"[DB建模] config 不是合法 JSON: {e}")
    if not isinstance(cfg, dict):
        raise ValueError("[DB建模] config 顶层必须是 JSON 对象")
    if not cfg.get("dsn") and not (cfg.get("db_type") and cfg.get("database")):
        raise ValueError("[DB建模] config 需提供 dsn(连接串) 或 db_type+database")
    return cfg


def _list_tables(conn_cfg, db_loader):
    """列出库中所有业务表（sqlite_master / information_schema / pg_tables）。"""
    db_type = (conn_cfg.get("db_type") or "").lower()
    if db_type in ("postgres", "postgresql", "pg"):
        db_type = "postgres"
    try:
        if db_type == "sqlite":
            import sqlite3
            p = conn_cfg.get("database") or ""
            if not os.path.exists(p):
                raise FileNotFoundError(f"SQLite 库不存在: {p}")
            conn = sqlite3.connect(p)
            try:
                tabs = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            finally:
                conn.close()
            return tabs
        if db_type == "mysql":
            try:
                import pymysql
            except ImportError:
                return {"error": "MySQL 需安装驱动: pip install pymysql"}
            conn = pymysql.connect(host=conn_cfg.get("host"), port=conn_cfg.get("port") or 3306,
                                   user=conn_cfg.get("user"), password=conn_cfg.get("password"),
                                   database=conn_cfg.get("database"), charset="utf8mb4")
            cur = conn.cursor()
            cur.execute("SHOW TABLES")
            tabs = [r[0] for r in cur.fetchall()]
            conn.close()
            return tabs
        if db_type == "postgres":
            try:
                import psycopg2
            except ImportError:
                return {"error": "PostgreSQL 需安装驱动: pip install psycopg2-binary"}
            conn = psycopg2.connect(host=conn_cfg.get("host"), port=conn_cfg.get("port") or 5432,
                                    user=conn_cfg.get("user"), password=conn_cfg.get("password"),
                                    dbname=conn_cfg.get("database"))
            cur = conn.cursor()
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
            tabs = [r[0] for r in cur.fetchall()]
            conn.close()
            return tabs
    except Exception as e:
        return {"error": f"列出表失败: {e}"}
    return {"error": f"不支持的数据库类型: {db_type}（支持 sqlite/mysql/postgres）"}


def load_db_tables(cfg):
    """按 config 读表 → {表名: [行...]}。tables 缺省则自动列出全部表。"""
    db = _load("db_loader", os.path.join(ROOT, "db_loader.py"))
    conn_cfg = dict(cfg)
    # 有 dsn 时先解析出 db_type/host/port/user/password/database，供列表与读表复用
    if conn_cfg.get("dsn"):
        d = db.parse_dsn(conn_cfg["dsn"])
        conn_cfg.setdefault("db_type", d[0]); conn_cfg.setdefault("host", d[1])
        conn_cfg.setdefault("port", d[2]); conn_cfg.setdefault("user", d[3])
        conn_cfg.setdefault("password", d[4]); conn_cfg.setdefault("database", d[5])
    tables = cfg.get("tables")
    if not tables:
        found = _list_tables(conn_cfg, db)
        if isinstance(found, dict) and "error" in found:
            raise ValueError(f"[DB建模] {found['error']}")
        tables = found
        if not tables:
            raise ValueError("[DB建模] 库中无任何业务表")

    data = {}
    for t in tables:
        one = dict(conn_cfg)
        one["table"] = t
        res = db.load_db(one)
        if isinstance(res, dict) and "error" in res:
            raise ValueError(f"[DB建模] 读表 {t} 失败: {res['error']}")
        name, _headers, rows = res
        data[name] = rows
        print(f"  ✓ 读表 {name}: {len(rows)} 行")
    return data


def build_from_config(cfg_path):
    """一键从 DB 建本体。cfg_path 为 JSON 文件路径，返回 (table, tables, nt行数)。"""
    cfg = _read_config(cfg_path)
    print(f"[DB建模] 读取数据库并加载表清单…")
    data = load_db_tables(cfg)
    if not data:
        raise ValueError("[DB建模] 未从数据库读到任何数据")
    table = cfg.get("output") or cfg.get("table") or "factory_multi"
    print(f"[DB建模] 对 {len(data)} 张表复用 multi_model 建模，输出 {table}…")
    mm = _load("multi_model", os.path.join(ROOT, "multi_model.py"))
    return mm.build_data(data, table)


def main():
    if len(sys.argv) < 2:
        print("用法: python db_to_ontology.py <config.json>")
        print("     参考 config/db_ontology_config.json")
        sys.exit(1)
    try:
        table, tables, n = build_from_config(sys.argv[1])
        print(f"✅ 数据库一键建本体完成: {len(tables)} 表 -> output/{table}.nt ({n} 行 N-Triples)")
        print(f"   表: {tables}")
    except Exception as e:
        print(f"❌ 数据库建模失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
