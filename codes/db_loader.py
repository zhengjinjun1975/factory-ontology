#!/usr/bin/env python3
"""db_loader.py — 企业 ERP/MES 数据库直连（统一单表读取：SQLite / MySQL / PostgreSQL）

让知识库直接读企业 ERP/MES 的常见数据库，而非仅 CSV/Excel 文件。
支持三种连接写法，且统一返回 (表名, 列名列表, 行dict列表)：

1) DSN 连接串（推荐，便于 config 一键配置）:
   - SQLite:     sqlite:///C:/path/factory.db
   - MySQL:      mysql+pymysql://user:***@127.0.0.1:3306/erp
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

方言差异（引号/占位符/默认端口/驱动名/SQL 组装）已收口到 db_dialect.py 单点；
SQLite 默认库型，读默认以只读方式打开 + busy_timeout 忙等，避免误写与「database is locked」。
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
import sys as _sys
if ROOT not in _sys.path:
    _sys.path.insert(0, ROOT)

# 单点方言层（纯标准库，不引任何驱动依赖）
import db_dialect as _dd


def _current_tenant_id():
    """当前租户 id（来自 tenant.py 的请求级上下文）；未建立任何租户上下文 → None。

    None 用于触发数据访问层的 fail-closed：带租户列的表若缺上下文，宁可报错也不返回全量。
    """
    try:
        import tenant as _t
        return _t.current_id()
    except Exception:
        return None


def _sqlite_table_columns(conn, table):
    """PRAGMA table_info → 列名列表（真实探测表结构，决定是否需要行级租户过滤）。"""
    q = _dd.dialect_for("sqlite").quote_ident(table, "表名")
    cur = conn.execute("PRAGMA table_info(%s)" % q)
    return [r[1] for r in cur.fetchall()]


def _safe(name, what="表名"):
    """校验标识符合法性，非法即抛错（防 SQL 注入）。保留旧函数名供外部调用。"""
    _dd.validate_ident(name, what)
    return name


def parse_dsn(dsn):
    """解析 DSN 连接串 → (db_type, host, port, user, password, database)。"""
    import re as _re
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
        raise ValueError(f"[安全] 无法解析 DSN: {dsn!r}（支持 sqlite:///path、mysql://u:***@h:p/db、postgresql://u:***@h:p/db）")
    try:
        db_type = _dd.normalize_type(m.group("db_type") or "mysql")
    except ValueError as e:
        raise ValueError(str(e))
    return (db_type, m.group("host"), int(m.group("port") or 0),
            m.group("user") or "", m.group("password") or "", m.group("db"))


def _read_sqlite(db_path, table, limit=None, read_only=True, tenant=None, tenant_col=None):
    """读 SQLite 单表。默认只读打开 + busy_timeout 忙等（不影响返回值）。

    行级隔离（多租户，2026-09-24）：若表含租户列（默认 tenant_id），**强制**加租户条件；
    此时缺租户上下文 → 抛 TenantContextError（fail-closed，绝不静默返回全量）。
    不含租户列的历史表 → SQL 与改前逐字节一致（老行为不变）。
    """
    import sqlite3
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"SQLite 库不存在: {db_path}")
    db_type = "sqlite"
    timeout_s = _dd.busy_timeout_ms() / 1000.0
    conn = None
    if read_only:
        # 只读 URI 打开：即便后续误执行写语句也会被拒；失败则回退 rw 保持兼容。
        try:
            conn = sqlite3.connect(_dd.sqlite_uri(db_path, read_only=True),
                                   uri=True, timeout=timeout_s)
        except Exception:
            conn = None
    if conn is None:
        conn = sqlite3.connect(db_path, timeout=timeout_s)
    try:
        conn.execute(f"PRAGMA busy_timeout={_dd.busy_timeout_ms()}")
        col = tenant_col or _dd.TENANT_COLUMN
        try:
            has_tenant = col in _sqlite_table_columns(conn, table)
        except Exception:
            has_tenant = False
        if has_tenant:
            tid = tenant if tenant is not None else _current_tenant_id()
            # 缺租户上下文即 fail-closed（select_all_scoped 内部抛 TenantContextError）
            sql, params = _dd.select_all_scoped(table, db_type, tid,
                                                limit=limit, tenant_col=col)
            cur = conn.execute(sql, params)
        else:
            sql = _dd.select_all(table, db_type, limit=limit)
            cur = conn.execute(sql)
        headers = [d[0] for d in cur.description]
        rows = [dict(zip(headers, ["" if x is None else str(x) for x in row]))
                for row in cur.fetchall()]
    finally:
        conn.close()
    return table, headers, rows


def _read_mysql(cfg, limit=None, tenant=None):
    mod, hint = _dd.driver_for("mysql")
    if not _dd.is_available("mysql"):
        return {"error": f"MySQL 需安装驱动: {hint}"}
    # 配置缺字段时返回错误 dict(而非 KeyError), 让调用方按统一错误通道处理。
    _missing = [k for k in ("host", "user", "database") if not cfg.get(k)]
    if _missing:
        return {"error": f"MySQL 配置缺字段: {', '.join(_missing)}"}
    import pymysql
    dialect = _dd.dialect_for("mysql")
    port = cfg.get("port") or dialect.default_port
    conn = pymysql.connect(host=cfg["host"], port=port, user=cfg["user"],
                           password=cfg.get("password", ""), database=cfg["database"],
                           charset="utf8mb4",  # MySQL 专用；其它库型无此参数
                           connect_timeout=int(cfg.get("connect_timeout") or 10))
    try:
        cur = conn.cursor()
        # 行级隔离：仅当配置显式声明 tenant_col 时加租户条件（否则视为历史非分区表）。
        tenant_col = cfg.get("tenant_col")
        if tenant_col:
            tid = tenant if tenant is not None else _current_tenant_id()
            sql, params = _dd.select_all_scoped(cfg["table"], "mysql", tid,
                                               limit=limit, tenant_col=tenant_col)
            cur.execute(sql, params)
        else:
            cur.execute(dialect.select_all(cfg["table"], limit=limit))  # 单点组装, 表名已白名单
        headers = [d[0] for d in cur.description]
        rows = [dict(zip(headers, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return (cfg["table"], headers, rows)


def _read_postgres(cfg, limit=None, tenant=None):
    mod, hint = _dd.driver_for("postgres")
    if not _dd.is_available("postgres"):
        return {"error": f"PostgreSQL 需安装驱动: {hint}"}
    _missing = [k for k in ("host", "user", "database") if not cfg.get(k)]
    if _missing:
        return {"error": f"PostgreSQL 配置缺字段: {', '.join(_missing)}"}
    import psycopg2
    dialect = _dd.dialect_for("postgres")
    port = cfg.get("port") or dialect.default_port
    conn = psycopg2.connect(host=cfg["host"], port=port, user=cfg["user"],
                            password=cfg.get("password", ""), dbname=cfg["database"],
                            connect_timeout=int(cfg.get("connect_timeout") or 10))
    try:
        cur = conn.cursor()
        # 行级隔离：同 MySQL，仅当配置显式声明 tenant_col 时加租户条件。
        tenant_col = cfg.get("tenant_col")
        if tenant_col:
            tid = tenant if tenant is not None else _current_tenant_id()
            sql, params = _dd.select_all_scoped(cfg["table"], "postgres", tid,
                                               limit=limit, tenant_col=tenant_col)
            cur.execute(sql, params)
        else:
            cur.execute(dialect.select_all(cfg["table"], limit=limit))  # 单点组装
        headers = [d[0] for d in cur.description]
        rows = [dict(zip(headers, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return (cfg["table"], headers, rows)


def load_db(cfg):
    """从 SQLite/MySQL/PostgreSQL 读一张表。cfg 可为 DSN 串或 dict。

    返回 (表名, 列名列表, 行dict列表)；驱动缺失或类型不支持时返回 {"error": ...}。
    可选 limit: 只取前 N 行（默认 None = 全表，保持历史行为）。

    多租户行级隔离（2026-09-24）：可选 cfg["tenant"] / cfg["tenant_col"]。
    · 表含租户列（默认 tenant_id）时**强制**加租户条件；cfg 未给 tenant 则取请求级
      租户上下文；两者都没有 → 抛 TenantContextError（fail-closed，不静默返回全量）。
    · 不含租户列的历史表 → 行为与改前逐字段一致。
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

    try:
        # 兼容历史: dict 配置缺 db_type 时旧行为按 mysql 处理（保留, 不改成 sqlite, 免得静默换库型）
        db_type = _dd.normalize_type(cfg.get("db_type") or "mysql")
    except ValueError as e:
        return {"error": str(e)}
    table = _safe(cfg["table"], "表名")

    limit = cfg.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return {"error": f"limit 必须为整数: {cfg.get('limit')!r}"}

    # 多租户：显式 cfg["tenant"] 优先；缺省(None) 由数据访问层回退到请求级租户上下文。
    tenant = cfg.get("tenant") if "tenant" in cfg else None
    tenant_col = cfg.get("tenant_col")

    if db_type == "sqlite":
        # 只读 + busy_timeout；read_only 可由配置覆盖（默认 True）
        read_only = cfg.get("read_only", True)
        return _read_sqlite(cfg.get("database") or "", table, limit=limit,
                            read_only=bool(read_only), tenant=tenant, tenant_col=tenant_col)

    cfg["table"] = table
    if db_type == "mysql":
        cfg["database"] = _safe(cfg.get("database"), "库名")
        return _read_mysql(cfg, limit=limit, tenant=tenant)
    if db_type == "postgres":
        cfg["database"] = _safe(cfg.get("database"), "库名")
        return _read_postgres(cfg, limit=limit, tenant=tenant)

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
        if "table" not in cfg:
            # dict 用法缺 table 时报清晰错误
            print("❌ 配置缺 table 字段"); sys.exit(1)
    res = load_db(cfg)
    if isinstance(res, dict) and "error" in res:
        print("❌", res["error"]); sys.exit(1)
    name, headers, rows = res
    print(f"✅ 读表 {name}: {len(rows)} 行, 列 {headers}")


if __name__ == "__main__":
    main()
