#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_flows.py — 批 4b（编排上半截）自检脚本（终端真实输出）。

覆盖规格门：
  ① 一条 5 步流程（含并行 + 门控）跑通且有事件流
  ② 门控打回能回到指定上游步
  ③ 流程加载失败在**接口**输出真实原因
  ④ 预设与流程定义一一对应
外加：no_input 不判整条失败；失败停在该步且原因真实；audit_chain 可回放。

运行： C:/Python312/python.exe scripts/verify_flows.py
纪律： 纯标准库；临时文件全部落 %TEMP%；自起临时端口，收尾关掉；不动真实数据。
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

HERE = os.path.dirname(os.path.abspath(__file__))
CODES = os.path.join(os.path.dirname(HERE), "codes")
PY = sys.executable
sys.path.insert(0, CODES)

import flow_engine as fe                      # noqa: E402
from audit_chain import AuditChain            # noqa: E402
from event_bus import EventBus                # noqa: E402

FAILURES = []
PASSES = []


def check(name, cond, detail=""):
    mark = "✅ PASS" if cond else "❌ FAIL"
    print(f"  {mark}  {name}" + (f"  —— {detail}" if detail else ""))
    (PASSES if cond else FAILURES).append(name)
    return cond


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def tmp_flows_dir():
    """构造临时流程目录：2 个好流程副本 + 2 个**故意损坏**的定义（暴露真实原因）。"""
    d = tempfile.mkdtemp(prefix="verify_flows_")
    src = os.path.join(CODES, "flows")
    for fn in ("demo_5step_parallel_gate.json", "demo_branch_loop.json", "presets.json"):
        shutil.copy(os.path.join(src, fn), os.path.join(d, fn))
    # 损坏 1：flow_id 与文件名不一致
    with open(os.path.join(d, "broken_name_mismatch.json"), "w", encoding="utf-8") as f:
        json.dump({"flow_id": "wrong_id", "name": "坏流程1",
                   "steps": [{"id": "x", "kind": "data_source", "ref": "records"}],
                   "edges": []}, f, ensure_ascii=False)
    # 损坏 2：边指向不存在的步骤
    with open(os.path.join(d, "broken_bad_edge.json"), "w", encoding="utf-8") as f:
        json.dump({"flow_id": "broken_bad_edge", "name": "坏流程2",
                   "entry": "a",
                   "steps": [{"id": "a", "kind": "data_source", "ref": "records"}],
                   "edges": [{"from": "a", "to": "ghost"}]}, f, ensure_ascii=False)
    return d


# ═══════════════════════════════════════════════════════════════════
def test_1_five_step_flow():
    """① 5 步流程（并行+门控）跑通 + 事件流。"""
    section("① 5 步流程（含并行 + 门控）跑通，且有事件流")
    db = os.path.join(tempfile.gettempdir(), f"verify_flows_audit_{os.getpid()}.db")
    for p in (db,):
        if os.path.exists(p):
            os.remove(p)
    bus = EventBus()
    audit = AuditChain(db)
    eng, reg, pm = fe.build_engine(with_builtins=True)
    eng.bus = bus
    eng.audit = audit

    flow = eng.flow_registry.get("demo_5step_parallel_gate")
    print(f"流程定义: {flow['flow_id']}  步骤数={len(flow['_order'])}  "
          f"步骤={flow['_order']}")
    r = eng.run(flow)

    print("\n时间线（真实输出）:")
    print("  seq  步骤        控制      状态        -> 下一步   说明")
    for t in r["timeline"]:
        print(f"  {t['seq']:<4} {t['step_id']:<11} {t['control']:<9} "
              f"{t['status']:<10} -> {str(t['next']):<9} {t.get('reason','')}")
    print(f"\n流程状态: {r['status']}   ok={r['ok']}   走步数={len(r['timeline'])}")

    check("流程共 5 个定义步骤", len(flow["_order"]) == 5, str(flow["_order"]))
    check("流程整体跑通(不 failed)", r["status"] != "failed" and r["ok"] is True)
    check("含并行步且执行成功",
          any(t["control"] == "parallel" and t["status"] == "success" for t in r["timeline"]))
    check("含门控步", any(t["control"] == "gate" for t in r["timeline"]))
    check("收尾步执行到", r["steps"].get("s3", {}).get("status") == "success")

    evs = bus.events()
    types = bus.count_by_type()
    print(f"\n事件流（event_bus 真实记录，共 {len(evs)} 条）: {types}")
    for e in evs[:4]:
        print(f"  {e['type']:<14} {e['payload'].get('step_id', e['payload'].get('status',''))}")
    check("有事件流且含 flow.started", types.get(fe.FLOW_STARTED, 0) >= 1, str(types))
    check("有每步事件 flow.step", types.get(fe.FLOW_STEP, 0) >= 5)
    check("有 flow.finished", types.get(fe.FLOW_FINISHED, 0) == 1)

    rp = fe.replay(audit, flow["flow_id"])
    print(f"\naudit_chain 回放: {rp['count']} 条记录")
    for e in rp["entries"][:3]:
        print(f"  #{e['sequence_id']} {e['step_id']} -> {e['status']} | {e['notice']}")
    ok_chain, bad = audit.verify_chain()
    check("全过程落 audit_chain 且链完整", rp["count"] >= 5 and ok_chain,
          f"回放条数={rp['count']} 链完整={ok_chain} bad={bad}")

    global _GOOD_RUN
    _GOOD_RUN = r
    return r


def test_2_gate_reject_back():
    """② 门控打回能回到指定上游步。"""
    section("② 门控打回能回到定义里指定的上游步")
    r = _GOOD_RUN
    g = r["steps"].get("g1", {})
    print(f"门控 g1: on_reject={g.get('on_reject')}  on_pass={g.get('on_pass')}")
    print(f"打回次数 gate_rejects={r['gate_rejects']}")

    # timeline 里门控 rejected 之后紧接的步必须 == on_reject
    tl = r["timeline"]
    idx = next((i for i, t in enumerate(tl) if t["control"] == "gate"
                and t["status"] == fe.STATUS_REJECTED), None)
    print("\n门控打回处的真实时间线片段:")
    for t in tl[max(0, (idx or 0) - 2):(idx or 0) + 3]:
        print(f"  {t['seq']:<3} {t['step_id']:<8} {t['status']:<10} -> {t['next']}")

    check("门控确实发生过打回(rejected)", idx is not None)
    check("打回目标 = 定义里的 on_reject",
          idx is not None and tl[idx]["next"] == g.get("on_reject"),
          f"next={tl[idx]['next'] if idx is not None else None}")
    check("上游步被重新执行(attempt 递增)",
          r["steps"].get("s2", {}).get("attempt", 0) == 2,
          f"s2.attempt={r['steps'].get('s2', {}).get('attempt')}")
    check("打回后重跑通过并继续到收尾",
          r["steps"].get("g1", {}).get("status") == "success"
          and r["steps"].get("s3", {}).get("status") == "success")
    check("打回次数如实记录", r["gate_rejects"].get("g1") == 1, str(r["gate_rejects"]))


def test_3_no_input_and_failure():
    """补充：上游无输入写「没有输入」不判失败；失败停在该步且原因真实。"""
    section("③ 补充纪律：上游无输入不判失败；失败停在该步、原因真实")
    eng, reg, pm = fe.build_engine()
    bus = EventBus()
    eng.bus = bus

    flow = {
        "flow_id": "inline_no_input",
        "name": "空上游演示",
        "entry": "e1",
        "steps": [
            {"id": "e1", "kind": "data_source", "ref": "empty"},
            {"id": "d1", "kind": "decision", "ref": "aggregate", "requires": ["e1"]},
        ],
        "edges": [{"from": "e1", "to": "d1"}],
    }
    r = eng.run(flow)
    print("空上游流程时间线:")
    for t in r["timeline"]:
        print(f"  {t['step_id']:<4} {t['status']:<10} {t.get('reason','')}")
    check("上游无输入步写『没有输入』",
          r["steps"]["e1"]["status"] == fe.STATUS_NO_INPUT
          and r["steps"]["e1"]["reason"] == "没有输入",
          str(r["steps"]["e1"].get("reason")))
    check("下游依赖无输入 → 也写『没有输入』",
          r["steps"]["d1"]["status"] == fe.STATUS_NO_INPUT
          and r["steps"]["d1"]["reason"] == "没有输入")
    check("整条流程**不判失败**", r["status"] != "failed" and r["ok"] is True,
          f"status={r['status']}")

    # 失败：引用不存在的扩展点 → 失败真实原因 + 停在该步
    flow2 = {
        "flow_id": "inline_fail",
        "name": "失败演示",
        "entry": "f1",
        "steps": [
            {"id": "f1", "kind": "data_source", "ref": "records", "params": {"n": 1}},
            {"id": "f2", "kind": "decision", "ref": "no_such_ext"},
            {"id": "f3", "kind": "data_source", "ref": "records"},
        ],
        "edges": [{"from": "f1", "to": "f2"}, {"from": "f2", "to": "f3"}],
    }
    r2 = eng.run(flow2)
    print("\n失败流程时间线:")
    for t in r2["timeline"]:
        print(f"  {t['step_id']:<4} {t['status']:<10} {t.get('reason','')}")
    print(f"  stopped_at={r2['stopped_at']}  error={r2['error']}")
    check("失败停在该步", r2["status"] == "failed" and r2["stopped_at"] == "f2")
    check("失败原因真实(含扩展点不存在)",
          "no_such_ext" in (r2["error"] or ""), str(r2.get("error")))
    check("失败后不再执行后续步", "f3" not in r2["steps"])


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http_get(url, key=None):
    req = urllib.request.Request(url)
    if key:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"raw": "<non-json>"}


def http_post(url, key=None, body=None):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("X-API-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"raw": "<non-json>"}


def test_4_api_interface(flows_dir):
    """③ 接口暴露流程加载失败的真实原因（自起临时端口实测，收尾关掉）。"""
    section("③ 接口 /api/flows 输出流程加载失败的**真实原因**（临时端口实测）")
    port = free_port()
    tmp = tempfile.mkdtemp(prefix="verify_flows_run_")
    env = dict(os.environ)
    env.update({
        "FLOWS_DIR": flows_dir,
        "FOOD_READ_KEY": "verify-read-key",
        "FOOD_ADMIN_KEY": "verify-admin-key",
        "AUDIT_DB": os.path.join(tmp, "audit.db"),
        "FOOD_AUDIT_FILE": os.path.join(tmp, "audit.log"),
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.Popen(
        [PY, "api_server.py", "--port", str(port), "--host", "127.0.0.1"],
        cwd=CODES, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    ready = False
    for _ in range(60):
        try:
            st, _ = http_get(base + "/api/flows")
            if st == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.5)
    try:
        check("临时服务就绪", ready, base)
        st, body = http_get(base + "/api/flows")
        print(f"\nGET /api/flows -> HTTP {st}")
        print(f"  load_errors: {json.dumps(body.get('load_errors'), ensure_ascii=False, indent=2)}")
        print(f"  可用流程: {[f['flow_id'] for f in body.get('flows', [])]}")
        errs = body.get("load_errors", [])
        joined = " | ".join(errs)
        check("接口返回可用流程", {f["flow_id"] for f in body.get("flows", [])}
              >= {"demo_5step_parallel_gate", "demo_branch_loop"})
        check("接口暴露加载失败真实原因(文件名不匹配)",
              "broken_name_mismatch.json" in joined and "不一致" in joined)
        check("接口暴露加载失败真实原因(边指向不存在步)",
              "broken_bad_edge.json" in joined and "ghost" in joined)

        # 触发路由：带 key 一键运行 + 事件/步骤结果
        st, run = http_post(base + "/api/flows/demo_5step_parallel_gate/run",
                            key="verify-read-key", body={})
        print(f"\nPOST /api/flows/demo_5step_parallel_gate/run -> HTTP {st}")
        print(f"  status={run.get('status')}  gate_rejects={run.get('gate_rejects')}  "
              f"timeline_steps={len(run.get('timeline', []))}")
        check("触发路由一键跑通流程",
              st == 200 and run.get("ok") is True and run.get("status") == "success")
        check("触发结果含时间线", len(run.get("timeline", [])) >= 8)

        st, deny = http_post(base + "/api/flows/demo_5step_parallel_gate/run", key=None, body={})
        check("触发路由鉴权 fail-closed(无 key→401)", st == 401, f"HTTP {st}")

        # 无回归：既有路由仍在（openapi 里可见旧+新路由）
        st, spec = http_get(base + "/openapi.json")
        paths = set(spec.get("paths", {}))
        check("既有路由无回归(/api/ontology/structure 仍在)",
              "/api/ontology/structure" in paths)
        check("新增只读/触发路由已挂载",
              {"/api/flows", "/api/flows/presets", "/api/flows/{flow_id}/run"} <= paths,
              str(sorted(p for p in paths if p.startswith("/api/flows"))))

        st, bad = http_post(base + "/api/flows/ghost_flow/run",
                            key="verify-read-key", body={})
        print(f"\nPOST /api/flows/ghost_flow/run -> HTTP {st}")
        print(f"  error={bad.get('error')}")
        check("不存在流程经接口如实报错", bad.get("ok") is False and "ghost_flow" in (bad.get("error") or ""))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        out = proc.stdout.read().decode("utf-8", "ignore") if proc.stdout else ""
        print("\n(临时服务已关闭)")
        tail = [ln for ln in out.splitlines() if ln.strip()][-3:]
        for ln in tail:
            print(f"  server| {ln}")
        shutil.rmtree(tmp, ignore_errors=True)


def test_5_presets():
    """④ 预设与流程定义一一对应；每个预设可一键触发（含分支/循环）。"""
    section("④ 预设与流程定义一一对应；每个预设可一键触发")
    fr = fe.FlowRegistry()   # 真实流程目录
    st = fr.status()
    preset_ids = {p["flow_id"] for p in st["presets"]}
    flagged = {f["flow_id"] for f in st["flows"] if f["preset"]}
    print(f"预设配置({os.path.basename(st['preset_config'])}): {sorted(preset_ids)}")
    print(f"标 preset:true 的流程: {sorted(flagged)}")
    for p in st["presets"]:
        print(f"  · {p['flow_id']:<28} available={p['available']} "
              f"{p.get('error','')}")
    check("预设配置可解析", st["preset_error"] is None, str(st["preset_error"]))
    check("预设集合 == preset:true 流程集合（一一对应）",
          preset_ids == flagged, f"presets={preset_ids} flagged={flagged}")
    check("每个预设卡都可用", all(p["available"] for p in st["presets"]))
    check("真实流程目录无加载错误", not st["load_errors"], str(st["load_errors"]))

    # 一键触发每个预设，并核验分支/循环真跑过
    eng, reg, pm = fe.build_engine()
    bus = EventBus()
    eng.bus = bus
    print("\n逐个预设一键触发（engine 真实执行）:")
    for p in st["presets"]:
        r = eng.run_flow_id(p["flow_id"])
        tl = " ".join(f"{t['step_id']}:{t['status']}" for t in r["timeline"])
        print(f"  · {p['flow_id']:<28} status={r['status']:<8} 时间线: {tl}")
        check(f"预设 {p['flow_id']} 可一键触发并成功", r["status"] != "failed" and r["ok"])
    rb = eng.run_flow_id("demo_branch_loop")
    check("分支步按条件选中分支",
          rb["steps"].get("br1", {}).get("chosen") == "small",
          f"chosen={rb['steps'].get('br1', {}).get('chosen')}")
    check("循环步按出口条件迭代(iter>=2 停)",
          rb["steps"].get("l1", {}).get("iterations") == 2
          and rb["steps"].get("l1", {}).get("until_met") is True,
          f"iterations={rb['steps'].get('l1', {}).get('iterations')} "
          f"until_met={rb['steps'].get('l1', {}).get('until_met')}")


def main():
    tmp_dir = tmp_flows_dir()
    try:
        test_1_five_step_flow()
        test_2_gate_reject_back()
        test_3_no_input_and_failure()
        test_4_api_interface(tmp_dir)
        test_5_presets()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        db = os.path.join(tempfile.gettempdir(), f"verify_flows_audit_{os.getpid()}.db")
        for p in (db,):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    section("汇总")
    print(f"通过 {len(PASSES)} 项，失败 {len(FAILURES)} 项")
    if FAILURES:
        for f in FAILURES:
            print(f"  ❌ {f}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
