#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_kb_registry.py — KB 注册表 + 词典分层 自检（真实运行，不造假）。

覆盖任务要求的 5 项：
  T1 注册一个新 KB 不动引擎代码即生效
  T2 删掉后无残留（注册表干净；磁盘残留只报告不删）
  T3 切库零重启（同进程连续切 KB / 改注册表后立即生效）
  T4 数据就绪探测（真实 stat：数据在→ready；数据缺→not_ready，不编）
  T5 词典层冲突报错（公共层 vs 工厂层同词不同名 → 抛错，不静默覆盖）

纪律：全程用 %TEMP% 临时副本，**不碰真实 config/kbs.json、不删任何真实数据**。
运行： C:/Python312/python.exe scripts/verify_kb_registry.py
退出码 0 = 全过；非 0 = 有失败用例。
"""
import os
import sys
import json
import shutil
import subprocess
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # D:/factory-ontology
CODES = os.path.join(REPO, "codes")
REAL_KBS = os.path.join(CODES, "config", "kbs.json")

sys.path.insert(0, CODES)

_RESULTS = []


def check(name, ok, detail=""):
    _RESULTS.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  — " + detail) if detail else ""))
    return ok


def _sha(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    tmp = tempfile.mkdtemp(prefix="verify_kbreg_")
    tmp_kbs = os.path.join(tmp, "kbs.json")
    shutil.copy2(REAL_KBS, tmp_kbs)
    # 注册表副本可通过 env 覆盖，kb_registry 全部函数都认它
    os.environ["KB_REGISTRY_FILE"] = tmp_kbs

    import kb_registry as KR
    import industrial_dict_loader as IDL
    import dict_asset

    # 引擎文件（只读审查对象）—— 自检前后必须逐字节不变
    ENGINE_FILES = ["codes/api_server.py", "codes/ontology_qa_v3.py", "codes/ask_service.py"]
    engine_before = {f: _sha(os.path.join(REPO, f)) for f in ENGINE_FILES}
    real_sha_before = _sha(REAL_KBS)

    print("=" * 72)
    print("KB 注册表 + 词典分层 自检")
    print("注册表副本: %s" % tmp_kbs)
    print("=" * 72)

    # ── T1 注册新 KB 不动引擎代码即生效 ─────────────────────────────────────
    print("\n[T1] 注册一个新 KB，引擎代码零改动即生效")
    new_data = os.path.join(tmp, "data_verify_kb")
    os.makedirs(new_data, exist_ok=True)
    with open(os.path.join(new_data, "equipment.csv"), "w", encoding="utf-8") as f:
        f.write("id,name\n1,泵A\n")
    nt_path = os.path.join(tmp, "verify_kb.nt")
    with open(nt_path, "w", encoding="utf-8") as f:
        f.write('<http://e/x> <http://e/t> "v" .\n')
    lex_path = os.path.join(tmp, "lexicon_verify_kb.json")
    with open(lex_path, "w", encoding="utf-8") as f:
        json.dump({"description": "临时自检 KB", "type_cn2en": {"测试泵": "test_pump"}}, f, ensure_ascii=False)

    detail = KR.register_kb("verify_kb", {
        "name": "自检临时KB",
        # 绝对路径（跨盘临时目录）；kb_registry 用 os.path.join(ROOT, x)，绝对 x 原样生效
        "data_dir": new_data,
        "nt": nt_path,
        "lexicon": lex_path,
        "examples": ["有多少台设备"],
    })
    check("注册后 get_kb 返回该 KB", detail is not None and detail["kb_id"] == "verify_kb",
          "name=%s" % (detail or {}).get("name"))
    check("字段齐全 {kb_id,name,data_dir,dict_layer,data_ready}",
          all(k in detail for k in ("kb_id", "name", "data_dir", "dict_layer", "data_ready")),
          "keys=%s" % sorted(detail.keys()))
    check("注册被真实写入临时注册表文件", "verify_kb" in json.load(open(tmp_kbs, encoding="utf-8"))["kbs"])

    # 引擎感知：api_server._load_kbs 的读法与这里一致（json.load(...).get("kbs")）
    engine_view = json.load(open(tmp_kbs, encoding="utf-8")).get("kbs", {})
    check("引擎读法(同 api_server._load_kbs)即可感知新 KB，无需改引擎",
          "verify_kb" in engine_view, "引擎视图 KB 数=%d" % len(engine_view))

    engine_after = {f: _sha(os.path.join(REPO, f)) for f in ENGINE_FILES}
    check("引擎三文件逐字节未变（未动引擎代码）",
          engine_before == engine_after,
          "; ".join(f for f in ENGINE_FILES if engine_before[f] != engine_after[f]) or "sha256 全等")

    # ── T4 数据就绪探测（先做，用同一注册表）────────────────────────────────
    print("\n[T4] 数据就绪真实探测")
    p_ok = KR.probe_kb("verify_kb")
    check("数据齐备 → data_ready=True/status=ready",
          p_ok["data_ready"] and p_ok["status"] == "ready",
          "files=%d nt_exists=%s" % (p_ok["data_file_count"], p_ok["nt_exists"]))

    KR.register_kb("verify_missing", {"name": "缺数据KB",
                                      "data_dir": "data_不存在_verify", "nt": "output/不存在.nt"})
    p_missing = KR.probe_kb("verify_missing")
    check("数据缺失 → data_ready=False/status=not_ready（不编造）",
          (not p_missing["data_ready"]) and p_missing["status"] == "not_ready",
          "missing=%s" % p_missing["missing"])
    check("缺失原因被真实列出（数据目录不存在）",
          any("数据目录不存在" in m for m in p_missing["missing"]))

    KR.unregister_kb("verify_missing")

    p_real = KR.probe_kb("valve")
    check("真实 KB 'valve' 探测为 ready（对照真库）",
          p_real["status"] == "ready", "data_dir=%s files=%d" % (p_real["data_dir"], p_real["data_file_count"]))
    p_huaneng = KR.probe_kb("huaneng")
    check("真实 KB 'huaneng' 数据在→data_ready=True、本体缺→servable=False（真实原因列出）",
          p_huaneng["data_ready"] is True and p_huaneng["servable"] is False
          and any("本体文件缺失" in m for m in p_huaneng["missing"]),
          "files=%d nt_exists=%s missing=%s"
          % (p_huaneng["data_file_count"], p_huaneng["nt_exists"], p_huaneng["missing"]))

    # ── T3 切库零重启 ───────────────────────────────────────────────────────
    print("\n[T3] 切库零重启（同进程连切）")
    a = KR.get_kb("valve")
    b = KR.get_kb("chem")
    c = KR.get_kb("verify_kb")
    check("同进程连切三个 KB，各自 data_dir 独立",
          len({a["data_dir"], b["data_dir"], c["data_dir"]}) == 3,
          "%s | %s | %s" % (a["kb_id"], b["kb_id"], c["kb_id"]))
    # 改注册表后同进程立即生效 —— 零重启的实证
    KR.register_kb("verify_hot", {"name": "热插KB", "data_dir": "data", "nt": "output/valve.nt"})
    check("改动注册表后同进程立即可见（零重启）",
          KR.get_kb("verify_hot") is not None)
    KR.unregister_kb("verify_hot")

    # ── T5 词典层冲突报错 ───────────────────────────────────────────────────
    print("\n[T5] 词典层冲突：报错、不静默覆盖")
    layers = KR.dict_layers("valve")
    check("词典层分离：公共层只读 + 工厂层可写",
          layers["public"]["read_only"] is True and layers["factory"]["read_only"] is False,
          "公共=%s 工厂=%s" % (layers["public"]["files"], layers["factory"]["file"]))

    # 公共层（00_basis）: 设备→equipment；工厂层故意冲突: 设备→device
    conflict_factory = {"type_cn2en": {"设备": "device"}, "entity_cn2en": {"设备": "equipment"}}
    conflicts = IDL.detect_layer_conflicts(conflict_factory, industry="基础")
    check("冲突可被探测到（设备: 公共=equipment 工厂=device）",
          any(c["cn"] == "设备" and c["public"] == "equipment" and c["factory"] == "device"
              for c in conflicts),
          "conflicts=%s" % conflicts)

    raised = False
    err = ""
    try:
        KR.merge_dict_layers(kb_dict=conflict_factory, industry="基础", on_conflict="error")
    except KR.DictLayerConflictError as e:  # noqa
        raised = True
        err = str(e)[:120]
    check("on_conflict=error → 抛 DictLayerConflictError（不静默覆盖）", raised, err)

    ok_merge = KR.merge_dict_layers(kb_dict=conflict_factory, industry="基础", on_conflict="kb_wins")
    check("on_conflict=kb_wins 保留旧行为（工厂层覆盖）",
          ok_merge["type_cn2en"].get("设备") == "device")

    # 入参不被修改（兑现 merge_industrial_dict 文档承诺；曾为浅拷贝静默 mutate 的真 bug）
    import copy
    before = copy.deepcopy(conflict_factory)
    KR.merge_dict_layers(kb_dict=conflict_factory, industry="基础", on_conflict="kb_wins")
    check("合并词典不改写入参（深层比对相等）", conflict_factory == before,
          "入参=%s" % conflict_factory)

    # 无冲突合并：公共层兜底生效
    clean = KR.merge_dict_layers(kb_dict={"type_cn2en": {}}, industry="基础", on_conflict="error")
    check("无冲突时公共层兜底合并（设备/机器 等进入结果）",
          "设备" in clean.get("type_cn2en", {}),
          "type 条数=%d" % len(clean.get("type_cn2en", {})))

    # 真实 KB 的工厂词典：既有 kb-wins 语义下存在「工厂层覆盖公共层」的不一致项，
    # 分层报告使其**可见**（非静默），严格模式会把这些覆盖升级为报错。
    rep = KR.dict_layer_report("valve")
    check("真实 KB 'valve' 分层报告暴露工厂覆盖公共的不一致项（非静默）",
          rep["overrides_count"] > 0,
          "不一致项=%d by_key=%s" % (rep["overrides_count"], rep["overrides_by_key"]))
    strict_raised = False
    try:
        KR.merge_dict_layers(kb_id="valve", on_conflict="error")
    except KR.DictLayerConflictError as e:
        strict_raised = True
        _ = str(e)[:60]
    check("真实 KB 'valve' 严格模式 on_conflict=error 会报错（覆盖升级为报错）",
          strict_raised)

    # 分层守卫：工厂词典不得写入公共层（import/export 两条路径共用同一守卫）
    # 正向：导出到临时目录正常；反向：守卫对公共层路径拒绝（不实际写公共层，零副作用）
    exp = dict_asset.export_lexicon("valve", out_dir=os.path.join(tmp, "export"))
    check("dict_asset 正常导出到普通目录成功", exp.get("ok") is True,
          "target=%s" % exp.get("path"))

    guard_ok, guard_msg = False, ""
    try:
        dict_asset._assert_not_public_layer(os.path.join(CODES, "industrial_dict", "lexicon_valve.json"))
    except PermissionError as e:
        guard_ok, guard_msg = True, str(e)[:80]
    except Exception as e:
        guard_msg = "异常类型不符: %r" % e
    check("dict_asset 守卫：拒绝把工厂词典写进只读公共层（import/export 共用守卫）",
          guard_ok, guard_msg)

    # ── T2 删掉后无残留 ─────────────────────────────────────────────────────
    print("\n[T2] 注销后无残留")
    res = KR.unregister_kb("verify_kb")
    check("unregister 报告 removed=True", res["removed"])
    check("注册表中已无该 KB（list/get 查不到）",
          KR.get_kb("verify_kb") is None and "verify_kb" not in [k["kb_id"] for k in KR.list_kbs()])
    check("磁盘残留被真实报告（只报告不删）",
          any(r["kind"] in ("lexicon", "nt", "data_dir") for r in res["residue"]),
          "residue=%s" % [r["kind"] for r in res["residue"]])

    # 引擎文件再次核对（全流程结束）
    engine_final = {f: _sha(os.path.join(REPO, f)) for f in ENGINE_FILES}
    check("全流程结束引擎三文件仍逐字节未变", engine_before == engine_final)

    # 真实注册表未被触碰
    check("真实 config/kbs.json 未被本自检修改（sha256 前后一致）",
          _sha(REAL_KBS) == real_sha_before,
          "real_kbs sha=%s" % _sha(REAL_KBS)[:16])
    real_now = KR.load_registry(REAL_KBS)
    check("真实注册表仍为 33 个 KB 且无 verify_* 项",
          len(real_now) == 33 and not any(k.startswith("verify_") for k in real_now),
          "total=%d" % len(real_now))

    shutil.rmtree(tmp, ignore_errors=True)

    total = len(_RESULTS)
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    print("\n" + "=" * 72)
    print("自检结果: %d/%d 通过" % (passed, total))
    if passed != total:
        print("失败用例:")
        for n, ok, d in _RESULTS:
            if not ok:
                print("  - %s  %s" % (n, d))
    print("=" * 72)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
