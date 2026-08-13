#!/usr/bin/env python3
"""db_loader.py — 企业 ERP/MES 数据库直连（统一单表读取：SQLite / MySQL / PostgreSQL）

让知识库直接读企业 ERP/MES 的常见数据库，而非仅 CSV/Excel 文件。
支持三种连接写法，且统一返回 (表名, 列名列表, 行dict列表)：

1) DSN 连接串（推荐，便于 config 一键配置）:
   - SQLite:     sqlite:///C:/path/factory.db
   - MySQL:      mysql+pymysql://user:pass@127.0.0.1:3306/erp
   - PostgreSQL: postgresql+psycopg2://user:pass@127.0.0.1:5432/erp

2) dict 配置（兼容旧写法）:
   {"db_type":"mysql","host":"127.0.0.1","port":3306,
    "user":"erp","password":"***","database":"erp","table":"products"}

3) db_type 简写 dsn: {"db_type":"sqlite","database":"C:/x/factory.db","table":"equipment"}

用法:
  from db_loader import load_db
  name, headers, rows = load_db({"dsn":"sqlite:///factory.db","table":"equipment"})

依赖(可选): mysql → pip install pymysql; postgres → pip install psycopg2-binary
未装驱动时给出清晰提示；安全上所有表名/库名只允许合法标识符（防 SQL 注入）。
"""
import os
import re as _re

ROOT = os.path.dirname(os.path.abspath(__file__))

# 合法标识符（防 SQL 注入）：字母/数字/下划线，字母或下划线开头
_IDENT = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe(name, what="表名"):
    """校验标识符合法性，非法即抛错（防 SQL 注入）。"""
    if not _IDENT.fullmatch(str(name)):
        raise ValueError(f"[安全] 非法{what}: {name!r}（仅允许字母/数字/下划线，且非数字开头）")
    return name


def parse_dsn(dsn):
    """解析 DSN 连接串 → (db_type, host, port, user, password, database)。"""
    dsn = str(dsn).strip()
    # SQLite: sqlite:///relative 或 sqlite:///C:/abs/path 或 sqlite:////C:/abs
    m = _re.match(r"^sqlite://(?P<db>/.+)$", dsn, _re.I)
    if m:
        db = m.group("db")
        # 统一路径：/C:/x → C:/x ; 多个前导 / 压缩为一个
        db = _re.sub(r"^/+", "", db)
        return ("sqlite", "", 0, "", "", db)
    # MySQL / PostgreSQL: driver://user:pass@host:port/db  (driver 前缀可省略)
    m = _re.match(
        r"^(?:(?P<db_type>[a-z]+)(?:\+[a-z0-9_]+)?)://"
        r"(?:(?P<user>[^:@/]*)(?::(?P<password>[^@/]*))?@)?"
        r"(?P<host>[^:/@]+)(?::(?P<port>\d+))?"
        r"/(?P<db>[^?]+)",
        dsn, _re.I)
    if not m:
        raise ValueError(f"[安全] 无法解析 DSN: {dsn!r}（支持 sqlite:///path、mysql://u:p@h:p/db、postgresql://u:p@h:p/db）")
    db_type = (m.group("db_type") or "mysql").lower()
    if db_type in ("postgres", "postgresql", "pg"):
        db_type = "postgres"
    elif db_type not in ("mysql", "sqlite"):
        raise ValueError(f"[安全] 不支持的数据库类型: {db_type}（支持 sqlite/mysql/postgres）")
    return (db_type, m.group("host"), int(m.group("port") or 0),
            m.group("user") or "", m.group("password") or "", m.group("db"))


def _read_sqlite(db_path, table):
    import sqlite3
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite 库不存在: {db_path}")
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(f'SELECT * FROM "{table}"')
        headers = [d[0] for d in cur.description]
        rows = [dict(zip(headers, [""
                if x is None else str(x) for x in row])) for row in cur.fetchall()]
    finally:
        conn.close()
    return table, headers, rows


def _read_mysql(cfg):
    try:
        import pymysql
    except ImportError:
        return {"error": "MySQL 需安装驱动: pip install pymysql"}
    conn = pymysql.connect(host=cfg["host"], port=cfg["port"] or 3306,
                           user=cfg["user"], password=cfg["password"],
                           database=cfg["database"], charset="utf8mb4")
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM `{cfg['table']}`")   # 表名已白名单校验
    headers = [d[0] for d in cur.description]
    rows = [dict(zip(headers, r)) for r in cur.fetchall()]
    conn.close()
    return (cfg["table"], headers, rows)


def _read_postgres(cfg):
    try:
        import psycopg2
    except ImportError:
        return {"error": "PostgreSQL 需安装驱动: pip install psycopg2-binary"}
    conn = psycopg2.connect(host=cfg["host"], port=cfg["port"] or 5432,
                            user=cfg["user"], password=cfg["password"],
                            dbname=cfg["database"])
    cur = conn.cursor()
    cur.execute(f'SELECT * FROM "{cfg["table"]}"')   # 表名已白名单校验
    headers = [d[0] for d in cur.description]
    rows = [dict(zip(headers, r)) for r in cur.fetchall()]
    conn.close()
    return (cfg["table"], headers, rows)


def load_db(cfg):
    """从 SQLite/MySQL/PostgreSQL 读一张表。cfg 可为 DSN 串或 dict。

    返回 (表名, 列名列表, 行dict列表)；驱动缺失或类型不支持时返回 {"error": ...}。
    """
    if isinstance(cfg, str):
        db_type, host, port, user, password, database = parse_dsn(cfg)
        cfg = {"db_type": db_type, "host": host, "port": port,
               "user": user, "password": password, "database": database}
    else:
        cfg = dict(cfg)
        if cfg.get("dsn"):
            d = parse_dsn(cfg["dsn"])
            cfg.setdefault("db_type", d[0]); cfg.setdefault("host", d[1])
            cfg.setdefault("port", d[2]); cfg.setdefault("user", d[3])
            cfg.setdefault("password", d[4]); cfg.setdefault("database", d[5])

    db_type = (cfg.get("db_type") or "mysql").lower()
    if db_type in ("postgres", "postgresql", "pg"):
        db_type = "postgres"
    table = _safe(cfg["table"], "表名")

    if db_type == "sqlite":
        return _read_sqlite(cfg.get("database") or "", table)

    cfg["table"] = table
    if db_type == "mysql":
        cfg["database"] = _safe(cfg.get("database"), "库名")
        return _read_mysql(cfg)
    if db_type == "postgres":
        cfg["database"] = _safe(cfg.get("database"), "库名")
        return _read_postgres(cfg)

    return {"error": f"不支持的数据库类型: {db_type}（支持 sqlite/mysql/postgres）"}


def main():
    import sys
    import json
    if len(sys.argv) < 2:
        print("用法: python db_loader.py <连接配置.json | DSN串>")
        sys.exit(1)
    arg = sys.argv[1]
    cfg = json.loads(arg) if arg.strip().startswith("{") else arg
    if isinstance(cfg, dict) and not cfg.get("dsn"):
        table = cfg.get("table") or "SELECT ?"
        if "table" not in cfg:
            # dict 用法缺 table 时报清晰错误
            pass
    res = load_db(cfg)
    if isinstance(res, dict) and "error" in res:
        print("❌", res["error"]); sys.exit(1)
    name, headers, rows = res
    print(f"✅ 读表 {name}: {len(rows)} 行, 列 {headers}")


if __name__ == "__main__":
    main()
