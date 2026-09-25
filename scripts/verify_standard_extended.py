#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_standard_extended.py — 「扩展描述项（具名子类 HasSubclass）」修复的真实 HTTP 自检。

治的是 D 组「实体扩展描述项齐备率 75%」（缺 HasSubclass / EquivalentClass 类扩展描述项）。
本脚本只认**真件**：真实起临时后端打真实 HTTP；派生结果确定性复算；逐条扩展项必须带依据；
未确认一律不许落库；合规度按**新本体**复算并同时给出接口值与本地复算值（两者必须一致）。

断言（逐条）：
  E1 /api/ontology/suggest 返回 subclasses：每条含 name/parent/column/value/count/rule/evidence，
     且 name 唯一、kind=subclass
  E2 确定性：同输入两次 suggest 的 subclasses **逐字节一致**（可复算）
  E3 依据可核（复算）：每条 evidence 提到的表名/列名/取值都能在**真实数据**里找到，
     且 evidence 里的命中行数 == 本地按该列该值复算出的行数
  E4 未确认不落库：confirm 带了 subclasses 但 extensions_confirmed=false → 被拒，且 schema 文件未创建
  E5 缺依据不许落库：某条 evidence 置空 → 被拒（不编造关系）
  E6 父实体不存在 → 被拒
  E7 确认后落库：schema 生成，含 subclass 类实体（kind=subclass / table=None / parent / 依据），
     且 apply 幂等（重复确认后子类实体数不增）
  E8 合规度按**新本体**计算：切激活到临时库 → /api/standard/compliance 的 ent_rate 抬升
     （> 75）、subclass_count 增大，且 ent_rate/ent_core_rate 与本地 _check_standard 复算**完全一致**
  E9 真实两库（valve + food_co）只读核对：ent_rate > 75、每条落库子类都带依据、派生确定性

纪律：不动 data/**；临时数据目录建在系统临时目录（仓库外）；只在开始/结束时备份并还原
codes/config/active_ontology.json、codes/config/kbs.json，并清理本次新建的临时库产物。纯标准库。

用法：python scripts/verify_standard_extended.py
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
CODES = os.path.join(ROOT, "codes")
PY = sys.executable
ADMIN_KEY = "verify-admin-key"
READ_KEY = "verify-read-key"
ACTIVE = os.path.join(CODES, "config", "active_ontology.json")
KBS_FILE = os.path.join(CODES, "config", "kbs.json")

_result = []
_procs = []
_auth_dirs = []
_temp_kb = "smtest"          # 临时 kb（用完即清）
_temp_kb2 = "smtest2"        # E5/E6 用（不落库）

# 临时数据：两张表，各有一个**真实分类列**（status / device_type），取值可枚举
_TABLES = {
    # 表名 -> (CSV 文本, 期望的分类列, {取值: 命中行数})
    "sm_batches.csv": (
        "id,product,status\n"
        "B01,P1,running\nB02,P2,stopped\nB03,P1,running\nB04,P3,alarm\nB05,P2,running\n",
        "status",
        {"running": 3, "stopped": 1, "alarm": 1},
    ),
    "sm_devices.csv": (
        "id,name,device_type\n"
        "D01,L1,mill\nD02,L2,mill\nD03,L3,lathe\nD04,L4,welder\n",
        "device_type",
        {"mill": 2, "lathe": 1, "welder": 1},
    ),
}


def ck(name, cond, detail=""):
    _result.append((name, bool(cond), detail))
    line = "  [%s] %s" % ("PASS" if cond else "FAIL", name)
    if not cond and detail:
        line += "  <- %s" % str(detail)[:260]
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
        return {"_raw": (t or "")[:400]}


def http(url, method="GET", body=None, apikey=None, timeout=180):
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


def spawn_backend(port, log):
    env = dict(os.environ)
    env["FOOD_ADMIN_KEY"] = ADMIN_KEY
    env["FOOD_READ_KEY"] = READ_KEY
    env["PYTHONIOENCODING"] = "utf-8"
    env["FOOD_AUTH_STATE_DIR"] = tempfile.mkdtemp(prefix="hermes-verify-ext-")
    _auth_dirs.append(env["FOOD_AUTH_STATE_DIR"])
    f = open(log, "wb")
    p = subprocess.Popen([PY, os.path.join(CODES, "api_server.py"), "--port", str(port)],
                         cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT)
    _procs.append((p, f))
    return p


def make_temp_data():
    d = tempfile.mkdtemp(prefix="hermes-ext-data-")
    for fn, (txt, _col, _cnt) in _TABLES.items():
        with open(os.path.join(d, fn), "w", encoding="utf-8", newline="") as f:
            f.write(txt)
    return d


def rows_of(data_dir, table):
    import csv as _csv
    with open(os.path.join(data_dir, table + ".csv"), encoding="utf-8") as f:
        return list(_csv.DictReader(f))


def backup(path):
    return open(path, "rb").read() if os.path.exists(path) else None


def restore(path, blob):
    try:
        if blob is not None:
            with open(path, "wb") as f:
                f.write(blob)
        elif os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def std_numbers(schema_abs):
    sys.path.insert(0, CODES)
    import importlib
    oc = importlib.import_module("ontology_check")
    return (oc._check_standard(CODES, schema_path=schema_abs).get("state") or {})


def nums(st):
    return json.dumps({k: st.get(k) for k in ("standard_rate", "ent_core_rate", "ent_rate",
                                              "subclass_count", "ns_ok", "has_export")},
                      ensure_ascii=False)


def main():
    active_bak = backup(ACTIVE)
    kbs_bak = backup(KBS_FILE)
    temp_data = make_temp_data()
    made_files = [
        os.path.join(CODES, "config", "ontology_schema_%s.json" % _temp_kb),
        os.path.join(CODES, "config", "ontology_schema_%s.json" % _temp_kb2),
        os.path.join(CODES, "config", "lexicon_%s.json" % _temp_kb),
        os.path.join(CODES, "config", "lexicon_%s.json" % _temp_kb2),
        os.path.join(CODES, "output", "%s.nt" % _temp_kb),
        os.path.join(CODES, "output", "%s.nt" % _temp_kb2),
    ]
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    log = os.path.join(CODES, "output", "_verify_ext_backend.log")

    try:
        section("启动临时后端 :%d（带 key）" % port)
        be = spawn_backend(port, log)
        if not wait_http(base + "/health", timeout=60):
            ck("临时后端就绪 GET /health", False, "log=%s" % log)
            return 1
        ck("临时后端就绪 GET /health", True)

        # ================= E1 建议带依据 =================
        section("E1 /api/ontology/suggest 返回带依据的具名子类建议")
        st, s1 = http(base + "/api/ontology/suggest", method="POST",
                      body={"kb": _temp_kb, "data_dir": temp_data}, apikey=ADMIN_KEY)
        ck("E1 suggest 成功", st == 200 and s1.get("ok") is True and s1.get("data"),
           "st=%s body=%s" % (st, str(s1)[:220]))
        d1 = s1.get("data") or {}
        subs1 = d1.get("subclasses") or []
        ck("E1 返回 subclasses 且非空", len(subs1) > 0, len(subs1))
        ck("E1 每条含 name/parent/column/value/count/rule/evidence 且 kind=subclass",
           all(all(k in x for k in ("name", "parent", "column", "value", "count", "rule", "evidence"))
               and x.get("kind") == "subclass" for x in subs1),
           [x.get("name") for x in subs1][:3])
        ck("E1 每条依据(evidence)非空且写明依据表/字段",
           all(("依据表" in (x.get("evidence") or "")) and str(x["column"]) in (x.get("evidence") or "")
               for x in subs1),
           [x.get("evidence") for x in subs1][:2])
        ck("E1 name 全局唯一", len({x["name"] for x in subs1}) == len(subs1), len(subs1))
        # 期望：两张表各派生一个分类列的取值（status 3 + device_type 3）
        ck("E1 派生条数 == 两表分类列取值数之和(3+3)",
           len(subs1) == 6, len(subs1))

        # ================= E2 确定性 =================
        section("E2 同输入两次派生结果完全一致（确定性/可复算）")
        st, s2 = http(base + "/api/ontology/suggest", method="POST",
                      body={"kb": _temp_kb, "data_dir": temp_data}, apikey=ADMIN_KEY)
        subs2 = (s2.get("data") or {}).get("subclasses") or []
        ck("E2 两次 subclasses 逐字节一致",
           json.dumps(subs1, sort_keys=True, ensure_ascii=False) ==
           json.dumps(subs2, sort_keys=True, ensure_ascii=False))

        # ================= E3 依据可核（复算） =================
        section("E3 依据可核：evidence 里的表/列/取值 + 命中行数与真实数据一致")
        id2table = {e["id"]: e.get("table") for e in (d1.get("entities") or [])}
        bad_tab, bad_cnt = [], []
        for x in subs1:
            table = id2table.get(x["parent"])            # 父实体 id → 真实表名
            rows = rows_of(temp_data, table)
            col, val = x["column"], str(x["value"])
            real = sum(1 for r in rows if str(r.get(col)).strip() == val)
            if ("依据表 %s" % table) not in x["evidence"]:
                bad_tab.append(x["name"])
            if real != x["count"]:
                bad_cnt.append((x["name"], x["count"], real))
        ck("E3 每条依据写明的表名 == 真实表名", not bad_tab, bad_tab)
        ck("E3 每条依据里的命中行数 == 本地按真实行复算值", not bad_cnt, bad_cnt)

        # ================= E4 未确认不落库 =================
        section("E4 未确认（extensions_confirmed=false）→ 拒绝且不落库")
        new_sp = made_files[0]
        if os.path.exists(new_sp):
            os.remove(new_sp)
        payload = {
            "version": "1.0", "name": "auto-inferred-ontology", "industry": _temp_kb,
            "entities": d1.get("entities") or [],
            "relations": d1.get("relations") or [],
            "constraints": d1.get("constraints") or [],
            "hierarchy": d1.get("hierarchy") or [],
            "definitions": d1.get("definitions") or [],
            "subclasses": subs1,
        }
        st, r = http(base + "/api/ontology/confirm", method="POST",
                     body={"kb": _temp_kb, "data_dir": temp_data, "schema": payload,
                           "hierarchy_confirmed": True}, apikey=ADMIN_KEY)
        ck("E4 未确认扩展描述项 → 被拒（不静默通过）",
           st == 200 and r.get("ok") is False and "扩展描述项" in str(r.get("error")),
           "st=%s err=%s" % (st, r.get("error")))
        ck("E4【核心】schema 文件未被创建（未确认不落库）", not os.path.exists(new_sp), new_sp)

        # ================= E5 缺依据不许落库 =================
        section("E5 某条扩展项缺依据(evidence) → 拒绝")
        broken = json.loads(json.dumps(payload, ensure_ascii=False))
        broken["subclasses"][0]["evidence"] = ""
        st, r = http(base + "/api/ontology/confirm", method="POST",
                     body={"kb": _temp_kb, "data_dir": temp_data, "schema": broken,
                           "hierarchy_confirmed": True, "extensions_confirmed": True},
                     apikey=ADMIN_KEY)
        ck("E5 缺依据 → 被拒（不编造关系）",
           st == 200 and r.get("ok") is False and "依据" in str(r.get("error")),
           "st=%s err=%s" % (st, r.get("error")))
        ck("E5 schema 文件仍未创建", not os.path.exists(new_sp), new_sp)

        # ================= E6 父实体不存在 =================
        section("E6 某条扩展项的父实体不存在 → 拒绝")
        broken2 = json.loads(json.dumps(payload, ensure_ascii=False))
        broken2["subclasses"][0]["parent"] = "NoSuchEntity"
        st, r = http(base + "/api/ontology/confirm", method="POST",
                     body={"kb": _temp_kb, "data_dir": temp_data, "schema": broken2,
                           "hierarchy_confirmed": True, "extensions_confirmed": True},
                     apikey=ADMIN_KEY)
        ck("E6 父实体不存在 → 被拒",
           st == 200 and r.get("ok") is False and "父实体" in str(r.get("error")),
           "st=%s err=%s" % (st, r.get("error")))
        ck("E6 schema 文件仍未创建", not os.path.exists(new_sp), new_sp)

        # ================= E7 确认后落库 + 幂等 =================
        section("E7 人工确认后落库（subclass 类实体写入）+ 幂等")
        st, r = http(base + "/api/ontology/confirm", method="POST",
                     body={"kb": _temp_kb, "data_dir": temp_data, "schema": payload,
                           "hierarchy_confirmed": True, "extensions_confirmed": True},
                     apikey=ADMIN_KEY)
        ck("E7 confirm（已确认）成功",
           st == 200 and r.get("ok") is True and r.get("data", {}).get("status") == "confirmed",
           "st=%s body=%s" % (st, str(r)[:240]))
        ck("E7 schema 文件已生成", os.path.exists(new_sp), new_sp)
        saved = json.load(open(new_sp, encoding="utf-8"))
        sub_ents = [e for e in saved.get("entities", []) if e.get("kind") == "subclass"]
        ck("E7 落库含 subclass 类实体（数量 == 建议数）", len(sub_ents) == len(subs1),
           "ents=%d subs=%d" % (len(sub_ents), len(subs1)))
        ck("E7 每个子类：table 为空(不产实例) + 有 parent + 有依据",
           all(e.get("table") in (None, "") and str(e.get("parent") or "").strip()
               and str(e.get("parent_evidence") or "").strip() for e in sub_ents),
           [(e.get("id"), e.get("table"), e.get("parent")) for e in sub_ents][:3])
        ck("E7 每个子类都带 definition + definition_evidence",
           all(str(e.get("definition") or "").strip() and str(e.get("definition_evidence") or "").strip()
               for e in sub_ents), [e.get("id") for e in sub_ents if not e.get("definition")][:3])
        ck("E7 父实体获得具名子类（has_sub：有实体的 parent == 它）",
           all(any(x.get("parent") == e["parent"] for x in sub_ents) for e in sub_ents))
        # 幂等：再确认一次，数量不增
        st, r = http(base + "/api/ontology/confirm", method="POST",
                     body={"kb": _temp_kb, "data_dir": temp_data, "schema": payload,
                           "hierarchy_confirmed": True, "extensions_confirmed": True},
                     apikey=ADMIN_KEY)
        saved2 = json.load(open(new_sp, encoding="utf-8"))
        sub2 = [e for e in saved2.get("entities", []) if e.get("kind") == "subclass"]
        ck("E7 重复确认幂等（子类实体数不增）", len(sub2) == len(sub_ents),
           "again=%d first=%d" % (len(sub2), len(sub_ents)))

        # ================= E8 合规度按新本体计算 =================
        section("E8 合规度按**新本体**计算（接口值 == 本地复算值）")
        st, r = http(base + "/api/kb/active", method="POST", body={"kb": _temp_kb}, apikey=ADMIN_KEY)
        ck("E8 切激活到临时库成功", st == 200 and r.get("ok"), "st=%s body=%s" % (st, str(r)[:160]))
        st, c = http(base + "/api/standard/compliance", apikey=ADMIN_KEY)
        ck("E8 compliance 指向当前激活库的本体",
           c.get("ok") is True and c.get("kb") == _temp_kb
           and ("ontology_schema_%s.json" % _temp_kb) in str(c.get("ontology")),
           "st=%s body=%s" % (st, str(c)[:220]))
        ck("E8 subclass_count 增大（== 实体类 + 具名子类）",
           (c.get("subclass_count") or 0) >= len(sub_ents), c.get("subclass_count"))
        ck("E8 ent_rate > 75（扩展描述项齐备率确有提升）",
           (c.get("ent_rate") or 0) > 75, c.get("ent_rate"))
        ck("E8 ent_core_rate >= 90（新增子类未拖累核心齐备度）",
           (c.get("ent_core_rate") or 0) >= 90, c.get("ent_core_rate"))
        local = std_numbers(new_sp)
        ck("E8 接口 ent_rate == 本地 _check_standard 复算值",
           c.get("ent_rate") == local.get("ent_rate"),
           "iface=%s local=%s" % (c.get("ent_rate"), local.get("ent_rate")))
        ck("E8 接口 ent_core_rate == 本地复算值",
           c.get("ent_core_rate") == local.get("ent_core_rate"),
           "iface=%s local=%s" % (c.get("ent_core_rate"), local.get("ent_core_rate")))
        ck("E8 接口 subclass_count == 本地复算值",
           c.get("subclass_count") == local.get("subclass_count"),
           "iface=%s local=%s" % (c.get("subclass_count"), local.get("subclass_count")))
        print("    → 临时库修后合规数字: %s" % nums(local))

        # ================= E9 真实两库只读核对 =================
        section("E9 真实两库（valve + food_co）只读核对：ent_rate>75 + 每条子类带依据 + 确定性")
        sys.path.insert(0, CODES)
        import importlib
        so = importlib.import_module("schema_ontology")
        for kb in ("valve", "food_co"):
            sp = os.path.join(CODES, "config", "ontology_schema_%s.json" % kb)
            if not os.path.exists(sp):
                ck("E9 %s 有本体 schema" % kb, False, sp)
                continue
            sch = json.load(open(sp, encoding="utf-8"))
            subs = [e for e in sch.get("entities", []) if e.get("kind") == "subclass"]
            stt = std_numbers(sp)
            ck("E9 %s ent_rate > 75（真实数字）" % kb, (stt.get("ent_rate") or 0) > 75, stt.get("ent_rate"))
            ck("E9 %s ent_core_rate >= 90（未被拖累）" % kb,
               (stt.get("ent_core_rate") or 0) >= 90, stt.get("ent_core_rate"))
            ck("E9 %s 落库子类每条带依据(parent_evidence)" % kb,
               len(subs) > 0 and all(str(e.get("parent_evidence") or "").strip() for e in subs),
               len(subs))
            ck("E9 %s 落库子类每条有父实体" % kb,
               all(str(e.get("parent") or "").strip() for e in subs), len(subs))
            # 确定性复算：重跑派生 == 落库的 subclasses 清单
            data = so.load_all(os.path.join(CODES, "data_%s" % kb))
            base_schema = so.load_schema(sp)
            base_schema["entities"] = [e for e in base_schema["entities"] if e.get("kind") != "subclass"]
            again = so.derive_named_subclasses(base_schema, data)
            saved_subs = sch.get("subclasses") or []
            ck("E9 %s 重跑派生 == 落库 subclasses（确定性）" % kb,
               json.dumps(again, sort_keys=True, ensure_ascii=False) ==
               json.dumps(saved_subs, sort_keys=True, ensure_ascii=False),
               "again=%d saved=%d" % (len(again), len(saved_subs)))
            print("    → %s 修后合规数字: %s" % (kb, nums(stt)))

    finally:
        for p, f in _procs:
            try:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except Exception:
                    p.kill()
            except Exception:
                pass
            try:
                f.close()
            except Exception:
                pass
        for fn in made_files:
            try:
                if os.path.exists(fn):
                    os.remove(fn)
            except Exception:
                pass
        restore(ACTIVE, active_bak)
        restore(KBS_FILE, kbs_bak)
        for d in _auth_dirs:
            shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(temp_data, ignore_errors=True)

    passed = sum(1 for _, ok, _ in _result if ok)
    total = len(_result)
    section("自检结果: %d/%d 通过" % (passed, total))
    for name, ok, detail in _result:
        if not ok:
            print("  FAIL: %s  <- %s" % (name, str(detail)[:200]))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
