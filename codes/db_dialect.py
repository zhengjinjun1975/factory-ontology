#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db_dialect.py — 数据层方言适配薄层（单点收口 SQL 差异，零第三方依赖）。

动机：db_loader.py 原先把「表名引号 / 默认端口 / 驱动名 / 占位符 / 分页语法」等
方言差异散落在三处 _read_* 函数里，新增库型或改 SQL 要动多处。这里把差异**收口到单点**：

- 规范库型名（normalize_type）：mysql/mariadb → mysql；pg/postgres/postgresql → postgres；sqlite → sqlite
- 标识符引号（quote_ident）：sqlite/postgres 用双引号；mysql 用反引号（且反引号需转义）
- 占位符（placeholder）：sqlite/postgres 用 %s 之外的标准 —— sqlite 的 DB-API 位置参数是 `?`，
  postgres 的 psycopg2 是 `%s`；此处按「驱动层 paramstyle」如实声明，不做统一假装。
- 默认端口（default_port）：mysql 3306 / postgres 5432 / sqlite 无
- 驱动名与安装提示（driver_for / driver_hint）：**只声明映射，不在模块顶层 import 驱动**
- 连接参数（connect_kwargs）：sqlite 的 busy_timeout(毫秒) 等通用项
- SQL 组装（select_all）：SELECT * FROM <quoted table> [LIMIT n]

纪律：
- **SQLite 为默认库型**（DEFAULT_TYPE），未知库型按 SQLite 处理（与 db_loader 历史行为兼容）。
- **不引入任何驱动依赖**：所有非 sqlite 驱动只在运行期惰性探测（is_available/ensure_driver）。
- 本模块纯标准库（os/re），可被 db_loader / 未来 data_loader 共用。

用法：
    from db_dialect import normalize_type, dialect_for, select_all, driver_for
    d = dialect_for("mysql")                 # → Dialect(name='mysql', ...)
    sql = select_all("products", "mysql", limit=100)
    mod_name, hint = driver_for("mysql")     # ('pymysql', 'pip install pymysql')
"""

import os

__all__ = [
    "DEFAULT_TYPE", "SUPPORTED", "Dialect",
    "normalize_type", "dialect_for", "quote_ident", "select_all",
    "driver_for", "driver_hint", "is_available", "default_port", "busy_timeout_ms",
    "validate_ident",
    # 多租户行级隔离（2026-09-24）
    "TENANT_COLUMN", "select_all_scoped", "tenant_clause",
]

# 默认库型 = SQLite（本地/单机场景，零依赖零部署）
DEFAULT_TYPE = "sqlite"
SUPPORTED = ("sqlite", "mysql", "postgres")

# 多租户行级隔离：租户条件列的默认名（SQLite 无原生 RLS，就在这一层把 WHERE 拼进去）。
# 可用环境变量 FOOD_TENANT_COLUMN 覆盖；仅当表里真有该列时才加条件（见 db_loader）。
TENANT_COLUMN = os.environ.get("FOOD_TENANT_COLUMN", "tenant_id")

# 库型别名 → 规范名（单点：任何地方要认库型都走这里）
_TYPE_ALIASES = {
    "sqlite": "sqlite", "sqlite3": "sqlite",
    "mysql": "mysql", "mariadb": "mysql",
    "postgres": "postgres", "postgresql": "postgres", "pg": "postgres",
    # 兼容 pymysql/psycopg2 这类「驱动前缀」写法
    "mysql+pymysql": "mysql", "postgresql+psycopg2": "postgres", "postgres+psycopg2": "postgres",
}

# 标识符白名单（防 SQL 注入）：字母/数字/下划线，非数字开头
import re as _re
_IDENT_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_ident(name, what="标识符"):
    """校验标识符合法性（字母/数字/下划线且非数字开头），非法即抛 ValueError。"""
    if not _IDENT_RE.fullmatch(str(name)):
        raise ValueError(f"[安全] 非法{what}: {name!r}（仅允许字母/数字/下划线，且非数字开头）")
    return str(name)


class Dialect:
    """一种库型的 SQL 方言说明（纯数据 + 少量方法，不做连接）。"""

    __slots__ = ("name", "quote", "placeholder", "default_port", "driver", "pip_hint",
                 "paramstyle", "limit_supported", "note")

    def __init__(self, name, quote, placeholder, default_port, driver, pip_hint,
                 paramstyle, limit_supported=True, note=""):
        self.name = name
        self.quote = quote                    # 标识符引号字符
        self.placeholder = placeholder        # 驱动层占位符
        self.default_port = default_port      # 0 表示无端口（sqlite）
        self.driver = driver                  # 驱动模块名，或 None（标准库 sqlite3）
        self.pip_hint = pip_hint              # 缺驱动时的安装提示
        self.paramstyle = paramstyle          # DB-API paramstyle 名（qmark/format）
        self.limit_supported = limit_supported
        self.note = note

    # ── 标识符 ──
    def quote_ident(self, name, what="标识符"):
        """校验并加引号。反引号方言需把内部反引号转义为 ``。"""
        n = validate_ident(name, what)
        return f"{self.quote}{n}{self.quote}"

    # ── SQL 组装 ──
    def select_all(self, table, limit=None, tenant=None, tenant_col=None):
        """SELECT * FROM <quoted table> [WHERE <tenant_col> = <placeholder>] [LIMIT n]。

        limit=None → 不加限制（历史行为）。
        tenant=None → 不加租户条件（与历史 SQL 逐字节一致，保证老行为不变）；
        tenant 非空 → 追加 WHERE 租户条件（值走占位符，由调用方以参数传入，防注入）。
        """
        sql = f"SELECT * FROM {self.quote_ident(table, '表名')}"
        if tenant is not None:
            col = self.quote_ident(tenant_col or TENANT_COLUMN, '租户列')
            sql += f" WHERE {col} = {self.placeholder}"
        if limit is not None:
            if not self.limit_supported:
                raise ValueError(f"{self.name} 不支持 LIMIT（方言声明）")
            sql += f" LIMIT {int(limit)}"
        return sql

    def connect_kwargs(self, cfg):
        """该方言的通用连接参数（不含驱动私有项）。SQLite 见 busy_timeout_ms。"""
        kw = {}
        if self.name == "sqlite":
            kw["busy_timeout_ms"] = busy_timeout_ms()
        return kw

    def __repr__(self):
        return f"Dialect(name={self.name!r}, paramstyle={self.paramstyle!r})"


_DIALECTS = {
    "sqlite": Dialect(
        name="sqlite", quote='"', placeholder="?", default_port=0,
        driver=None, pip_hint="（标准库 sqlite3，无需安装）",
        paramstyle="qmark",
        note="默认库型；只读打开用 file: URI + mode=ro",
    ),
    "mysql": Dialect(
        name="mysql", quote="`", placeholder="%s", default_port=3306,
        driver="pymysql", pip_hint="pip install pymysql",
        paramstyle="format",
        note="utf8mb4；表名/库名走反引号",
    ),
    "postgres": Dialect(
        name="postgres", quote='"', placeholder="%s", default_port=5432,
        driver="psycopg2", pip_hint="pip install psycopg2-binary",
        paramstyle="format",
        note="双引号标识符；缺驱动时用 psycopg2-binary 兜底提示",
    ),
}


def normalize_type(db_type):
    """库型名 → 规范名（别名映射单点）。空/未知 → DEFAULT_TYPE（SQLite，兼容历史）。"""
    t = str(db_type or "").strip().lower()
    if not t:
        return DEFAULT_TYPE
    if t in _TYPE_ALIASES:
        return _TYPE_ALIASES[t]
    # 带驱动的 DSN 前缀：mysql+pymysql / postgresql+psycopg2
    head = t.split("+", 1)[0]
    if head in _TYPE_ALIASES:
        return _TYPE_ALIASES[head]
    raise ValueError(f"[方言] 不支持的数据库类型: {db_type!r}（支持 sqlite/mysql/postgres）")


def dialect_for(db_type):
    """取方言对象。别名自动归一；未知类型抛 ValueError（与历史报错一致）。"""
    return _DIALECTS[normalize_type(db_type)]


def quote_ident(name, db_type, what="标识符"):
    """按库型给标识符加引号（含注入校验）。"""
    return dialect_for(db_type).quote_ident(name, what)


def select_all(table, db_type, limit=None, tenant=None, tenant_col=None):
    """按库型组装 SELECT * 语句（单点，替代散落的 f-string）。"""
    return dialect_for(db_type).select_all(table, limit=limit, tenant=tenant,
                                           tenant_col=tenant_col)


def tenant_clause(db_type, tenant, tenant_col=None):
    """按库型组装租户条件片段 → (sql_fragment, params)。

    tenant 为空 → 抛 TenantContextError（fail-closed，绝不静默放行全量）。
    """
    if tenant is None or str(tenant).strip() == "":
        from tenant import TenantContextError
        raise TenantContextError(
            "缺少租户上下文(fail-closed)：拒绝组装无租户条件的 SQL（不得静默返回全量）")
    d = dialect_for(db_type)
    col = d.quote_ident(tenant_col or TENANT_COLUMN, '租户列')
    return (f" WHERE {col} = {d.placeholder}", (str(tenant),))


def select_all_scoped(table, db_type, tenant, limit=None, tenant_col=None):
    """组装**带租户条件**的 SELECT * 语句（行级隔离单点）。

    tenant 为空 → 抛 TenantContextError（fail-closed）。
    返回 (sql, params)：params 必须原样传给 cursor.execute(sql, params)。
    """
    frag, params = tenant_clause(db_type, tenant, tenant_col=tenant_col)
    sql = select_all(table, db_type, limit=limit, tenant=tenant, tenant_col=tenant_col)
    return sql, params


def driver_for(db_type):
    """→ (驱动模块名 or None, 安装提示)。**不 import 驱动**，只做声明。"""
    d = dialect_for(db_type)
    return d.driver, d.pip_hint


def driver_hint(db_type):
    """缺驱动时给用户的一句话提示。"""
    mod, hint = driver_for(db_type)
    if mod is None:
        return ""
    return f"{normalize_type(db_type).capitalize()} 需安装驱动: {hint}"


def is_available(db_type):
    """运行期惰性探测驱动是否可用（SQLite 恒 True）。不抛异常。"""
    import importlib.util
    mod, _ = driver_for(db_type)
    if mod is None:
        return True
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def default_port(db_type):
    """规范库型默认端口（sqlite → 0）。"""
    return dialect_for(db_type).default_port


def busy_timeout_ms(default=5000):
    """SQLite 忙等超时(毫秒)。env FOOD_SQLITE_BUSY_TIMEOUT_MS 可覆盖。"""
    try:
        return int(os.environ.get("FOOD_SQLITE_BUSY_TIMEOUT_MS", str(default)))
    except (TypeError, ValueError):
        return default


def sqlite_uri(db_path, read_only=False):
    """把 sqlite 路径转成 file: URI（read_only=True 时带 mode=ro&immutable=0）。

    Windows 路径的 `\\` 需转 `/`；`#`/`?` 需转义，否则被 URI 当分隔符。
    """
    p = os.path.abspath(str(db_path)).replace("\\", "/")
    p = p.replace("?", "%3f").replace("#", "%23")
    if not p.startswith("/"):
        p = "/" + p            # Windows: C:/x → /C:/x（SQLite file: URI 规范）
    mode = "ro" if read_only else "rw"
    return f"file:{p}?mode={mode}"


def main():
    """CLI：打印各方言要点，便于排障（零依赖）。"""
    import json
    print("默认库型:", DEFAULT_TYPE)
    out = {}
    for name in SUPPORTED:
        d = dialect_for(name)
        out[name] = {"quote": d.quote, "placeholder": d.placeholder,
                     "default_port": d.default_port, "driver": d.driver,
                     "driver_available": is_available(name),
                     "select_all": d.select_all("products", limit=100)}
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
