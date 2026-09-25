#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_standard_hierarchy.py — 「类层次 + 实体 Definition」修复的真实 HTTP 自检。

治的是 D1「自动建模产出实体平铺本体」：合规面板实测 `类层次 0`、核心描述项 83%<90%、
提示「无 subClassOf：实体平铺，国标 §5.3 要求派生层次」。

断言（逐条，真实起临时服务 + 打真实 HTTP）：
  H1  /api/ontology/suggest 返回**带依据**的类层次建议与 Definition 建议
        · 每个非根节点都有 rule 与 evidence（依据非空、写明字段/关键词/表名）
        · 每个实体都有父类（不再平铺）
  H2  同输入两次派生结果**完全一致**（确定性 / 可复算）
  H3  依据字段存在：定义建议的 evidence 指向真实表字段；层次建议的 evidence 写明派生证据
  H4  未确认**不落库**：hierarchy_confirmed=false → confirm 拒绝，且 schema 文件未被创建
  H5  层次不完整**不落库**：父类被清空 → confirm 拒绝（不静默通过）
  H6  确认后落库：schema 文件生成，且带 class_hierarchy（根/业务域/命名词干子类）
  H7  激活库合规度按**新本体**计算：切到新库 → /api/standard/compliance 返回该库、
        subclass_count>0、ent_core_rate>=90、standard_rate 抬升（真实数字）
  H8  真实两库（valve + food_co）当前本体已带类层次与定义（只读核对修后不变量）

纪律：不动 data/** 数据目录；临时库数据目录建在系统临时目录（仓库外）；
只在开始/结束时备份并还原 codes/config/active_ontology.json、codes/config/kbs.json、
codes/export/ 导出物，并清理本次新建的临时库产物。纯标准库。

用法：python scripts/verify_standard_hierarchy.py
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
ADMIN_KEY = "verify-admin-key"
READ_KEY = "verify-read-key"
ACTIVE = os.path.join(ROOT, "codes", "config", "active_ontology.json")
KBS_FILE = os.path.join(ROOT, "codes", "config", "kbs.json")
EXPORT_DIR = os.path.join(ROOT, "codes", "export")

_result = []
_procs = []
_auth_dirs = []
_temp_kb = "smhiertest"


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


def http(url, method="GET", body=None, apikey=None, timeout=120):
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
    env["FOOD_AUTH_STATE_DIR"] = tempfile.mkdtemp(prefix="hermes-verify-hier-")
    _auth_dirs.append(env["FOOD_AUTH_STATE_DIR"])
    f = open(log, "wb")
    p = subprocess.Popen([PY, os.path.join(ROOT, "codes", "api_server.py"), "--port", str(port)],
                         cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT)
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


def make_temp_data():
    """造一个仓库外的临时多表数据目录（同域命名词干 → 可派生一级子类）。

    sm_batches / sm_batch_items 共享词干 batch（生产域）→ 派生 BatchGroup；
    sm_customers 命名单例 → 直接挂业务域类。
    """
    d = tempfile.mkdtemp(prefix="hermes-hier-data-")
    with open(os.path.join(d, "sm_batches.csv"), "w", encoding="utf-8", newline="") as f:
        f.write("id,product,quantity,status\nB01,P1,120,running\nB02,P2,80,done\n")
    with open(os.path.join(d, "sm_batch_items.csv"), "w", encoding="utf-8", newline="") as f:
        f.write("id,batch_id,item,amount\nI01,B01,steel,30\nI02,B01,seal,12\n")
    with open(os.path.join(d, "sm_customers.csv"), "w", encoding="utf-8", newline="") as f:
        f.write("id,customer_name,city,credit\nC01,North Refining,Beijing,A\nC02,East Chem,Shanghai,B\n")
    return d


def backup(path):
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    return None


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
    """按给定 schema 文件复算合规数字（与实际面板同源 ontology_check._check_standard）。"""
    import importlib
    sys.path.insert(0, os.path.join(ROOT, "codes"))
    oc = importlib.import_module("ontology_check")
    r = oc._check_standard(os.path.join(ROOT, "codes"), schema_path=schema_abs)
    return r.get("state") or {}


def main():
    active_bak = backup(ACTIVE)
    kbs_bak = backup(KBS_FILE)
    export_bak = {}
    for fn in ("ontology.ttl", "shapes.ttl", "ontology.jsonld"):
        export_bak[fn] = backup(os.path.join(EXPORT_DIR, fn))

    temp_data = make_temp_data()
    made_files = [
        os.path.join(ROOT, "codes", "config", "ontology_schema_%s.json" % _temp_kb),
        os.path.join(ROOT, "codes", "config", "lexicon_%s.json" % _temp_kb),
        os.path.join(ROOT, "codes", "output", "%s.nt" % _temp_kb),
    ]
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    log = os.path.join(ROOT, "codes", "output", "_verify_hier_backend.log")

    try:
        section("启动临时后端 :%d（带 key）" % port)
        be = spawn_backend(port, log)
        up = wait_http(base + "/health", timeout=60)
        ck("临时后端就绪 GET /health", up, "log=%s" % log)
        if not up:
            return 1

        # ================= H1 建议带依据 =================
        section("H1 /api/ontology/suggest 返回带依据的类层次 + Definition 建议")
        st, s1 = http(base + "/api/ontology/suggest", method="POST",
                      body={"kb": _temp_kb, "data_dir": temp_data}, apikey=ADMIN_KEY)
        ck("H1 suggest 成功", st == 200 and s1.get("ok") is True and s1.get("data"),
           "st=%s body=%s" % (st, str(s1)[:220]))
        d1 = s1.get("data") or {}
        nodes = d1.get("hierarchy") or []
        defs = d1.get("definitions") or []
        ck("H1 返回 hierarchy（>=4 节点：根/一级/业务域/子类/实体）", len(nodes) >= 4, len(nodes))
        ck("H1 返回 definitions（每实体一条）",
           len(defs) == len(d1.get("entities") or []) and len(defs) > 0,
           "defs=%d ents=%d" % (len(defs), len(d1.get("entities") or [])))
        nonroot = [n for n in nodes if n.get("parent")]
        ck("H1 每条非根层次都写出 rule + evidence（依据非空）",
           all((n.get("rule") or "").strip() and (n.get("evidence") or "").strip() for n in nonroot),
           [n for n in nonroot if not (n.get("rule") or "").strip() or not (n.get("evidence") or "").strip()][:2])
        ent_nodes = [n for n in nodes if n.get("kind") == "entity"]
        ck("H1 每个实体都有父类（不再实体平铺）",
           len(ent_nodes) == len(d1.get("entities") or []) and all((n.get("parent") or "").strip() for n in ent_nodes),
           [(n.get("name"), n.get("parent")) for n in ent_nodes])
        ck("H1 定义了派生层次：至少一条子类→业务域边（含根→一级）",
           any(n.get("kind") == "stem" for n in nodes), [n.get("name") for n in nodes])
        # 依据要写明实件：关键词 / 词干 / 表名
        ck("H1 业务域节点的依据写明了命中关键词",
           any("关键词" in (n.get("evidence") or "") for n in nodes if n.get("kind") == "domain"),
           [n.get("evidence") for n in nodes if n.get("kind") == "domain"][:2])
        if any(n.get("kind") == "stem" for n in nodes):
            stem_ev = [n.get("evidence") or "" for n in nodes if n.get("kind") == "stem"][0]
            ck("H1 命名词干子类的依据写明了共享词干 + 成员表", "词干" in stem_ev and "_" in stem_ev, stem_ev)

        # ================= H2 确定性 =================
        section("H2 同输入两次派生结果完全一致（确定性/可复算）")
        st, s2 = http(base + "/api/ontology/suggest", method="POST",
                      body={"kb": _temp_kb, "data_dir": temp_data}, apikey=ADMIN_KEY)
        d2 = s2.get("data") or {}
        same_h = json.dumps(d1.get("hierarchy"), sort_keys=True, ensure_ascii=False) == \
                 json.dumps(d2.get("hierarchy"), sort_keys=True, ensure_ascii=False)
        same_d = json.dumps(d1.get("definitions"), sort_keys=True, ensure_ascii=False) == \
                 json.dumps(d2.get("definitions"), sort_keys=True, ensure_ascii=False)
        ck("H2 两次 hierarchy 逐字节一致", same_h)
        ck("H2 两次 definitions 逐字节一致", same_d)

        # ================= H3 依据字段存在 =================
        section("H3 依据指向真实表/字段（可核，不黑箱）")
        tables = {e.get("table") for e in (d1.get("entities") or [])}
        bad_def = []
        for dd in defs:
            ev = dd.get("evidence") or ""
            if "依据表" not in ev:
                bad_def.append(dd.get("entity"))
        ck("H3 每条 Definition 的依据写明来源表与字段", not bad_def, bad_def)
        ck("H3 依据里的表名都在真实实体表集合内",
           all(any(t in (dd.get("evidence") or "") for t in tables if t) for dd in defs),
           [dd.get("entity") for dd in defs][:3])
        ck("H3 定义非空且含取值样例（来自真实行）",
           all(len((dd.get("definition") or "")) > 8 for dd in defs)
           and any("取值样例" in (dd.get("definition") or "") for dd in defs),
           [dd.get("definition")[:60] for dd in defs][:2])

        # ================= H4 未确认不落库 =================
        section("H4 未确认层生 → confirm 拒绝且不落库")
        new_sp = made_files[0]
        if os.path.exists(new_sp):
            os.remove(new_sp)
        payload_schema = {"version": "1.0", "name": "auto-inferred-ontology",
                          "industry": _temp_kb,
                          "entities": d1.get("entities") or [],
                          "relations": d1.get("relations") or [],
                          "constraints": d1.get("constraints") or [],
                          "hierarchy": nodes,
                          "definitions": defs}
        st, r = http(base + "/api/ontology/confirm", method="POST",
                     body={"kb": _temp_kb, "data_dir": temp_data, "schema": payload_schema},
                     apikey=ADMIN_KEY)
        ck("H4 未传 hierarchy_confirmed → 被拒（不静默通过）",
           st == 200 and r.get("ok") is False and "未确认" in str(r.get("error")),
           "st=%s err=%s" % (st, r.get("error")))
        ck("H4【核心】schema 文件未被创建（未确认不落库）", not os.path.exists(new_sp), new_sp)

        # ================= H5 层次不完整不落库 =================
        section("H5 层次不完整（实体缺父类）→ 拒绝")
        broken = json.loads(json.dumps(payload_schema, ensure_ascii=False))
        broken["hierarchy"] = [dict(n) for n in nodes
                              if not (n.get("kind") == "entity" and n.get("name") == ent_nodes[0].get("name"))]
        broken["entities"][0].pop("parent", None)
        st, r = http(base + "/api/ontology/confirm", method="POST",
                     body={"kb": _temp_kb, "data_dir": temp_data, "schema": broken,
                           "hierarchy_confirmed": True}, apikey=ADMIN_KEY)
        ck("H5 缺父类的实体 → 拒绝且提示缺类层次",
           st == 200 and r.get("ok") is False and "缺类层次" in str(r.get("error")),
           "st=%s err=%s" % (st, r.get("error")))
        ck("H5 schema 文件仍未创建", not os.path.exists(new_sp), new_sp)

        # ================= H6 确认后落库 =================
        section("H6 人工确认后落库（class_hierarchy 写入）")
        st, r = http(base + "/api/ontology/confirm", method="POST",
                     body={"kb": _temp_kb, "data_dir": temp_data, "schema": payload_schema,
                           "hierarchy_confirmed": True}, apikey=ADMIN_KEY)
        ck("H6 confirm（已确认）成功",
           st == 200 and r.get("ok") is True and r.get("data", {}).get("status") == "confirmed",
           "st=%s body=%s" % (st, str(r)[:240]))
        ck("H6 schema 文件已生成", os.path.exists(new_sp), new_sp)
        saved = {}
        try:
            saved = json.load(open(new_sp, encoding="utf-8"))
        except Exception:
            pass
        ck("H6 落库 schema 带 class_hierarchy（根/业务域/子类）",
           bool(saved.get("class_hierarchy")), saved.get("class_hierarchy"))
        ck("H6 落库 schema 每个实体都带 parent",
           bool(saved.get("entities")) and all((e.get("parent") or "").strip() for e in saved["entities"]),
           [(e.get("id"), e.get("parent")) for e in (saved.get("entities") or [])][:4])
        ck("H6 落库 schema 每条层次都带依据(parent_evidence)",
           bool(saved.get("entities")) and all((e.get("parent_evidence") or "").strip() for e in saved["entities"]),
           [(e.get("id"), (e.get("parent_evidence") or "")[:40]) for e in (saved.get("entities") or [])][:3])
        ck("H6 落库 schema 定义带取值样例 + 依据(definition_evidence)",
           bool(saved.get("entities"))
           and all((e.get("definition_evidence") or "").strip() for e in saved["entities"])
           and any("取值样例" in (e.get("definition") or "") for e in saved["entities"]),
           [e.get("definition", "")[:60] for e in (saved.get("entities") or [])][:2])

        # ================= H7 激活库合规度按新本体 =================
        section("H7 激活库合规度按新本体计算（修后数字）")
        st, r = http(base + "/api/kb/active", method="POST", body={"kb": _temp_kb}, apikey=ADMIN_KEY)
        ck("H7 切激活到临时库成功", st == 200 and r.get("ok"), "st=%s body=%s" % (st, str(r)[:160]))
        st, c = http(base + "/api/standard/compliance", apikey=ADMIN_KEY)
        ck("H7 compliance 指向当前激活库的本体",
           c.get("ok") is True and c.get("kb") == _temp_kb
           and ("ontology_schema_%s.json" % _temp_kb) in str(c.get("ontology")),
           "st=%s body=%s" % (st, str(c)[:220]))
        ck("H7 subclass_count > 0（不再是实体平铺）", (c.get("subclass_count") or 0) > 0, c.get("subclass_count"))
        ck("H7 ent_core_rate >= 90（核心描述项达标）",
           (c.get("ent_core_rate") or 0) >= 90, c.get("ent_core_rate"))
        ck("H7 standard_rate 有真实数字（>=95）",
           isinstance(c.get("standard_rate"), (int, float)) and c.get("standard_rate") >= 95, c.get("standard_rate"))
        msgs = " | ".join(i.get("message", "") for i in (c.get("issues") or []))
        ck("H7「无类层次(subClassOf): 实体平铺」提示已消失", "实体平铺" not in msgs, msgs[:200])
        ck("H7「核心描述项齐备率 < 90%」提示已消失", "核心描述项齐备率" not in msgs, msgs[:200])
        print("    → 修后合规数字: %s" % json.dumps(
            {k: c.get(k) for k in ("standard_rate", "ent_core_rate", "ent_rate", "attr_rate",
                                   "ns_ok", "subclass_count", "has_export")}, ensure_ascii=False))

        # ================= H8 真实两库修后不变量 =================
        section("H8 真实两库（valve + food_co）本体已带类层次与定义（只读核对）")
        for kb in ("valve", "food_co"):
            sp = os.path.join(ROOT, "codes", "config", "ontology_schema_%s.json" % kb)
            if not os.path.exists(sp):
                ck("H8 %s 有本体 schema" % kb, False, sp)
                continue
            stt = std_numbers(sp)
            ck("H8 %s subclass_count > 0（已派生层次）" % kb, (stt.get("subclass_count") or 0) > 0, stt.get("subclass_count"))
            ck("H8 %s ent_core_rate >= 90" % kb, (stt.get("ent_core_rate") or 0) >= 90, stt.get("ent_core_rate"))
            ck("H8 %s standard_rate >= 95" % kb,
               isinstance(stt.get("standard_rate"), (int, float)) and stt.get("standard_rate") >= 95,
               stt.get("standard_rate"))
            print("    → %s 当前合规数字: %s" % (kb, json.dumps(
                {k: stt.get(k) for k in ("standard_rate", "ent_core_rate", "ent_rate", "attr_rate",
                                         "ns_ok", "subclass_count", "has_export")}, ensure_ascii=False)))
            try:
                sch = json.load(open(sp, encoding="utf-8"))
            except Exception:
                sch = {}
            hier_ok = []
            for e in sch.get("entities", []):
                ev = e.get("parent_evidence") or ""
                if (e.get("parent") or "").strip() and ev.strip():
                    hier_ok.append(e["id"])
            ck("H8 %s 每条实体层次都带 parent_evidence 依据" % kb,
               len(hier_ok) == len(sch.get("entities", [])) and len(hier_ok) > 0,
               [e.get("id") for e in sch.get("entities", []) if e.get("id") not in hier_ok])

    finally:
        for p, f in _procs:
            stop(p)
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
        for fn, blob in export_bak.items():
            restore(os.path.join(EXPORT_DIR, fn), blob)
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
