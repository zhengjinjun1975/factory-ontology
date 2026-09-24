#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_hardening.py — 商用级加固自检（真实执行，非纸面断言）。

跑: <python> scripts/verify_hardening.py
（或: cd codes && python ../scripts/verify_hardening.py）

三块，全部真实跑：
  A. 鉴权：先单元级(令牌过期/身份解析/fail-closed)，再**起临时端口 8899 实测**三例
     —— 无 token 必须 401、错 token 必须 401、对 token 必须放行；
     附：read 不能进 admin 端点、签发的短期令牌可用、过期令牌被拒、审计含主体/时间/动作/结果。
  B. 方言层单测：db_dialect 归一/引号/占位符/默认端口/SQL 组装/注入拦截 + db_loader 回归。
  C. 并发小压测：多线程打 HTTP 端点给出 p50/p95/max；并发写哈希链审计账本验证忙等不炸。

退出码: 全过 0；有 FAIL 1。输出全部真实测量，不编数字。
只用标准库（与项目零依赖纪律一致）。不触碰真实数据/服务：临时端口 + %TEMP% 临时库。
"""
import os
import sys
import json
import time
import math
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
PORT = int(os.environ.get("VERIFY_PORT", "8899"))
BASE = f"http://127.0.0.1:{PORT}"

ADMIN_KEY = "vh-admin-key"
READ_KEY = "vh-read-key"
TOKEN_SECRET = "vh-token-secret"

TMP = tempfile.mkdtemp(prefix="verify_hardening_")
AUDIT_FILE = os.path.join(TMP, "audit.log")
AUDIT_DB = os.path.join(TMP, "chain.db")

FAILS = []
PASSES = [0]


def ck(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + (f" | {detail}" if detail else ""))
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)
    return bool(cond)


def section(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


def http(method, path, headers=None, body=None, timeout=30):
    """返回 (status, json_or_text)。不发请求异常时抛。"""
    url = BASE + path
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


# ─────────────────────────────────────────────────────────────
# A-1  单元级：令牌/身份/过期（in-process，env 先设好）
# ─────────────────────────────────────────────────────────────
def part_a_unit():
    section("A-1 鉴权单元级（令牌验签 / 过期 / 身份解析 / fail-closed 语义）")
    os.environ["FOOD_ADMIN_KEY"] = ADMIN_KEY
    os.environ["FOOD_READ_KEY"] = READ_KEY
    os.environ["FOOD_TOKEN_SECRET"] = TOKEN_SECRET
    os.environ["FOOD_AUDIT_FILE"] = AUDIT_FILE
    os.environ["FOOD_ADMIN_KEY_EXPIRES"] = "2999-01-01T00:00:00+00:00"   # 未过期
    os.environ["FOOD_READ_KEY_EXPIRES"] = "2000-01-01T00:00:00+00:00"    # 已过期(用于验证过期比对)
    sys.path.insert(0, CODES)
    import api_server as api

    # 静态 admin key 正常 → admin 主体
    p = api._principal(ADMIN_KEY)
    ck("admin key → 主体 static-admin/role=admin",
       p and p.get("role") == "admin" and p.get("auth") == "static", repr(p))

    # 静态 read key 已配过期(2000年) → 必须被拒
    p = api._principal(READ_KEY)
    ck("已过期的静态 read key → 拒绝(denied_reason 非空)",
       p and p.get("denied_reason"), repr(p))

    # 未知 key → None(fail-closed)
    ck("未知 key → None(拒绝)", api._principal("totally-wrong") is None)
    ck("空 key → None(拒绝)", api._principal("") is None)

    # 临时放开 read 过期，验证 read key 正常路径
    api._KEY_EXPIRES["read"] = "2999-01-01T00:00:00+00:00"
    p = api._principal(READ_KEY)
    ck("read key(未过期) → 主体 static-read/role=read",
       p and p.get("role") == "read" and p.get("auth") == "static", repr(p))

    # 签发令牌
    tok, exp = api.make_token("read", 60)
    ck("签发令牌格式 fotk1.<role>.<exp>.<sig>",
       tok.startswith("fotk1.read.") and len(tok.split(".")) == 4, tok[:28] + "...")
    ck("签发令牌 exp ≈ now+60", abs(exp - (int(time.time()) + 60)) <= 2, f"exp={exp}")
    p = api._principal(tok)
    ck("有效令牌 → role=read/auth=token", p and p["role"] == "read" and p["auth"] == "token", repr(p))

    # 篡改签名 → 拒
    bad = tok[:-4] + ("aaaa" if not tok.endswith("aaaa") else "bbbb")
    ck("签名被篡改的令牌 → 拒绝", api._verify_token(bad) is None)

    # 过期令牌 → denied_reason 非空
    etok, eexp = api.make_token("admin", -10)
    p = api._principal(etok)
    ck("已过期令牌 → 拒绝(令牌已过期)", p and p.get("denied_reason") == "令牌已过期", repr(p))

    # 令牌角色不能越权: read 令牌 role=read
    rtok, _ = api.make_token("read", 60)
    p = api._principal(rtok)
    ck("read 令牌 role != admin(管理端点将被 require_admin 拒)", p and p["role"] != "admin")

    # 未配 TOKEN_SECRET 时不签发
    saved = api.TOKEN_SECRET
    api.TOKEN_SECRET = ""
    try:
        api.make_token("read", 60)
        ck("未配 secret 时 make_token 应报错", False, "未抛错")
    except ValueError as e:
        ck("未配 secret 时 make_token 报错", True, str(e)[:30])
    ck("未配 secret 时任何 fotk1 令牌验签失败(fail-closed)", api._verify_token(tok) is None)
    api.TOKEN_SECRET = saved

    # Bearer 解析
    ck("_extract_key 支持 Authorization: Bearer",
       api._extract_key("", "Bearer abc.def") == "abc.def")
    ck("_extract_key 优先 X-API-Key", api._extract_key("K1", "Bearer K2") == "K1")

    # 审计函数存在且写明动作/结果
    ck("审计留存期 ≥6 个月(默认天数 ≥183)", api._AUDIT_RETENTION_DAYS >= 183,
       f"_AUDIT_RETENTION_DAYS={api._AUDIT_RETENTION_DAYS}")
    return api


# ─────────────────────────────────────────────────────────────
# A-2  HTTP 实测：临时端口 8899（无 token 拒 / 错 token 拒 / 对 token 放行）
# ─────────────────────────────────────────────────────────────
def start_server():
    env = dict(os.environ)
    env.update({
        "FOOD_ADMIN_KEY": ADMIN_KEY,
        "FOOD_READ_KEY": READ_KEY,
        "FOOD_TOKEN_SECRET": TOKEN_SECRET,
        "FOOD_AUDIT_FILE": AUDIT_FILE,
        "AUDIT_DB": AUDIT_DB,
        "PYTHONPATH": CODES + os.pathsep + env.get("PYTHONPATH", ""),
    })
    env.pop("FOOD_ADMIN_KEY_EXPIRES", None)
    env.pop("FOOD_READ_KEY_EXPIRES", None)
    log = open(os.path.join(TMP, "server.log"), "wb")
    p = subprocess.Popen([PY, "api_server.py", "--host", "127.0.0.1", "--port", str(PORT)],
                         cwd=CODES, env=env, stdout=log, stderr=subprocess.STDOUT)
    return p, log


def wait_ready(proc, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False, "服务进程已退出"
        try:
            st, _ = http("GET", "/health", timeout=3)
            if st == 200:
                return True, f"{time.time()-t0:.1f}s"
        except Exception:
            pass
        time.sleep(0.7)
    return False, f"超时 {timeout}s"


def part_a_http():
    section(f"A-2 鉴权 HTTP 实测（临时端口 {PORT}，实时进程）")
    proc, log = start_server()
    try:
        ok, info = wait_ready(proc)
        if not ck("服务就绪(/health 200)", ok, info):
            log.flush()
            print("  --- server.log 尾部 ---")
            print(open(os.path.join(TMP, "server.log"), encoding="utf-8", errors="replace").read()[-1500:])
            return
        print(f"  服务就绪耗时 {info}")

        # ① 无 token 必须拒
        st, _ = http("GET", "/api/stats")
        ck("① 无 token → 401(拒绝)", st == 401, f"status={st}")
        st, _ = http("GET", "/api/trace/forward?batch=B001")
        ck("① 无 token(trace) → 401", st == 401, f"status={st}")

        # ② 错 token 必须拒
        st, _ = http("GET", "/api/stats", {"X-API-Key": "wrong-token"})
        ck("② 错 token → 401(拒绝)", st == 401, f"status={st}")
        st, _ = http("GET", "/api/stats", {"X-API-Key": "fotk1.read.9999999999.deadbeef"})
        ck("② 伪造令牌 → 401(拒绝)", st == 401, f"status={st}")

        # ③ 对 token 则放行
        st, body = http("GET", "/api/stats", {"X-API-Key": READ_KEY})
        ck("③ 正确 read key → 200(放行)", st == 200, f"status={st}")

        # 角色边界: read 不能进 admin
        st, _ = http("GET", "/api/admin/kbs", {"X-API-Key": READ_KEY})
        ck("read key 进 admin 端点 → 401", st == 401, f"status={st}")
        st, body = http("GET", "/api/admin/kbs", {"X-API-Key": ADMIN_KEY})
        ck("admin key 进 admin 端点 → 200", st == 200, f"status={st}, kbs={body.get('kbs') if isinstance(body, dict) else body}")

        # 签发短期令牌 → 用它访问
        st, body = http("POST", "/api/auth/token", {"X-API-Key": ADMIN_KEY},
                        {"role": "read", "ttl_seconds": 60})
        ck("POST /api/auth/token(admin) → 200 + 令牌", st == 200 and isinstance(body, dict) and body.get("token"),
           f"status={st}, expires_at={body.get('expires_at') if isinstance(body, dict) else ''}")
        tok = body["token"] if isinstance(body, dict) else ""
        st, _ = http("GET", "/api/stats", {"X-API-Key": tok})
        ck("签发的令牌可用(X-API-Key) → 200", st == 200, f"status={st}")
        st, _ = http("GET", "/api/auth/whoami", {"Authorization": "Bearer " + tok})
        ck("Authorization: Bearer 令牌 → 200 且逐请求校验", st == 200, f"status={st}")

        # 过期令牌(负 ttl 由 admin 签发的等价物: 用进程内私钥造) → 拒
        st, body = http("POST", "/api/auth/token", {"X-API-Key": ADMIN_KEY},
                        {"role": "read", "ttl_seconds": 0})
        ck("ttl_seconds=0 → 400(参数校验)", st == 400, f"status={st}")

        # 越权 secret 缺失场景略(in-process 已测)

        # 审计检查: 主体/时间/动作/结果
        time.sleep(0.3)
        recs = []
        if os.path.exists(AUDIT_FILE):
            with open(AUDIT_FILE, encoding="utf-8") as f:
                recs = [json.loads(l) for l in f if l.strip()]
        login = [r for r in recs if r.get("kind") == "login"]
        have_deny = [r for r in login if r.get("result") == "deny"]
        have_grant = [r for r in login if r.get("result") == "grant"]
        ck("审计含 login 事件", len(login) > 0, f"login={len(login)} 条")
        ck("审计 deny 记录含 主体/时间/动作/结果",
           bool(have_deny) and all(k in have_deny[0] for k in ("subject", "ts", "action", "result")),
           json.dumps(have_deny[0], ensure_ascii=False) if have_deny else "无")
        ck("审计 grant 记录含 主体/时间/动作/结果",
           bool(have_grant) and all(k in have_grant[0] for k in ("subject", "ts", "action", "result")),
           json.dumps(have_grant[0], ensure_ascii=False) if have_grant else "无")
        ck("审计记录 subject 有具体主体(非空)",
           any(r.get("subject") for r in login),
           ",".join(sorted({str(r.get("subject")) for r in login})))
        ck("issue_token 动作已审计", any(r.get("action") == "issue_token" for r in recs),
           "有" if any(r.get("action") == "issue_token" for r in recs) else "无")

        # 哈希链审计账本(防删改) — 由服务进程写入
        chain_ok = False
        chain_n = 0
        if os.path.exists(AUDIT_DB):
            try:
                con = sqlite3.connect(AUDIT_DB)
                chain_n = con.execute("SELECT COUNT(*) FROM ledger WHERE kind='access'").fetchone()[0]
                con.close()
                chain_ok = chain_n > 0
            except Exception as e:
                chain_ok = False
        ck("鉴权事件进入哈希链账本(kind=access)", chain_ok, f"access 条数={chain_n}")

        # C 部分 并发压测 复用这个服务
        part_c_load(READ_KEY)
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


# ─────────────────────────────────────────────────────────────
# B 方言层单测
# ─────────────────────────────────────────────────────────────
def part_b_dialect():
    section("B 数据层方言适配薄层单测（db_dialect + db_loader 回归）")
    sys.path.insert(0, CODES)
    import db_dialect as dd
    import db_loader as dl

    ck("默认库型 = sqlite", dd.DEFAULT_TYPE == "sqlite", dd.DEFAULT_TYPE)
    ck("别名归一: PostgreSQL→postgres", dd.normalize_type("PostgreSQL") == "postgres")
    ck("别名归一: 空→sqlite", dd.normalize_type("") == "sqlite")
    ck("别名归一: mysql+pymysql→mysql", dd.normalize_type("mysql+pymysql") == "mysql")
    ck("别名归一: pg→postgres", dd.normalize_type("pg") == "postgres")
    ck("非法库型报错", _raises(dd.normalize_type, "oracle"))

    ck('SQLite 引号 = 双引号', dd.quote_ident("products", "sqlite") == '"products"',
       dd.quote_ident("products", "sqlite"))
    ck("MySQL 引号 = 反引号", dd.quote_ident("products", "mysql") == "`products`",
       dd.quote_ident("products", "mysql"))
    ck("Postgres 引号 = 双引号", dd.quote_ident("products", "postgres") == '"products"')

    ck("标识符注入拦截(分号)", _raises(dd.quote_ident, "a;DROP TABLE x", "sqlite"))
    ck("标识符注入拦截(空格/引号)", _raises(dd.quote_ident, 'a" OR 1=1', "sqlite"))

    ck("默认端口 mysql=3306", dd.default_port("mysql") == 3306)
    ck("默认端口 postgres=5432", dd.default_port("postgres") == 5432)
    ck("默认端口 sqlite=0(无端口)", dd.default_port("sqlite") == 0)

    mod_mys, hint_mys = dd.driver_for("mysql")
    ck("驱动映射 mysql→pymysql(仅声明, 不 import)", mod_mys == "pymysql" and "pymysql" in hint_mys,
       f"{mod_mys} / {hint_mys}")
    ck("驱动映射 sqlite→None(标准库)", dd.driver_for("sqlite")[0] is None)

    ck("SQL 组装 sqlite", dd.select_all("equipment", "sqlite", limit=5) == 'SELECT * FROM "equipment" LIMIT 5',
       dd.select_all("equipment", "sqlite", limit=5))
    ck("SQL 组装 mysql", dd.select_all("equipment", "mysql") == "SELECT * FROM `equipment`",
       dd.select_all("equipment", "mysql"))
    ck("limit=None 不加 LIMIT(保持历史全表行为)",
       "LIMIT" not in dd.select_all("equipment", "sqlite"))

    src = open(os.path.join(CODES, "db_dialect.py"), encoding="utf-8").read()
    ck("db_dialect 顶层无 import pymysql/psycopg2",
       "\nimport pymysql" not in src and "\nimport psycopg2" not in src)

    # ── db_loader 回归（临时 SQLite 库）──
    dbp = os.path.join(TMP, "dialect_test.db")
    con = sqlite3.connect(dbp)
    con.execute("CREATE TABLE equipment(id INTEGER, name TEXT, kw REAL)")
    con.executemany("INSERT INTO equipment VALUES(?,?,?)",
                    [(i, f"设备{i}", i * 1.5) for i in range(1, 8)])
    con.commit()
    con.close()

    name, headers, rows = dl.load_db({"db_type": "sqlite", "database": dbp, "table": "equipment"})
    ck("db_loader SQLite 全表读取", name == "equipment" and headers == ["id", "name", "kw"] and len(rows) == 7,
       f"{name} {headers} n={len(rows)}")
    ck("db_loader 行值统一转 str", all(isinstance(v, str) for v in rows[0].values()), repr(rows[0]))
    name, headers, rows = dl.load_db({"db_type": "sqlite", "database": dbp, "table": "equipment", "limit": 3})
    ck("db_loader limit 生效", len(rows) == 3, f"n={len(rows)}")
    name, headers, rows = dl.load_db({"dsn": "sqlite:///" + dbp.replace("\\", "/"), "table": "equipment"})
    ck("db_loader DSN 写法可用", len(rows) == 7, f"n={len(rows)}")
    ck("db_loader 表名注入拦截", _raises(dl.load_db, {"db_type": "sqlite", "database": dbp, "table": "x; DROP"}))
    r = dl.load_db({"db_type": "mysql", "table": "t"})
    ck("db_loader 缺配置 → 错误 dict(回归 e2e 期望)", isinstance(r, dict) and r.get("error"), str(r)[:60])
    r = dl.load_db({"db_type": "oracle", "table": "t"})
    ck("db_loader 不支持库型 → 错误 dict", isinstance(r, dict) and "不支持" in str(r.get("error")), str(r)[:60])

    ck("sqlite 只读 URI 形式正确", dd.sqlite_uri(dbp, read_only=True).endswith("?mode=ro"),
       dd.sqlite_uri(dbp, read_only=True))

    # 只读打开实测: 写被拒
    con3 = sqlite3.connect(dd.sqlite_uri(dbp, read_only=True), uri=True)
    def _try_write():
        con3.execute("INSERT INTO equipment VALUES(99,'x',1)")
    ck("只读 URI 打开后写入被拒", _raises(_try_write), "只读模式生效")
    con3.close()


def _raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return False
    except Exception:
        return True


# ─────────────────────────────────────────────────────────────
# C 并发小压测
# ─────────────────────────────────────────────────────────────
def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(math.ceil(p / 100.0 * len(s))) - 1))
    return s[k]


def part_c_load(read_key):
    """并发打只读端点(带 token)，给出 p50/p95/max；再并发写哈希链账本。"""
    print("\n  C-1 HTTP 并发小压测（8 线程 × 3 轮 × 10 请求 = 240 次 /api/stats）")
    threads, rounds, per = 8, 3, 10
    lat = []
    codes = []
    lock = threading.Lock()

    def worker():
        local = []
        localc = []
        for _ in range(rounds * per):
            t0 = time.perf_counter()
            try:
                st, _b = http("GET", "/api/stats", {"X-API-Key": read_key}, timeout=30)
            except Exception:
                st = -1
            local.append((time.perf_counter() - t0) * 1000.0)
            localc.append(st)
        with lock:
            lat.extend(local)
            codes.extend(localc)

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.perf_counter() - t0
    ok_n = sum(1 for c in codes if c == 200)
    ck(f"并发压测全部 200(240/240)", ok_n == threads * rounds * per, f"200={ok_n}, 其它={len(codes)-ok_n}")
    print(f"  HTTP: n={len(lat)} 200={ok_n} wall={wall:.2f}s "
          f"qps={len(lat)/wall:.1f} | p50={pct(lat,50):.1f}ms p95={pct(lat,95):.1f}ms "
          f"p99={pct(lat,99):.1f}ms max={max(lat):.1f}ms")
    ck("HTTP p95 在合理区间(<3000ms)", pct(lat, 95) < 3000, f"p95={pct(lat,95):.1f}ms")

    print("\n  C-2 并发写哈希链审计账本（8 线程 × 25 条，测忙等/锁）")
    from audit_chain import AuditChain
    dbp = os.path.join(TMP, "concurrent_chain.db")
    ac = AuditChain(dbp)
    errs = []
    nper = 25

    def writer(i):
        try:
            for j in range(nper):
                ac.record_access(subject=f"t{i}", action="authenticate", result="grant", role="read")
        except Exception as e:
            errs.append(f"{type(e).__name__}: {e}")

    tw = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    t1 = time.perf_counter()
    for t in tw:
        t.start()
    for t in tw:
        t.join()
    wtime = time.perf_counter() - t1
    ok, bad = ac.verify_chain()
    n = ac._count()
    ck("并发写审计链无异常", not errs, str(errs[:2]))
    ck("并发写条数 = 8×25 = 200", n == 8 * nper, f"n={n}")
    ck("并发写后哈希链完整(PASS)", ok, f"bad={bad[:2]}")
    print(f"  审计链并发写: n={n} wall={wtime:.2f}s 吞吐={n/wtime:.0f} 条/s (每次操作短连接+忙等)")
    print(f"  sqlite3 默认 busy_timeout = {sqlite3.connect(':memory:').execute('PRAGMA busy_timeout').fetchone()[0]}ms "
          f"(db_dialect 显式设为 {_bt()}ms)")


def _bt():
    sys.path.insert(0, CODES)
    import db_dialect as dd
    return dd.busy_timeout_ms()


# ─────────────────────────────────────────────────────────────
# D 热路径证据扫描（只读，供报告引用）
# ─────────────────────────────────────────────────────────────
def part_d_hotpaths():
    section("D 热路径证据扫描（只读; 仅列清单, 本脚本不改算法）")
    pats = {
        "api_server.py": [
            ("def _find(", "全图线性扫描 O(V)/次"),
            ("for bi, rels in graph.items():", "遍历全图 O(V)"),
            ("_resolve_readable(ab[", "循环内再调 _find → O(B×V)"),
            ("for k, rec in qd.items():", "每次请求全量扫 QDATA"),
        ],
        "ontology_qa_v3.py": [
            ("for n, d in data.items()", "全量实体扫描/问"),
            ("if _uri in k.lower()", "子串扫描全量 key"),
        ],
    }
    for fn, items in pats.items():
        path = os.path.join(CODES, fn)
        if not os.path.exists(path):
            continue
        lines = open(path, encoding="utf-8").read().splitlines()
        print(f"\n  [{fn}]")
        for needle, why in items:
            hits = [i + 1 for i, l in enumerate(lines) if needle in l]
            if hits:
                print(f"    L{','.join(map(str,hits[:8]))}{' ...' if len(hits)>8 else ''}  ×{len(hits)}  {needle!r}  —— {why}")
            else:
                print(f"    (未命中) {needle!r}")


# ─────────────────────────────────────────────────────────────
def main():
    print("verify_hardening —— 商用级加固自检")
    print(f"仓库: {REPO}")
    print(f"解释器: {PY}")
    print(f"临时目录: {TMP}")
    try:
        part_a_unit()
        part_a_http()
        part_b_dialect()
        part_d_hotpaths()
    finally:
        pass
    section("结果汇总")
    print(f"  通过 {PASSES[0]} 项, 失败 {len(FAILS)} 项")
    if FAILS:
        print("  失败项:")
        for f in FAILS:
            print("    - " + f)
    print(f"\n  临时产物: {TMP} （可删; 真实仓库数据未被改动）")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
