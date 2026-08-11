#!/usr/bin/env python3
"""db_loader.py — ERP 数据库直连（A2 多源接入）

让知识库直接读企业 ERP 的 MySQL/PostgreSQL，而非仅 CSV/Excel 文件。

用法:
  from db_loader import load_db
  rows = load_db({"db_type":"mysql","host":"127.0.0.1","port":3306,
                  "user":"erp","password":"***","database":"erp","table":"products"})

依赖(可选): mysql → pip install pymysql; postgres → pip install psycopg2-binary
未装驱动时给出清晰提示; 无真实 ERP 时不会真的连接。
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_db(cfg):
    """从 MySQL/PostgreSQL 读一张表。cfg: {db_type, host, port, user, password, database, table}。
    返回 (表名, 列名列表, 行dict列表)。"""
    import re as _re
    db_type = (cfg.get("db_type") or "mysql").lower()
    table = cfg["table"]
    # 表名白名单校验（防 SQL 注入）：只允许合法标识符，与 data_loader 一致
    if not _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError(f"[安全] 非法表名: {table!r}（仅允许字母/数字/下划线开头）")
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port")
    user = cfg.get("user", "")
    password = cfg.get("password", "")
    database = cfg.get("database", "")

    if db_type == "mysql":
        try:
            import pymysql
        except ImportError:
            return {"error": "需安装 pymysql: pip install pymysql"}
        conn = pymysql.connect(host=host, port=port or 3306, user=user,
                               password=password, database=database, charset="utf8mb4")
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM `{table}`")   # 表名来自配置, 非用户输入拼接
        headers = [d[0] for d in cur.description]
        rows = [dict(zip(headers, r)) for r in cur.fetchall()]
        conn.close()
        return (table, headers, rows)

    if db_type in ("postgres", "postgresql", "pg"):
        try:
            import psycopg2
        except ImportError:
            return {"error": "需安装 psycopg2-binary: pip install psycopg2-binary"}
        conn = psycopg2.connect(host=host, port=port or 5432, user=user,
                                password=password, dbname=database)
        cur = conn.cursor()
        cur.execute(f'SELECT * FROM "{table}"')    # 表名来自配置, 非用户输入拼接
        headers = [d[0] for d in cur.description]
        rows = [dict(zip(headers, r)) for r in cur.fetchall()]
        conn.close()
        return (table, headers, rows)

    return {"error": f"不支持的数据库类型: {db_type} (支持 mysql/postgres)"}


def main():
    import json
    if len(sys.argv) < 2:
        print("用法: python db_loader.py <连接配置.json>")
        sys.exit(1)
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    res = load_db(cfg)
    if isinstance(res, dict) and "error" in res:
        print("❌", res["error"]); sys.exit(1)
    name, headers, rows = res
    print(f"✅ 从 {cfg.get('db_type')} 读 {name}: {len(rows)} 行, 列 {headers}")


if __name__ == "__main__":
    main()
