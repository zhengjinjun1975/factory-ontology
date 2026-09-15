"""P0 对照实验：裸LLM / 朴素RAG / 轻量本体 三组同题对比。

为什么必须做：没有对照，"55% 命中率"无法解释 —— 是本体起了作用，
还是模型本来就会？三组同题同判法，才能把本体的增量贡献量出来。

  组A 裸 LLM       只给问题，不给任何数据（模型靠先验瞎猜）
  组B 朴素 RAG     给该表全部行（CSV 原文）+ 问题（不做本体结构）
  组C 轻量本体     调 /api/ask（本体+词典+规则）

用法:
  python scripts/eval_ablation.py --kb valve --n 12
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODES = os.path.join(ROOT, "codes")
sys.path.insert(0, CODES)

LEX_ENT, LEX_ATTR = {}, {}


def load_tables(data_dir):
    out = {}
    for f in sorted(glob.glob(os.path.join(data_dir, "*.csv"))):
        t = os.path.splitext(os.path.basename(f))[0]
        with open(f, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        if rows:
            out[t] = rows
    return out


def load_lexicon(kb):
    p = os.path.join(CODES, "config", f"lexicon_{kb}.json")
    if not os.path.exists(p):
        return {}, {}
    d = json.load(open(p, encoding="utf-8"))
    return ({v: k for k, v in (d.get("entity_cn2en") or {}).items()},
            {v: k for k, v in (d.get("attr_cn2en") or {}).items()})


# 复用评测集生成器（同一评测集/同一判法 —— 口径唯一，不另造一套）
import importlib.util
_spec = importlib.util.spec_from_file_location("ev", os.path.join(ROOT, "scripts", "eval_hit_rate.py"))
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def llm(prompt, max_tokens=400):
    """调统一模型入口（自动路由 + 降级）；失败返回空串，不抛异常。"""
    try:
        import model_llm as ml
        out, _ = ml.llm_generate_auto(prompt, question="评测对照", temperature=0.1, max_tokens=max_tokens)
        if out and not str(out).startswith(("[模型错误]", "[模型不可用]")):
            return str(out)
    except Exception:
        pass
    return ""


def group_a(q, tables):
    """裸 LLM：不给数据，只给问题。"""
    return llm(f"请回答这个工厂管理问题（你没有任何数据，只能凭常识回答）：{q['q']}\n只给答案，不要解释。")


def group_b(q, tables):
    """朴素 RAG：把该表 CSV 全文塞进 prompt（不做本体结构）。"""
    t = q["table"]
    rows = tables.get(t, [])
    head = ",".join(list(rows[0].keys())) if rows else ""
    body = "\n".join(",".join(str(r.get(c, "")) for c in rows[0].keys()) for r in rows[:60]) if rows else ""
    return llm(f"下面是工厂数据表 {t}（CSV 格式）：\n{head}\n{body}\n\n请回答：{q['q']}\n只给答案，不要解释。")


def group_c(q, kb, base, key):
    """轻量本体：调 /api/ask。"""
    body = json.dumps({"question": q["q"], "kb": kb}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + "/api/ask", data=body,
                                 headers={"X-API-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.load(r)
    dd = d.get("data") or d
    return str(dd.get("answer") or dd.get("text") or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", required=True)
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--base", default=(os.environ.get("FACTORY_API_BASE") or "http://127.0.0.1:8000"))
    ap.add_argument("--key", default="test-read-key")
    ap.add_argument("--groups", default="abc", help="要跑的组，如 ab")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    global LEX_ENT, LEX_ATTR
    LEX_ENT, LEX_ATTR = load_lexicon(a.kb)
    ev.LEX_ENT, ev.LEX_ATTR = LEX_ENT, LEX_ATTR
    tables = load_tables(os.path.join(CODES, f"data_{a.kb}"))
    qs = ev.gen_questions(tables, a.n)
    print(f"评测集 {len(qs)} 题 | kb={a.kb} | 组={a.groups}")

    res = {}
    for g in a.groups:
        hit = 0
        t0 = time.time()
        by_t = {}
        for q in qs:
            try:
                if g == "a":
                    ans = group_a(q, tables)
                elif g == "b":
                    ans = group_b(q, tables)
                else:
                    ans = group_c(q, a.kb, a.base, a.key)
            except Exception as e:
                ans = f"[失败] {e}"
            ok = ev.judge(q, ans)
            hit += 1 if ok else 0
            d = by_t.setdefault(q["type"], {"hit": 0, "n": 0})
            d["n"] += 1
            d["hit"] += 1 if ok else 0
        rate = hit / len(qs) if qs else 0
        res[g] = {"hit": hit, "n": len(qs), "rate": rate, "by_type": by_t, "sec": round(time.time() - t0, 1)}
        name = {"a": "裸LLM", "b": "朴素RAG", "c": "轻量本体"}[g]
        print(f"\n=== 组{g} {name} ===")
        print(f"  命中率 {hit}/{len(qs)} = {rate:.1%}  耗时 {res[g]['sec']}s")
        for tp in ("count", "enum", "extreme", "aggregate", "filter"):
            d = by_t.get(tp)
            if d:
                print(f"    {tp:10s} {d['hit']}/{d['n']} = {d['hit']/d['n']:.0%}")

    out = a.out or os.path.join(ROOT, "scripts", f"eval_ablation_{a.kb}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"kb": a.kb, "n": len(qs), "groups": res, "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False, indent=2)
    print(f"\n落盘: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
