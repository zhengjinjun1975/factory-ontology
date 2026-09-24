#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eval_qa.py — 工厂本体问答引擎 · 问答评测集 + 命中率基线（纯标准库，不启 HTTP）。

为什么要有它
------------
没有固定评测集，"改造后问答变好了吗"就只是感觉，不是结论。本脚本把"量尺"固定下来：
  1) 从各 KB 的真实 CSV 数据 + 该 KB 词典，**确定性地**生成五类问题与 golden 答案
     （答案由数据算出，不靠人写、不调模型），落盘成评测集 fixture；
  2) 直接 import 问答引擎核心 `ontology_qa_v3.answer`（**不起 HTTP / 不碰 api_server**），
     用 fixture 逐题跑，逐题判分，输出每 KB 与总体的命中/未命中/误答/编造数字。

五类问法（覆盖问答系统必须能答的基本盘）
----------------------------------------
  attr   普通属性问        —— 计数 / 极值 / 均值 / 枚举
  type   类型词作主语      —— "<X>类型有哪些""<X>类型有多少种"
  multi  多实体 / 多表     —— 库内第二个实体计数、跨表关联（批次↔原料）
  neg    否定或比较问法    —— 最小极值、区间（大于 N）、按状态/枚举值过滤
  noans  无答案类（库里没有的东西）—— 期望引擎**诚实拒绝**；若给出具体答案即记"编造"

判分口径（关键：未命中 ≠ 误答 ≠ 编造，三者分开统计）
----------------------------------------------------
  有答案类（answerable=True）：
    hit   命中    —— 答案含 golden 事实
    miss  未命中  —— 引擎拒答 / 报"暂不支持"（诚实但答不出）
    wrong 误答    —— 引擎给了具体答案但事实错误（有答案但答错）
  无答案类（answerable=False）：
    refuse_ok   正确拒绝 —— 命中拒答话术，或明确答"0/无"
    fabricate   编造     —— 断言了库中不存在的具体事实（数字 / 实体名）
    empty_zero  擦边     —— 返回了具体 "0/N 台" 之类空答（既非拒答也非明确编造，单独列出）

用法
----
  # 1) 生成/刷新评测集 fixture（确定性，可复现）
  python scripts/eval_qa.py --gen
  # 2) 跑基线（单库 / 全库）
  python scripts/eval_qa.py --kb valve --out scripts/_tmp_valve.json
  python scripts/eval_qa.py --out scripts/eval_qa_baseline.json
  # 3) 只看评测集不跑引擎
  python scripts/eval_qa.py --dry
"""
import argparse
import csv
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODES = os.path.join(ROOT, "codes")
CFG = os.path.join(CODES, "config")
FIXDIR = os.path.join(CODES, "tests", "fixtures", "qa_eval")
KBS_FILE = os.path.join(CFG, "kbs.json")

# ------------------------------------------------------------------ 元数据
# 每 KB：实体(中文名/表/量词)、数值列(列→中文)、类别列(列→中文)、无答案问法的假概念、跨表关联。
# 全部来自真实 CSV 列 + 该 KB 词典的中文名，保证问法自然、golden 可从数据推出。
KB_META = {
    "energy_station": {
        "entities": [("设备", "energy_station", "台")],
        "num": {"power_kw": "功率", "temperature": "温度", "run_hours": "运行时长"},
        "cat": {"device_type": "设备类型", "status": "状态"},
        "fake_entities": ["洗衣机", "空调", "员工", "客户"],
        "fake_attrs": ["转速", "纯度"],
        "joins": [],
        "multi_note": "单表 KB：多实体类问法以 状态×类型 组合题代替",
    },
    "manufacturing": {
        "entities": [("设备", "equipment", "台"), ("产线", "line", "条"), ("供应商", "supplier", "家")],
        "num": {"power_kw": "功率"},
        "cat": {"device_type": "设备类型", "status": "状态", "zone": "区域", "location": "所在地"},
        "fake_entities": ["客户", "订单", "产品", "锅炉"],
        "fake_attrs": ["纯度"],
        "joins": [],
        "multi_note": "三表 KB：第二/第三实体计数即为多表问",
    },
    "food": {
        "entities": [("产品", "food_products", "个"), ("设备", "food_equipment", "台"),
                     ("批次", "food_batches", "批"), ("原料", "food_raw_materials", "种"),
                     ("质检", "food_qc", "条")],
        "num": {"expiry_days": "保质期", "price": "价格", "power_kw": "功率", "quantity": "数量",
                "stock_qty": "库存"},
        "cat": {"category": "品类", "storage": "储存方式", "device_type": "设备类型",
                "workshop": "车间", "status": "状态", "qc_result": "质检结果", "result": "结果"},
        "fake_entities": ["客户", "订单", "供应商", "阀门"],
        "fake_attrs": ["纯度"],
        "joins": [("food_batch_ingredient", "batch_id", "raw_id", "B001", "B001 批次用了多少种原料")],
        "multi_note": "六表 KB：含批次↔原料关联表",
    },
    "valve": {
        "entities": [("产品", "valve_products", "个"), ("设备", "valve_equipment", "台"),
                     ("客户", "valve_customers", "家"), ("批次", "valve_batches", "批"),
                     ("原料", "valve_raw_materials", "种"), ("质检", "valve_qc", "条"),
                     ("销售", "valve_sales", "笔")],
        "num": {"price": "价格", "size_mm": "尺寸", "power_kw": "功率", "vibration_mm_s": "振动",
                "temp_c": "温度", "current_a": "电流", "quantity": "数量", "stock": "库存",
                "hold_sec": "保压秒数", "amount": "金额"},
        "cat": {"type": "类型", "pressure_grade": "压力等级", "body_material": "阀体材质",
                "connection": "连接方式", "device_type": "设备类型", "workshop": "车间",
                "status": "状态", "region": "区域", "industry": "行业", "credit_level": "信用等级",
                "team": "班组", "result": "结果", "check_item": "检查项目", "material": "材质",
                "qc_result": "质检结果"},
        "fake_entities": ["洗衣机", "机器人", "员工", "催化剂", "卡车"],
        "fake_attrs": ["纯度"],
        "joins": [("valve_batch_ingredient", "batch_id", "raw_id", "B001", "B001 批次用了多少种原料")],
        "multi_note": "八表 KB：含批次↔原料关联表",
    },
    "chem": {
        "entities": [("产品", "chem_products", "个"), ("设备", "chem_equipment", "台"),
                     ("批次", "chem_batches", "批"), ("原料", "chem_raw_materials", "种")],
        "num": {"purity_pct": "纯度", "price": "价格", "capacity_l": "容量", "pressure_mpa": "压力",
                "temp_c": "温度", "stir_speed_rpm": "搅拌转速", "batch_size_l": "批次产量",
                "yield_pct": "收率", "stock_kg": "库存", "price_per_kg": "单价"},
        "cat": {"category": "品类", "device_type": "设备类型", "workshop": "车间", "status": "状态",
                "quality_status": "质量状态", "team": "班组", "type": "类型", "supplier": "供应商"},
        "fake_entities": ["客户", "订单", "催化剂", "阀门", "电池"],
        "fake_attrs": ["收率等级"],
        "joins": [],
        "multi_note": "四表 KB：第二/第三实体计数即为多表问",
    },
    "ai4i": {
        "entities": [("机器", "ai4i", "台")],
        "num": {"Air_temperature_": "空气温度", "Process_temperature_": "工艺温度",
                "Rotational_speed_": "转速", "Torque_": "扭矩", "Tool_wear_": "刀具磨损"},
        "cat": {"Type": "类型", "Machine_failure": "机器故障"},
        "fake_entities": ["客户", "订单", "产品", "供应商", "批次"],
        "fake_attrs": ["刀具寿命"],
        "joins": [],
        "multi_note": "单表 KB：多实体类问法以 类型×故障 组合题代替",
    },
    "library": {
        "entities": [("图书", "library_inventory", "本")],
        "num": {"price": "价格", "stock": "库存", "borrowed": "借阅量"},
        "cat": {"category": "类型", "publisher": "出版社"},
        "fake_entities": ["客户", "订单", "设备", "员工", "作者"],
        "fake_attrs": ["页数"],
        "joins": [],
        "multi_note": "单表 KB：多实体类问法以 类型×出版社 组合题代替",
    },
    "auto_parts": {
        "entities": [("产品", "products", "个"), ("设备", "equipment", "台"),
                     ("客户", "customers", "家"), ("订单", "orders", "个")],
        "num": {"price": "价格", "stock_qty": "库存", "power_kw": "功率", "temp_c": "温度",
                "vibration_mm_s": "振动", "quantity": "数量", "amount": "金额"},
        "cat": {"product_type": "产品类型", "material": "材质", "status": "状态",
                "device_type": "设备类型", "workshop": "车间", "region": "区域",
                "industry": "行业", "credit_level": "信用等级", "delivery_status": "交付状态"},
        "fake_entities": ["供应商", "质检", "锅炉", "员工"],
        "fake_attrs": ["纯度"],
        "joins": [],
        "multi_note": "四表 KB：第二/第三/第四实体计数即为多表问",
    },
}

REFUSE_MARKERS = ("暂不支持", "未找到", "无相关数据", "不含所问", "没有找到", "未提供",
                  "无法回答", "答不了", "没有相关", "未收录", "无该", "不存在")


# ------------------------------------------------------------------ 基础 IO
def load_kbs():
    with open(KBS_FILE, encoding="utf-8") as f:
        return json.load(f)["kbs"]


def read_tables(data_dir):
    out = {}
    for f in sorted(glob.glob(os.path.join(data_dir, "*.csv"))):
        t = os.path.splitext(os.path.basename(f))[0]
        with open(f, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        if rows:
            out[t] = rows
    return out


def _num(v):
    try:
        return float(str(v).strip())
    except Exception:
        return None


def _nums(rows, col):
    return [x for x in (_num(r.get(col)) for r in rows) if x is not None]


def _distinct(rows, col):
    return [v for v in sorted({str(r.get(col)) for r in rows if r.get(col) not in (None, "")})]


def _fmt(v):
    """数值→简洁字符串（整数不带 .0），用于 expect 展示。"""
    if isinstance(v, float) and abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return str(v)


# ------------------------------------------------------------------ 生成评测集
def gen_kb_questions(kb, meta, tables):
    """确定性生成某 KB 的五类问题 + golden。golden 全部由 CSV 数据算出。"""
    qs = []
    seq = [0]

    def add(category, question, expect, check, target=None, absent=None, answerable=True):
        seq[0] += 1
        qs.append({"id": "%s-%03d" % (kb, seq[0]), "category": category,
                   "question": question, "expect": expect, "check": check,
                   "target": target, "absent": absent, "answerable": answerable})

    # ---- 多实体/多表：其余实体计数（先算，供 multi 用） ----
    ent_names = {}
    for cn, tbl, measure in meta["entities"]:
        rows = tables.get(tbl)
        if not rows:
            continue
        ent_names[cn] = (tbl, measure, rows)

    # 主实体（第一张表）承载大部分 attr/type/neg 问
    for cn, tbl, measure in meta["entities"]:
        rows = tables.get(tbl)
        if not rows:
            continue
        # ① 普通属性问：计数
        add("attr", "有多少%s%s" % (measure, cn), "%d" % len(rows), "num", float(len(rows)))
        add("attr", "%s总数" % cn, "%d" % len(rows), "num", float(len(rows)))
        # ② 普通属性问：极值 / 均值；③ 否定或比较：最小极值 / 区间
        for col, cn_attr in meta["num"].items():
            if col not in (rows[0].keys() if rows else []):
                continue
            vals = _nums(rows, col)
            if len(vals) < 2:
                continue
            mx, mn = max(vals), min(vals)
            mean = sum(vals) / len(vals)
            add("attr", "%s中%s最大是多少" % (cn, cn_attr), _fmt(mx), "num", mx)
            add("attr", "%s的%s平均是多少" % (cn, cn_attr), "%.2f" % mean, "num", round(mean, 2))
            add("neg", "%s中%s最小是多少" % (cn, cn_attr), _fmt(mn), "num", mn)
            add("neg", "%s最小的%s" % (cn_attr, cn), _fmt(mn), "num", mn)
            # 区间问：取均值附近阈值，golden=满足条数（排除退化为全/零的阈值）
            for thr in sorted({round(mean, 2)}):
                thrf = float(thr)
                cnt = sum(1 for v in vals if v > thrf)
                if cnt in (0, len(vals)):
                    continue
                add("neg", "%s中%s大于%s的%s有多少" % (cn, cn_attr, _fmt(thrf), cn),
                    "%d" % cnt, "num_paren", float(cnt))
        # ④ 类型词作主语 / 枚举
        for col, cn_attr in meta["cat"].items():
            if col not in (rows[0].keys() if rows else []):
                continue
            vals = _distinct(rows, col)
            if not (1 < len(vals) <= 12):
                continue
            cat = "type" if "类型" in cn_attr else "attr"
            add(cat, "%s的%s有哪些" % (cn, cn_attr), "、".join(vals), "contains", list(vals))
            add(cat, "%s的%s有多少种" % (cn, cn_attr), "%d" % len(vals), "num", float(len(vals)))
            # ⑤ 否定或比较：按枚举值过滤 -> 列出实体（含 absent 反作弊：不得列出不属于该值的）
            if cn_attr == "状态":
                for v in vals[:3]:
                    hit_rows = [r for r in rows if str(r.get(col)) == v]
                    names = [_row_name(r, rows[0].keys()) for r in hit_rows if _row_name(r, rows[0].keys())]
                    others = [_row_name(r, rows[0].keys()) for r in rows
                              if str(r.get(col)) != v and _row_name(r, rows[0].keys())]
                    if names:
                        add("neg", "%s的%s有哪些" % (v, cn), "、".join(names[:20]),
                            "contains_not", names[:20], absent=others[:20])

    # ---- 多实体 / 多表：库内其他实体计数（排除主实体自身） ----
    if len(ent_names) > 1:
        for cn, tbl, measure in meta["entities"][1:]:
            if cn in ent_names:
                add("multi", "%s总数" % cn, "%d" % len(ent_names[cn][2]), "num",
                    float(len(ent_names[cn][2])))
        # 跨表关联：批次↔原料（有则用，无则用组合题兜）
    for jt, kcol, vcol, key, qtext in meta.get("joins", []):
        rows = tables.get(jt)
        if rows:
            vs = {str(r.get(vcol)) for r in rows if str(r.get(kcol)) == key}
            add("multi", qtext, "%d 种" % len(vs), "num", float(len(vs)))
    # 单表 KB：用 类型值过滤计数 组合题补 multi 类（本 KB 只有一张表，无跨表可问）
    if len(ent_names) <= 1:
        for cn, (tbl, measure, rows) in ent_names.items():
            ty_col = next((c for c, m in meta["cat"].items() if "类型" in m), None)
            if ty_col and ty_col in rows[0]:
                ty_vals = _distinct(rows, ty_col)
                for tv in ty_vals[:3]:
                    if not tv:
                        continue
                    cnt = sum(1 for r in rows if str(r.get(ty_col)) == tv)
                    add("multi", "%s类%s有多少%s" % (tv, cn, measure), "%d" % cnt, "num", float(cnt))

    # ---- 无答案类：问库里没有的东西，期望诚实拒绝 ----
    for fe in meta["fake_entities"]:
        add("noans", "有多少个%s" % fe, "拒答(库中无此实体)", "refuse", answerable=False)
        add("noans", "%s总数" % fe, "拒答(库中无此实体)", "refuse", answerable=False)
    for fa in meta["fake_attrs"][:2]:
        cn0 = meta["entities"][0][0]
        add("noans", "%s中%s最大是多少" % (cn0, fa), "拒答(库中无此属性)", "refuse", answerable=False)
    # 均衡裁剪：避免大表 KB（如 valve）属性题数量碾压，保持五类可比
    caps = {"attr": 16, "type": 8, "multi": 8, "neg": 12, "noans": 12}
    kept, cnt = [], {}
    for q in qs:
        c = q["category"]
        if cnt.get(c, 0) < caps.get(c, 99):
            kept.append(q)
            cnt[c] = cnt.get(c, 0) + 1
    for i, q in enumerate(kept, 1):
        q["id"] = "%s-%03d" % (kb, i)
    return kept


def _row_name(row, keys):
    """取该行最像"名字"的列作实体显示名。"""
    for k in ("device_name", "book_name", "product_name", "customer_name", "customer", "name",
              "part_name", "raw_name", "supplier_name", "line_name", "check_item", "id"):
        if k in row and row.get(k):
            return str(row[k])
    for k in keys:
        if row.get(k):
            return str(row[k])
    return ""


def gen_all(kbs):
    """为每个可用 KB 生成评测集。返回 {kb: [q,...]}。"""
    out = {}
    for kb, meta in KB_META.items():
        cfg = kbs.get(kb)
        if not cfg:
            continue
        data_dir = os.path.join(CODES, cfg.get("data_dir") or "")
        tables = read_tables(data_dir) if os.path.isdir(data_dir) else {}
        out[kb] = gen_kb_questions(kb, meta, tables)
    return out


# ------------------------------------------------------------------ 引擎
_ENGINE_CACHE = {}


def get_engine(kb):
    """构建该 KB 的问答引擎调用器（直接 import 核心，不起 HTTP）。"""
    if kb in _ENGINE_CACHE:
        return _ENGINE_CACHE[kb]
    if CODES not in sys.path:
        sys.path.insert(0, CODES)
    import ontology_qa_v3 as v3
    cfg = load_kbs()[kb]
    nt = os.path.join(CODES, cfg["nt"])
    lex = os.path.join(CODES, "config", cfg["lexicon"])
    D = v3.load_dict(lex)
    data = v3.build_data(v3.parse_nt(nt), D)

    def _ask(q):
        try:
            return str(v3.answer(q, data, D))
        except Exception as e:  # 引擎异常按"拒答/未命中"记，不掩盖
            return "[引擎异常] %s" % e

    _ENGINE_CACHE[kb] = _ask
    return _ask


# ------------------------------------------------------------------ 判分
_NUM_RE = re.compile(r"(?<![\d.])-?\d+(?:\.\d+)?(?![\d.])")


def _ans_nums(ans):
    return [float(x) for x in _NUM_RE.findall(ans.replace(",", ""))]


def _has_num(ans, target):
    try:
        t = float(target)
    except Exception:
        return False
    for n in _ans_nums(ans):
        if abs(t) < 1e-9:
            if abs(n) < 1e-9:
                return True
        elif abs(n - t) <= max(1e-9, abs(t) * 0.01):
            return True
    return False


def _is_refuse(ans):
    return (not ans) or any(m in ans for m in REFUSE_MARKERS)


def _is_empty_zero(ans):
    """无答案类里返回的是"空答"(数字全为 0 / 无任何非零断言)，非明确编造。"""
    nums = _ans_nums(ans)
    return bool(nums) and all(abs(n) < 1e-9 for n in nums)


def judge(q, ans):
    """返回 (bucket, ok)。bucket ∈ hit/miss/wrong/refuse_ok/fabricate/empty_zero。"""
    if not q.get("answerable", True):  # 无答案类
        if _is_refuse(ans):
            return "refuse_ok", True
        if _is_empty_zero(ans):
            return "empty_zero", False
        return "fabricate", False
    # 有答案类
    ck = q["check"]
    if ck == "skip":
        return "skip", True
    if _is_refuse(ans):
        return "miss", False
    if ck == "num":
        return ("hit", True) if _has_num(ans, q["target"]) else ("wrong", False)
    if ck == "num_paren":
        m = re.search(r"\((\d+)\)", ans)
        if m:
            return ("hit", True) if _has_num(m.group(1), q["target"]) else ("wrong", False)
        return ("hit", True) if _has_num(ans, q["target"]) else ("wrong", False)
    if ck == "contains":
        ok = all(str(v) in ans for v in (q["target"] or []))
        return ("hit", True) if ok else ("wrong", False)
    if ck == "contains_not":
        ok = all(str(v) in ans for v in (q["target"] or [])) and \
            not any(str(v) in ans for v in (q.get("absent") or []))
        return ("hit", True) if ok else ("wrong", False)
    return "wrong", False


# ------------------------------------------------------------------ 主流程
def run_kb(kb, questions):
    ask = get_engine(kb)
    per = {"total": 0, "answerable": 0, "hit": 0, "miss": 0, "wrong": 0,
           "noans": 0, "refuse_ok": 0, "fabricate": 0, "empty_zero": 0,
           "by_category": {}, "wrong_samples": [], "fabricate_samples": []}
    details = []
    for q in questions:
        ans = ask(q["question"])
        bucket, ok = judge(q, ans)
        per["total"] += 1
        cat = q["category"]
        c = per["by_category"].setdefault(cat, {"n": 0, "ok": 0})
        c["n"] += 1
        if q.get("answerable", True):
            per["answerable"] += 1
        else:
            per["noans"] += 1
        per[bucket] = per.get(bucket, 0) + 1
        if ok:
            c["ok"] += 1
        details.append({"id": q["id"], "category": cat, "question": q["question"],
                        "expect": q["expect"], "bucket": bucket, "answer": ans[:200]})
        if bucket == "wrong" and len(per["wrong_samples"]) < 6:
            per["wrong_samples"].append({"q": q["question"], "golden": q["expect"],
                                         "ans": _stable_text(ans)[:120]})
        if bucket == "fabricate" and len(per["fabricate_samples"]) < 6:
            per["fabricate_samples"].append({"q": q["question"], "ans": _stable_text(ans)[:120]})
    return per, details


def aggregate(per_kb):
    agg = {k: 0 for k in ("total", "answerable", "hit", "miss", "wrong",
                          "noans", "refuse_ok", "fabricate", "empty_zero")}
    for kb, per in per_kb.items():
        for k in agg:
            agg[k] += per.get(k, 0)
    return agg


def fmt_rates(agg):
    a = agg["answerable"] or 1
    return {"hit_rate": round(agg["hit"] / a, 4),
            "miss_rate": round(agg["miss"] / a, 4),
            "wrong_rate": round(agg["wrong"] / a, 4)}


def _stable_text(ans):
    """把样本答案做顺序归一（排序 "- " 行与顿号并列项），消除引擎集合迭代顺序带来的非确定性，
    使基线 JSON 字节可复现。仅用于存样例，不影响判分。"""
    lines = ans.split("\n")
    dashes = sorted(l.strip() for l in lines if l.strip().startswith("- "))
    body = "\n".join(l for l in lines if not l.strip().startswith("- "))
    body = "、".join(sorted(body.split("、")))
    if dashes:
        body += "\n" + "\n".join(dashes)
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", default=None, help="只跑某个 KB（缺省=全部）")
    ap.add_argument("--out", default=None, help="基线 JSON 输出路径")
    ap.add_argument("--gen", action="store_true", help="生成/刷新评测集 fixture")
    ap.add_argument("--dry", action="store_true", help="只打印评测集，不跑引擎")
    ap.add_argument("--quiet", action="store_true", help="只打印机器可读汇总")
    a = ap.parse_args()

    kbs = load_kbs()
    evalset = gen_all(kbs)

    if a.gen:
        os.makedirs(FIXDIR, exist_ok=True)
        for kb, qs in evalset.items():
            with open(os.path.join(FIXDIR, "%s.json" % kb), "w", encoding="utf-8") as f:
                json.dump({"kb": kb, "n": len(qs), "questions": qs}, f,
                          ensure_ascii=False, indent=2)
            print("[gen] %-16s %d 题 -> %s.json" % (kb, len(qs), kb))
        return 0

    # 评测集优先读 fixture（固定量尺）；无 fixture 时用即时生成
    def load_set(kb):
        p = os.path.join(FIXDIR, "%s.json" % kb)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)["questions"]
        return evalset[kb]

    targets = [a.kb] if a.kb else list(evalset.keys())
    if a.dry:
        for kb in targets:
            qs = load_set(kb)
            from collections import Counter
            print("%s: %d 题  %s" % (kb, len(qs), dict(Counter(q["category"] for q in qs))))
        return 0

    per_kb, details_all = {}, {}
    for kb in targets:
        qs = load_set(kb)
        per, details = run_kb(kb, qs)
        per["hit_rate"], per["miss_rate"], per["wrong_rate"] = (
            fmt_rates(per)["hit_rate"], fmt_rates(per)["miss_rate"], fmt_rates(per)["wrong_rate"])
        per_kb[kb] = per
        details_all[kb] = details
        if not a.quiet:
            print("%-16s 题%3d 可答%3d | 命中%3d(%.0f%%) 未命中%3d 误答%3d | 无答案%3d 正确拒%3d 编造%3d 空答%3d"
                  % (kb, per["total"], per["answerable"], per["hit"], per["hit_rate"] * 100,
                     per["miss"], per["wrong"], per["noans"], per["refuse_ok"],
                     per["fabricate"], per["empty_zero"]))

    agg = aggregate(per_kb)
    rates = fmt_rates(agg)
    if not a.quiet:
        print("-" * 90)
        print("总体  题%d 可答%d | 命中%d(%.1f%%) 未命中%d(%.1f%%) 误答%d(%.1f%%) | 无答案%d 正确拒%d 编造%d 空答%d"
              % (agg["total"], agg["answerable"], agg["hit"], rates["hit_rate"] * 100,
                 agg["miss"], rates["miss_rate"] * 100, agg["wrong"], rates["wrong_rate"] * 100,
                 agg["noans"], agg["refuse_ok"], agg["fabricate"], agg["empty_zero"]))

    out = a.out or os.path.join(ROOT, "scripts", "eval_qa_baseline.json")
    # 基线 JSON 只放确定性数字（命中/未命中/误答/编造计数 + 分维度 + 率），
    # 不放原始答案文本：引擎对并列实体/并列极值的输出顺序不稳定（集合迭代），
    # 存文本会让 JSON 每次不同、丧失"可复现量尺"的意义。样例只打到 stdout 供人工看。
    per_kb_det = {kb: {k: v for k, v in per.items()
                       if k not in ("wrong_samples", "fabricate_samples")}
                  for kb, per in per_kb.items()}
    payload = {
        "engine": "ontology_qa_v3.answer (in-process, 无 HTTP)",
        "metric_def": {
            "hit_rate": "hit / answerable", "miss_rate": "miss / answerable",
            "wrong_rate": "wrong / answerable", "fabricate": "无答案类中断言了库中不存在的具体事实",
            "empty_zero": "无答案类中返回具体 0/N 空答（非拒答非编造，单独计）"},
        "per_kb": per_kb_det, "overall": dict(agg, **rates),
        "skipped_kb": {kb: "kbs.json 未注册或 data_dir 缺数据" for kb, qs in evalset.items() if not qs},
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    if not a.quiet:
        print("\n-- 误答/编造样例（仅 stdout，不入基线 JSON）--")
        for kb, per in per_kb.items():
            for s in per.get("wrong_samples", [])[:3]:
                print("  [%s 误答] %s | golden=%s | 实际=%s" % (kb, s["q"], s["golden"], s["ans"][:80]))
            for s in per.get("fabricate_samples", [])[:3]:
                print("  [%s 编造] %s | 实际=%s" % (kb, s["q"], s["ans"][:80]))
        print("\n基线落盘: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
