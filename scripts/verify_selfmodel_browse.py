#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_selfmodel_browse.py — 自助建模「目录浏览 + 自动匹配」自检（真实起临时服务打真实 HTTP）

跑: <python> scripts/verify_selfmodel_browse.py
（可选: VERIFY_SM_BFF_PORT 指定临时 BFF 端口，默认 3011）

对应本次交付的 5 条硬要求，每条都打真实 HTTP / 跑真实代码路径，给真实输出：
  ① 目录浏览接口「仅本机」：非 127.0.0.1/::1 访问 → 403（用本机局域网 IP 真发请求）；
  ② 列目录只返回目录名 + 基本元信息，绝不返回文件内容（独立核验：真目录、无文件名、无 CSV 表头）；
  ③ 自动匹配规则逐条断言（kb↔目录双向推导、占用冲突、未匹配→新目录、危险路径、外部目录）；
  ④ 危险路径（盘符根 / 系统目录）被拦；
  ⑤ 外部数据目录被标注（BFF 校验 external=true；后端 _update_kbs 写 kbs.json external=true）。

纪律：全程 %TEMP% 临时副本（users/sessions/web_state 备份后恢复），临时端口自起自收，
      不接后端、不建真实模型、不碰真实 kbs.json / data 数据；纯标准库。
退出码: 全过 0；有 FAIL 1。
"""
import os
import sys
import json
import time
import shutil
import socket
import tempfile
import subprocess
import urllib.request
import urllib.error
import urllib.parse

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
CODES = os.path.join(REPO, "codes")
WEB = os.path.join(REPO, "web")
PY = sys.executable

PORT = int(os.environ.get("VERIFY_SM_BFF_PORT", "3011"))

TMP = tempfile.mkdtemp(prefix="verify_selfmodel_")
EXT_DIR = os.path.join(TMP, "ext_data_probe")          # 仓库外数据目录（含 csv，未注册）
os.makedirs(EXT_DIR, exist_ok=True)
with open(os.path.join(EXT_DIR, "probe_table.csv"), "w", encoding="utf-8") as fh:
    fh.write("id,name,value\n1,甲,10\n2,乙,20\n")

FAILS = []
PASSES = [0]


def ck(name, cond, detail=""):
    if cond:
        PASSES[0] += 1
        print("  [PASS] %s%s" % (name, ("  —— " + str(detail)) if detail else ""))
    else:
        FAILS.append(name)
        print("  [FAIL] %s%s" % (name, ("  —— " + str(detail)) if detail else ""))


# ── HTTP 工具（纯标准库）──────────────────────────────────────────────
def http(method, url, body=None, headers=None, timeout=15, token=None):
    """返回 (status, text)。不改全局代理。"""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if token:
        hdrs["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, "连接失败: %s" % e


def local_ip():
    """本机局域网 IP（非 127.*），用于「非本机访问」测试。"""
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
    except Exception:
        ips = []
    for ip in ips:
        if ip.startswith("127.") or ip.startswith("169.254."):
            continue
        return ip
    return ""


# ── 起临时 BFF ────────────────────────────────────────────────────────
BACKUPS = {}
for rel in ("users.json", "sessions.json", "web_state.json"):
    src = os.path.join(WEB, rel)
    if os.path.exists(src):
        dst = os.path.join(TMP, rel + ".bak")
        shutil.copy2(src, dst)
        BACKUPS[src] = dst

NODE = shutil.which("node") or "node"
log_path = os.path.join(TMP, "bff.log")
log = open(log_path, "wb")
env = dict(os.environ, PORT=str(PORT), PYTHONIOENCODING="utf-8")
proc = subprocess.Popen([NODE, "server/index.js"], cwd=WEB, env=env, stdout=log, stderr=subprocess.STDOUT)
TOKEN = ""


def wait_ready(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if proc.poll() is not None:
            return False
        st, _ = http("GET", "http://127.0.0.1:%d/health" % PORT, timeout=3)
        if st == 200:
            return True
        time.sleep(0.5)
    return False


def cleanup():
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
    except Exception:
        pass
    # 恢复备份（临时用户/会话/激活态不残留到真实运行态）
    for dst, src in BACKUPS.items():
        try:
            shutil.copy2(src, dst)
        except Exception:
            pass
    try:
        log.close()
    except Exception:
        pass


print("=" * 78)
print("自助建模 · 目录浏览与自动匹配自检  verify_selfmodel_browse.py")
print("=" * 78)
print("仓库根            : %s" % REPO)
print("临时 BFF 端口     : %d" % PORT)
print("外部数据目录(探测) : %s" % EXT_DIR)
print()

if not os.path.exists(os.path.join(CODES, "config", "kbs.json")):
    print("[!] 未找到 codes/config/kbs.json，无法自检")
    cleanup()
    sys.exit(2)

if not wait_ready():
    print("[!] 临时 BFF 未就绪，日志尾部：")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            print("".join(fh.readlines()[-20:]))
    except Exception:
        pass
    cleanup()
    sys.exit(2)
print("[i] 临时 BFF 已就绪 http://127.0.0.1:%d\n" % PORT)

try:
    base = "http://127.0.0.1:%d" % PORT
    lan = local_ip()

    # ── 登录（临时用户，事后恢复 users.json）─────────────────────────
    st, txt = http("POST", base + "/api/auth/register",
                   {"username": "verify_selfmodel_tmp", "password": "vt-pass-123",
                    "enterpriseName": "自检临时企业", "industry": ""})
    if st == 200:
        TOKEN = (json.loads(txt) or {}).get("token", "")
    if not TOKEN:
        st, txt = http("POST", base + "/api/auth/login",
                       {"username": "verify_selfmodel_tmp", "password": "vt-pass-123"})
        if st == 200:
            TOKEN = (json.loads(txt) or {}).get("token", "")
    ck("鉴权会话可用（用于访问自助建模端点）", bool(TOKEN), "token 长度 %d" % len(TOKEN))

    # ── ① 列盘符（本机）────────────────────────────────────────────
    print("\n[① 目录浏览接口 · 本机可用 / 非本机 403]")
    st, txt = http("GET", base + "/api/fs/drives")
    drv = json.loads(txt) if st == 200 else {}
    ck("GET /api/fs/drives 本机 200", st == 200, "status=%s" % st)
    ck("盘符列表非空", bool(drv.get("drives")), [d.get("label") for d in drv.get("drives", [])])
    ck("盘符项只含 root/label（无文件内容字段）",
       all(set(d.keys()) <= {"root", "label"} for d in drv.get("drives", [])))

    st, _ = http("GET", base + "/api/fs/dirs?path=" + urllib.parse.quote(CODES))
    ck("GET /api/fs/dirs 本机 200", st == 200, "status=%s" % st)

    if lan:
        st2, t2 = http("GET", "http://%s:%d/api/fs/drives" % (lan, PORT), timeout=8)
        ck("非本机(%s) 访问 /api/fs/drives → 403" % lan, st2 == 403, "status=%s body=%s" % (st2, t2[:80]))
        st3, t3 = http("GET", "http://%s:%d/api/fs/dirs?path=%s" % (lan, PORT, urllib.parse.quote(CODES)), timeout=8)
        ck("非本机(%s) 访问 /api/fs/dirs → 403" % lan, st3 == 403, "status=%s body=%s" % (st3, t3[:80]))
    else:
        print("  [SKIP] 本机无可用非回环 IP，无法测「非本机 403」")

    # ── ② 列目录只返回目录名 + 元信息，不含文件内容 ──────────────────
    print("\n[② 列目录不含文件内容（独立核验）]")
    st, txt = http("GET", base + "/api/fs/dirs?path=" + urllib.parse.quote(CODES))
    body = json.loads(txt) if st == 200 else {}
    entries = body.get("dirs", [])
    allow = {"name", "path", "has_csv", "csv_count", "registered_by", "suggested_kb", "external"}
    ck("目录项字段白名单一致", all(set(e.keys()) <= allow for e in entries),
       sorted({k for e in entries for k in e.keys()}))
    bad_dir = [e["path"] for e in entries if not os.path.isdir(e["path"])]
    ck("列出的每一项都是真实目录（不是文件）", not bad_dir, bad_dir[:3])
    real_files = [f for f in os.listdir(CODES) if os.path.isfile(os.path.join(CODES, f))]
    leaked = [f for f in real_files if f in txt]
    ck("响应未泄露任何真实文件名", not leaked, leaked[:3])
    dv = next((e for e in entries if e["name"] == "data_valve"), None)
    dv_real = len([f for f in os.listdir(os.path.join(CODES, "data_valve")) if f.lower().endswith(".csv")]) \
        if os.path.isdir(os.path.join(CODES, "data_valve")) else -1
    ck("data_valve 的 csv 计数与磁盘实际一致", dv is not None and dv["csv_count"] == dv_real,
       "接口=%s 磁盘=%s" % (dv["csv_count"] if dv else None, dv_real))
    try:
        with open(os.path.join(CODES, "data_valve", sorted(os.listdir(os.path.join(CODES, "data_valve")))[0]),
                  encoding="utf-8", errors="ignore") as fh:
            header = fh.readline().strip()
        ck("响应不含 CSV 表头内容", header not in txt, "header=%r" % header[:40])
    except Exception:
        print("  [SKIP] 无 CSV 可用于表头泄漏核验")

    st, t = http("GET", base + "/api/fs/dirs?path=" + urllib.parse.quote(os.path.join(CODES, "no_such_dir_xyz")))
    ck("不存在的目录 → 非 200（明确拒绝，不静默回退）", st != 200, "status=%s" % st)

    # ── ③ 候选枚举（真实数据）与自动匹配逐条 ────────────────────────
    print("\n[③ 候选目录从真实数据枚举 + 自动匹配规则]")
    st, txt = http("GET", base + "/api/ontology/selfmodel/candidates", token=TOKEN)
    c = json.loads(txt) if st == 200 else {}
    cands = c.get("candidates", [])
    ck("GET /api/ontology/selfmodel/candidates 200", st == 200, "status=%s" % st)
    ck("候选非空（真实枚举）", len(cands) > 0, "%d 项" % len(cands))
    paths = {x["path"] for x in cands}
    ck("含 kbs.json 已注册目录 data_valve", "data_valve" in paths)
    ck("含仓库内真实 data* 目录 data_precision", "data_precision" in paths, sorted(paths)[:6])
    dv_c = next((x for x in cands if x["path"] == "data_valve"), {})
    ck("候选带证据：已被 valve kb 注册", "valve" in (dv_c.get("registered_by") or []),
       dv_c.get("registered_by"))
    # 独立核验每个存在候选的 csv 计数
    mism = []
    for x in cands:
        if not x.get("exists"):
            continue
        ap = x["path"] if os.path.isabs(x["path"]) else os.path.join(CODES, x["path"])
        if not os.path.isdir(ap):
            continue
        real = len([f for f in os.listdir(ap) if f.lower().endswith(".csv")])
        if real != x.get("csv_count"):
            mism.append((x["path"], x.get("csv_count"), real))
    ck("候选 csv 计数与磁盘实际一致", not mism, mism[:3])

    def val(kb, d):
        st, t = http("POST", base + "/api/ontology/selfmodel/validate", {"kb": kb, "dir": d}, token=TOKEN)
        return (json.loads(t).get("data") or {}) if st == 200 else {}

    v = val("valve", "data_valve")
    ck("规则A 输入 kb=valve → 命中 data_valve（registered/name-match）",
       v.get("match") in ("registered", "name-match") and not v.get("blocked"),
       "match=%s blocked=%s" % (v.get("match"), v.get("blocked")))
    v = val("whatever_new", "data_precision")
    ck("规则B 选中 data_precision → 反推建议 kb=precision",
       v.get("suggested_kb") == "precision", "suggested_kb=%s" % v.get("suggested_kb"))
    v = val("probe_x", EXT_DIR)
    ck("规则C 未注册外部目录 → match=none 且显式提示新目录", v.get("match") == "none" and v.get("note"), v.get("note"))
    ck("规则C2 外部数据目录被标注 external=true", v.get("external") is True, "external=%s" % v.get("external"))
    v = val("brand_new_kb", "data_valve")
    ck("规则D 目录已被别的 kb 占用 → blocked（冲突要挡）",
       v.get("blocked") and any("占用" in p for p in (v.get("problems") or [])),
       v.get("problems"))
    v = val("brand_new_kb", "data_not_exist_abc")
    ck("规则E 目录不存在 → blocked（存在性校验）",
       v.get("blocked") and any("不存在" in p for p in (v.get("problems") or [])), v.get("problems"))

    # ── ④ 危险路径被拦 ──────────────────────────────────────────────
    print("\n[④ 危险路径（盘符根 / 系统目录）被拦]")
    for dp in ("C:/", "C:/Windows", "C:/Windows/System32"):
        v = val("kb_x", dp)
        ck("危险路径 %s → dangerous=true 且 blocked" % dp,
           v.get("dangerous") is True and v.get("blocked") is True, v.get("problems"))

    # ── ⑤ 外部目录写进 kbs.json 时被标注（真实后端代码路径）────────
    print("\n[⑤ 外部数据目录写入 kbs.json 被标注 external]")
    sys.path.insert(0, CODES)
    os.environ.setdefault("FOOD_AUDIT_FILE", os.path.join(TMP, "audit.log"))
    tmp_kbs = os.path.join(TMP, "kbs_probe.json")
    try:
        with open(os.path.join(CODES, "config", "kbs.json"), encoding="utf-8") as fh:
            shutil.copyfileobj(fh, open(tmp_kbs, "w", encoding="utf-8"))
        import api_server as api
        api.KBS_FILE = tmp_kbs
        api._update_kbs("probe_ext_kb", "output/probe_ext_kb.nt", "config/lexicon_probe_ext_kb.json",
                        data_dir=EXT_DIR)
        api._update_kbs("probe_in_kb", "output/probe_in_kb.nt", "config/lexicon_probe_in_kb.json",
                        data_dir="data_valve")
        with open(tmp_kbs, encoding="utf-8") as fh:
            kbs = (json.load(fh) or {}).get("kbs", {})
        ck("外部绝对路径 → entry.external=true", kbs.get("probe_ext_kb", {}).get("external") is True,
           kbs.get("probe_ext_kb"))
        ck("仓库内相对目录 → 不标 external（无冗余字段）",
           "external" not in kbs.get("probe_in_kb", {}), kbs.get("probe_in_kb"))
        ck("后端危险路径判定与 BFF 同口径",
           api._is_dangerous_path("C:/Windows") and api._is_dangerous_path("C:/") and not api._is_dangerous_path(EXT_DIR))
    except Exception as e:
        ck("后端 _update_kbs 外部标注（代码路径实测）", False, "import/调用失败: %s" % e)

    # ── ⑥ 反向断链已修 ──────────────────────────────────────────────
    print("\n[⑥ check_chainbreak 不再报 self-onboard 反向断链]")
    try:
        r = subprocess.run([PY, os.path.join(HERE, "check_chainbreak.py")],
                           cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        out = r.stdout or ""
        ck("不再出现「/api/ontology/self-onboard 后端无此路由」",
           "self-onboard             后端无此路由" not in out and "后端无此路由  (ontology.js:selfOnboard)" not in out)
        ck("反向断链计数=0", "BFF 指向不存在端点: 0" in out,
           next((ln.strip() for ln in out.splitlines() if "反向汇总" in ln), ""))
    except Exception as e:
        ck("check_chainbreak 运行", False, str(e))

except Exception as e:
    import traceback
    traceback.print_exc()
    FAILS.append("脚本异常: %s" % e)
finally:
    cleanup()

print()
print("-" * 78)
print("通过 %d 项；失败 %d 项" % (PASSES[0], len(FAILS)))
if FAILS:
    print("失败项: %s" % FAILS)
    print("结论: 自助建模目录浏览/自动匹配自检 未全过")
else:
    print("结论: 自助建模目录浏览/自动匹配自检 全过")
print("-" * 78)
sys.exit(1 if FAILS else 0)
