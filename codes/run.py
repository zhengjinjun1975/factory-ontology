#!/usr/bin/env python3
"""run.py — 现场一站式入口（交付包核心，路径全相对，零硬编码）。

在交付包内任意位置运行，路径基于脚本自身位置自适应。
纯标准库，无第三方依赖（LLM 兜底需 requests + 本地 Ollama，可选）。

用法:
  python run.py setup <数据文件> [表名]   # 自动建模(本体+词典+验证)，支持 csv/json/sqlite/xlsx
  python run.py ask "<问题>"              # 交互问答(规则+LLM兜底)
  python run.py test                       # 自检示例数据
"""

import os
import sys
import json

__version__ = "2.2.0"
import importlib.util

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
CFG = os.path.join(ROOT, "config")
STATE = os.path.join(ROOT, "current.json")


def _load(mod, path):
    spec = importlib.util.spec_from_file_location(mod, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _name(source_path):
    return os.path.splitext(os.path.basename(source_path))[0]


def setup(source_path, table=None, use_llm=True):
    os.makedirs(OUT, exist_ok=True)
    table = table or _name(source_path)
    nt = os.path.join(OUT, f"{table}.nt")
    lex = os.path.join(CFG, f"lexicon_{table}.json")

    print(f"\n[工厂智能体] 建模: {table}")
    print(f"[1/3] 转本体 {source_path}")
    csv2owl = _load("csv_to_owl", os.path.join(ROOT, "csv_to_owl.py"))
    old = sys.argv
    # 若有对象属性配置则传入 --relations
    rel_path = os.path.join(CFG, "relations.json")
    rel_args = ["--relations", rel_path] if os.path.exists(rel_path) else []
    sys.argv = ["csv_to_owl", source_path, nt] + rel_args
    try:
        csv2owl.main()
    except SystemExit:
        pass
    sys.argv = old
    if not os.path.exists(nt):
        print("❌ 本体生成失败"); return None, None

    print("[2/3] 自动词典 (LLM推断字段语义)...")
    from agents.lexicon_agent import LexiconAgent
    r = LexiconAgent().run({"source_csv": source_path, "out_lexicon": lex,
                            "use_llm": use_llm, "table_name": table})
    if not r.ok:
        print(f"⚠️ 词典: {r.error}")
    if not os.path.exists(lex):
        print("❌ 词典未生成"); return None, None

    # 存状态 + 概要（存相对路径，包可整体迁移，不绑定本机盘符）
    json.dump({"nt": os.path.relpath(nt, ROOT), "lexicon": os.path.relpath(lex, ROOT),
               "table": table}, open(STATE, "w", encoding="utf-8"))
    D = json.load(open(lex, encoding="utf-8"))
    print("[3/3] 建模完成 ✅")
    print("\n=== 词典概要(请人工确认关键字段中文名是否合理) ===")
    for k, v in list(D.get("attr_cn2en", {}).items())[:10]:
        print(f"  {v} = {k}")

    # 本体深度增强：区域层级 + 设备类型父子类（若存在 line.csv）
    try:
        line_csv = os.path.join(ROOT, "data", "line.csv")
        if os.path.exists(line_csv):
            import subprocess
            deep_nt = os.path.join(OUT, f"{table}_deep.nt")
            r = subprocess.run([sys.executable, os.path.join(ROOT, "ontology_depth.py"),
                                source_path, line_csv, nt, deep_nt],
                               capture_output=True, text=True, timeout=120)
            if os.path.exists(deep_nt):
                nt = deep_nt
                json.dump({"nt": os.path.relpath(deep_nt, ROOT), "lexicon": os.path.relpath(lex, ROOT),
                           "table": table}, open(STATE, "w", encoding="utf-8"))
                print(f"🧬 深度增强已应用: {table}_deep.nt")
            else:
                print(f"⚠️ 深度增强失败: {r.stderr[:200]}")
    except Exception as e:
        print(f"⚠️ 深度增强跳过: {e}")
    return nt, lex


def ask(question, nt=None, lex=None):
    if not os.path.exists(STATE):
        print("请先运行: python run.py setup <数据文件>")
        return
    st = json.load(open(STATE, encoding="utf-8"))
    nt = nt or st["nt"]
    lex = lex or st["lexicon"]
    # 兼容相对/绝对路径：相对则基于包根 ROOT 解析（迁移后路径自动重定位）
    nt = nt if os.path.isabs(nt) else os.path.join(ROOT, nt)
    lex = lex if os.path.isabs(lex) else os.path.join(ROOT, lex)
    v3 = _load("ontology_qa_v3", os.path.join(ROOT, "ontology_qa_v3.py"))
    D = v3.load_dict(lex)
    data = v3.build_data(v3.parse_nt(nt), D)
    if not data:
        print("本体无实例"); return
    ans = v3.answer(question, data, D)
    if ans == "暂不支持该问题":
        # 关系问答（对象属性反查：车间A的设备/L1产线的设备）
        try:
            rel = _load("ontology_relation_qa", os.path.join(ROOT, "ontology_relation_qa.py"))
            rel_ans = rel.relation_answer(question, data, D)
            if rel_ans:
                print(rel_ans)
                return
        except Exception:
            pass  # 关系问答不可用则跳过，走 LLM 兜底
        try:
            v2 = _load("ontology_qa_v2", os.path.join(ROOT, "ontology_qa_v2.py"))
            # Text-to-Query 优先（生成可执行查询，确定性结果），纯LLM问答兜底
            code_ans, _mode = v2.code_answer(question, data)
            if not code_ans.startswith("[LLM") and not code_ans.startswith("[查询"):
                print(code_ans)
            else:
                print(v2.llm_answer(question, data))
        except Exception as e:
            print(f"暂不支持(LLM兜底失败: {e})")
    else:
        print(ans)


def self_test():
    """自检: 用示例数据跑通全流程。"""
    data_csv = os.path.join(ROOT, "data", "ai4i.csv")
    if not os.path.exists(data_csv):
        print("无示例数据, 跳过自检")
        return
    print("=== 自检: 示例设备数据 (UCI AI4I 预测性维护) ===")
    nt, lex = setup(data_csv, "ai4i", use_llm=False)
    if nt:
        for q in ["有多少台运行中的设备", "列出所有空压机", "功率最大的设备"]:
            print(f"\n问: {q}")
            ask(q, nt, lex)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__); return
    if args[0] == "setup":
        if len(args) < 2:
            print("用法: python run.py setup <数据文件> [表名]"); return
        setup(args[1], args[2] if len(args) > 2 else None)
    elif args[0] == "ask":
        if len(args) < 2:
            print("用法: python run.py ask '<问题>'"); return
        ask(args[1])
    elif args[0] == "test":
        self_test()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
