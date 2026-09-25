#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""apply_standard_extensions.py — 把「扩展描述项（具名子类 HasSubclass）」按真实依据、
经【人确认】落到指定 kb 的本体 schema。

流程（每一步都是真实 HTTP，走与前端「确认生效」同一条后端链路）：
  1) 起临时后端（带 key）；
  2) POST /api/ontology/suggest → 拿到带依据的具名子类建议（只读预览，不落盘）；
  3) 从 kb 现有 schema 读出 class_hierarchy / definitions（上一轮已确认的）作为确认内容，
     连同本轮的 subclasses 一起，POST /api/ontology/confirm，并显式传
     hierarchy_confirmed=true + extensions_confirmed=true（=「人已点头」）；
  4) 打印该 kb 的修前/修后真实合规数字（standard_rate/ent_core_rate/ent_rate/
     subclass_count/ns_ok/has_export）。

纪律：
  · 不 commit / 不 push；不动 data/**；只在当前工作区状态上追加；
  · confirm 会顺带重算 nt 与 lexicon —— 词典(lexicon_*)若已被公共词典合并，
    本脚本会在落库后**还原**它（避免覆盖已合并的公共层），并还原 active_ontology.json；
  · 纯标准库。

用法：
  python scripts/apply_standard_extensions.py                 # 落库 valve + food_co
  python scripts/apply_standard_extensions.py --kb valve      # 只落一个
  python scripts/apply_standard_extensions.py --dry-run       # 只看建议，不落库
"""
import argparse
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根
CODES = os.path.join(ROOT, "codes")
PY = sys.executable
ADMIN_KEY = "verify-admin-key"
READ_KEY = "verify-read-key"
ACTIVE = os.path.join(CODES, "config", "active_ontology.json")


def _json(t):
    try:
        return json.loads(t)
    except Exception:
        return {"_raw": (t or "")[:400]}


def http(url, method="GET", body=None, apikey=None, timeout=300):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if apikey:
        headers["X-API-Key"] = apikey
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _json(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read().decode("utf-8", "replace"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_http(url, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            st, _ = http(url, timeout=3)
            if st:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def std_numbers(schema_abs):
    sys.path.insert(0, CODES)
    import importlib
    oc = importlib.import_module("ontology_check")
    return (oc._check_standard(CODES, schema_path=schema_abs).get("state") or {})


def numbers_line(st):
    return json.dumps({k: st.get(k) for k in ("standard_rate", "ent_core_rate", "ent_rate",
                                              "subclass_count", "ns_ok", "has_export")},
                      ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", action="append", default=None,
                    help="要落库的 kb（可多次）；缺省 valve + food_co")
    ap.add_argument("--dry-run", action="store_true", help="只打印建议，不落库")
    args = ap.parse_args()
    kbs = args.kb or ["valve", "food_co"]

    port = free_port()
    base = "http://127.0.0.1:%d" % port
    env = dict(os.environ)
    env["FOOD_ADMIN_KEY"] = ADMIN_KEY
    env["FOOD_READ_KEY"] = READ_KEY
    env["PYTHONIOENCODING"] = "utf-8"
    env["FOOD_AUTH_STATE_DIR"] = tempfile.mkdtemp(prefix="hermes-apply-ext-")
    logp = os.path.join(CODES, "output", "_apply_ext_backend.log")
    be = subprocess.Popen([PY, os.path.join(CODES, "api_server.py"), "--port", str(port)],
                          cwd=ROOT, env=env, stdout=open(logp, "wb"), stderr=subprocess.STDOUT)

    act_bak = open(ACTIVE, "rb").read() if os.path.exists(ACTIVE) else None
    lex_bak, nt_bak = {}, {}
    for kb in kbs:
        lp = os.path.join(CODES, "config", "lexicon_%s.json" % kb)
        lex_bak[kb] = open(lp, "rb").read() if os.path.exists(lp) else None
        np_ = os.path.join(CODES, "output", "%s.nt" % kb)
        nt_bak[kb] = open(np_, "rb").read() if os.path.exists(np_) else None

    try:
        if not wait_http(base + "/health", timeout=60):
            print("临时后端未就绪，见", logp)
            return 1
        rc = 0
        for kb in kbs:
            sp = os.path.join(CODES, "config", "ontology_schema_%s.json" % kb)
            if not os.path.exists(sp):
                print("[%s] 无 schema，跳过" % kb)
                continue
            print("=" * 72)
            print("[%s] 修前: %s" % (kb, numbers_line(std_numbers(sp))))
            schema = json.load(open(sp, encoding="utf-8"))
            # 1) 只读预览建议
            st, s = http(base + "/api/ontology/suggest", "POST",
                         {"kb": kb, "data_dir": "data_%s" % kb}, apikey=ADMIN_KEY)
            if st != 200 or not s.get("ok"):
                print("[%s] suggest 失败: %s" % (kb, str(s)[:200]))
                rc = 1
                continue
            subs = (s.get("data") or {}).get("subclasses") or []
            print("[%s] 具名子类建议 %d 条（逐条带依据）" % (kb, len(subs)))
            for x in subs[:4]:
                print("      %-32s <- %s" % (x["name"], x["evidence"]))
            if len(subs) > 4:
                print("      ...（其余 %d 条同规则）" % (len(subs) - 4))
            if args.dry_run:
                continue
            # 2) 人确认 → 落库（剔除历史子类实体，保证幂等）
            ents = [e for e in schema.get("entities", []) if e.get("kind") != "subclass"]
            payload = {
                "version": schema.get("version", "1.0"),
                "name": schema.get("name", "auto-inferred-ontology"),
                "industry": kb,
                "entities": ents,
                "relations": schema.get("relations", []),
                "constraints": schema.get("constraints", []),
                "hierarchy": schema.get("hierarchy", []),
                "definitions": schema.get("definitions", []),
                "subclasses": subs,
            }
            st, r = http(base + "/api/ontology/confirm", "POST",
                         {"kb": kb, "schema": payload, "data_dir": "data_%s" % kb,
                          "hierarchy_confirmed": True, "extensions_confirmed": True},
                         apikey=ADMIN_KEY)
            if st != 200 or not r.get("ok"):
                print("[%s] confirm 失败: %s" % (kb, str(r)[:300]))
                rc = 1
                continue
            print("[%s] 落库成功: %s" % (kb, json.dumps({k: r["data"].get(k) for k in
                                                        ("schema_path", "nt", "lexicon", "status")},
                                                       ensure_ascii=False)))
            print("[%s] 修后: %s" % (kb, numbers_line(std_numbers(sp))))
        return rc
    finally:
        try:
            be.terminate()
            be.wait(timeout=10)
        except Exception:
            try:
                be.kill()
            except Exception:
                pass
        # 还原：active（必须留在 valve）+ lexicon（保留已合并的公共层）+ nt（保守起见保持原样）
        try:
            if act_bak is not None:
                open(ACTIVE, "wb").write(act_bak)
        except Exception:
            pass
        for kb in kbs:
            try:
                lp = os.path.join(CODES, "config", "lexicon_%s.json" % kb)
                if lex_bak[kb] is not None:
                    open(lp, "wb").write(lex_bak[kb])
            except Exception:
                pass
        shutil.rmtree(env["FOOD_AUTH_STATE_DIR"], ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
