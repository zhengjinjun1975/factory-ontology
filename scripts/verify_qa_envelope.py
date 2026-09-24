#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_qa_envelope.py — 批 2 自检：问答统一信封 + 未命中如实说（从终端跑真实输出）。

跑什么
------
1. **信封契约**：对批 1 评测集 fixture 的全部问题，逐题调 `ontology_qa_v3.answer_envelope`
   并断言：
     - 所有分支都带 `hit`（以及 answer/reason/evidence）——键集合断言；
     - 有答案且 `hit=true` → `evidence` **非空**，且每条 evidence 满足
       `{kind∈entity|relation|dict|doc, id, label, source}`；
     - 无答案类（fixture `answerable=false`）→ `hit=false`、`evidence` 为空、
       `answer` 以"未命中"开头、且**不回填**引擎原始(可能编造的)答案文本。
2. **代表性真实输出**：打印若干道题的信封全文（有答案 / 无答案 / 已修的数值比较问法）。
3. **向后兼容**：`answer()` 仍返回 `str`；`ask_service.envelope_from_result` 只新增字段、
   老字段(answer/evidence/no_basis/engines/...)一个不少；证据归一化形状符合契约。
4. **回归对比**：真跑批 1 的 `scripts/eval_qa.py --out <tmp>`，与基线条目
   `scripts/eval_qa_baseline.json` 并列：**命中率不降、误答率不升**。

用法
----
  C:/Python312/python.exe scripts/verify_qa_envelope.py
  C:/Python312/python.exe scripts/verify_qa_envelope.py --kb valve
  C:/Python312/python.exe scripts/verify_qa_envelope.py --baseline scripts/eval_qa_baseline.json
退出码：全部通过 0；有断言失败 1（并逐条打印失败原因，不静默）。
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODES = os.path.join(ROOT, "codes")
CFG = os.path.join(CODES, "config")
FIXDIR = os.path.join(CODES, "tests", "fixtures", "qa_eval")
KBS_FILE = os.path.join(CFG, "kbs.json")

if CODES not in sys.path:
    sys.path.insert(0, CODES)

import ontology_qa_v3 as v3          # noqa: E402
import ask_service                    # noqa: E402
import evidence                        # noqa: E402

ENV_KEYS = ("answer", "hit", "reason", "evidence")
ENV_KINDS = ("entity", "relation", "dict", "doc")

FAILS = []          # [(section, msg)]


def _fail(section, msg):
    FAILS.append((section, msg))
    print("  [FAIL] %s: %s" % (section, msg))


def _ok(msg):
    print("  [ok] %s" % msg)


# ------------------------------------------------------------------ 引擎构建
def load_kbs():
    with open(KBS_FILE, encoding="utf-8") as f:
        return json.load(f)["kbs"]


def build(kb, kbs):
    cfg = kbs[kb]
    D = v3.load_dict(os.path.join(CFG, cfg["lexicon"]))
    data = v3.build_data(v3.parse_nt(os.path.join(CODES, cfg["nt"])), D)
    return data, D


def load_fixture(kb):
    p = os.path.join(FIXDIR, "%s.json" % kb)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)["questions"]


# ------------------------------------------------------------------ 1. 信封契约
def check_contract(kbs, only_kb=None):
    print("\n== 1. 信封契约（全评测集逐题） ==")
    total = ansq = anshit = noans = 0
    ans_bad = noans_bad = 0
    for f in sorted(glob.glob(os.path.join(FIXDIR, "*.json"))):
        kb = os.path.basename(f)[:-5]
        if only_kb and kb != only_kb:
            continue
        qs = load_fixture(kb)
        if not qs:
            continue
        data, D = build(kb, kbs)
        a = h = n = 0
        for q in qs:
            env = v3.answer_envelope(q["question"], data, D)
            total += 1
            # 键集合断言（所有分支都带 hit）
            missing = [k for k in ENV_KEYS if k not in env]
            if missing:
                _fail("契约", "%s/%s 缺键 %s" % (kb, q["id"], missing))
                continue
            if not isinstance(env["hit"], bool):
                _fail("契约", "%s/%s hit 非 bool" % (kb, q["id"]))
            if q.get("answerable", True):
                ansq += 1
                a += 1
                if env["hit"]:
                    h += 1
                    anshit += 1
                    if not env["evidence"]:
                        ans_bad += 1
                        _fail("①有答案→有据", "%s/%s hit=true 但 evidence 空: %s"
                              % (kb, q["id"], env["answer"][:40]))
                    for e in env["evidence"]:
                        if not all(k in e for k in ("kind", "id", "label", "source")) \
                                or e.get("kind") not in ENV_KINDS:
                            _fail("③证据可回溯", "%s/%s 证据形状不合契约: %r"
                                  % (kb, q["id"], e))
                            break
            else:
                noans += 1
                n += 1
                if env["hit"] or env["evidence"] or not env["answer"].startswith("未命中"):
                    noans_bad += 1
                    _fail("②无答案→如实说", "%s/%s 应 hit=false/证据空/答未命中, 实为 hit=%s ev=%d ans=%s"
                          % (kb, q["id"], env["hit"], len(env["evidence"]), env["answer"][:40]))
                elif env.get("raw_answer") and env["answer"] == env["raw_answer"]:
                    _fail("②无答案→如实说", "%s/%s 把原始(可能编造)答案当答案回填" % (kb, q["id"]))
        print("  %-16s 题%3d 可答%3d 命中%3d 无答案%3d" % (kb, len(qs), a, h, n))
    print("  小计: 题%d 可答%d 命中%d 无答案%d" % (total, ansq, anshit, noans))
    if not ans_bad:
        _ok("① 所有 hit=true 的可答问题都带 evidence")
    if not noans_bad:
        _ok("② 所有无答案类问题 hit=false 且未回填编造内容")
    return {"total": total, "answerable": ansq, "hit": anshit, "noans": noans}


# ------------------------------------------------------------------ 2. 代表性真实输出
def show_samples(kbs):
    print("\n== 2. 代表性真实输出（终端真跑） ==")
    cases = [
        ("valve", "法兰的数量"),                 # 值过滤计数(兜底, 有据)
        ("valve", "产品中价格大于1711.62的产品有多少"),  # 已修的数值比较问法
        ("ai4i", "机器中工艺温度大于310.01的机器有多少"),  # 批 2 已知缺陷 1
        ("library", "图书中页数最大是多少"),      # 批 2 已知缺陷 2(编造→如实未命中)
        ("chem", "有多少个催化剂"),              # 无答案(跨域假实体)
        ("valve", "产品中纯度最大是多少"),        # 无答案(假属性)
    ]
    for kb, q in cases:
        data, D = build(kb, kbs)
        env = v3.answer_envelope(q, data, D)
        print("  [%s] Q: %s" % (kb, q))
        print("        hit=%s reason=%s" % (env["hit"], env["reason"]))
        print("        answer=%s" % env["answer"].replace("\n", " ⏎ ")[:100])
        print("        evidence(%d)=%s" % (len(env["evidence"]), env["evidence"][:2]))


# ------------------------------------------------------------------ 3. 向后兼容
def check_backward_compat(kbs):
    print("\n== 3. 向后兼容（老字段不删 / 前端读取不坏） ==")
    data, D = build("valve", kbs)
    # answer() 仍返回 str
    txt = v3.answer("产品的数量是多少", data, D)
    if not isinstance(txt, str):
        _fail("兼容", "answer() 返回类型不再是 str: %r" % type(txt))
    else:
        _ok("answer() 仍返回 str（老调用方 eval_qa / api_server 不受影响）")
    # envelope_from_result：只加字段，老字段不动
    old = {"ok": True, "mode": "rule", "answer": "有 8 个产品", "evidence": [
               {"entity": "电动闸阀", "attr": "类型", "value": "蝶阀", "source": "rule", "score": 1.0}],
           "engines": ["rule"], "structured": {"rule": "count_by_type"},
           "no_basis": False, "kb": "valve"}
    env = ask_service.envelope_from_result(old)
    for k in ("ok", "mode", "answer", "evidence", "engines", "structured", "no_basis", "kb"):
        if k not in env or env[k] != old[k]:
            _fail("兼容", "老字段 %s 被改/删" % k)
    if not all(k in env for k in ("hit", "reason", "advisory", "evidence_trace")):
        _fail("兼容", "新增字段不全: %s" % [k for k in ("hit", "reason", "advisory",
                                                    "evidence_trace") if k not in env])
    if env.get("hit") is not True or env.get("no_basis") is not False:
        _fail("兼容", "hit 与 no_basis 判定不一致: %r" % env.get("hit"))
    if not env["evidence_trace"] or env["evidence_trace"][0].get("kind") != "entity":
        _fail("兼容", "evidence_trace 形状不合契约: %r" % env.get("evidence_trace"))
    _ok("envelope_from_result 只新增 hit/reason/advisory/evidence_trace，老字段原样保留")
    # no_basis=True 的老返回 → hit=False
    nb = ask_service.envelope_from_result(
        {"ok": True, "mode": "rule", "answer": "暂不支持该问题", "evidence": [],
         "no_basis": True, "kb": "valve"})
    if nb.get("hit") is not False:
        _fail("兼容", "no_basis=True 未映射成 hit=False")
    else:
        _ok("老字段 no_basis=True → 新字段 hit=False（口径一致）")
    # doc 证据归一化
    doc = ask_service.normalize_envelope_evidence(
        [{"doc_id": "D1", "title": "手册", "chunk": "……", "score": 0.9}])
    if not doc or doc[0].get("kind") != "doc":
        _fail("兼容", "doc 证据未归一化为 kind=doc: %r" % doc)
    else:
        _ok("doc 证据归一化为 kind=doc（可回溯到文档）")


# ------------------------------------------------------------------ 4. eval 回归对比
def row(name, agg):
    a = agg.get("answerable") or 1
    return ("%s 题%d 可答%d | 命中%d(%.1f%%) 未命中%d(%.1f%%) 误答%d(%.1f%%) | "
            "无答案%d 正确拒%d 编造%d 空答%d") % (
        name, agg["total"], agg["answerable"], agg["hit"], agg["hit"] / a * 100,
        agg["miss"], agg["miss"] / a * 100, agg["wrong"], agg["wrong"] / a * 100,
        agg["noans"], agg["refuseok"], agg["fabricate"], agg["emptyzero"])


def _norm_agg(o):
    return {"total": o["total"], "answerable": o["answerable"], "hit": o["hit"],
            "miss": o["miss"], "wrong": o["wrong"], "noans": o["noans"],
            "refuseok": o.get("refuse_ok", 0), "fabricate": o.get("fabricate", 0),
            "emptyzero": o.get("empty_zero", 0)}


def check_eval_regression(kbs, baseline_path, only_kb=None):
    print("\n== 4. 批 1 评测门回归对比（真跑 eval_qa.py） ==")
    if not os.path.exists(baseline_path):
        _fail("回归", "基线文件不存在: %s" % baseline_path)
        return
    with open(baseline_path, encoding="utf-8") as f:
        base = json.load(f)["overall"]
    tmp = os.path.join(tempfile.gettempdir(), "verify_qa_envelope_after.json")
    cmd = [sys.executable, os.path.join(ROOT, "scripts", "eval_qa.py"), "--out", tmp, "--quiet"]
    if only_kb:
        cmd += ["--kb", only_kb]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        _fail("回归", "eval_qa.py 退出码 %d\n%s" % (proc.returncode, proc.stderr[-500:]))
        return
    with open(tmp, encoding="utf-8") as f:
        after = json.load(f)["overall"]

    b, a = _norm_agg(base), _norm_agg(after)
    b["hit_rate"] = base["hit_rate"]
    b["wrong_rate"] = base["wrong_rate"]
    a["hit_rate"] = after["hit_rate"]
    a["wrong_rate"] = after["wrong_rate"]
    print("  BEFORE(基线 %s):" % os.path.relpath(baseline_path, ROOT))
    print("    " + row("基线", b))
    print("    " + "命中率 %.4f  误答率 %.4f" % (b["hit_rate"], b["wrong_rate"]))
    print("  AFTER (本次改动, --out %s):" % tmp)
    print("    " + row("本次", a))
    print("    " + "命中率 %.4f  误答率 %.4f" % (a["hit_rate"], a["wrong_rate"]))
    print("    Δ命中率 %+.4f   Δ误答率 %+.4f   Δ编造 %+d" % (
        a["hit_rate"] - b["hit_rate"], a["wrong_rate"] - b["wrong_rate"],
        a["fabricate"] - b["fabricate"]))
    if a["hit_rate"] + 1e-9 < b["hit_rate"]:
        _fail("回归门", "命中率下降: %.4f → %.4f" % (b["hit_rate"], a["hit_rate"]))
    else:
        _ok("命中率不降（%.4f → %.4f）" % (b["hit_rate"], a["hit_rate"]))
    if a["wrong_rate"] - 1e-9 > b["wrong_rate"]:
        _fail("回归门", "误答率上升: %.4f → %.4f" % (b["wrong_rate"], a["wrong_rate"]))
    else:
        _ok("误答率下降或持平（%.4f → %.4f）" % (b["wrong_rate"], a["wrong_rate"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", default=None, help="只跑某个 KB（缺省=全部）")
    ap.add_argument("--baseline", default=os.path.join(ROOT, "scripts", "eval_qa_baseline.json"),
                    help="批 1 基线 JSON（对比用）")
    ap.add_argument("--no-eval", action="store_true", help="跳过 eval_qa 回归（只跑信封自检）")
    args = ap.parse_args()

    kbs = load_kbs()
    print("verify_qa_envelope — 批 2 问答统一信封 + 未命中如实说")
    print("解释器: %s" % sys.executable)
    summary = check_contract(kbs, args.kb)
    show_samples(kbs)
    check_backward_compat(kbs)
    if not args.no_eval:
        check_eval_regression(kbs, args.baseline, args.kb)

    print("\n" + "=" * 78)
    if FAILS:
        print("结果: FAIL（%d 项）" % len(FAILS))
        for s, m in FAILS:
            print("  - [%s] %s" % (s, m))
        return 1
    print("结果: PASS  题%d 可答%d 命中%d 无答案%d（hit=true 均有 evidence；无答案零编造）"
          % (summary["total"], summary["answerable"], summary["hit"], summary["noans"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
