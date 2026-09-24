#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_advisory.py — 批 3 自检：模型建议层（可选适配）从终端真跑。

比对基准
--------
**批 2 的真实实现**（不是"我以为的批 2"）：从 git 取 `e197585:codes/ask_service.py`
（批 2 提交版本）落到 %TEMP%，用 importlib 以另一个模块名加载，与本次改动后的
`codes/ask_service.py` 对**同一批问题**逐字段比对。这样"无模型时行为零变化"是
两版代码的实测对照，而不是同一份代码的自我比较。

跑什么
------
1. **默认态（无模型）**：断言 `model_advisory.is_enabled() is False`；
   对批 1 评测集（codes/tests/fixtures/qa_eval/*.json）的全部问题，
   生成同一批输入 result，分别喂给「批 2 版 envelope_from_result」与「批 3 版」，
   **逐字段比对（键集合 + 每个键的取值）一致率必须 100%**。
2. **启用态（假适配器注入固定候选）**：
   - `hit` 与默认态逐题一致（**模型命中不算 hit=true**）；
   - 除 `advisory` 外所有字段与默认态一致（模型不改结论、不改证据、不改答案）；
   - `advisory` 键集合**恰好** {candidates, basis, model}，候选/依据非空；
   - **硬红线断言**：advisory 里任何候选/依据文本都不出现在 `answer` 中；
   - 反向取证：对默认态 `hit=false` 的题，启用态 `hit` 仍为 False 且 answer 未变。
3. **红线闸真的会红（负样本）**：注入一个把 answer 原文当候选返回的"恶意适配器"，
   断言系统丢弃 advisory 并写 `advisory_error`（证明红线是闸门，不只是注释）。
4. **适配器无产出**：适配器返回 None（模型不可用/答不上）→ 结果原样，不编造。

用法
----
  <python> scripts/verify_advisory.py
  <python> scripts/verify_advisory.py --kb valve      # 只跑某 KB
  <python> scripts/verify_advisory.py --snapshot e197585   # 换基准提交
退出码：全通过 0；有断言失败 1（逐条打印失败原因，不静默）。
"""
import argparse
import glob
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODES = os.path.join(ROOT, "codes")
CFG = os.path.join(CODES, "config")
FIXDIR = os.path.join(CODES, "tests", "fixtures", "qa_eval")

if CODES not in sys.path:
    sys.path.insert(0, CODES)

import ontology_qa_v3 as v3          # noqa: E402
import ask_service as ask_new        # noqa: E402  (批 3 版)
import model_advisory as ma          # noqa: E402

FAILS = []


def _fail(section, msg):
    FAILS.append((section, msg))
    print("  [FAIL] %s: %s" % (section, msg))


def _ok(msg):
    print("  [ok] %s" % msg)


# ─────────────────────────────────────────── 批 2 基准（从 git 取真实旧版代码）
def load_batch2_module(snapshot):
    src = subprocess.run(["git", "-C", ROOT, "show", "%s:codes/ask_service.py" % snapshot],
                         capture_output=True, text=True, encoding="utf-8")
    if src.returncode != 0:
        _fail("基准", "取 %s:codes/ask_service.py 失败: %s" % (snapshot, src.stderr[-300:]))
        return None
    if "attach_if_enabled" in src.stdout or "model_advisory" in src.stdout:
        _fail("基准", "%s 版 ask_service 已含批 3 接线，不是干净的批 2 基准" % snapshot)
        return None
    tmp = os.path.join(tempfile.gettempdir(), "ask_service_b2_%s.py" % snapshot)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(src.stdout)
    spec = importlib.util.spec_from_file_location("ask_service_b2", tmp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ask_service_b2"] = mod
    spec.loader.exec_module(mod)
    print("  基准模块: %s（git %s:codes/ask_service.py，%d 字节）" % (tmp, snapshot, len(src.stdout)))
    return mod


# ─────────────────────────────────────────── 输入构造（同一批真实问题）
def load_kbs():
    with open(os.path.join(CFG, "kbs.json"), encoding="utf-8") as f:
        return json.load(f)["kbs"]


def result_of(kb, env):
    """把 ontology_qa_v3.answer_envelope 的输出转成 /api/ask 形态的 result（两边喂同一份）。"""
    return {"ok": True, "mode": env.get("source") or "rule", "answer": env["answer"],
            "evidence": list(env.get("evidence") or []), "engines": ["rule"],
            "structured": None, "no_basis": not env.get("hit"), "kb": kb,
            "confidence": "high" if env.get("hit") else "none"}


def build_cases(kbs, only_kb=None):
    cases = []
    for f in sorted(glob.glob(os.path.join(FIXDIR, "*.json"))):
        kb = os.path.basename(f)[:-5]
        if only_kb and kb != only_kb:
            continue
        if kb not in kbs:
            continue
        cfg = kbs[kb]
        D = v3.load_dict(os.path.join(CFG, cfg["lexicon"]))
        data = v3.build_data(v3.parse_nt(os.path.join(CODES, cfg["nt"])), D)
        with open(f, encoding="utf-8") as fh:
            qs = json.load(fh)["questions"]
        for q in qs:
            env = v3.answer_envelope(q["question"], data, D)
            cases.append({"kb": kb, "qid": q["id"], "question": q["question"],
                          "expected_answerable": q.get("answerable", True),
                          "result": result_of(kb, env)})
    return cases


def diff_fields(a, b):
    """逐字段比对两个 dict：返回 (不一致的键列表, 字段总数)。"""
    bad = []
    keys = sorted(set(a.keys()) | set(b.keys()))
    for k in keys:
        va, vb = a.get(k, "<missing>"), b.get(k, "<missing>")
        if json.dumps(va, ensure_ascii=False, sort_keys=True, default=str) != \
                json.dumps(vb, ensure_ascii=False, sort_keys=True, default=str):
            bad.append(k)
    return bad, len(keys)


# ─────────────────────────────────────────── 1. 默认态 vs 批 2 逐字段
def check_default_vs_batch2(cases, b2):
    print("\n== 1. 无模型（默认态）逐字段比对批 2（git 基准） ==")
    if b2 is None:
        return None
    st = ma.model_advisory_status()
    print("  启用态: enabled=%s opt_in=%s adapter=%s config_exists=%s"
          % (st["enabled"], st["opt_in"], st["adapter"], st["config_exists"]))
    if st["enabled"]:
        _fail("默认态", "无模型配置时 is_enabled() 应为 False，实为 True")
    else:
        _ok("无模型配置时 model_advisory.is_enabled() is False（完全不启用）")

    total_fields = same_fields = 0
    bad_cases = []
    for c in cases:
        e2 = b2.envelope_from_result(dict(c["result"]), "知识库")
        e3 = ask_new.envelope_from_result(dict(c["result"]), "知识库")
        bad, n = diff_fields(e2, e3)
        total_fields += n
        same_fields += (n - len(bad))
        if bad or "advisory_error" in e3:
            bad_cases.append((c, bad))
    rate = (same_fields / total_fields * 100.0) if total_fields else 0.0
    print("  比对: %d 题 / %d 个字段  一致 %d  不一致 %d  一致率 %.4f%%"
          % (len(cases), total_fields, same_fields, total_fields - same_fields, rate))
    for c, bad in bad_cases[:5]:
        _fail("默认态", "%s/%s 字段不一致: %s" % (c["kb"], c["qid"], bad))
    if not bad_cases:
        _ok("逐字段一致率 100.00%（无模型时行为与批 2 零变化）")
    if rate != 100.0:
        _fail("默认态", "一致率非 100%%: %.4f%%" % rate)
    # 默认态下 advisory 必须仍是批 2 的 None
    e3 = ask_new.envelope_from_result(dict(cases[0]["result"]))
    if e3.get("advisory") is not None:
        _fail("默认态", "默认态 advisory 非 None: %r" % e3.get("advisory"))
    else:
        _ok("默认态 advisory 仍为 None（沿用批 2 占位，不产空壳）")
    return rate


# ─────────────────────────────────────────── 2. 启用态（假适配器）
FAKE_CANDS = ["查一下该设备的维护记录", "确认是否为同型号替换件"]
FAKE_BASIS = ["候选来自建议模型，仅供人工参考"]


def check_enabled(cases, b2):
    print("\n== 2. 启用态（假适配器注入固定候选） ==")
    os.environ[ma.ENV_ENABLE] = "1"
    ma.set_active_adapter(ma.StaticAdapter(FAKE_CANDS, FAKE_BASIS, model="fake-adapter:test"))
    st = ma.model_advisory_status()
    print("  启用态: enabled=%s adapter=%s" % (st["enabled"], st["adapter"]))
    if not st["enabled"]:
        _fail("启用态", "注入适配器并置 %s=1 后仍为未启用" % ma.ENV_ENABLE)
        return

    hit_same = ans_same = adv_ok = leak_bad = keybad = 0
    miss_kept = 0
    n = 0
    for c in cases:
        off = b2.envelope_from_result(dict(c["result"]), "知识库")   # 参照：批 2 真实实现（无模型）
        on = ask_new.envelope_from_result(dict(c["result"]), "知识库")
        n += 1
        adv = on.get("advisory")
        # 2.1 hit 不被模型影响
        if on.get("hit") == off.get("hit"):
            hit_same += 1
        else:
            _fail("②hit不被模型影响", "%s/%s hit 变了: %r → %r"
                  % (c["kb"], c["qid"], off.get("hit"), on.get("hit")))
        # 2.2 除 advisory 外所有字段不变（含 answer/evidence/reason/evidence_trace）
        o2, n2 = dict(off), dict(on)
        o2.pop("advisory", None)
        n2.pop("advisory", None)
        bad, _tot = diff_fields(o2, n2)
        if not bad:
            ans_same += 1
        else:
            _fail("②模型不改结论/答案", "%s/%s 非 advisory 字段被改: %s" % (c["kb"], c["qid"], bad))
        # 2.3 advisory 形状恰三键 + 候选非空
        if isinstance(adv, dict) and set(adv.keys()) == set(ma.ADVISORY_KEYS) \
                and adv.get("candidates") and adv.get("model"):
            adv_ok += 1
        else:
            keybad += 1
            _fail("②advisory形状", "%s/%s advisory 不合契约: %r" % (c["kb"], c["qid"], adv))
        # 2.4 红线：advisory 文本不得出现在 answer 中
        a = str(on.get("answer") or "")
        texts = list((adv or {}).get("candidates") or []) + list((adv or {}).get("basis") or [])
        if not any(t in a for t in texts):
            leak_bad += 0
        else:
            leak_bad += 1
            _fail("③红线", "%s/%s advisory 文本出现在 answer 中" % (c["kb"], c["qid"]))
        # 2.5 反向取证：本应未命中的题，模型给了候选也不算命中
        if not c["expected_answerable"]:
            if off.get("hit") is False and on.get("hit") is False \
                    and on.get("answer") == off.get("answer"):
                miss_kept += 1
            else:
                _fail("②hit不被模型影响", "%s/%s 无答案题被模型候选抬成命中: hit=%r ans=%s"
                      % (c["kb"], c["qid"], on.get("hit"), str(on.get("answer"))[:40]))

    print("  题%d | hit 一致 %d | 非 advisory 字段全同 %d | advisory 形状合规 %d | 红线泄漏 %d"
          % (n, hit_same, ans_same, adv_ok, leak_bad))
    if hit_same == n:
        _ok("② hit 与默认态逐题一致（模型命中不算 hit=true）")
    if ans_same == n:
        _ok("② answer/evidence/reason 等全部字段与默认态一致（模型只加 advisory）")
    if adv_ok == n and keybad == 0:
        _ok("② advisory 恰好 {candidates, basis, model}，候选/依据非空")
    if leak_bad == 0:
        _ok("③ advisory 内容未进入 answer（硬红线，%d 题全过）" % n)
    print("  无答案类题目（%d 题）命中判定与 answer 均未受模型影响" % miss_kept)

    # 2.6 代表性真实输出
    print("\n  -- 代表性真实输出（同一问，默认态 vs 启用态）--")
    pick = None
    for c in cases:
        if not c["expected_answerable"]:
            pick = c
            break
    for c in (pick, cases[0]):
        if c is None:
            continue
        off = b2.envelope_from_result(dict(c["result"]), "知识库")
        on = ask_new.envelope_from_result(dict(c["result"]), "知识库")
        print("  [%s] Q: %s" % (c["kb"], c["question"]))
        print("    批2/默认态: hit=%s advisory=%s answer=%s"
              % (off["hit"], off.get("advisory"), str(off["answer"]).replace("\n", " ⏎ ")[:60]))
        print("    启用态: hit=%s advisory=%s" % (on["hit"], json.dumps(on.get("advisory"), ensure_ascii=False)))
        print("             answer=%s" % str(on["answer"]).replace("\n", " ⏎ ")[:60])

    # 2.7 适配器无产出 → 不编造
    ma.set_active_adapter(ma.StaticAdapter([], [], model="fake-empty"))
    e = ask_new.envelope_from_result(dict(cases[0]["result"]), "知识库")
    if e.get("advisory") is None:
        _ok("适配器无候选（返回空）→ advisory 保持 None，不产空壳、不编造")
    else:
        _fail("启用态", "空候选仍产出 advisory: %r" % e.get("advisory"))


# ─────────────────────────────────────────── 3. 红线闸会红（负样本）
def check_redline_gate(cases):
    print("\n== 3. 红线闸负样本（把 answer 原文当候选的恶意适配器） ==")
    c = None
    for x in cases:
        if x["expected_answerable"] and str(x["result"].get("answer") or "").strip():
            c = x
            break
    if c is None:
        _fail("红线闸", "找不到可用于负样本的题目")
        return
    leaked_text = str(c["result"]["answer"]).strip()[:30]
    ma.set_active_adapter(ma.StaticAdapter([leaked_text], ["跟答案一样的依据"], model="evil"))
    e = ask_new.envelope_from_result(dict(c["result"]), "知识库")
    if e.get("advisory") is None and e.get("advisory_error"):
        _ok("泄漏候选被闸门拦下：advisory 丢弃 + advisory_error=%s" % e["advisory_error"])
    else:
        _fail("红线闸", "泄漏候选未被拦下: advisory=%r err=%r"
              % (e.get("advisory"), e.get("advisory_error")))
    if e.get("answer") == c["result"]["answer"] and e.get("hit") is not None:
        _ok("拦下后 answer 仍为原文，未受影响")
    else:
        _fail("红线闸", "answer 被改动")


# ─────────────────────────────────────────── 4. 内置真适配器的失败路径（不依赖外网）
def check_builtin_adapter_offline(cases):
    print("\n== 4. 内置 Ollama 适配器：模型不可达时如实不产 advisory（不编造） ==")
    ma.clear_active_adapter()
    os.environ[ma.ENV_MODEL] = "advisory-test:latest"
    os.environ[ma.ENV_BASE] = "http://127.0.0.1:9/api/generate"   # 必然连不上
    os.environ[ma.ENV_TIMEOUT] = "2"
    st = ma.model_advisory_status()
    print("  配置态: opt_in=%s enabled=%s adapter=%s" % (st["opt_in"], st["enabled"], st["adapter"]))
    if not (st["opt_in"] and st["enabled"] and st["adapter"] == "ollama-advisory"):
        _fail("内置适配器", "显式配置后未按预期解析出 ollama-advisory: %r" % st)
    c = cases[0]
    on = ask_new.envelope_from_result(dict(c["result"]), "知识库")
    if on.get("advisory") is None and "advisory" in on:
        _ok("模型不可达 → advisory 保持 None（失败不编造），键仍在：%s" % ("advisory" in on))
    else:
        _fail("内置适配器", "模型不可达却产出了 advisory: %r" % on.get("advisory"))
    print("  last_error=%s" % ma.model_advisory_status()["last_error"])
    bad, _n = diff_fields(dict(c["result"]), {k: v for k, v in on.items()
                                             if k in c["result"]})
    if not bad:
        _ok("失败路径下原字段一个未动")
    else:
        _fail("内置适配器", "失败路径改动了原字段: %s" % bad)
    for k in (ma.ENV_MODEL, ma.ENV_BASE, ma.ENV_TIMEOUT):
        os.environ.pop(k, None)


# ─────────────────────────────────────────── 收尾清理
def cleanup():
    ma.clear_active_adapter()
    os.environ.pop(ma.ENV_ENABLE, None)
    st = ma.model_advisory_status()
    print("\n收尾: 适配器已清除, %s 已移除 → enabled=%s" % (ma.ENV_ENABLE, st["enabled"]))
    if st["enabled"]:
        _fail("收尾", "清理后仍为启用态")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", default=None, help="只跑某个 KB（缺省=全部）")
    ap.add_argument("--snapshot", default="e197585", help="批 2 基准提交（默认 e197585）")
    args = ap.parse_args()

    print("verify_advisory — 批 3 模型建议层（可选适配）")
    print("解释器: %s" % sys.executable)
    print("项目: %s" % ROOT)

    b2 = load_batch2_module(args.snapshot)
    kbs = load_kbs()
    cases = build_cases(kbs, args.kb)
    print("用例: %d 题（批 1 评测集 fixture）" % len(cases))
    if not cases:
        _fail("用例", "没有可用题目")
        return 1

    rate = check_default_vs_batch2(cases, b2)
    check_enabled(cases, b2)
    check_redline_gate(cases)
    check_builtin_adapter_offline(cases)
    cleanup()

    print("\n" + "=" * 78)
    if FAILS:
        print("结果: FAIL（%d 项）" % len(FAILS))
        for s, m in FAILS:
            print("  - [%s] %s" % (s, m))
        return 1
    print("结果: PASS  无模型时逐字段一致率 %.4f%%；启用时只加 advisory，hit/answer 零影响" % (rate or 0.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
