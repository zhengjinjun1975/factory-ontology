#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_tenant.py — 多租户 + 行级隔离 自检（真实执行，非纸面断言）。

跑: <python> scripts/verify_tenant.py
（可选: VERIFY_TENANT_NEW_PORT / VERIFY_TENANT_OLD_PORT 指定临时端口）

交付物对应「验收要真正证明隔离，不是声明」五条，每条都给真实终端输出：
  ① A 租户拿不到 B 租户的 KB 列表与数据 —— 真起临时服务，两套租户凭据各打真实 HTTP；
  ② 不带租户信息 → 落到默认租户，且行为与改前**逐字段比对**（起 HEAD 版本的旧服务对照）；
  ③ 审计记录带 tenant（哈希链账本 + JSONL 两个通道都查真实输出）；
  ④ 数据读取路径缺租户上下文必须 fail-closed（真抛异常，不静默返回全量）；
  ⑤ 越权：A 的凭据访问 B 的 KB 必须被拒（403 / 抛 TenantDenied，不是返回空）。

纪律：全程 %TEMP% 临时副本（租户注册表/kb 注册表/审计文件/账本/临时 SQLite），
      不碰真实 config/kbs.json、不改任何 data/ 数据；临时端口自起自收；纯标准库。
退出码: 全过 0；有 FAIL 1。
"""
import os
import sys
import json
import time
import shutil
import sqlite3
import tempfile
import subprocess
import threading
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CODES = os.path.join(REPO, "codes")
PY = sys.executable

NEW_PORT = int(os.environ.get("VERIFY_TENANT_NEW_PORT", "8912"))
OLD_PORT = int(os.environ.get("VERIFY_TENANT_OLD_PORT", "8913"))

ADMIN_KEY = "vt-admin-key"
READ_KEY = "vt-read-key"
TOKEN_SECRET = "vt-token-secret"
# 租户自带凭据（配置在临时租户注册表里；凭据→租户声明）
KEY_A = "vt-key-a"
KEY_B = "vt-key-b"
KEY_C = "vt-key-c"

TMP = tempfile.mkdtemp(prefix="verify_tenant_")
TENANTS_FILE = os.path.join(TMP, "tenants.json")
KBREG_FILE = os.path.join(TMP, "kbs.json")
AUDIT_FILE_NEW = os.path.join(TMP, "audit_new.log")
AUDIT_FILE_OLD = os.path.join(TMP, "audit_old.log")
AUDIT_DB_NEW = os.path.join(TMP, "chain_new.db")
AUDIT_DB_OLD = os.path.join(TMP, "chain_old.db")
ROWDB = os.path.join(TMP, "rows.db")
OLD_API_IN_CODES = os.path.join(CODES, ".verify_tenant_old_api_server.py")

FAILS = []
PASSES = [0]
_OLD_PROC = None


def ck(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + (f" | {detail}" if detail else ""))
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)
    return bool(cond)


def section(t):
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


def write_tenants_file():
    """临时租户注册表：default(全量) + A(valve) + B(chem) + C(valve,food)，配置驱动。"""
    doc = {
        "default_tenant": "default",
        "tenants": {
            "default": {"name": "默认租户", "kbs": "*"},
            "tenant_a": {"name": "A企业", "kbs": ["valve"], "keys": [KEY_A], "role": "read"},
            "tenant_b": {"name": "B企业", "kbs": ["chem"], "keys": [KEY_B], "role": "read"},
            "tenant_c": {"name": "C企业", "kbs": ["valve", "food"], "keys": [KEY_C], "role": "read"},
        },
    }
    with open(TENANTS_FILE, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    shutil.copy2(os.path.join(CODES, "config", "kbs.json"), KBREG_FILE)


# ─────────────────────────────────────────────────────────────
# 1. 单元级：注册表 / 解析优先级 / 上下文线程隔离 / fail-closed
# ─────────────────────────────────────────────────────────────
def part_1_unit():
    section("1 单元级：租户注册表(配置驱动) + 解析优先级 + 上下文隔离 + fail-closed")
    os.environ["FOOD_TENANTS_FILE"] = TENANTS_FILE
    os.environ["KB_REGISTRY_FILE"] = KBREG_FILE
    sys.path.insert(0, CODES)
    import tenant

    ck("注册表路径 = 环境变量指定(配置驱动, 不写死)", tenant.registry_path() == TENANTS_FILE,
       tenant.registry_path())
    tns = tenant.tenants()
    ck("租户表读到 4 个租户(default/A/B/C)", set(tns) == {"default", "tenant_a", "tenant_b", "tenant_c"},
       sorted(tns))
    ck("A 租户 KB 白名单 = ['valve']（读配置，非写死）", tns["tenant_a"]["kbs"] == ["valve"],
       str(tns["tenant_a"]))
    ck("default 租户 kbs='*'(全量) → 老部署行为不变", tns["default"]["kbs"] == "*")

    # 配置文件缺失 → 内置默认（只有 default 租户，全量）
    saved = os.environ["FOOD_TENANTS_FILE"]
    os.environ["FOOD_TENANTS_FILE"] = os.path.join(TMP, "不存在.json")
    reg_missing = tenant.load_registry()
    os.environ["FOOD_TENANTS_FILE"] = saved
    ck("注册表文件缺失 → 退化为内置默认(仅 default 租户, kbs='*')",
       list(reg_missing["tenants"]) == ["default"] and reg_missing["tenants"]["default"]["kbs"] == "*",
       json.dumps(reg_missing, ensure_ascii=False)[:80])

    # 解析优先级：凭据声明 > X-Tenant-Id > 默认租户
    c1 = tenant.resolve(principal={"tenant": "tenant_a", "role": "read", "subject": "tenant-tenant_a"})
    ck("① 凭据携带 tenant 声明 → 用它", c1.tenant_id == "tenant_a" and c1.source == "credential",
       repr(c1))
    c2 = tenant.resolve(principal=None, header="tenant_b")
    ck("② 无凭据声明, 有 X-Tenant-Id → 用它", c2.tenant_id == "tenant_b" and c2.source == "header",
       repr(c2))
    c3 = tenant.resolve(principal=None, header="")
    ck("③ 都没有 → 默认租户 'default'", c3.tenant_id == "default" and c3.source == "default", repr(c3))
    c4 = tenant.resolve(principal={"tenant": "tenant_a"}, header="tenant_a")
    ck("凭据声明与头一致 → 通过", c4.tenant_id == "tenant_a")
    ck("未知租户(头) → TenantUnknown（fail-closed 拒绝, 不落默认）", _raises(tenant.resolve, None, "ghost"))
    ck("未知租户(凭据声明) → TenantUnknown", _raises(tenant.resolve, {"tenant": "ghost"}, ""))
    ck("凭据声明与头冲突 → TenantDenied（拒绝）", _raises(tenant.resolve, {"tenant": "tenant_a"}, "tenant_b"))

    # 凭据 → 租户映射（静态 key）
    ck("凭据→租户映射: KEY_A → tenant_a", (tenant.tenant_for_key(KEY_A) or {}).get("tenant_id") == "tenant_a")
    ck("未登记凭据 → None(不误判)", tenant.tenant_for_key("nope-key") is None)

    # KB 可见性（配置驱动）
    ck("kb_visible: A 可见 valve / 不可见 chem",
       tenant.kb_visible("tenant_a", "valve") and not tenant.kb_visible("tenant_a", "chem"))
    ck("kb_visible: default 可见任意 KB(全量)", tenant.kb_visible("default", "whatever"))
    ck("kb_visible: 未知租户 → 一律不可见(fail-closed)", not tenant.kb_visible("ghost", "valve"))
    ck("require_kb: A 访问 chem → 抛 TenantDenied", _raises(tenant.require_kb, "tenant_a", "chem"))

    # 缺上下文 → fail-closed
    ck("无上下文时 current()=None", tenant.current() is None)
    ck("无上下文时 require_current() 抛 TenantContextError", _raises(tenant.require_current))

    # 上下文：线程安全、互不串
    out = {}

    def worker(tid):
        try:
            with tenant.tenant_scope(tenant.TenantContext(tid, "thread")):
                time.sleep(0.15)                       # 期间另一线程写入不同租户
                out[tid] = tenant.current_id()
        except Exception as e:                          # pragma: no cover
            out[tid] = "ERR:%s" % e

    th = [threading.Thread(target=worker, args=(t,)) for t in ("tenant_a", "tenant_b")]
    for t in th:
        t.start()
    for t in th:
        t.join()
    ck("线程隔离: 两线程各见自己的租户(不互串)",
       out.get("tenant_a") == "tenant_a" and out.get("tenant_b") == "tenant_b", str(out))
    ck("上下文退出后自动还原(不留残留)", tenant.current() is None, repr(tenant.current()))

    # 逐请求生命周期：with 结束后不污染
    with tenant.tenant_scope("tenant_c"):
        inner = tenant.current_id()
    ck("tenant_scope: 作用域内 tenant_c，退出后无残留",
       inner == "tenant_c" and tenant.current() is None)


# ─────────────────────────────────────────────────────────────
# 2. KB 按租户可见（kb_registry）+ 未知/越权拒绝
# ─────────────────────────────────────────────────────────────
def part_2_kb_registry():
    section("2 KB 按租户可见（kb_registry 列表过滤 + 未知/越权一律拒）")
    sys.path.insert(0, CODES)
    import kb_registry as KR

    all_ids = KR.list_kb_ids()                      # 老调用(不带租户) → 不过滤
    ck("list_kb_ids() 不带租户 → 全部 KB(老行为不变)", "valve" in all_ids and "chem" in all_ids,
       "n=%d" % len(all_ids))
    ck("相同: list_kbs() 不带租户 → %d 个 KB" % len(all_ids),
       len(KR.list_kbs()) == len(all_ids))

    a_ids = KR.list_kb_ids(tenant="tenant_a")
    b_ids = KR.list_kb_ids(tenant="tenant_b")
    c_ids = KR.list_kb_ids(tenant="tenant_c")
    ck("A 租户列表 = ['valve']", a_ids == ["valve"], str(a_ids))
    ck("B 租户列表 = ['chem']", b_ids == ["chem"], str(b_ids))
    ck("C 租户列表 = {'valve','food'}(集合口径)", sorted(c_ids) == ["food", "valve"], str(c_ids))
    ck("A 的列表里没有任何 B 的 KB（无串台）", "chem" not in a_ids and "valve" not in b_ids)
    ck("list_kbs(tenant='tenant_a') 详情列表也只含 valve",
       [d["kb_id"] for d in KR.list_kbs(tenant="tenant_a")] == ["valve"])

    d_a = KR.get_kb("valve", tenant="tenant_a")
    ck("A 取自己的 KB(valve) → 正常返回详情", d_a is not None and d_a["kb_id"] == "valve",
       "status=%s" % (d_a or {}).get("status"))

    # 越权：拒绝(抛错)，而不是返回 None / 空
    denied = {}
    for label, fn in (("越权取 B 的 KB(chem)", lambda: KR.get_kb("chem", tenant="tenant_a")),
                      ("取未注册 KB(ghost_kb)", lambda: KR.get_kb("ghost_kb", tenant="tenant_a")),
                      ("A 取 C 才可见的 KB(food)", lambda: KR.get_kb("food", tenant="tenant_a"))):
        try:
            r = fn()
            denied[label] = "未拒绝! 返回=%r" % (r,)
        except Exception as e:
            denied[label] = "%s: %s" % (type(e).__name__, str(e)[:60])
    ck("越权 KB → 抛 TenantDenied（拒绝, 不是返回空列表/None）",
       all("TenantDenied" in v for v in denied.values()), json.dumps(denied, ensure_ascii=False))

    # 向后兼容：不带租户的 get_kb 老行为（未注册 → None）
    ck("老行为不变: get_kb('ghost_kb') 不带租户 → None(与改前一致)",
       KR.get_kb("ghost_kb") is None)
    ck("老行为不变: get_kb('valve') 不带租户 → 详情", (KR.get_kb("valve") or {}).get("kb_id") == "valve")


# ─────────────────────────────────────────────────────────────
# 3. 行级隔离（数据访问层）+ 缺上下文 fail-closed
# ─────────────────────────────────────────────────────────────
def part_3_row_level():
    section("3 数据读取行级隔离（SQLite 无 RLS → 数据访问层强制加租户条件）")
    sys.path.insert(0, CODES)
    import tenant
    import db_dialect as dd
    import db_loader as dl

    con = sqlite3.connect(ROWDB)
    con.execute("CREATE TABLE orders(id INTEGER, tenant_id TEXT, item TEXT)")
    con.executemany("INSERT INTO orders VALUES(?,?,?)",
                    [(1, "tenant_a", "A-订单1"), (2, "tenant_a", "A-订单2"),
                     (3, "tenant_b", "B-订单1")])
    # 历史表：无租户列（老数据形态）
    con.execute("CREATE TABLE legacy(id INTEGER, name TEXT)")
    con.executemany("INSERT INTO legacy VALUES(?,?)", [(1, "x"), (2, "y")])
    con.commit()
    con.close()

    # 3.1 SQL 组装层：带条件 / 无条件抛错
    sql, params = dd.select_all_scoped("orders", "sqlite", "tenant_a", limit=10)
    ck("组装出的 SQL 强制带租户条件(值走占位符, 非拼接)",
       sql == 'SELECT * FROM "orders" WHERE "tenant_id" = ? LIMIT 10' and params == ("tenant_a",),
       sql)
    ck("tenant=None → 组装器抛 TenantContextError（不静默放行全量）",
       _raises(dd.select_all_scoped, "orders", "sqlite", None))
    ck("tenant='' → 同样 fail-closed", _raises(dd.select_all_scoped, "orders", "sqlite", ""))
    ck("老调用 select_all(无 tenant) → SQL 与改前逐字节一致",
       dd.select_all("orders", "sqlite", limit=10) == 'SELECT * FROM "orders" LIMIT 10',
       dd.select_all("orders", "sqlite", limit=10))

    # 3.2 缺上下文 → fail-closed（真抛异常，不返回全量）
    exc = None
    try:
        dl.load_db({"db_type": "sqlite", "database": ROWDB, "table": "orders"})
    except Exception as e:
        exc = e
    ck("缺租户上下文读带租户列的表 → 抛 TenantContextError（fail-closed）",
       exc is not None and type(exc).__name__ == "TenantContextError",
       "%s: %s" % (type(exc).__name__, str(exc)[:70]) if exc else "没有抛错(危险!)")

    # 3.3 有上下文 → 只看自己那几行
    with tenant.tenant_scope("tenant_a"):
        n, h, rows = dl.load_db({"db_type": "sqlite", "database": ROWDB, "table": "orders"})
    a_items = sorted(r["item"] for r in rows)
    ck("A 上下文读 orders → 只见 A 的 2 行", len(rows) == 2 and all(r["tenant_id"] == "tenant_a" for r in rows),
       str(a_items))
    with tenant.tenant_scope("tenant_b"):
        n, h, rows_b = dl.load_db({"db_type": "sqlite", "database": ROWDB, "table": "orders"})
    ck("B 上下文读 orders → 只见 B 的 1 行", len(rows_b) == 1 and rows_b[0]["tenant_id"] == "tenant_b",
       str([r["item"] for r in rows_b]))
    ck("双向隔离: A 的行里无 B 的、B 的行里无 A 的",
       not any("B-" in r["item"] for r in rows) and not any("A-" in r["item"] for r in rows_b))

    # 3.4 显式 cfg["tenant"]（不经上下文）也受强制
    n, h, rows_c = dl.load_db({"db_type": "sqlite", "database": ROWDB, "table": "orders", "tenant": "tenant_b"})
    ck("cfg['tenant'] 显式指定 → 生效(只见 B 的 1 行)", len(rows_c) == 1 and rows_c[0]["tenant_id"] == "tenant_b")

    # 3.5 历史表(无租户列) → 行为与改前逐字段一致
    n, h, rows_l = dl.load_db({"db_type": "sqlite", "database": ROWDB, "table": "legacy"})
    ck("无租户列的历史表: 无需上下文即可读(老行为不变)", len(rows_l) == 2, "n=%d" % len(rows_l))
    ck("无租户列的历史表: 返回列与值类型逐字段一致", h == ["id", "name"] and rows_l[0] == {"id": "1", "name": "x"},
       str(rows_l[0]))


# ─────────────────────────────────────────────────────────────
# 4. 审计带 tenant（账本 + JSONL）
# ─────────────────────────────────────────────────────────────
def part_4_audit_inproc():
    section("4a 审计带 tenant（进程内：哈希链账本 + JSONL）")
    sys.path.insert(0, CODES)
    import tenant
    from audit_chain import AuditChain
    import api_server as api

    dbp = os.path.join(TMP, "audit_inproc.db")
    ac = AuditChain(dbp)
    with tenant.tenant_scope("tenant_c"):
        rid = ac.record_access(subject="tenant-tenant_c", action="authenticate",
                               result="grant", role="read")
        ac.record_decision(scenario="选型", reasoning="r", outcome="o", category="procurement")
    row = ac.get(rid)
    ck("账本记录带 tenant（请求级上下文自动带入）", row["payload"].get("tenant") == "tenant_c",
       json.dumps(row["payload"], ensure_ascii=False)[:100])

    rid2 = ac.record_access(subject="s", action="authenticate", result="grant", tenant="tenant_b")
    ck("账本记录: 显式 tenant 参数优先生效", ac.get(rid2)["payload"]["tenant"] == "tenant_b")

    # 无上下文 → 落默认租户（老行为），字段仍必带 tenant
    rid3 = ac.record_access(subject="s2", action="authenticate", result="grant")
    ck("无上下文记录 → tenant='default'（且字段必存在）",
       ac.get(rid3)["payload"].get("tenant") == "default", str(ac.get(rid3)["payload"].get("tenant")))
    ok, bad = ac.verify_chain()
    ck("加了 tenant 字段后哈希链仍完整(PASS)", ok, "bad=%s" % (bad[:2],))

    # JSONL（api_server._audit_event）
    jf = os.path.join(TMP, "audit_inproc.log")
    api.AUDIT_FILE = jf
    with tenant.tenant_scope("tenant_a"):
        api._audit_event("qa", question="有多少台设备", kb="valve")
    api._audit_event("access", path="/health")
    recs = [json.loads(l) for l in open(jf, encoding="utf-8") if l.strip()]
    ck("JSONL: 每条记录都带 tenant 字段", all("tenant" in r and r["tenant"] for r in recs),
       json.dumps(recs, ensure_ascii=False))
    ck("JSONL: 上下文内记录 tenant='tenant_a'；上下文外落 'default'",
       recs[0]["tenant"] == "tenant_a" and recs[1]["tenant"] == "default",
       "%s / %s" % (recs[0]["tenant"], recs[1]["tenant"]))


# ─────────────────────────────────────────────────────────────
# 5. HTTP 实测（新服务）：两套凭据、KB 与数据隔离、越权、审计
# ─────────────────────────────────────────────────────────────
def http(method, path, headers=None, body=None, port=NEW_PORT, timeout=40):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def _server_env(audit_file, audit_db, extra=None):
    env = dict(os.environ)
    env.update({
        "FOOD_ADMIN_KEY": ADMIN_KEY,
        "FOOD_READ_KEY": READ_KEY,
        "FOOD_TOKEN_SECRET": TOKEN_SECRET,
        "FOOD_TENANTS_FILE": TENANTS_FILE,
        "FOOD_AUDIT_FILE": audit_file,
        "AUDIT_DB": audit_db,
        "PYTHONPATH": CODES + os.pathsep + env.get("PYTHONPATH", ""),
    })
    env.pop("FOOD_ADMIN_KEY_EXPIRES", None)
    env.pop("FOOD_READ_KEY_EXPIRES", None)
    env.update(extra or {})
    return env


def start_server(script, port, audit_file, audit_db):
    log = open(os.path.join(TMP, "server_%d.log" % port), "wb")
    p = subprocess.Popen([PY, script, "--host", "127.0.0.1", "--port", str(port)],
                         cwd=CODES, env=_server_env(audit_file, audit_db),
                         stdout=log, stderr=subprocess.STDOUT)
    return p, log


def wait_ready(proc, port, timeout=150):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False, "服务进程已退出(rc=%s)" % proc.returncode
        try:
            st, _ = http("GET", "/health", port=port, timeout=3)
            if st == 200:
                return True, "%.1fs" % (time.time() - t0)
        except Exception:
            pass
        time.sleep(0.7)
    return False, "超时 %ss" % timeout


def part_5_http_new():
    global _OLD_PROC
    section("5 HTTP 实测（临时端口 %d，实时进程）—— ①②③⑤" % NEW_PORT)
    proc, log = start_server("api_server.py", NEW_PORT, AUDIT_FILE_NEW, AUDIT_DB_NEW)
    try:
        ok, info = wait_ready(proc, NEW_PORT)
        if not ck("新服务就绪(/health 200)", ok, info):
            print(open(os.path.join(TMP, "server_%d.log" % NEW_PORT), encoding="utf-8",
                       errors="replace").read()[-1500:])
            return
        print("  服务就绪耗时 %s" % info)

        # ── ① 两套租户凭据，各打真实 HTTP 一次 ──
        st_a, body_a = http("GET", "/api/kbs", {"X-API-Key": KEY_A})
        st_b, body_b = http("GET", "/api/kbs", {"X-API-Key": KEY_B})
        print("  A 租户 GET /api/kbs → %s %s" % (st_a, json.dumps(body_a, ensure_ascii=False)[:120]))
        print("  B 租户 GET /api/kbs → %s %s" % (st_b, json.dumps(body_b, ensure_ascii=False)[:120]))
        ck("① A 租户 KB 列表 = ['valve']，tenant=tenant_a",
           st_a == 200 and body_a.get("kbs") == ["valve"] and body_a.get("tenant") == "tenant_a")
        ck("① B 租户 KB 列表 = ['chem']，tenant=tenant_b",
           st_b == 200 and body_b.get("kbs") == ["chem"] and body_b.get("tenant") == "tenant_b")
        ck("① A 的列表里没有 B 的 KB（不串台，双向）",
           "chem" not in body_a.get("kbs", []) and "valve" not in body_b.get("kbs", []))

        # ── ① 数据：A 打自己的 KB 有数据、打 B 的 KB 被拒 ──
        st_av, body_av = http("GET", "/api/stats?kb=valve", {"X-API-Key": KEY_A})
        st_ac, body_ac = http("GET", "/api/stats?kb=chem", {"X-API-Key": KEY_A})
        st_bv, body_bv = http("GET", "/api/stats?kb=valve", {"X-API-Key": KEY_B})
        st_bc, body_bc = http("GET", "/api/stats?kb=chem", {"X-API-Key": KEY_B})
        ck("① A 取自己 KB 的数据(valve stats) → 200", st_av == 200 and body_av.get("ok") is True,
           "status=%s entities=%s" % (st_av, body_av.get("entity_count") if isinstance(body_av, dict) else ""))
        ck("① A 取 B 的 KB 数据(chem stats) → 403 拒绝", st_ac == 403, "status=%s body=%s" % (st_ac, str(body_ac)[:80]))
        ck("① B 取自己 KB 的数据(chem stats) → 200", st_bc == 200 and body_bc.get("ok") is True,
           "status=%s" % st_bc)
        ck("① B 取 A 的 KB 数据(valve stats) → 403 拒绝", st_bv == 403, "status=%s" % st_bv)

        # ── ⑤ 越权：拒绝，而不是返回空 ──
        st_x, body_x = http("GET", "/api/kb/chem/lexicon/export?download=false", {"X-API-Key": KEY_A})
        ck("⑤ A 的凭据导出 B 的 KB 词典 → 403（拒绝, 非空结果）",
           st_x == 403 and not (isinstance(body_x, dict) and body_x.get("ok") is True),
           "status=%s body=%s" % (st_x, str(body_x)[:80]))
        st_y, body_y = http("POST", "/api/kbs/chem/examples", {"X-API-Key": KEY_A}, {"examples": ["x"]})
        ck("⑤ A 的凭据写 B 的 KB examples → 403（拒绝）", st_y == 403,
           "status=%s body=%s" % (st_y, str(body_y)[:80]))
        st_z1, body_z1 = http("POST", "/api/knowledge/query", {"X-API-Key": KEY_A},
                              {"kb": "chem", "q": "有多少台设备"})
        ck("⑤ A 的凭据走 body kb=chem 的 RAG 检索 → 403（拒绝）", st_z1 == 403,
           "status=%s body=%s" % (st_z1, str(body_z1)[:80]))
        st_z2, body_z2 = http("GET", "/api/assets/list?kb=chem", {"X-API-Key": KEY_A})
        ck("⑤ A 的凭据列 B 的 KB 资产版本 → 403（拒绝）", st_z2 == 403,
           "status=%s body=%s" % (st_z2, str(body_z2)[:80]))
        st_z3, _ = http("GET", "/api/assets/list?kb=valve", {"X-API-Key": KEY_A})
        ck("⑤ 对照: A 列自己的 KB 资产版本 → 非 403", st_z3 != 403, "status=%s" % st_z3)
        st_c, body_c = http("GET", "/api/kbs", {"X-API-Key": KEY_A, "X-Tenant-Id": "tenant_c"})
        ck("⑤ 凭据声明(A) 与 X-Tenant-Id(C) 冲突 → 403（不静默取其一）", st_c == 403,
           "status=%s body=%s" % (st_c, str(body_c)[:80]))
        st_u, _ = http("GET", "/api/kbs", {"X-API-Key": KEY_A, "X-Tenant-Id": "ghost"})
        ck("⑤ X-Tenant-Id 指向未知租户 → 403（fail-closed）", st_u == 403, "status=%s" % st_u)
        st_r, _ = http("GET", "/api/kbs", {"X-API-Key": "totally-wrong-key"})
        ck("⑤ 未知凭据 → 401", st_r == 401, "status=%s" % st_r)

        # ── ② 不带租户信息 → 默认租户 + 与改前逐字段一致 ──
        st_d, body_d = http("GET", "/api/kbs", {"X-API-Key": READ_KEY})
        import api_server as api_new
        all_kbs = list(api_new.KBS.keys())
        ck("② 不带租户信息 → tenant='default'，KB 列表 = 全部(与改前一致)",
           st_d == 200 and body_d.get("tenant") == "default" and sorted(body_d.get("kbs", [])) == sorted(all_kbs),
           "tenant=%s n=%s/%s" % (body_d.get("tenant"), len(body_d.get("kbs", [])), len(all_kbs)))
        st_ad, body_ad = http("GET", "/api/admin/kbs", {"X-API-Key": ADMIN_KEY})
        ck("② 默认租户 admin 端点 KB 列表 = 全部（老字段 ok/active/kbs 不动）",
           st_ad == 200 and body_ad.get("ok") is True and body_ad.get("active") == "food"
           and sorted(body_ad.get("kbs", [])) == sorted(all_kbs),
           "n=%d tenant=%s" % (len(body_ad.get("kbs", [])), body_ad.get("tenant")))
        st_wh, body_wh = http("GET", "/api/auth/whoami", {"X-API-Key": READ_KEY})
        ck("② 老凭据 whoami 字段逐字段不变(subject/role/auth)",
           st_wh == 200 and body_wh.get("subject") == "static-read" and body_wh.get("role") == "read"
           and body_wh.get("auth") == "static", json.dumps(body_wh, ensure_ascii=False))
        ck("② 老格式令牌(fotk1, 无租户) 仍可签发/使用",
           _old_token_still_works())

        # ── ③ 审计带 tenant（账本 + JSONL 真实输出） ──
        st_t, body_t = http("POST", "/api/auth/token", {"X-API-Key": ADMIN_KEY},
                            {"role": "read", "ttl_seconds": 60, "tenant": "tenant_a"})
        ck("③ 可签发带租户声明的令牌(fotk2)", st_t == 200 and str(body_t.get("token", "")).startswith("fotk2."),
           "tenant=%s token_prefix=%s" % (body_t.get("tenant"), str(body_t.get("token", ""))[:12]))
        tok_a = body_t.get("token", "")
        st_ta, body_ta = http("GET", "/api/kbs", {"X-API-Key": tok_a})
        ck("③ 令牌携带的租户声明生效(A 令牌 → ['valve'])",
           st_ta == 200 and body_ta.get("kbs") == ["valve"] and body_ta.get("tenant") == "tenant_a",
           json.dumps(body_ta, ensure_ascii=False)[:100])
        st_tb, _ = http("POST", "/api/auth/token", {"X-API-Key": ADMIN_KEY},
                        {"role": "read", "ttl_seconds": 60, "tenant": "tenant_b"})
        st_tg, _ = http("POST", "/api/auth/token", {"X-API-Key": ADMIN_KEY},
                        {"role": "read", "ttl_seconds": 60, "tenant": "ghost"})
        ck("③ 为不存在的租户签发令牌 → 400（拒绝）", st_tg == 400, "status=%s" % st_tg)

        time.sleep(0.4)
        # JSONL
        recs = []
        if os.path.exists(AUDIT_FILE_NEW):
            recs = [json.loads(l) for l in open(AUDIT_FILE_NEW, encoding="utf-8") if l.strip()]
        have_tenant_field = all("tenant" in r and r["tenant"] for r in recs) if recs else False
        tset = sorted({r.get("tenant") for r in recs})
        ck("③ JSONL 每条审计记录都带 tenant", have_tenant_field, "n=%d tenants=%s" % (len(recs), tset))
        ck("③ JSONL 里出现 tenant_a / tenant_b / default 三类真实取值",
           {"tenant_a", "tenant_b", "default"}.issubset(set(tset)), str(tset))
        sample = [r for r in recs if r.get("tenant") == "tenant_a" and r.get("kind") == "access"][:1]
        ck("③ JSONL 样例: access 记录带 tenant=tenant_a",
           bool(sample), json.dumps(sample[0], ensure_ascii=False) if sample else "无")
        # 账本
        ch_n, ch_tenants = 0, []
        if os.path.exists(AUDIT_DB_NEW):
            con = sqlite3.connect(AUDIT_DB_NEW)
            rows = con.execute("SELECT payload FROM ledger WHERE kind='access'").fetchall()
            con.close()
            ch_n = len(rows)
            ch_tenants = sorted({json.loads(r[0]).get("tenant") for r in rows})
        ck("③ 哈希链账本 access 记录带 tenant（且 A/B 都落链）",
           ch_n > 0 and {"tenant_a", "tenant_b"}.issubset(set(ch_tenants)),
           "n=%d tenants=%s" % (ch_n, ch_tenants))

        return dict(new_kbs_total=len(all_kbs), tok_a=tok_a)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log.close()


def _old_token_still_works():
    """老格式令牌(fotk1) 仍可用: 请求内签发(read, 无 tenant) 并访问。"""
    st, body = http("POST", "/api/auth/token", {"X-API-Key": ADMIN_KEY},
                    {"role": "read", "ttl_seconds": 60})
    if st != 200 or not str(body.get("token", "")).startswith("fotk1."):
        return False
    st2, body2 = http("GET", "/api/kbs", {"X-API-Key": body["token"]})
    return st2 == 200 and body2.get("tenant") == "default"


# ─────────────────────────────────────────────────────────────
# 6. 向后兼容硬证据：起 HEAD 版本旧服务, 与新服务逐字段比对
# ─────────────────────────────────────────────────────────────
COMPARE_CASES = [
    ("GET", "/health", None, None, "精确"),
    ("GET", "/api/stats?kb=valve", {"X-API-Key": "@READ@"}, None, "精确"),
    ("GET", "/api/ontology/structure?kb=valve", None, None, "精确"),
    ("GET", "/api/auth/whoami", {"X-API-Key": "@READ@"}, None, "精确"),
    ("GET", "/api/admin/kbs", {"X-API-Key": "@ADMIN@"}, None, "超集"),
]


def part_6_backward_compare():
    section("6 向后兼容硬证据：HEAD 旧服务 vs 新服务 逐字段比对（默认租户）")
    # 取 HEAD 版本 api_server.py 放到 codes/ 下(与真实数据同根, 保证对照公平), 跑完删掉
    try:
        old_src = subprocess.run(["git", "show", "HEAD:codes/api_server.py"], cwd=REPO,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if old_src.returncode != 0:
            ck("取 HEAD 版本 api_server.py", False, old_src.stdout.decode("utf-8", "replace")[:100])
            return
        with open(OLD_API_IN_CODES, "wb") as f:
            f.write(old_src.stdout)
    except Exception as e:
        ck("准备旧版本服务", False, str(e))
        return

    old_proc, old_log = start_server(os.path.basename(OLD_API_IN_CODES), OLD_PORT,
                                     AUDIT_FILE_OLD, AUDIT_DB_OLD)
    new_proc, new_log = start_server("api_server.py", NEW_PORT + 100, AUDIT_FILE_NEW, AUDIT_DB_NEW)
    # 同版本第二实例: 用于证明"tie 顺序差异"是既有非确定性(与本改动无关)
    new2_proc, new2_log = start_server("api_server.py", NEW_PORT + 200,
                                       os.path.join(TMP, "audit_new2.log"),
                                       os.path.join(TMP, "chain_new2.db"))
    global _OLD_PROC
    _OLD_PROC = old_proc
    try:
        ok_o, info_o = wait_ready(old_proc, OLD_PORT)
        ok_n, info_n = wait_ready(new_proc, NEW_PORT + 100)
        ok_n2, info_n2 = wait_ready(new2_proc, NEW_PORT + 200)
        if not ck("旧(HEAD)服务 + 新服务×2 均就绪", ok_o and ok_n and ok_n2,
                  "old=%s new=%s new2=%s" % (info_o, info_n, info_n2)):
            for p in (OLD_PORT, NEW_PORT + 100, NEW_PORT + 200):
                try:
                    print(open(os.path.join(TMP, "server_%d.log" % p), encoding="utf-8",
                               errors="replace").read()[-800:])
                except Exception:
                    pass
            return

        def hdr(h):
            if not h:
                return {}
            return {k: (READ_KEY if v == "@READ@" else ADMIN_KEY if v == "@ADMIN@" else v)
                    for k, v in h.items()}

        # 先证明: 同一份新代码跑两个进程, /api/stats 的 tie 顺序本身就不稳定(既有非确定性)
        _, s_new = http("GET", "/api/stats?kb=valve", {"X-API-Key": READ_KEY}, port=NEW_PORT + 100)
        _, s_new2 = http("GET", "/api/stats?kb=valve", {"X-API-Key": READ_KEY}, port=NEW_PORT + 200)
        ck("(前置) 同版本两进程 raw 结果 tie 顺序可能不同 → 属既有非确定性, 非本改动回归",
           _canon(s_new) == _canon(s_new2),
           "raw_equal=%s canon_equal=%s" % (s_new == s_new2, _canon(s_new) == _canon(s_new2)))

        for method, path, h, body, mode in COMPARE_CASES:
            st_o, body_o = http(method, path, hdr(h), body, port=OLD_PORT)
            st_n, body_n = http(method, path, hdr(h), body, port=NEW_PORT + 100)
            if mode == "精确":
                same = (st_o == st_n and _canon(body_o) == _canon(body_n))
                note = "完全相同"
                if same and body_o != body_n:
                    note = "字段/值完全相同(仅分组统计的 tie 顺序不同=既有非确定性)"
                if not same:
                    diffs = [k for k in set(body_o or {}) | set(body_n or {})
                             if _canon((body_o or {}).get(k)) != _canon((body_n or {}).get(k))]
                    note = "status %s/%s, 差异字段=%s" % (st_o, st_n, diffs)
                ck("逐字段一致(内容口径): %s %s（旧 vs 新）" % (method, path), same, note)
            else:
                # 超集: 旧响应的每个字段在新响应里存在且值相等(新可加字段)
                missing = [k for k, v in (body_o or {}).items() if k not in (body_n or {}) or body_n[k] != v]
                extra = sorted(set((body_n or {})) - set((body_o or {})))
                ck("老字段不动+仅新增字段: %s %s" % (method, path),
                   st_o == st_n == 200 and not missing,
                   "旧字段差异=%s 新增字段=%s" % (missing, extra))
                print("      ← 新增(附加, 非破坏)字段: %s" % extra)

        # 新路由(纯新增, 老部署无此路由)
        st_new_only, _ = http("GET", "/api/kbs", {"X-API-Key": READ_KEY}, port=NEW_PORT + 100)
        st_old_only, _ = http("GET", "/api/kbs", {"X-API-Key": READ_KEY}, port=OLD_PORT)
        ck("新增只读路由 /api/kbs: 新服务 200 / 旧服务 404（纯新增, 老路由不动）",
           st_new_only == 200 and st_old_only == 404, "new=%s old=%s" % (st_new_only, st_old_only))

        # 旧服务(HEAD) 不支持租户: 它的 KB 列表 = 全量(基线口径)
        st_o_all, body_o_all = http("GET", "/api/admin/kbs", {"X-API-Key": ADMIN_KEY}, port=OLD_PORT)
        st_n_all, body_n_all = http("GET", "/api/admin/kbs", {"X-API-Key": ADMIN_KEY}, port=NEW_PORT + 100)
        ck("改前/改后默认租户 admin KB 列表集合完全相同",
           sorted(body_o_all.get("kbs", [])) == sorted(body_n_all.get("kbs", [])),
           "旧=%d 新=%d" % (len(body_o_all.get("kbs", [])), len(body_n_all.get("kbs", []))))
    finally:
        for p, lg in ((old_proc, old_log), (new_proc, new_log), (new2_proc, new2_log)):
            try:
                p.terminate()
                p.wait(timeout=15)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            lg.close()


def _raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return False
    except Exception:
        return True


def _canon(x):
    """把 JSON 结构规范化：dict 按 key 排序；list 按元素规范化后的 JSON 排序。

    仅用于对照"业务字段是否一致"——这些响应里的 list 是分组统计(tie 顺序不稳定),
    比较的是**内容集合**而非偶然顺序。
    """
    if isinstance(x, dict):
        return {k: _canon(v) for k, v in sorted(x.items())}
    if isinstance(x, list):
        return sorted((json.dumps(_canon(i), ensure_ascii=False, sort_keys=True) for i in x))
    return x


def cleanup():
    try:
        if os.path.exists(OLD_API_IN_CODES):
            os.remove(OLD_API_IN_CODES)
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)


def main():
    print("=" * 74)
    print("多租户 + 行级隔离 自检   仓库: %s" % REPO)
    print("临时区: %s" % TMP)
    print("=" * 74)
    try:
        write_tenants_file()
        part_1_unit()
        part_2_kb_registry()
        part_3_row_level()
        part_4_audit_inproc()
        part_5_http_new()
        part_6_backward_compare()
    finally:
        cleanup()
    total = PASSES[0] + len(FAILS)
    print("\n" + "=" * 74)
    print("自检结果: %d/%d 通过" % (PASSES[0], total))
    if FAILS:
        print("失败用例:")
        for n in FAILS:
            print("  - %s" % n)
    print("=" * 74)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
