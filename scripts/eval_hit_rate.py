"""轻量本体问答 · 评测集生成 + 命中率基线（P0 研究有效性核心）。

为什么要它：没有对照实验，"本体提升检索命中率"只是假设，不是结论。
本脚本做三件事（全部确定性，零人工标注）：
  1. 从真实 CSV 数据确定性地生成 5 类问题 + golden 答案（答案由数据算出，不靠人写）
  2. 调 /api/ask 跑"轻量本体"组，逐题比对 golden，得命中率
  3. 输出分维度命中率明细，落盘 JSON 供跨会话对比（口径固定：同评测集/同题量/同判法）

用法:
  python scripts/eval_hit_rate.py --kb valve --n 12          # 单库
  python scripts/eval_hit_rate.py --kb valve --n 12 --dry    # 只生成不调接口
"""
import argparse
import csv
import glob
import json
import os
import re
import statistics
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODES = os.path.join(ROOT, "codes")
# 中文名映射（由 main 按 kb 载入）：表名→中文实体名、列名→中文属性名
LEX_ENT, LEX_ATTR = {}, {}


def load_tables(data_dir):
    """读目录下所有 CSV → {表名: [行dict]}。"""
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


def load_lexicon(kb):
    """读该库词典，取中文实体名/属性名 —— 让问题文本贴近真实用户问法（说中文不说列名）。"""
    p = os.path.join(CODES, "config", f"lexicon_{kb}.json")
    if not os.path.exists(p):
        return {}, {}
    d = json.load(open(p, encoding="utf-8"))
    ent = {v: k for k, v in (d.get("entity_cn2en") or {}).items()}      # 表名 → 中文实体名
    attr = {v: k for k, v in (d.get("attr_cn2en") or {}).items()}       # 列名 → 中文属性名
    return ent, attr


def gen_questions(tables, n_per_type=12):
    """确定性生成 5 类问题 + golden 答案。答案全部由数据算出，零人工标注。

    5 类（覆盖问答系统必须能答的基本盘）：
      count      计数：X 有多少条
      enum       枚举：X 的 Y 有哪些
      extreme    极值：X 中 Y 最大的/最小的
      aggregate  聚合：X 的 Y 总计/平均
      filter     条件：X 中 Y 为 Z 的有几条
    """
    qs = []
    ent_cn, attr_cn = LEX_ENT, LEX_ATTR
    for t, rows in tables.items():
        if not rows:
            continue
        cols = list(rows[0].keys())
        # 中文表名：优先词典里的中文实体名（贴近真实问法），退回去行业前缀的表名末段
        cn = ent_cn.get(t) or (t.split("_", 1)[1] if "_" in t else t)
        cn_of = lambda c: attr_cn.get(c, c)          # 列名 → 中文属性名
        num_cols = [c for c in cols if sum(1 for r in rows if _num(r.get(c)) is not None) >= max(2, len(rows) // 2)]
        cat_cols = [c for c in cols if c not in num_cols and 1 < len({r.get(c) for r in rows}) <= max(6, len(rows) // 3)]

        # ① 计数
        qs.append({"type": "count", "table": t,
                   "q": f"{cn}有多少条记录", "golden": str(len(rows)),
                   "check": "num", "golden_num": float(len(rows))})
        # ② 枚举
        for c in cat_cols[:2]:
            vals = sorted({str(r.get(c)) for r in rows if r.get(c) not in (None, "")})
            if 1 < len(vals) <= 12:
                qs.append({"type": "enum", "table": t,
                           "q": f"{cn}的{cn_of(c)}有哪些", "golden": "、".join(vals),
                           "check": "contains_all", "golden_vals": vals})
        # ③ 极值
        for c in num_cols[:2]:
            pairs = [(r.get(c), _num(r.get(c))) for r in rows if _num(r.get(c)) is not None]
            if len(pairs) >= 2:
                mx = max(pairs, key=lambda x: x[1])
                qs.append({"type": "extreme", "table": t,
                           "q": f"{cn}中{cn_of(c)}最大是多少", "golden": str(mx[1]),
                           "check": "num", "golden_num": mx[1]})
        # ④ 聚合
        for c in num_cols[:1]:
            vals = [_num(r.get(c)) for r in rows if _num(r.get(c)) is not None]
            if len(vals) >= 2:
                qs.append({"type": "aggregate", "table": t,
                           "q": f"{cn}的{cn_of(c)}总计是多少", "golden": str(round(sum(vals), 2)),
                           "check": "num", "golden_num": round(sum(vals), 2)})
        # ⑤ 条件筛选
        for c in cat_cols[:1]:
            vals = [str(r.get(c)) for r in rows if r.get(c) not in (None, "")]
            if vals:
                v = max(set(vals), key=vals.count)
                cnt = sum(1 for x in vals if x == v)
                qs.append({"type": "filter", "table": t,
                           "q": f"{cn}中{cn_of(c)}为{v}的有多少条", "golden": str(cnt),
                           "check": "num", "golden_num": float(cnt)})
    # 每类截前 n 个，保证维度均衡
    out = []
    for tp in ("count", "enum", "extreme", "aggregate", "filter"):
        out += [q for q in qs if q["type"] == tp][:n_per_type]
    return out


def ask(question, kb, base=(os.environ.get("FACTORY_API_BASE") or "http://127.0.0.1:8000"),
        key=(os.environ.get("FACTORY_READ_KEY") or "test-read-key")):
    body = json.dumps({"question": question, "kb": kb}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + "/api/ask", data=body,
                                 headers={"X-API-Key": key, "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=90) as resp:
        d = json.load(resp)
    dt = time.time() - t0
    dd = d.get("data") or d
    return str(dd.get("answer") or dd.get("text") or ""), dt


def judge(q, answer):
    """判分：答案里是否出现 golden。确定性比对，不调模型。"""
    if q["check"] == "num":
        g = q["golden_num"]
        nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", answer.replace(",", ""))]
        # 整数按相等判；小数允许 1% 误差（金额/百分比常有四舍五入）
        for n in nums:
            if abs(g) < 1e-9:
                if abs(n) < 1e-9:
                    return True
            elif abs(n - g) <= max(1e-9, abs(g) * 0.01):
                return True
        return False
    if q["check"] == "contains_all":
        return all(str(v) in answer for v in q["golden_vals"])
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", required=True)
    ap.add_argument("--n", type=int, default=12, help="每类问题数")
    ap.add_argument("--dry", action="store_true", help="只生成评测集，不调接口")
    ap.add_argument("--base", default=(os.environ.get("FACTORY_API_BASE") or "http://127.0.0.1:8000"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    data_dir = os.path.join(CODES, f"data_{a.kb}")
    if not os.path.isdir(data_dir):
        print(f"数据目录不存在: {data_dir}")
        return 2
    tables = load_tables(data_dir)
    global LEX_ENT, LEX_ATTR
    LEX_ENT, LEX_ATTR = load_lexicon(a.kb)
    print(f"中文名映射: 实体 {len(LEX_ENT)} 项, 属性 {len(LEX_ATTR)} 项")
    qs = gen_questions(tables, a.n)
    print(f"评测集: {len(qs)} 题 (kb={a.kb}, 表数={len(tables)})")
    from collections import Counter
    print("  维度分布:", dict(Counter(q["type"] for q in qs)))

    if a.dry:
        for q in qs[:5]:
            print(f"   [{q['type']:9s}] {q['q']}  → golden={q['golden']}")
        return 0

    hit, miss, by_type = 0, [], {}
    t_all = []
    for q in qs:
        try:
            ans, dt = ask(q["q"], a.kb, base=a.base)
        except Exception as e:
            ans, dt = f"[调用失败] {e}", 0.0
        ok = judge(q, ans)
        t_all.append(dt)
        d = by_type.setdefault(q["type"], {"hit": 0, "n": 0})
        d["n"] += 1
        d["hit"] += 1 if ok else 0
        if ok:
            hit += 1
        else:
            miss.append({"q": q["q"], "golden": q["golden"], "answer": ans[:150], "type": q["type"]})

    rate = hit / len(qs) if qs else 0
    print()
    print(f"命中率: {hit}/{len(qs)} = {rate:.1%}")
    print(f"时延: p50={statistics.median(t_all):.2f}s  平均={statistics.mean(t_all):.2f}s")
    print()
    print("分维度:")
    for tp in ("count", "enum", "extreme", "aggregate", "filter"):
        d = by_type.get(tp)
        if d:
            print(f"  {tp:10s} {d['hit']}/{d['n']} = {d['hit']/d['n']:.0%}")
    if miss:
        print()
        print(f"未命中 {len(miss)} 题（前 8 条）:")
        for m in miss[:8]:
            print(f"  [{m['type']:9s}] {m['q']}")
            print(f"        golden={str(m['golden'])[:60]}  实际={m['answer'][:90]}")
    out = a.out or os.path.join(ROOT, "scripts", f"eval_result_{a.kb}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"kb": a.kb, "n": len(qs), "hit": hit, "rate": rate,
                   "by_type": by_type, "miss": miss,
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, f, ensure_ascii=False, indent=2)
    print(f"\n结果落盘: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
