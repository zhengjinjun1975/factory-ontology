#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""词典资产闭环 —— 端到端验证（零污染）。

验证「企业结束导出 → 同行业导入复用 → 独立来源达阈值才沉淀进行业层 → 新企业建模消费」闭环。
手法：把 PUBLIC_DIR / CONFIG_DIR / OUTPUT_DIR 全部指到临时目录（monkeypatch 模块常量），
不触碰真实 industrial_dict/ 与 config/。

用法：python scripts/verify_dict_asset_loop.py
"""
import os
import sys
import json
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CODES = os.path.join(ROOT, "codes")
sys.path.insert(0, CODES)

import absorb_public_dict as ab          # noqa: E402
import industrial_dict_loader as idl     # noqa: E402
import dict_asset as da                  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  " + detail) if detail else ""))


def J(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------- 临时隔离 ----------------
tmp = tempfile.mkdtemp(prefix="dictloop_")
PUB = os.path.join(tmp, "industrial_dict")
CFG = os.path.join(tmp, "config")
OUT = os.path.join(tmp, "output")
for d in (PUB, CFG, OUT):
    os.makedirs(d)
ab.PUBLIC_DIR, ab.CONFIG_DIR = PUB, CFG
ab.CAND_PATH = os.path.join(PUB, "_candidates.json")   # 模块级常量：导入时已算好，必须同步
idl._DICT_DIR = PUB
da.CONFIG_DIR, da.OUTPUT_DIR = CFG, OUT

J(os.path.join(PUB, "00_basis.json"),
  {"type_cn2en": {"设备": "设备"}, "status_cn2en": {"运行中": "运行中"}, "synonym_map": {}})
J(os.path.join(PUB, "01_valve_pump.json"), {})
J(os.path.join(PUB, "02_fine_chem.json"), {})
J(os.path.join(PUB, "03_geophysics.json"), {})

print("临时隔离目录:", tmp)

# ---------------- T1 导出/导入 round-trip ----------------
print("\n[T1] 企业A 导出 → 企业B 导入（round-trip）")
lexA = {"type_cn2en": {"球阀": "球阀", "闸阀": "闸阀"}, "synonym_map": {"球阀": ["球形阀"]},
        "attr_cn2en": {"批次编号": "batch_id"}}
J(os.path.join(CFG, "lexicon_A.json"), lexA)
exp = da.export_lexicon("A", out_dir=tmp)
check("A 导出成功", exp.get("ok") is True, str(exp.get("error", "")))
imp = da.import_lexicon("B", exp["path"], mode="merge", dry_run=False)
check("B 导入成功", imp.get("ok") is True, str(imp.get("error", "")))
b_lex = json.load(open(os.path.join(CFG, "lexicon_B.json"), encoding="utf-8"))
same = all(b_lex.get(k) == lexA.get(k) for k in ("type_cn2en", "synonym_map"))
check("B 词条与 A 一致", same, "type=%s" % sorted(b_lex.get("type_cn2en", {})))
check("工厂字段未被导入覆盖（attr 不参与资产复用）", "attr_cn2en" not in b_lex or not b_lex.get("attr_cn2en"))

# ---------------- T2 同源判定（模板复制只算 1 个来源） ----------------
print("\n[T2] 同源判定：复制同一份词典不该增加独立来源")
J(os.path.join(CFG, "lexicon_c1.json"), {"type_cn2en": {"共识设备": "共识设备", "甲词": "甲词"}})
J(os.path.join(CFG, "lexicon_c1b.json"), {"type_cn2en": {"共识设备": "共识设备", "甲词": "甲词"}})
cl = ab.source_clusters([os.path.join(CFG, "lexicon_c1.json"), os.path.join(CFG, "lexicon_c1b.json")])
check("完全相同两份 → 1 个来源簇", len(cl) == 1, "簇数 %d" % len(cl))
cnt, diag = ab.independent_source_counter([os.path.join(CFG, "lexicon_c1.json"),
                                           os.path.join(CFG, "lexicon_c1b.json")])
check("同源复制不累加来源数", cnt.get("共识设备") == 1, "共识设备来源数 = %s" % cnt.get("共识设备"))

# ---------------- T3 异构词典分别计源 ----------------
print("\n[T3] 异构词典 → 各自独立来源")
J(os.path.join(CFG, "lexicon_c2.json"), {"type_cn2en": {"共识设备": "共识设备", "乙词": "乙词"}})
cnt2, diag2 = ab.independent_source_counter([os.path.join(CFG, "lexicon_c1.json"),
                                             os.path.join(CFG, "lexicon_c2.json")])
check("结构不同的两份 → 2 个来源", diag2["independent_sources"] == 2, str(diag2["independent_sources"]))
check("共同词来源数 = 2", cnt2.get("共识设备") == 2, str(cnt2.get("共识设备")))

# ---------------- T4 阈值闸门 ----------------
print("\n[T4] 阈值闸门：来源不足不升级，达到才升级")
# 2 个来源（< 3）→ 不升级
promo2 = ab.promote_candidates(cnt2, threshold=3, industry="泵阀", verbose=False)
check("2 个独立来源不升级（< 阈值 3）", not promo2, str(promo2))
pub_after2 = json.load(open(os.path.join(PUB, "01_valve_pump.json"), encoding="utf-8"))
check("行业层未被写入", not (pub_after2.get("type_cn2en") or {}).get("共识设备"), "")

# 第 3 个独立来源（内容显著不同）→ 升级
J(os.path.join(CFG, "lexicon_c3.json"),
  {"type_cn2en": {"共识设备": "共识设备", "丙词": "丙词", "丁词": "丁词"}})
cnt3, diag3 = ab.independent_source_counter([os.path.join(CFG, "lexicon_c%d.json" % i) for i in (1, 2, 3)])
check("3 个独立来源", diag3["independent_sources"] == 3, str(diag3["independent_sources"]))
check("共识词来源数 = 3", cnt3.get("共识设备") == 3, str(cnt3.get("共识设备")))
promo3 = ab.promote_candidates(cnt3, threshold=3, industry="泵阀", verbose=False)
check("3 个独立来源 → 升级进行业层", bool(promo3), str(promo3))
pub_after3 = json.load(open(os.path.join(PUB, "01_valve_pump.json"), encoding="utf-8"))
check("行业层真的落盘了该词", "共识设备" in (pub_after3.get("type_cn2en") or {}),
      str(sorted((pub_after3.get("type_cn2en") or {}))[:6]))

# ---------------- T5 闭环：沉淀的词被新企业建模消费 ----------------
print("\n[T5] 闭环：沉淀进行业层的词，新企业建模时能被消费")
merged_base = idl.merge_industrial_dict({})
merged_ind = idl.merge_industrial_dict({}, industry="泵阀")
check("不传行业时看不到该词（只基础层）", "共识设备" not in (merged_base.get("type_cn2en") or {}))
check("传行业后该词进入合并结果（行业层被消费）",
      "共识设备" in (merged_ind.get("type_cn2en") or {}),
      "泵阀合并后 type 词数 %d" % len(merged_ind.get("type_cn2en") or {}))
check("行业层非空键位带进工厂词典", bool(merged_ind.get("type_cn2en")))

# ---------------- T6 候选池留痕 ----------------
print("\n[T6] 未达阈值的词留痕候选池")
res = ab.learn_from_kb(os.path.join(CFG, "lexicon_c1.json"), industry="泵阀", verbose=False)
check("learn 成功", res.get("ok") is True, str(res.get("error", "")))
cand = ab.load_candidates()
check("候选池记录来源", bool(cand) and all("sources" in v for v in cand.values()),
      "%d 词, 样例 %s" % (len(cand), list(cand.items())[:1]))
check("候选池带首见时间", all("first_seen" in v for v in cand.values()))

# ---------------- 清理 ----------------
shutil.rmtree(tmp, ignore_errors=True)
left = os.path.exists(tmp)
check("临时目录已清理", not left)

print("\nPASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项: " + "、".join(FAIL))
sys.exit(1 if FAIL else 0)
