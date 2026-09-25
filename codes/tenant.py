#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tenant.py — 多租户：租户解析 / 租户注册表 / 请求级租户上下文（纯标准库，零第三方依赖）。

商业化的硬门槛是「能按租户卖」：数据/知识库/审计必须能按客户隔离，而不是「一套代码大家共用」。
SQLite 没有原生 RLS，本模块提供三件事，其余隔离在数据访问层（db_dialect / db_loader）与
注册表层（kb_registry）落地：

  ① 租户解析（resolve）——优先级严格如下，逐请求计算，**不读任何全局可变登录态**：
       1) 凭据/令牌携带的 tenant 声明（令牌 fotk2.<role>.<tenant>.<exp>.<sig>，或注册表 keys→tenant）
       2) X-Tenant-Id 请求头
       3) 都没有 → 默认租户（默认 id = `default`，老部署不带租户信息时行为与今天一致）
     声明的租户必须存在于注册表，否则 fail-closed 拒绝（TenantUnknown/TenantDenied）。

  ② 租户注册表（配置驱动，不写死）——读 codes/config/tenants.json（可用 FOOD_TENANTS_FILE 覆盖）：
       { "default_tenant": "default",
         "tenants": {
           "default":  {"name": "默认租户", "kbs": "*"},
           "tenant_a": {"name": "A企业", "kbs": ["valve"], "keys": ["k-a"], "role": "read"}
         } }
     · kbs 为 "*" 表示全部 KB；为列表表示该租户可见的 KB id 白名单。
     · keys 为该租户自带的 API 凭据（凭据→租户声明的映射）。
     **文件缺失 → 内置默认注册表（只有 default 租户，kbs="*"）**，老部署行为逐字段不变。

  ③ 请求级租户上下文（contextvars）——线程/协程安全，随请求生命周期：
       set_current(ctx) / current() / current_id() / require_current() / tenant_scope(...)
     绝不用模块级可变全局量互串：contextvars 在「线程 / async 任务」维度天然隔离。

fail-closed 纪律：
  缺租户上下文时，`require_current()` 抛 TenantContextError；数据访问层在拼接 SQL 前调用它
  （见 db_dialect.select_all_scoped / db_loader），**宁可报错，也不静默返回全量**。

用法：
    import tenant
    ctx = tenant.resolve(principal={"tenant": "tenant_a"}, header="")
    tok = tenant.set_current(ctx)
    try:
        ...  # 请求处理
    finally:
        tenant.reset(tok)
    # 或
    with tenant.tenant_scope(tenant.TenantContext("tenant_a", "test")):
        ...
"""
import os
import sys
import json
import hmac
import contextvars
import contextlib

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TENANT = "default"
DEFAULT_REGISTRY = os.path.join(ROOT, "config", "tenants.json")
ENV_FILE = "FOOD_TENANTS_FILE"          # 指向注册表文件（自检脚本用 %TEMP% 副本）
ENV_TENANT_COLUMN = "FOOD_TENANT_COLUMN"  # 行级隔离用的租户列名（默认 tenant_id）
DEFAULT_TENANT_COLUMN = os.environ.get(ENV_TENANT_COLUMN, "tenant_id")

__all__ = [
    "DEFAULT_TENANT", "DEFAULT_TENANT_COLUMN",
    "TenantError", "TenantUnknown", "TenantDenied", "TenantContextError",
    "TenantContext",
    "registry_path", "load_registry", "save_registry", "default_tenant_id",
    "tenants", "get_tenant", "kb_visible", "require_kb", "tenant_for_key",
    "resolve",
    "current", "current_id", "require_current", "set_current", "reset",
    "tenant_scope", "tenant_column",
]


# ── 异常（全部继承 TenantError，调用方可一把兜住）──────────────────────────────
class TenantError(Exception):
    """租户相关错误基类。"""


class TenantUnknown(TenantError):
    """声明的租户不存在于注册表（fail-closed 拒绝）。"""


class TenantDenied(TenantError):
    """越权：租户存在，但访问的 KB/资源不在其可见范围。"""


class TenantContextError(TenantError):
    """缺少租户上下文（数据访问层 fail-closed）。"""


# ── 注册表（配置驱动）─────────────────────────────────────────────────────────
# 内置默认注册表：只有 default 租户且 kbs="*"。文件缺失/损坏时用它 → 老部署行为不变。
_BUILTIN = {
    "default_tenant": DEFAULT_TENANT,
    "tenants": {
        DEFAULT_TENANT: {"name": "默认租户(未启用多租户时的既有部署)", "kbs": "*"},
    },
}


def registry_path(path=None):
    """解析注册表文件路径：显式入参 > 环境变量 FOOD_TENANTS_FILE > 默认 config/tenants.json。

    （与 kb_registry.registry_path 同口径：环境变量只给自检脚本指临时副本用，生产路径不变。）
    """
    if path:
        return os.path.abspath(path)
    return os.path.abspath(os.environ.get(ENV_FILE) or DEFAULT_REGISTRY)


def _normalize(doc):
    """把注册表文档规整为 {'default_tenant': str, 'tenants': {id: {...}}}。非法输入退化为内置默认。"""
    if not isinstance(doc, dict):
        return json.loads(json.dumps(_BUILTIN))
    tns = doc.get("tenants")
    if not isinstance(tns, dict) or not tns:
        return json.loads(json.dumps(_BUILTIN))
    out = {
        "default_tenant": str(doc.get("default_tenant") or DEFAULT_TENANT),
        "tenants": {},
    }
    for tid, entry in tns.items():
        e = dict(entry) if isinstance(entry, dict) else {}
        out["tenants"][str(tid)] = e
    # 默认租户必须存在，否则补一个全量可见的 default（避免老部署被配置错误锁死）
    if out["default_tenant"] not in out["tenants"]:
        out["tenants"][out["default_tenant"]] = {"name": "默认租户", "kbs": "*"}
    return out


def load_registry(path=None):
    """读取租户注册表。文件缺失/损坏 → 内置默认（不抛，保证老部署可用）。"""
    try:
        with open(registry_path(path), encoding="utf-8") as f:
            return _normalize(json.load(f))
    except Exception:
        return json.loads(json.dumps(_BUILTIN))


def save_registry(doc, path=None):
    """写回租户注册表（保留顶层其它字段）。只对给定路径生效。"""
    p = registry_path(path)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data.update(_normalize(doc))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return p


def tenants(path=None):
    """→ {tenant_id: entry}。"""
    return load_registry(path)["tenants"]


def default_tenant_id(path=None):
    """默认租户 id（配置驱动，缺省 'default'）。"""
    return load_registry(path)["default_tenant"]


def get_tenant(tenant_id, path=None):
    """单租户配置 dict；不存在 → None。"""
    if not tenant_id:
        return None
    return tenants(path).get(str(tenant_id).strip())


def _ct_eq(a, b):
    """常量时间字符串比较（防时序侧信道）。"""
    try:
        return hmac.compare_digest(str(a), str(b))
    except Exception:
        return str(a) == str(b)


def tenant_for_key(key, path=None):
    """凭据(API key) → 租户声明 dict {tenant_id, role, name}；未命中/未配置 → None。

    这是「凭据携带的 tenant 声明」的静态 key 版本：每租户在注册表里配自己的 keys。
    """
    if not key:
        return None
    for tid, t in tenants(path).items():
        for k in (t.get("keys") or []):
            if _ct_eq(key, k):
                return {"tenant_id": tid, "role": str(t.get("role") or "read"),
                        "name": t.get("name", "")}
    return None


def kb_visible(tenant_id, kb, path=None):
    """该租户是否可见某 KB（配置驱动）。未知租户 → 一律不可见（fail-closed）。"""
    t = get_tenant(tenant_id, path)
    if t is None:
        return False
    scope = t.get("kbs", "*")
    if isinstance(scope, str):
        return scope.strip().lower() in ("*", "all", "")
    try:
        return str(kb) in {str(x) for x in (scope or [])}
    except Exception:
        return False


def require_kb(tenant_id, kb, path=None):
    """校验租户对 KB 的可见性：不可见即抛 TenantDenied（拒绝，绝不返回空列表）。"""
    if not kb_visible(tenant_id, kb, path):
        raise TenantDenied("越权/未知 KB: 租户 %r 不可见 KB %r" % (tenant_id, kb))
    return True


def tenant_column(default=None):
    """行级隔离用的租户列名（注册表顶层 tenant_column > env > 默认 tenant_id）。"""
    col = load_registry().get("tenant_column")
    return str(col or default or DEFAULT_TENANT_COLUMN)


# ── 租户上下文（contextvars：线程 / 协程安全，无全局可变互串）──────────────────
_CTX = contextvars.ContextVar("factory_tenant_ctx", default=None)


class TenantContext(object):
    """一次请求 / 一段作用域内的租户上下文。不可变（无 setter），避免被下游改写串台。"""

    __slots__ = ("tenant_id", "source", "role", "subject")

    def __init__(self, tenant_id, source="explicit", role="", subject=""):
        self.tenant_id = str(tenant_id)
        self.source = str(source or "")
        self.role = str(role or "")
        self.subject = str(subject or "")

    def as_dict(self):
        return {"tenant_id": self.tenant_id, "source": self.source,
                "role": self.role, "subject": self.subject}

    def __repr__(self):
        return "TenantContext(tenant_id=%r, source=%r)" % (self.tenant_id, self.source)


def current():
    """当前租户上下文 TenantContext，或 None（未建立）。"""
    return _CTX.get()


def current_id(default=None):
    """当前租户 id；未建立上下文 → default（入参，通常 None）。"""
    c = _CTX.get()
    return c.tenant_id if c is not None else default


def require_current(msg=None):
    """要求存在租户上下文，否则抛 TenantContextError（数据访问层 fail-closed 用）。"""
    c = _CTX.get()
    if c is None or not c.tenant_id:
        raise TenantContextError(
            msg or "缺少租户上下文(fail-closed)：拒绝在无租户条件下读写数据/SQL")
    return c


def set_current(ctx):
    """写入租户上下文，返回 token（配合 reset 用）。ctx 可为 TenantContext / str / None。"""
    if ctx is None or isinstance(ctx, TenantContext):
        return _CTX.set(ctx)
    return _CTX.set(TenantContext(ctx))


def reset(token):
    """恢复到 set_current 之前的状态。"""
    try:
        _CTX.reset(token)
    except Exception:
        pass


@contextlib.contextmanager
def tenant_scope(ctx):
    """with 语法糖：进入时建立租户上下文，退出时恢复（异常也恢复）。"""
    tok = set_current(ctx)
    try:
        yield ctx
    finally:
        reset(tok)


# ── 解析（凭据声明 > X-Tenant-Id > 默认租户）──────────────────────────────────
def resolve(principal=None, header=None, path=None):
    """解析请求租户 → TenantContext。

    principal: 鉴权主体 dict（可含 "tenant" 声明，来自令牌或 keys→tenant 映射）
    header:    X-Tenant-Id 头的值
    优先级：凭据 tenant 声明 > X-Tenant-Id > 默认租户。
    声明的租户不存在 → TenantUnknown；凭据声明与头冲突 → TenantDenied（fail-closed）。
    """
    claim = None
    if isinstance(principal, dict):
        claim = principal.get("tenant")
    claim = str(claim).strip() if claim else ""
    hdr = str(header or "").strip()

    if claim:
        if get_tenant(claim, path) is None:
            raise TenantUnknown("未知租户(凭据声明): %r" % claim)
        if hdr and hdr != claim:
            raise TenantDenied("租户冲突: 凭据声明 %r 与 X-Tenant-Id %r 不一致" % (claim, hdr))
        role = (principal or {}).get("role", "") if isinstance(principal, dict) else ""
        subject = (principal or {}).get("subject", "") if isinstance(principal, dict) else ""
        return TenantContext(claim, "credential", role=role, subject=subject)

    if hdr:
        if get_tenant(hdr, path) is None:
            raise TenantUnknown("未知租户(X-Tenant-Id): %r" % hdr)
        return TenantContext(hdr, "header")

    return TenantContext(default_tenant_id(path), "default")


def _main():
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        reg = load_registry()
        print("租户注册表: %s" % registry_path())
        print("默认租户: %s" % reg["default_tenant"])
        print("  %-16s %-10s %-28s %s" % ("tenant_id", "role", "kbs", "name"))
        for tid, t in reg["tenants"].items():
            scope = t.get("kbs", "*")
            scope_s = scope if isinstance(scope, str) else ",".join(map(str, scope))
            print("  %-16s %-10s %-28s %s" % (tid, t.get("role", "read"), scope_s, t.get("name", "")))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
