#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_token_guard.py — 第3轮「令牌吊销/刷新 + 限速防爆破」自检（真实执行，全走真实 HTTP）。

跑: <python> scripts/verify_token_guard.py
（可选: VERIFY_TG_PORT 覆盖临时端口，默认 8951）

真实验证（每条都给终端输出）：
  ① 默认配置语义：阈值/窗口/封禁时长默认 10/300s/300s；状态目录默认 <repo>/var/auth_state，
     **不在**任何 KB 数据目录(codes/data*)下（直接核验实现）。
  ② 吊销：签发带 jti 的 fotk3 令牌 → 用通(200) → 按 jti 吊销 → **立即 401**；
     按主体批量吊销 → 同名主体多令牌全部 401。
  ③ 刷新轮替：用后即废；轮替出的**新刷新令牌可用**；**旧刷新令牌复用 → 401 且审计 refresh_reuse**。
  ④ 限速/防爆破：突发失败超阈值 → 429；**已封禁期间，正常凭据请求仍 200（不误伤）**；
     封禁/解除均写审计；手动 unban 后计数清零 → 过期/解除后再试回到 401。
  ⑤ 不误伤：并发正常业务/问答请求（含 /api/ask）全部非 429。
  ⑥ 状态目录有界：真实产生令牌/失败后，读数体积/条数；并用硬上限(max_rows)证明有界清理。
  ⑦ 兼容底线：默认签发**仍是 fotk1 老格式(无 jti)**，且老令牌照常可用。

纪律：全程 %TEMP% 临时副本；不碰真实 data/ 与 config；临时端口自起自收；纯标准库。
退出码: 全过 0；有 FAIL 1。
"""
import os
import sys
import json
import time
import shutil
import tempfile
import subprocess
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CODES = os.path.join(REPO, "codes")
PY = sys.executable
PORT = int(os.environ.get("VERIFY_TG_PORT", "8951"))

ADMIN_KEY = "vtg-admin-key"
READ_KEY = "vtg-read-key"
TOKEN_SECRET = "vtg-token-secret"
# 限速测试用小阈值/短封禁，便于快速观察到封禁与自动解除；同时核验"可配"。
THRESHOLD = 3
BAN_SECONDS = 5
WINDOW = 60

TMP = tempfile.mkdtemp(prefix="verify_token_guard_")
STATE = os.path.join(TMP, "auth_state")
AUDIT = os.path.join(TMP, "audit.log")
CHAIN = os.path.join(TMP, "chain.db")

FAILS = []
PASSES = [0]


def ck(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ((" | " + str(detail)) if detail else ""))
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)
    return bool(cond)


def section(t):
    print("\n" + "=" * 76)
    print(t)
    print("=" * 76)


# ─────────────────────────────────────────────────────────────
def http(method, path, headers=None, body=None, port=PORT, timeout=90):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = json.dumps(body).encode("utf-8") if (body is not None and not isinstance(body, bytes)) else body
    req = urllib.request.Request(url, data=data, method=method)
    if not isinstance(body, bytes):
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
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            raw = ""
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def issue(**kw):
    body = {"role": "read"}
    body.update(kw)
    st, b = http("POST", "/api/auth/token", {"X-API-Key": ADMIN_KEY}, body)
    return st, b


def audit_records():
    recs = []
    try:
        with open(AUDIT, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        recs.append(json.loads(ln))
                    except Exception:
                        pass
    except Exception:
        pass
    return recs


# ─────────────────────────────────────────────────────────────
def _server_env(extra=None):
    env = dict(os.environ)
    env.update({
        "FOOD_ADMIN_KEY": ADMIN_KEY, "FOOD_READ_KEY": READ_KEY,
        "FOOD_TOKEN_SECRET": TOKEN_SECRET,
        "FOOD_AUTH_STATE_DIR": STATE,
        "FOOD_AUTH_FAIL_THRESHOLD": str(THRESHOLD),
        "FOOD_AUTH_FAIL_WINDOW": str(WINDOW),
        "FOOD_AUTH_BAN_SECONDS": str(BAN_SECONDS),
        "FOOD_AUDIT_FILE": AUDIT, "AUDIT_DB": CHAIN,
        "PYTHONPATH": CODES + os.pathsep + env.get("PYTHONPATH", ""),
    })
    env.pop("FOOD_STRICT_AUTH", None)
    env.update(extra or {})
    return env


def start_server(extra=None, port=PORT):
    log = open(os.path.join(TMP, "server_%d.log" % port), "wb")
    p = subprocess.Popen([PY, "api_server.py", "--host", "127.0.0.1", "--port", str(port)],
                         cwd=CODES, env=_server_env(extra),
                         stdout=log, stderr=subprocess.STDOUT)
    return p, log


def wait_ready(proc, port=PORT, timeout=180):
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


def dump_log(port=PORT):
    try:
        print(open(os.path.join(TMP, "server_%d.log" % port), encoding="utf-8",
                   errors="replace").read()[-1500:])
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# 1. 默认配置语义（直接核验实现，不依赖服务）
# ─────────────────────────────────────────────────────────────
def part_1_defaults():
    section("1 默认配置语义：阈值/窗口/封禁 + 状态目录位置（直接核验实现）")
    for v in ("FOOD_AUTH_FAIL_THRESHOLD", "FOOD_AUTH_FAIL_WINDOW",
              "FOOD_AUTH_BAN_SECONDS", "FOOD_AUTH_STATE_DIR", "FOOD_AUTH_STATE_MAX_ROWS"):
        os.environ.pop(v, None)
    sys.path.insert(0, CODES)
    import token_guard as tg
    cfg = tg.config()
    ck("默认失败阈值 = 10", cfg["fail_threshold"] == 10, "threshold=%s" % cfg["fail_threshold"])
    ck("默认失败窗口 = 300s", cfg["fail_window"] == 300, "window=%s" % cfg["fail_window"])
    ck("默认封禁时长 = 300s", cfg["ban_seconds"] == 300, "ban=%s" % cfg["ban_seconds"])
    sd = tg.state_dir()
    norm = sd.replace("\\", "/")
    ck("默认状态目录在仓库 var/ 下", "/var/" in norm + "/", "state_dir=%s" % sd)
    ck("默认状态目录不在任何 KB 数据目录(codes/data*)下",
       "codes/data" not in norm, "state_dir=%s" % sd)
    return tg


# ─────────────────────────────────────────────────────────────
# 2. 吊销（按 jti / 按主体批量）
# ─────────────────────────────────────────────────────────────
def part_2_revoke():
    section("2 吊销：带 jti 令牌吊销后立即 401；按主体批量吊销")
    st, b = issue(revocable=True)
    tok, jti = b.get("token", ""), b.get("jti", "")
    ck("签发可吊销令牌(fotk3, 带 jti) → 200", st == 200 and tok.startswith("fotk3.") and bool(jti),
       "prefix=%s jti=%s" % (tok.split(".")[0], jti))
    st_u, _ = http("GET", "/api/kbs", {"X-API-Key": tok})
    ck("吊销前该令牌可用 → 200", st_u == 200, "status=%s" % st_u)
    st_r, br = http("POST", "/api/auth/revoke", {"X-API-Key": ADMIN_KEY}, {"jti": jti, "reason": "test"})
    ck("按 jti 吊销 → 200 且命中 1 条", st_r == 200 and br.get("revoked_by_jti") == 1,
       "resp=%s" % br)
    st_rv, _ = http("GET", "/api/kbs", {"X-API-Key": tok})
    ck("吊销后该令牌**立即 401**", st_rv == 401, "status=%s" % st_rv)

    # 按主体批量: 同主体(token-read) 两张可吊销令牌
    t1 = issue(revocable=True)[1].get("token", "")
    t2 = issue(revocable=True)[1].get("token", "")
    st_bs, bb = http("POST", "/api/auth/revoke", {"X-API-Key": ADMIN_KEY},
                     {"subject": "token-read", "reason": "batch"})
    ck("按主体批量吊销 token-read → 命中 ≥2", st_bs == 200 and bb.get("revoked_by_subject", 0) >= 2,
       "resp=%s" % bb)
    s1, _ = http("GET", "/api/kbs", {"X-API-Key": t1})
    s2, _ = http("GET", "/api/kbs", {"X-API-Key": t2})
    ck("批量吊销后两张令牌均 401", s1 == 401 and s2 == 401, "t1=%s t2=%s" % (s1, s2))


# ─────────────────────────────────────────────────────────────
# 3. 刷新轮替（用后即废 + 复用检测）
# ─────────────────────────────────────────────────────────────
def part_3_refresh():
    section("3 刷新轮替：轮替生效；旧刷新令牌复用被拒并审计")
    st, b = issue(revocable=True, with_refresh=True)
    old_rt = b.get("refresh_token", "")
    ck("签发访问+刷新令牌(fotkr1) → 200", st == 200 and old_rt.startswith("fotkr1."),
       "refresh_prefix=%s" % old_rt.split(".")[0])
    # 刷新令牌不能当访问凭据
    st_bad, _ = http("GET", "/api/kbs", {"X-API-Key": old_rt})
    ck("刷新令牌当访问凭据用 → 401(拒绝)", st_bad == 401, "status=%s" % st_bad)
    st_f, bf = http("POST", "/api/auth/refresh", None, {"refresh_token": old_rt})
    new_at, new_rt = bf.get("token", ""), bf.get("refresh_token", "")
    ck("刷新(轮替) → 200 且返回新访问+新刷新令牌",
       st_f == 200 and new_at.startswith("fotk3.") and new_rt.startswith("fotkr1.") and new_rt != old_rt,
       "status=%s new_prefixes=%s/%s" % (st_f, new_at.split(".")[0], new_rt.split(".")[0]))
    st_nu, _ = http("GET", "/api/kbs", {"X-API-Key": new_at})
    ck("轮替出的新访问令牌可用 → 200", st_nu == 200, "status=%s" % st_nu)
    # 复用旧刷新令牌
    st_re, _ = http("POST", "/api/auth/refresh", None, {"refresh_token": old_rt})
    ck("旧刷新令牌复用 → 401(拒绝)", st_re == 401, "status=%s" % st_re)
    recs = audit_records()
    ck("复用留审计(action=refresh_reuse, result=deny)",
       any(r.get("action") == "refresh_reuse" and r.get("result") == "deny" for r in recs),
       "命中=%d" % sum(1 for r in recs if r.get("action") == "refresh_reuse"))
    # 新刷新令牌仍可用（未被误废）
    st_nr, bnr = http("POST", "/api/auth/refresh", None, {"refresh_token": new_rt})
    ck("新刷新令牌仍可轮替 → 200", st_nr == 200 and bnr.get("token", "").startswith("fotk3."),
       "status=%s" % st_nr)


# ─────────────────────────────────────────────────────────────
# 4. 限速/防爆破（突发失败→429；正常流量不误伤；封禁/解除审计）
# ─────────────────────────────────────────────────────────────
def part_4_ratelimit():
    section("4 限速/防爆破：突发失败 %d 次→429；封禁期间正常凭据仍 200" % THRESHOLD)
    # 用不被其它用例复用的 subject 维度的"坏 key"；ip 维度也会累计
    codes = []
    for i in range(THRESHOLD + 3):
        st, _ = http("GET", "/api/stats?kb=valve", {"X-API-Key": "vtg-bad-%d" % i})
        codes.append(st)
    ck("前若干次失败 = 401", codes[0] == 401, "codes=%s" % codes)
    ck("累计超阈值后出现 429(封禁)", any(c == 429 for c in codes), "codes=%s" % codes)
    # 封禁期间：正常凭据请求必须仍 200（不误伤）
    st_ok, _ = http("GET", "/api/stats?kb=valve", {"X-API-Key": READ_KEY})
    ck("**已封禁期间**正常 read 凭据请求仍 200(不误伤)", st_ok == 200, "status=%s" % st_ok)
    st_tok, _ = http("GET", "/api/auth/whoami", {"Authorization": "Bearer %s" % issue()[1]["token"]})
    ck("封禁期间另一正常令牌 whoami 仍 200", st_tok == 200, "status=%s" % st_tok)
    recs = audit_records()
    ck("封禁留审计(action=ban)", any(r.get("action") == "ban" for r in recs),
       "ban=%d" % sum(1 for r in recs if r.get("action") == "ban"))
    ck("限速命中留审计(action=throttle)", any(r.get("action") == "throttle" for r in recs),
       "throttle=%d" % sum(1 for r in recs if r.get("action") == "throttle"))
    # 手动解除封禁（审计留痕）+ 计数清零 → 再试回到 401
    st_ub, bub = http("POST", "/api/auth/unban", {"X-API-Key": ADMIN_KEY},
                      {"subject": "anon", "ip": "127.0.0.1"})
    ck("手动解除封禁 → 200 且确有解除", st_ub == 200 and len(bub.get("cleared", [])) >= 1,
       "resp=%s" % bub)
    st_after, _ = http("GET", "/api/stats?kb=valve", {"X-API-Key": "vtg-bad-again"})
    ck("解除后计数已清零 → 再试为 401(非 429)", st_after == 401, "status=%s" % st_after)
    recs = audit_records()
    ck("解除留审计(action=unban)", any(r.get("action") == "unban" for r in recs),
       "unban=%d" % sum(1 for r in recs if r.get("action") == "unban"))
    return codes


def part_4b_auto_expire():
    section("4b 封禁到期自动解除 + 审计留痕")
    # 重新打到封禁
    for i in range(THRESHOLD + 2):
        http("GET", "/api/stats?kb=valve", {"X-API-Key": "vtg-exp-%d" % i})
    st_b, _ = http("GET", "/api/stats?kb=valve", {"X-API-Key": "vtg-exp-final"})
    ck("再次突发 → 429", st_b == 429, "status=%s" % st_b)
    print("  等待封禁(%ds)到期…" % BAN_SECONDS)
    time.sleep(BAN_SECONDS + 2)
    http("GET", "/api/stats?kb=valve", {"X-API-Key": "vtg-exp-probe"})  # 触发到期检测
    recs = audit_records()
    ck("到期自动解除留审计(unban, detail 含 '到期')",
       any(r.get("action") == "unban" and "到期" in str(r.get("detail", "")) for r in recs),
       "auto_unban=%d" % sum(1 for r in recs
                             if r.get("action") == "unban" and "到期" in str(r.get("detail", ""))))


# ─────────────────────────────────────────────────────────────
# 5. 不误伤：并发正常业务/问答请求
# ─────────────────────────────────────────────────────────────
def part_5_no_collateral():
    section("5 不误伤：并发正常业务/问答请求全部非 429")
    # 先解除可能存在的封禁，确保从干净态起测
    http("POST", "/api/auth/unban", {"X-API-Key": ADMIN_KEY}, {"subject": "anon", "ip": "127.0.0.1"})
    at = issue()[1]["token"]   # 老格式 fotk1 令牌(无 jti) —— 也作为正常流量
    stats_codes, ask_codes = [], []

    def hit_stats(_):
        return http("GET", "/api/stats?kb=valve", {"X-API-Key": READ_KEY})[0]

    def hit_ask(_):
        return http("POST", "/api/ask", {"X-API-Key": at},
                    {"question": "阀门的数量", "kb": "valve"}, timeout=120)[0]

    with ThreadPoolExecutor(max_workers=10) as ex:
        stats_codes = list(ex.map(hit_stats, range(60)))
    with ThreadPoolExecutor(max_workers=6) as ex:
        ask_codes = list(ex.map(hit_ask, range(12)))
    ck("并发 60 次 /api/stats(正常凭据) 全部 200", all(c == 200 for c in stats_codes),
       "非200=%s" % sorted({c for c in stats_codes if c != 200}))
    ck("并发 12 次 /api/ask(正常令牌) 无 429/401（未被限速拦截）",
       all(c not in (429, 401, 403) for c in ask_codes),
       "codes=%s" % sorted(set(ask_codes)))


# ─────────────────────────────────────────────────────────────
# 6. 状态目录有界（体积/条数 + 硬上限）
# ─────────────────────────────────────────────────────────────
def part_6_bounded():
    section("6 状态目录有界：真实读数 + 硬上限证明")
    # 6a 真实产生令牌后读 /api/auth/state
    for _ in range(40):
        issue(revocable=True)
    st, bs = http("GET", "/api/auth/state", {"X-API-Key": ADMIN_KEY})
    ck("GET /api/auth/state → 200 且含体积/条数/配置",
       st == 200 and all(k in bs for k in ("db_bytes", "tokens_rows", "config")), "status=%s" % st)
    print("    实测: state_dir=%s" % bs.get("state_dir"))
    print("          db_bytes=%s  tokens_rows=%s  revoked_rows=%s  refresh_rows=%s  fail_rows=%s"
          % (bs.get("db_bytes"), bs.get("tokens_rows"), bs.get("revoked_rows"),
             bs.get("refresh_rows"), bs.get("fail_rows")))
    print("          config=%s" % bs.get("config"))
    ck("状态库体积有界(< 5MB)", int(bs.get("db_bytes", 0)) < 5 * 1024 * 1024,
       "db_bytes=%s" % bs.get("db_bytes"))

    # 6b 硬上限：直接对 guard 灌 1000 条，max_rows=200 → 清理后 ≤ 200
    os.environ["FOOD_AUTH_STATE_DIR"] = os.path.join(TMP, "bounded_state")
    os.environ["FOOD_AUTH_STATE_MAX_ROWS"] = "200"
    sys.path.insert(0, CODES)
    import importlib
    import token_guard as tg
    importlib.reload(tg)
    g = tg.TokenGuard()
    t0 = time.time()
    for i in range(1000):
        g.register_token("j%04d" % i, subject="s", role="read", kind="access", exp=t0 + 3600)
        g.record_failure("ip:bulk")
    stt = g.stats()
    print("    硬上限实测(max_rows=200, 灌 1000 令牌 + 1000 失败):")
    print("          tokens_rows=%s  fail_rows=%s  db_bytes=%s"
          % (stt["tokens_rows"], stt["fail_rows"], stt["db_bytes"]))
    ck("令牌表被硬上限裁剪到 ≤ 200", stt["tokens_rows"] <= 200, "tokens_rows=%s" % stt["tokens_rows"])
    ck("失败表被硬上限裁剪到 ≤ 200", stt["fail_rows"] <= 200, "fail_rows=%s" % stt["fail_rows"])
    # 复原环境
    os.environ.pop("FOOD_AUTH_STATE_DIR", None)
    os.environ.pop("FOOD_AUTH_STATE_MAX_ROWS", None)


# ─────────────────────────────────────────────────────────────
# 7. 兼容底线：老令牌(无 jti) 默认仍可用
# ─────────────────────────────────────────────────────────────
def part_7_compat():
    section("7 兼容底线：默认签发仍是 fotk1(无 jti) 且老令牌照常可用")
    st, b = issue()   # 默认 revocable=false
    tok = b.get("token", "")
    ck("默认签发令牌格式仍为 fotk1.<role>.<exp>.<sig>(4 段, 无 jti)",
       st == 200 and tok.startswith("fotk1.") and len(tok.split(".")) == 4 and "jti" not in b,
       "prefix=%s parts=%d" % (tok.split(".")[0], len(tok.split("."))))
    st_u, _ = http("GET", "/api/kbs", {"X-API-Key": tok})
    ck("老格式令牌(无 jti)照常可用 → 200", st_u == 200, "status=%s" % st_u)
    # 老令牌无法按 jti 吊销（无 jti 可吊销）——如实记录为"限制"
    st_rv, brv = http("POST", "/api/auth/revoke", {"X-API-Key": ADMIN_KEY}, {"jti": "NonexistentJti"})
    ck("对无 jti 的老令牌无法按 jti 吊销(命中 0，属已知限制)",
       st_rv == 200 and brv.get("revoked_by_jti") == 0, "resp=%s" % brv)


# ─────────────────────────────────────────────────────────────
def main():
    print("=" * 76)
    print("第3轮自检：令牌吊销/刷新 + 限速防爆破   仓库: %s" % REPO)
    print("临时工作目录: %s" % TMP)
    print("=" * 76)
    part_1_defaults()

    proc = log = None
    try:
        proc, log = start_server()
        ok, info = wait_ready(proc)
        if not ck("服务就绪(真实 HTTP)", ok, info):
            dump_log()
            raise SystemExit(1)
        part_2_revoke()
        part_3_refresh()
        part_4_ratelimit()
        part_5_no_collateral()
        part_4b_auto_expire()
        part_6_bounded()
        part_7_compat()
    finally:
        if proc is not None:
            try:
                proc.terminate(); proc.wait(timeout=15)
            except Exception:
                try: proc.kill()
                except Exception: pass
        if log is not None:
            try: log.close()
            except Exception: pass
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "-" * 76)
    print("自检结果：PASS %d / FAIL %d" % (PASSES[0], len(FAILS)))
    if FAILS:
        print("未过项：")
        for f in FAILS:
            print("  - %s" % f)
    print("-" * 76)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
