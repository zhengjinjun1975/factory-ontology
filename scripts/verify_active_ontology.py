#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_active_ontology.py — A/C 组修复的真实 HTTP 自检（起临时服务，打真实端口）。

覆盖（逐条断言）：
  A1  「当前生效本体」= 落盘单一真相源 codes/config/active_ontology.json
        · 写（建库/切库）→ 读 → **重启后端后仍是该库**（核心证据）
        · BFF setCurrentKb 写同一文件，BFF 重启后不丢
  A2  /api/standard/compliance 按当前激活 KB 的本体计算；找不到该库本体 → 明确报错，
      不静默回落全局 config/ontology_schema.json
  A3  /api/standard/export 同上，按当前激活 KB 导出
  A4  问答缺省跟随当前激活 KB；同一 data_dir 被别的 KB 占用 → 拒绝（不静默通过）
  C1  BFF 代理 /api/standard/ 前缀（返回 JSON，不退化成首页 HTML）
  C2  鉴权失败提示如实：401 → "未登录或会话已过期，请重新登录"；后端 401 不再被显示成"后端建模失败"
  C3  登录态落盘：BFF 重启后旧会话 token 仍有效

纪律：不打业务数据、不动 KB 数据目录；只在开始/结束时备份并还原
codes/config/active_ontology.json（激活态真相源）与 A3 会写的 codes/export/ 导出产物。纯标准库。

用法：python scripts/verify_active_ontology.py
退出码：0 = 全过；1 = 有断言未过。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
NODE = os.environ.get("NODE_BIN", "node")
ACTIVE = os.path.join(ROOT, "codes", "config", "active_ontology.json")

ADMIN_KEY = "verify-admin-key"
READ_KEY = "verify-read-key"

_results = []
_procs = []
_auth_dirs = []   # 临时限速状态目录，收尾清理


def ck(name, cond, detail=""):
    _results.append((name, bool(cond), detail))
    flag = "PASS" if cond else "FAIL"
    line = "  [%s] %s" % (flag, name)
    if not cond and detail:
        line += "  <- %s" % str(detail)[:240]
    print(line)


def section(t):
    print("\n" + "=" * 72 + "\n" + t + "\n" + "=" * 72)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _json(t):
    try:
        return json.loads(t)
    except Exception:
        return None


def http(url, method="GET", body=None, token=None, apikey=None, timeout=90, raw=False):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    if apikey:
        headers["X-API-Key"] = apikey
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
            return r.status, (txt if raw else _json(txt)), dict(r.headers)
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        return e.code, (txt if raw else _json(txt)), dict(e.headers)


def wait_http(url, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            st, _, _ = http(url, timeout=3)
            if st:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def read_active_file():
    try:
        with open(ACTIVE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def spawn_backend(port, log):
    env = dict(os.environ)
    env["FOOD_ADMIN_KEY"] = ADMIN_KEY
    env["FOOD_READ_KEY"] = READ_KEY
    env["PYTHONIOENCODING"] = "utf-8"
    # 隔离限速/防爆破状态（否则反复用错 key 会命中仓库共享的 var/auth_state 临时封禁 → 429，
    # 掩盖真正的 401，使 C2 用例变成 flaky）。每次起服务用独立临时状态目录。
    env["FOOD_AUTH_STATE_DIR"] = tempfile.mkdtemp(prefix="hermes-verify-authstate-")
    _auth_dirs.append(env["FOOD_AUTH_STATE_DIR"])
    f = open(log, "wb")
    p = subprocess.Popen([PY, os.path.join(ROOT, "codes", "api_server.py"), "--port", str(port)],
                         cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT)
    _procs.append((p, f))
    return p


def spawn_bff(port, backend_url, api_key, log):
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["API_URL"] = backend_url
    env["API_KEY"] = api_key
    f = open(log, "wb")
    p = subprocess.Popen([NODE, os.path.join(ROOT, "web", "server", "index.js")],
                         cwd=os.path.join(ROOT, "web", "server"), env=env,
                         stdout=f, stderr=subprocess.STDOUT)
    _procs.append((p, f))
    return p


def stop(p):
    try:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
    except Exception:
        pass


def main():
    # ── 备份激活态真相源（结束时还原）──
    backup = None
    if os.path.exists(ACTIVE):
        with open(ACTIVE, encoding="utf-8") as f:
            backup = f.read()
    # ── 备份 A3 会写的导出产物（结束时还原，避免污染仓库里的生成物）──
    export_dir = os.path.join(ROOT, "codes", "export")
    export_backup = {}
    for fn in ("ontology.ttl", "shapes.ttl", "ontology.jsonld"):
        p = os.path.join(export_dir, fn)
        if os.path.exists(p):
            with open(p, "rb") as f:
                export_backup[p] = f.read()

    pb = free_port()
    pf = free_port()
    pf2 = free_port()
    b_url = "http://127.0.0.1:%d" % pb
    f_url = "http://127.0.0.1:%d" % pf
    f2_url = "http://127.0.0.1:%d" % pf2
    blog = os.path.join(ROOT, "codes", "output", "_verify_active_backend.log")
    flog = os.path.join(ROOT, "codes", "output", "_verify_active_bff.log")
    flog2 = os.path.join(ROOT, "codes", "output", "_verify_active_bff2.log")

    try:
        section("启动临时后端 :%d（带 key）" % pb)
        be = spawn_backend(pb, blog)
        up = wait_http(b_url + "/health", timeout=60)
        ck("临时后端就绪 GET /health", up, "log=%s" % blog)
        if not up:
            return 1

        # ================= A1 =================
        section("A1 激活态 = 落盘单一真相源（写→读→重启仍在）")
        st, body, _ = http(b_url + "/api/kb/active", apikey=ADMIN_KEY)
        ck("A1 GET /api/kb/active 可读（返回 kb + file）",
           st == 200 and isinstance(body, dict) and body.get("kb") and body.get("file"),
           "st=%s body=%s" % (st, body))

        st, body, _ = http(b_url + "/api/kb/active", method="POST",
                           body={"kb": "chem"}, apikey=ADMIN_KEY)
        ck("A1 切库 POST /api/kb/active {kb:chem} 成功", st == 200 and body.get("ok"),
           "st=%s body=%s" % (st, body))
        ck("A1 落盘文件 kb==chem", read_active_file().get("kb") == "chem",
           read_active_file())

        st, body, _ = http(b_url + "/api/kb/active", apikey=ADMIN_KEY)
        ck("A1 读回 GET /api/kb/active kb==chem", body.get("kb") == "chem", body)

        # 重启后端（核心证据：重启后激活态仍是该库）
        print("  ... 重启临时后端（同一端口）")
        stop(be)
        be = spawn_backend(pb, blog)
        ck("A1 重启后后端再次就绪", wait_http(b_url + "/health", timeout=60))
        st, body, _ = http(b_url + "/api/kb/active", apikey=ADMIN_KEY)
        ck("A1【核心】重启后端后激活态仍是 chem（未回落默认）",
           body.get("kb") == "chem", "st=%s body=%s" % (st, body))

        # ================= A2 =================
        section("A2 /api/standard/compliance 按当前激活 KB 的本体计算")
        http(b_url + "/api/kb/active", method="POST", body={"kb": "valve"}, apikey=ADMIN_KEY)
        st, body, _ = http(b_url + "/api/standard/compliance", apikey=ADMIN_KEY)
        ck("A2 激活=valve → 合规度 ok 且 kb=valve 且指向 valve 本体",
           st == 200 and body.get("ok") and body.get("kb") == "valve"
           and "ontology_schema_valve.json" in str(body.get("ontology")),
           "st=%s body=%s" % (st, str(body)[:200]))
        ck("A2 激活=valve → 有实数 standard_rate",
           isinstance(body.get("standard_rate"), (int, float)), body.get("standard_rate"))

        http(b_url + "/api/kb/active", method="POST", body={"kb": "chem"}, apikey=ADMIN_KEY)
        st, body, _ = http(b_url + "/api/standard/compliance", apikey=ADMIN_KEY)
        ck("A2 激活=chem(无本体) → 明确报错、不静默回落全局那份",
           st == 200 and body.get("ok") is False and "找不到" in str(body.get("error")),
           "st=%s body=%s" % (st, str(body)[:220]))

        # ================= A3 =================
        section("A3 /api/standard/export 按当前激活 KB 的本体导出")
        http(b_url + "/api/kb/active", method="POST", body={"kb": "valve"}, apikey=ADMIN_KEY)
        st, body, _ = http(b_url + "/api/standard/export", method="POST", body={},
                           apikey=ADMIN_KEY)
        ck("A3 激活=valve → 导出 ok、kb=valve、≥3 个产物",
           st == 200 and body.get("ok") and body.get("kb") == "valve"
           and len(body.get("files") or []) >= 3,
           "st=%s body=%s" % (st, str(body)[:220]))
        ck("A3 导出用的 schema 是 valve 的", "ontology_schema_valve.json" in str(body.get("schema")),
           body.get("schema"))

        http(b_url + "/api/kb/active", method="POST", body={"kb": "chem"}, apikey=ADMIN_KEY)
        st, body, _ = http(b_url + "/api/standard/export", method="POST", body={},
                           apikey=ADMIN_KEY)
        ck("A3 激活=chem(无本体) → 明确报错、不静默回落",
           st == 200 and body.get("ok") is False and "找不到" in str(body.get("error")),
           "st=%s body=%s" % (st, str(body)[:220]))

        # ================= A4 =================
        section("A4 问答跟随当前激活 KB + data_dir 占用冲突校验")
        http(b_url + "/api/kb/active", method="POST", body={"kb": "valve"}, apikey=ADMIN_KEY)
        st, body, _ = http(b_url + "/api/ask", method="POST",
                           body={"question": "有多少台设备"}, apikey=ADMIN_KEY, timeout=120)
        ck("A4 缺省 kb 的问答 → 跟随激活库 valve", body.get("kb") == "valve",
           "st=%s kb=%s" % (st, body.get("kb")))
        http(b_url + "/api/kb/active", method="POST", body={"kb": "chem"}, apikey=ADMIN_KEY)
        st, body, _ = http(b_url + "/api/ask", method="POST",
                           body={"question": "有多少台设备"}, apikey=ADMIN_KEY, timeout=120)
        ck("A4 切到 chem 后缺省问答 → 跟随激活库 chem", body.get("kb") == "chem",
           "st=%s kb=%s" % (st, body.get("kb")))

        # data_dir 冲突：valve 想用 food 的 data_food → 必须拒绝
        st, body, _ = http(b_url + "/api/ontology/confirm", method="POST",
                           body={"kb": "valve", "data_dir": "data_food",
                                 "schema": {"entities": [{"id": "E1", "label": "测试实体"}]}},
                           apikey=ADMIN_KEY)
        ck("A4 同 data_dir 冲突 → 拒绝且写明占用者(不静默通过)",
           st == 200 and body.get("ok") is False and "冲突" in str(body.get("error")),
           "st=%s body=%s" % (st, str(body)[:240]))

        # ================= C1 / C2 / C3（BFF）=================
        section("启动临时 BFF :%d（正确 key）" % pf)
        ff = spawn_bff(pf, b_url, ADMIN_KEY, flog)
        ck("临时 BFF 就绪 GET /health", wait_http(f_url + "/health", timeout=60), "log=%s" % flog)
        st, hb, _ = http(f_url + "/health")
        ck("C4 BFF /health 暴露 apiKeyConfigured=True", hb.get("apiKeyConfigured") is True, hb)

        st, lr, _ = http(f_url + "/api/auth/login", method="POST",
                         body={"username": "admin", "password": "admin123"})
        token = (lr or {}).get("token")
        ck("登录获取会话 token", bool(token), "st=%s body=%s" % (st, str(lr)[:120]))

        section("C1 BFF 代理 /api/standard/（返回 JSON，不退化成 HTML）")
        http(b_url + "/api/kb/active", method="POST", body={"kb": "valve"}, apikey=ADMIN_KEY)
        st, body, hdrs = http(f_url + "/api/standard/compliance", token=token)
        ctype = (hdrs.get("Content-Type") or hdrs.get("content-type") or "")
        ck("C1 GET /api/standard/compliance 经 BFF → JSON(ok, standard_rate)",
           st == 200 and isinstance(body, dict) and body.get("ok") is True
           and "standard_rate" in body,
           "st=%s ctype=%s body=%s" % (st, ctype, str(body)[:200]))
        ck("C1 响应是 JSON 而非首页 HTML", "json" in ctype.lower(), ctype)

        section("C2 鉴权失败提示如实化")
        st, body, _ = http(f_url + "/api/ontology/kbs")  # 无 token
        ck("C2 未登录访问业务端点 → 401 且提示语如实",
           st == 401 and str(body.get("error")) == "未登录或会话已过期，请重新登录",
           "st=%s body=%s" % (st, body))

        section("C3 登录态落盘：BFF 重启后旧会话仍有效")
        print("  ... 重启临时 BFF（同一端口，正确 key）")
        stop(ff)
        ff = spawn_bff(pf, b_url, ADMIN_KEY, flog)
        ck("C3 重启后 BFF 再次就绪", wait_http(f_url + "/health", timeout=60))
        st, body, _ = http(f_url + "/api/auth/me", token=token)
        ck("C3 重启后旧 token 调 /api/auth/me → 仍 200（未被踢下线）",
           st == 200 and body.get("ok") is True, "st=%s body=%s" % (st, str(body)[:160]))

        section("A1(BFF 侧) setCurrentKb 写持久态，BFF 重启后不丢")
        st, body, _ = http(f_url + "/api/ontology/kb", method="POST",
                           body={"kb": "chem"}, token=token)
        ck("A1 BFF 切库 POST /api/ontology/kb {kb:chem} 成功", st == 200 and body.get("ok"),
           "st=%s body=%s" % (st, body))
        ck("A1 BFF 写入后持久态文件 kb==chem", read_active_file().get("kb") == "chem",
           read_active_file())
        print("  ... 再次重启临时 BFF")
        stop(ff)
        ff = spawn_bff(pf, b_url, ADMIN_KEY, flog)
        ck("A1 BFF 重启后就绪", wait_http(f_url + "/health", timeout=60))
        ck("A1【核心·BFF 侧】BFF 重启后激活态文件仍是 chem", read_active_file().get("kb") == "chem",
           read_active_file())
        st, body, _ = http(b_url + "/api/kb/active", apikey=ADMIN_KEY)
        ck("A1【核心·BFF 侧】后端读同一真相源 → kb==chem", body.get("kb") == "chem", body)

        section("C2 后端 401 不再被显示成\"后端建模失败\"")
        # 起一个 key 配错的 BFF → 调后端一律 401 → 应如实提示未登录/会话过期
        fb = spawn_bff(pf2, b_url, "WRONG-KEY-FOR-TEST", flog2)
        ck("错 key BFF 就绪", wait_http(f2_url + "/health", timeout=60), "log=%s" % flog2)
        st, lr2, _ = http(f2_url + "/api/auth/login", method="POST",
                          body={"username": "admin", "password": "admin123"})
        t2 = (lr2 or {}).get("token")
        ck("错 key BFF 登录拿到会话", bool(t2), "st=%s" % st)
        st, body, _ = http(f2_url + "/api/ontology/standard", token=t2)
        err = str((body or {}).get("error") or "")
        ck("C2 后端 401 经 BFF → 提示含\"未登录\"且**不含**\"后端建模失败\"",
           ("未登录" in err) and ("后端建模失败" not in err) and ("建模失败" not in err),
           "st=%s error=%s" % (st, err[:200]))

    finally:
        for p, f in _procs:
            stop(p)
            try:
                f.close()
            except Exception:
                pass
        # 还原激活态真相源
        try:
            if backup is not None:
                with open(ACTIVE, "w", encoding="utf-8", newline="\n") as f:
                    f.write(backup)
            elif os.path.exists(ACTIVE):
                os.remove(ACTIVE)
        except Exception:
            pass
        # 还原导出产物
        for p, data in export_backup.items():
            try:
                with open(p, "wb") as f:
                    f.write(data)
            except Exception:
                pass
        # 清理临时限速状态目录
        for d in _auth_dirs:
            shutil.rmtree(d, ignore_errors=True)

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    section("自检结果: %d/%d 通过" % (passed, total))
    for name, ok, detail in _results:
        if not ok:
            print("  FAIL: %s  <- %s" % (name, str(detail)[:200]))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
