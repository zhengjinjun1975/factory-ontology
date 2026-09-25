#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_strict_auth.py — 第2轮「严格鉴权灰度开关 + 上传体积上限」自检（真实执行）。

跑: <python> scripts/verify_strict_auth.py
（可选: VERIFY_SA_OLD_PORT / VERIFY_SA_OFF_PORT / VERIFY_SA_ON_PORT）

真实证明（每条都给终端输出）：
  ① 严格关(默认, FOOD_STRICT_AUTH 未设) → 匿名访问本体结构与图端点仍通, 且与 HEAD 旧服务
     **逐字段一致**（/health、/api/ontology/structure、/api/ontology/graph、graph-svg、
     /api/app-config、/admin、/api/flows… 全打真实 HTTP 对照）。
  ② 严格开(FOOD_STRICT_AUTH=yes) → 受保护端点匿名 401、带 read 凭据 200、
     read 角色访问 /admin 403、admin 凭据 200；/health 仍匿名 200(运维探活)，
     /metrics 严格模式下要凭据。
  ③ 开关真值语义：1/true/yes/on → 开；其余 → 关（直接调用实现函数核验）。
  ④ 上传体积上限：正常大小通过；超限 413；且落盘目录无残留（data 目录 / 临时目录都查）。
  ⑤ 读入有界：直接用假上传体喂分块读入函数，证明超限即中止、不读全量、不落盘。
纪律：全程 %TEMP% 临时副本；不碰真实 data/ 与 config；临时端口自起自收；纯标准库。
退出码: 全过 0；有 FAIL 1。
"""
import os
import io
import sys
import json
import time
import uuid
import asyncio
import shutil
import tempfile
import subprocess
import urllib.request
import urllib.error
import http.client

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CODES = os.path.join(REPO, "codes")
PY = sys.executable

OLD_PORT = int(os.environ.get("VERIFY_SA_OLD_PORT", "8921"))
OFF_PORT = int(os.environ.get("VERIFY_SA_OFF_PORT", "8922"))
ON_PORT = int(os.environ.get("VERIFY_SA_ON_PORT", "8923"))

ADMIN_KEY = "sa-admin-key"
READ_KEY = "sa-read-key"
TOKEN_SECRET = "sa-token-secret"

TMP = tempfile.mkdtemp(prefix="verify_strict_auth_")
TMP_DATA = os.path.join(TMP, "data")
TMP_UPLOADS = os.path.join(TMP, "uploads")
OLD_API_IN_CODES = os.path.join(CODES, ".verify_sa_old_api_server.py")

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
    print("\n" + "=" * 74)
    print(t)
    print("=" * 74)


# ─────────────────────────────────────────────────────────────
# HTTP / 多部件 辅助
# ─────────────────────────────────────────────────────────────
def http(method, path, headers=None, body=None, port=OFF_PORT, timeout=60):
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


def multipart(fields, files, boundary):
    """files: {name: (filename, bytes, content_type)}"""
    out = []
    for k, v in (fields or {}).items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, k, v)).encode("utf-8"))
    for k, (fn, data, ct) in (files or {}).items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                    "Content-Type: %s\r\n\r\n" % (boundary, k, fn, ct)).encode("utf-8"))
        out.append(data)
        out.append(b"\r\n")
    out.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(out)


def post_multipart(path, fields, files, headers=None, port=OFF_PORT, timeout=120):
    b = "----sa" + uuid.uuid4().hex
    payload = multipart(fields, files, b)
    h = dict(headers or {})
    h["Content-Type"] = "multipart/form-data; boundary=%s" % b
    return http("POST", path, h, payload, port=port, timeout=timeout)


# ─────────────────────────────────────────────────────────────
# 服务进程
# ─────────────────────────────────────────────────────────────
def _server_env(extra):
    env = dict(os.environ)
    env.update({
        "FOOD_ADMIN_KEY": ADMIN_KEY,
        "FOOD_READ_KEY": READ_KEY,
        "FOOD_TOKEN_SECRET": TOKEN_SECRET,
        "FOOD_AUDIT_FILE": os.path.join(TMP, "audit_%s.log" % uuid.uuid4().hex[:6]),
        "AUDIT_DB": os.path.join(TMP, "chain_%s.db" % uuid.uuid4().hex[:6]),
        "PYTHONPATH": CODES + os.pathsep + env.get("PYTHONPATH", ""),
    })
    env.pop("FOOD_ADMIN_KEY_EXPIRES", None)
    env.pop("FOOD_READ_KEY_EXPIRES", None)
    env.pop("FOOD_STRICT_AUTH", None)
    env.update(extra or {})
    return env


def start_server(script, port, extra=None):
    log = open(os.path.join(TMP, "server_%d.log" % port), "wb")
    p = subprocess.Popen([PY, script, "--host", "127.0.0.1", "--port", str(port)],
                         cwd=CODES, env=_server_env(extra),
                         stdout=log, stderr=subprocess.STDOUT)
    return p, log


def wait_ready(proc, port, timeout=180):
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


def dump_log(port):
    try:
        print(open(os.path.join(TMP, "server_%d.log" % port), encoding="utf-8",
                   errors="replace").read()[-1200:])
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# 1. 开关真值语义（直接调用实现函数）
# ─────────────────────────────────────────────────────────────
def part_1_flag_semantics():
    section("1 开关真值语义：1/true/yes/on → 开；其余 → 关（直接核验实现函数）")
    sys.path.insert(0, CODES)
    os.environ.setdefault("FOOD_ADMIN_KEY", ADMIN_KEY)
    import api_server as api
    on_vals = ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON", " true "]
    off_vals = ["0", "false", "no", "off", "", "junk", "2", "enable"]
    ok_on = all((os.environ.__setitem__("FOOD_STRICT_AUTH", v) or api._env_flag("FOOD_STRICT_AUTH"))
                for v in on_vals)
    ck("真值集合 %r → 全部判为『开』" % on_vals, ok_on)
    off_bad = [v for v in off_vals
               if (os.environ.__setitem__("FOOD_STRICT_AUTH", v) or api._env_flag("FOOD_STRICT_AUTH"))]
    ck("其余值 %r → 全部判为『关』" % off_vals, not off_bad, "误判=%r" % off_bad)
    os.environ.pop("FOOD_STRICT_AUTH", None)
    ck("未设 FOOD_STRICT_AUTH → 默认关（STRICT_AUTH=False）", api.STRICT_AUTH is False)
    ck("上传上限默认可配默认 50MB", api.MAX_UPLOAD_MB == 50.0 and api.MAX_UPLOAD_BYTES == 50 * 1024 * 1024,
       "MAX_UPLOAD_MB=%s" % api.MAX_UPLOAD_MB)
    return api


# ─────────────────────────────────────────────────────────────
# 2. 严格关(默认) vs HEAD 旧服务：匿名逐字段一致
# ─────────────────────────────────────────────────────────────
_COMPARE_ANON = [
    ("GET", "/health"),
    ("GET", "/api/ontology/structure?kb=valve"),
    ("GET", "/api/ontology/graph?kb=valve"),
    ("GET", "/api/ontology/graph-svg"),
    ("GET", "/api/app-config"),
    ("GET", "/admin"),
    ("GET", "/api/flows"),
    ("GET", "/api/flows/presets"),
]


def _canon(x):
    if isinstance(x, dict):
        return {k: _canon(v) for k, v in sorted(x.items())}
    if isinstance(x, list):
        return sorted((json.dumps(_canon(e), ensure_ascii=False, sort_keys=True) for e in x))
    return x


def part_2_off_vs_head():
    section("2 严格关（FOOD_STRICT_AUTH 未设）→ 匿名端点逐字段与 HEAD 旧服务一致")
    try:
        old_src = subprocess.run(["git", "show", "HEAD:codes/api_server.py"], cwd=REPO,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if old_src.returncode != 0:
            ck("取 HEAD 版本 api_server.py", False, old_src.stdout.decode("utf-8", "replace")[:120])
            return None, None
        with open(OLD_API_IN_CODES, "wb") as f:
            f.write(old_src.stdout)
    except Exception as e:
        ck("准备 HEAD 旧服务源码", False, str(e))
        return None, None

    old_p, old_log = start_server(os.path.basename(OLD_API_IN_CODES), OLD_PORT, None)
    off_p, off_log = start_server("api_server.py", OFF_PORT, {
        "FOOD_MAX_UPLOAD_MB": "1", "FOOD_DATA_DIR": TMP_DATA, "FOOD_TMP_UPLOAD_DIR": TMP_UPLOADS})
    ok_o, i_o = wait_ready(old_p, OLD_PORT)
    ok_n, i_n = wait_ready(off_p, OFF_PORT)
    if not ck("HEAD 旧服务 + 新服务(严格关) 均就绪", ok_o and ok_n, "old=%s off=%s" % (i_o, i_n)):
        dump_log(OLD_PORT); dump_log(OFF_PORT)
        return old_p, off_p

    for method, path in _COMPARE_ANON:
        st_o, body_o = http(method, path, None, None, port=OLD_PORT)
        st_n, body_n = http(method, path, None, None, port=OFF_PORT)
        same = (st_o == st_n == 200 and _canon(body_o) == _canon(body_n))
        note = "status %s/%s" % (st_o, st_n)
        if not same and st_o == st_n == 200:
            diffs = [k for k in set(body_o if isinstance(body_o, dict) else {})
                     | set(body_n if isinstance(body_n, dict) else {})
                     if _canon((body_o or {}).get(k) if isinstance(body_o, dict) else None)
                     != _canon((body_n or {}).get(k) if isinstance(body_n, dict) else None)]
            note += " 差异字段=%s" % diffs
        ck("匿名逐字段一致(旧 HEAD vs 严格关): %s %s" % (method, path), same, note)
    return old_p, off_p


# ─────────────────────────────────────────────────────────────
# 3. 严格开：401 / 403 / 200 与 /health、/metrics 决策
# ─────────────────────────────────────────────────────────────
_READ_ONLY = ["/api/ontology/structure?kb=valve", "/api/ontology/graph?kb=valve",
              "/api/ontology/graph-svg", "/api/app-config", "/api/flows",
              "/api/flows/presets", "/metrics"]


def part_3_strict_on(on_p):
    section("3 严格开（FOOD_STRICT_AUTH=yes）→ 受保护端点匿名 401 / read 200 / /admin 403")
    ok, info = wait_ready(on_p, ON_PORT)
    if not ck("严格开服务就绪", ok, info):
        dump_log(ON_PORT)
        return
    # 匿名 → 401
    for p in _READ_ONLY + ["/admin"]:
        st, _ = http("GET", p, None, None, port=ON_PORT)
        ck("匿名访问 %s → 401" % p, st == 401, "status=%s" % st)
    # 带 read 凭据 → 200
    for p in _READ_ONLY:
        st, body = http("GET", p, {"X-API-Key": READ_KEY}, None, port=ON_PORT)
        ck("read 凭据访问 %s → 200" % p, st == 200, "status=%s" % st)
    # /admin：read → 403；admin → 200
    st_r, _ = http("GET", "/admin", {"X-API-Key": READ_KEY}, None, port=ON_PORT)
    ck("read 角色访问 /admin → 403（角色不足）", st_r == 403, "status=%s" % st_r)
    st_a, _ = http("GET", "/admin", {"X-API-Key": ADMIN_KEY}, None, port=ON_PORT)
    ck("admin 凭据访问 /admin → 200", st_a == 200, "status=%s" % st_a)
    # 决策: /health 匿名 200; /metrics 匿名 401 且 read 200
    st_h, body_h = http("GET", "/health", None, None, port=ON_PORT)
    ck("/health 严格开下仍匿名 200（运维探活）", st_h == 200 and body_h.get("status") == "ok",
       "status=%s" % st_h)
    st_m, _ = http("GET", "/metrics", None, None, port=ON_PORT)
    st_mr, _ = http("GET", "/metrics", {"X-API-Key": READ_KEY}, None, port=ON_PORT)
    ck("/metrics 严格开下匿名 401 且 read 200", st_m == 401 and st_mr == 200,
       "anon=%s read=%s" % (st_m, st_mr))
    # 未受保护的既有业务端点行为不变（带凭据仍正常）
    st_ask, _ = http("GET", "/api/stats?kb=valve", {"X-API-Key": READ_KEY}, None, port=ON_PORT)
    ck("受保护集之外的既有端点(/api/stats)带凭据仍 200（未被误伤）", st_ask == 200,
       "status=%s" % st_ask)


# ─────────────────────────────────────────────────────────────
# 4. 上传体积上限：正常通过 / 超限 413 / 无残留
# ─────────────────────────────────────────────────────────────
def _listdir(d):
    try:
        return sorted(os.listdir(d))
    except Exception:
        return []


def part_4_upload(off_p):
    section("4 上传体积上限（FOOD_MAX_UPLOAD_MB=1）：正常通过 / 超限 413 / 目录无残留")
    ok, info = wait_ready(off_p, OFF_PORT)
    if not ck("上传测试服务(严格关, 上限1MB)就绪", ok, info):
        dump_log(OFF_PORT)
        return
    os.makedirs(TMP_DATA, exist_ok=True)
    os.makedirs(TMP_UPLOADS, exist_ok=True)

    # 4.1 正常大小 admin_upload（valve，无 schema → 不写真实 output）
    small = b"id,name\n" + b"".join(b"E%03d,dev%d\n" % (i, i) for i in range(50))
    before = _listdir(TMP_DATA)
    st, body = post_multipart("/api/admin/upload?table=equipment", None,
                              {"file": ("eq.csv", small, "text/csv")},
                              {"X-API-Key": ADMIN_KEY}, port=OFF_PORT)
    after = _listdir(TMP_DATA)
    ck("admin_upload 正常大小 → 200 且 ok=true", st == 200 and isinstance(body, dict) and body.get("ok") is True,
       "status=%s body=%s" % (st, str(body)[:120]))
    _tbl = (body or {}).get("table", "") if isinstance(body, dict) else ""
    ck("admin_upload 正常大小 → CSV 已落盘(按返回 table=%s)" % _tbl,
       bool(_tbl) and (_tbl + ".csv") in after and len(after) == len(before) + 1,
       "data目录=%s" % after)

    # 4.2 超限 admin_upload → 413 且不落盘
    before2 = _listdir(TMP_DATA)
    big = b"id,name\n" + b"A" * (2 * 1024 * 1024)   # 2MB > 1MB
    st2, body2 = post_multipart("/api/admin/upload?table=products", None,
                                {"file": ("big.csv", big, "text/csv")},
                                {"X-API-Key": ADMIN_KEY}, port=OFF_PORT)
    after2 = _listdir(TMP_DATA)
    ck("admin_upload 超限 → HTTP 413", st2 == 413, "status=%s body=%s" % (st2, str(body2)[:160]))
    ck("admin_upload 超限 → data 目录无新增残留（food_products.csv 不存在）",
       "food_products.csv" not in after2 and after2 == before2,
       "before=%s after=%s" % (before2, after2))

    # 4.3 正常大小 knowledge/ingest → 非 413（通过体积闸；下游结果如实记录）
    txt = ("设备编号,设备名称\n" + "\n".join("E%03d,设备%d" % (i, i) for i in range(80))).encode("utf-8")
    up_before = _listdir(TMP_UPLOADS)
    st3, body3 = post_multipart("/api/knowledge/ingest", {"kb": "valve", "doc_id": ""},
                                {"file": ("note.txt", txt, "text/plain")},
                                {"X-API-Key": READ_KEY}, port=OFF_PORT, timeout=120)
    up_after = _listdir(TMP_UPLOADS)
    ck("knowledge/ingest 正常大小 → 非 413（通过体积闸）", st3 != 413,
       "status=%s 下游 ok=%s" % (st3, (body3 or {}).get("ok") if isinstance(body3, dict) else "-"))
    ck("knowledge/ingest 正常大小 → 临时目录无残留（finally 已清理）",
       up_after == up_before == [], "before=%s after=%s" % (up_before, up_after))

    # 4.4 超限 knowledge/ingest → 413 且临时目录无残留
    up_before2 = _listdir(TMP_UPLOADS)
    bigtxt = b"A" * (3 * 1024 * 1024)   # 3MB > 1MB
    st4, body4 = post_multipart("/api/knowledge/ingest", {"kb": "valve", "doc_id": ""},
                                {"file": ("big.txt", bigtxt, "text/plain")},
                                {"X-API-Key": READ_KEY}, port=OFF_PORT)
    up_after2 = _listdir(TMP_UPLOADS)
    ck("knowledge/ingest 超限 → HTTP 413", st4 == 413, "status=%s body=%s" % (st4, str(body4)[:120]))
    ck("knowledge/ingest 超限 → 临时上传目录无残留文件", up_after2 == up_before2 and up_after2 == [],
       "before=%s after=%s" % (up_before2, up_after2))
    # 真实落盘目录 output/_tmp_uploads 也不该被本用例污染（未用 env 覆盖时的默认目录）
    default_tmp = os.path.join(CODES, "output", "_tmp_uploads")
    ck("默认临时目录 %s 无本用例残留" % default_tmp,
       not any("big" in f for f in _listdir(default_tmp)), "内容=%s" % _listdir(default_tmp))


# ─────────────────────────────────────────────────────────────
# 5. 读入有界：假上传体喂分块读入函数
# ─────────────────────────────────────────────────────────────
class _FakeUpload:
    """模拟 UploadFile.read(size)：按块吐出 total 字节，记录实际被读走的字节数。"""

    def __init__(self, total, chunk=256 * 1024):
        self.total = total
        self.chunk = chunk
        self.served = 0

    async def read(self, size=-1):
        if self.served >= self.total:
            return b""
        n = min(self.chunk if size in (-1, None) else size, self.total - self.served)
        self.served += n
        return b"x" * n


def part_5_bounded_read(api):
    section("5 读入有界：分块读入超限即中止、不读全量（直接用实现函数）")
    limit = 1024 * 1024            # 1MB
    total = 10 * 1024 * 1024       # 假体 10MB（远大于上限）
    f = _FakeUpload(total)
    raised = False
    try:
        asyncio.run(api._read_upload_capped(f, limit_bytes=limit))
    except api.UploadTooLarge as e:
        raised = True
        ck("超限 → 抛 UploadTooLarge", True, str(e))
    except Exception as e:
        ck("超限 → 抛 UploadTooLarge", False, "%s: %s" % (type(e).__name__, e))
    if not raised:
        ck("超限路径不读全量（中止在 ~上限，而非读满 10MB）", False,
           "served=%d" % f.served)
    else:
        # 中止时最多读到 limit + 一个 chunk，绝不读满 total
        ck("中止在 ~上限+1块（未读全量 10MB）",
           f.served <= limit + api._UPLOAD_CHUNK and f.served < total,
           "served=%dB limit=%dB total=%dB" % (f.served, limit, total))
    # 正常大小 → 完整返回
    g = _FakeUpload(300 * 1024)
    out = asyncio.run(api._read_upload_capped(g, limit_bytes=limit))
    ck("未超限 → 完整读入且长度正确", len(out) == 300 * 1024, "len=%d" % len(out))


# ─────────────────────────────────────────────────────────────
def main():
    print("=" * 74)
    print("第2轮自检：严格鉴权灰度开关 + 上传体积上限   仓库: %s" % REPO)
    print("临时工作目录: %s" % TMP)
    print("=" * 74)
    api = part_1_flag_semantics()

    old_p = off_p = None
    try:
        old_p, off_p = part_2_off_vs_head()
        if off_p is not None:
            part_4_upload(off_p)
        on_p, on_log = start_server("api_server.py", ON_PORT, {
            "FOOD_STRICT_AUTH": "yes", "FOOD_MAX_UPLOAD_MB": "1",
            "FOOD_DATA_DIR": TMP_DATA, "FOOD_TMP_UPLOAD_DIR": TMP_UPLOADS})
        try:
            part_3_strict_on(on_p)
        finally:
            for p in (on_p,):
                try:
                    p.terminate(); p.wait(timeout=15)
                except Exception:
                    try: p.kill()
                    except Exception: pass
            on_log.close()
        part_5_bounded_read(api)
    finally:
        for p, lg in ((old_p, None), (off_p, None)):
            if p is not None:
                try:
                    p.terminate(); p.wait(timeout=15)
                except Exception:
                    try: p.kill()
                    except Exception: pass
        if os.path.exists(OLD_API_IN_CODES):
            try:
                os.remove(OLD_API_IN_CODES)
            except Exception:
                pass
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "-" * 74)
    print("自检结果：PASS %d / FAIL %d" % (PASSES[0], len(FAILS)))
    if FAILS:
        print("未过项：")
        for f in FAILS:
            print("  - %s" % f)
    print("-" * 74)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
