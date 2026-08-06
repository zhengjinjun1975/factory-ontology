#!/usr/bin/env python3
"""benchmark.py — 本体问答 vs 纯 LLM 命中率对照评测

验证核心主张：本体建模是否提升知识检索/问答命中率。

方法（可复现，不需手写答案）：
1. 从源 CSV 用确定性逻辑算出"标准答案"（数量/极值/过滤/平均/总和）
2. 同一批问题分别跑两条路径：
   A. 本体问答(ontology_qa_v3 规则引擎, 无模型, 确定性)
   B. 纯 LLM(把源数据喂给模型, 不加本体)
3. 用标准答案模糊匹配两条路径的输出, 算命中率

用法:
  python benchmark.py data/ai4i.csv [--llm] [--top N] [--seed S]

默认只跑本体规则引擎(零模型, 可复现); --llm 加跑纯 LLM 对比(需配置 model_llm)。
--top 限制样本量(默认全部), --seed 固定抽样。
"""
import sys
import os
import csv
import json
import random
import re
import argparse

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


# ------------------------------------------------------------------ 标准答案(确定性, 从源数据算)
def compute_ground_truth(rows, headers, attr_cn2en):
    """从源 CSV 用确定性逻辑算标准答案。题目用中文属性名(词典), 标准答案用英文字段算。
    返回 {中文问题: 答案}"""
    gt = {}
    n = len(rows)
    gt["一共有多少条记录"] = str(n)

    # 数值属性极值/平均/总和 (用词典中文名出题, 英文名算答案)
    for cn, en in attr_cn2en.items():
        if en not in headers:
            continue
        nums = []
        for r in rows:
            try:
                nums.append(float(r[en]))
            except (ValueError, TypeError):
                pass
        if len(nums) < 2:
            continue
        gt[f"{cn}的最大值"] = f"{max(nums):g}"
        gt[f"{cn}的最小值"] = f"{min(nums):g}"
        gt[f"{cn}的平均值"] = f"{sum(nums)/len(nums):.2f}"
        gt[f"{cn}的总和"] = f"{sum(nums):g}"

    # 0/1 布尔字段过滤计数(用中文名出题)
    for cn, en in attr_cn2en.items():
        if en not in headers:
            continue
        vals = {r[en].strip() for r in rows if r.get(en)}
        if vals <= {"0", "1"} and "1" in vals:
            one = sum(1 for r in rows if r[en].strip() == "1")
            zero = sum(1 for r in rows if r[en].strip() == "0")
            gt[f"{cn}=1 的数量"] = str(one)
            gt[f"{cn}=0 的数量"] = str(zero)
    return gt


# ------------------------------------------------------------------ 本体问答路径
def ontology_answer(question, nt_file, lex_file):
    import ontology_qa_v3 as v3
    D = v3.load_dict(lex_file)
    data = v3.build_data(v3.parse_nt(nt_file), D)
    return v3.answer(question, data, D)


# ------------------------------------------------------------------ 纯 LLM 路径(原始数据, 无本体)
def llm_answer(question, rows, headers, top=50):
    from model_llm import llm_generate
    sample = rows[:top]
    csv_text = "\n".join([",".join(headers)] + [",".join(r.get(h, "") for h in headers) for r in sample])
    prompt = (
        f"下面是结构化数据(前{top}行):\n{csv_text}\n"
        f"只根据上面数据回答, 给出确定数字。问题: {question}\n"
        "直接输出答案数字, 不要解释。"
    )
    return llm_generate(prompt, temperature=0.0, max_tokens=100)


# ------------------------------------------------------------------ 模糊匹配
def fuzzy_match(answer, gt):
    """GT 是确定数字。数值容错比较(容忍0.1%)，兜底文本匹配。"""
    if not answer or answer.startswith("[模型"):
        return False
    g = gt.strip()
    # 数值容错: 提取答案第一个数字, 与 GT 数字比较
    try:
        gv = float(g.replace(",", ""))
        m = re.search(r'-?\d+(?:,\d{3})*(?:\.\d+)?', answer.replace("，", ","))
        if m:
            av = float(m.group().replace(",", ""))
            if abs(av - gv) <= max(1, abs(gv) * 0.001):
                return True
    except ValueError:
        pass
    # 文本匹配兜底
    return g in answer


def main():
    ap = argparse.ArgumentParser(description="本体问答 vs 纯LLM 命中率对照评测")
    ap.add_argument("data_csv", help="源数据 CSV(标准答案从它算)")
    ap.add_argument("--nt", help="本体 N-Triples 文件(默认同目录 output/<表名>.nt)")
    ap.add_argument("--lexicon", help="词典 json(默认同目录 config/lexicon_<表名>.json)")
    ap.add_argument("--llm", action="store_true", help="加跑纯 LLM 对比")
    ap.add_argument("--top", type=int, default=0, help="LLM 喂给模型的样本行数(默认 50)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    table = os.path.splitext(os.path.basename(args.data_csv))[0]
    nt = args.nt or os.path.join(ROOT, "output", f"{table}.nt")
    lex = args.lexicon or os.path.join(ROOT, "config", f"lexicon_{table}_v4.json")
    if not os.path.exists(lex):
        lex = args.lexicon or os.path.join(ROOT, "config", f"lexicon_{table}.json")

    if not os.path.exists(nt):
        print(f"❌ 本体不存在: {nt}\n请先运行: python run.py setup {args.data_csv} {table}")
        sys.exit(1)

    with open(args.data_csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    headers = list(rows[0].keys()) if rows else []

    dict_data = json.load(open(lex, encoding="utf-8"))
    gt = compute_ground_truth(rows, headers, dict_data.get("attr_cn2en", {}))
    questions = list(gt.keys())
    if args.top:
        random.seed(args.seed)
        questions = random.sample(questions, min(args.top, len(questions)))

    print(f"表: {table} | 样本 {len(rows)} 行 | 评测问题 {len(questions)} 个")
    print(f"{'问题':<28} {'本体':<8} {'纯LLM':<8}")
    print("-" * 50)

    o_hit = l_hit = 0
    for q in questions:
        gt_v = gt[q]
        oa = ontology_answer(q, nt, lex)
        o_ok = fuzzy_match(str(oa), gt_v)
        o_hit += o_ok

        la, l_ok = "", False
        if args.llm:
            la = llm_answer(q, rows, headers, top=50)
            l_ok = fuzzy_match(la, gt_v)
            l_hit += l_ok

        print(f"{q:<28} {'✅' if o_ok else '❌':<8} "
              f"{('✅' if l_ok else '❌') if args.llm else '-':<8}  (GT={gt_v})")

    print("-" * 50)
    o_acc = o_hit / len(questions)
    print(f"本体问答命中率: {o_hit}/{len(questions)} = {o_acc*100:.0f}%")
    if args.llm:
        l_acc = l_hit / len(questions)
        print(f"纯LLM命中率: {l_hit}/{len(questions)} = {l_acc*100:.0f}%")
        print(f"提升: {o_acc*100 - l_acc*100:+.0f} 个百分点")
    else:
        print("(加 --llm 可跑纯 LLM 对照)")


if __name__ == "__main__":
    main()
