#!/usr/bin/env python3
"""factory_agent.py — 工厂智能体一站式入口（人简单监督下自动建模→问答）。

给一个工厂数据源（CSV），自动完成：
  1. 转本体 (csv_to_owl)
  2. 全自动生成词典 (lexicon_agent, LLM推断字段语义)
  3. 验证 (问几个示例问题确认建模成功)
  4. 进入交互问答 (ontology_qa_v3)

人的监督点：建模完成后，展示生成的词典概要，确认后再进问答。
这是"完整工厂智能体"的第一级应用。

用法:
  python factory_agent.py setup <data.csv> [table_name]     # 自动建模, 出词典+本体
  python factory_agent.py ask "<问题>"                      # 用默认工厂问答
  python factory_agent.py ask --data <csv> "<问题>"         # 指定工厂
  python factory_agent.py ask --lexicon <lex.json> "<问题>" # 指定词典
"""

import os
import sys
import json
import importlib.util

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(APP_DIR, "..", "src")
OUT = os.path.join(APP_DIR, "..", "output")
CFG = os.path.join(APP_DIR, "..", "config")

sys.path.insert(0, APP_DIR)
sys.path.insert(0, SRC)


def _load(mod_name, path):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _name_from_csv(csv_path):
    return os.path.splitext(os.path.basename(csv_path))[0]


def setup(data_csv, table_name=None, use_llm=True):
    """自动建模: 转本体 + 全自动词典。返回 (nt_path, lex_path, 概要)。"""
    table = table_name or _name_from_csv(data_csv)
    nt_path = os.path.join(OUT, f"{table}.nt")
    lex_path = os.path.join(CFG, f"lexicon_{table}.json")

    print(f"\n=== 工厂智能体建模: {table} ===")
    print(f"[1/3] 转本体 {data_csv} -> {nt_path}")
    csv2owl = _load("csv_to_owl", os.path.join(SRC, "csv_to_owl.py"))
    sys.argv = ["csv_to_owl", data_csv, nt_path]
    try:
        csv2owl.main()
    except SystemExit:
        pass
    if not os.path.exists(nt_path):
        print("❌ 本体生成失败")
        return None, None, None

    print(f"[2/3] 全自动生成词典 (LLM推断字段语义)...")
    from agents.lexicon_agent import LexiconAgent
    r = LexiconAgent().run({
        "source_csv": data_csv, "out_lexicon": lex_path,
        "use_llm": use_llm, "table_name": table,
    })
    if not r.ok:
        print(f"⚠️ 词典生成异常: {r.error}，继续用规则回退")
    if not os.path.exists(lex_path):
        print("❌ 词典未生成")
        return None, None, None

    # 概要
    with open(lex_path, encoding="utf-8") as f:
        lex = json.load(f)
    summary = {
        "attr": list(lex.get("attr_cn2en", {}).items())[:8],
        "status": list(lex.get("status_cn2en", {}).keys()),
        "type": list(lex.get("type_cn2en", {}).keys()),
        "aliases": list(lex.get("field_aliases", {}).keys()),
    }
    print(f"[3/3] 建模完成: {nt_path} + {lex_path}")
    return nt_path, lex_path, summary


def demo_verify(nt_path, lex_path, table):
    """验证: 自动问几个示例问题。"""
    v3 = _load("ontology_qa_v3", os.path.join(SRC, "ontology_qa_v3.py"))
    D = v3.load_dict(lex_path)
    data = v3.build_data(v3.parse_nt(nt_path), D)
    if not data:
        print("⚠️ 本体无实例，无法验证")
        return
    print(f"\n=== 自动验证 ({len(data)} 条实例) ===")
    # 构造验证问题: 基于词典
    st_cn = next(iter(D.get("status_cn2en", {}).keys()), "运行中")
    ty_cn = next(iter(D.get("type_cn2en", {}).keys()), None)
    attr_cn = next(iter(D.get("attr_cn2en", {}).keys()), None)
    tests = [f"有多少台{st_cn}的", f"列出所有{st_cn}的"]
    if ty_cn:
        tests.append(f"列出所有{ty_cn}")
    if attr_cn:
        tests.append(f"{attr_cn}最大的")
    for t in tests:
        ans = v3.answer(t, data, D)
        print(f"  问: {t}\n  -> {ans[:60]}")


def ask(question, nt_path, lex_path):
    """问答。"""
    v3 = _load("ontology_qa_v3", os.path.join(SRC, "ontology_qa_v3.py"))
    D = v3.load_dict(lex_path)
    data = v3.build_data(v3.parse_nt(nt_path), D)
    if not data:
        print("本体无实例")
        return
    ans = v3.answer(question, data, D)
    if ans == "暂不支持该问题":
        # 图检索兜底(与 API 层一致): 开放式/关系问题走 GraphRAG
        from graph_rag import answer_graph
        gans, _ = answer_graph(question, nt_path)
        print(gans)
    else:
        print(ans)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    # 持久化当前建模状态
    state_path = os.path.join(APP_DIR, "current_factory.json")

    if args[0] == "setup":
        data_csv = args[1]
        table = args[2] if len(args) > 2 else None
        nt, lex, summary = setup(data_csv, table)
        if nt:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"nt": nt, "lexicon": lex, "table": table or _name_from_csv(data_csv)}, f, ensure_ascii=False)
            print(f"\n=== 词典概要(请人工确认) ===")
            print(f"  属性: {summary['attr']}")
            print(f"  状态: {summary['status']}")
            print(f"  类型: {summary['type']}")
            print(f"  识别到: {summary['aliases']}")
            demo_verify(nt, lex, table)
            print(f"\n✅ 建模完成。可开始问答: python factory_agent.py ask \"<问题>\"")
            print(f"   当前工厂: {nt} + {lex}")

    elif args[0] == "ask":
        if not os.path.exists(state_path):
            print("请先运行 setup 建模。用法见文档。")
            return
        state = json.load(open(state_path, encoding="utf-8"))
        q = args[1]
        # 支持覆盖
        nt = state["nt"]; lex = state["lexicon"]
        if "--data" in args:
            i = args.index("--data")
            nt = os.path.join(OUT, f"{_name_from_csv(args[i+1])}.nt")
            lex = os.path.join(CFG, f"lexicon_{_name_from_csv(args[i+1])}.json")
        if "--lexicon" in args:
            i = args.index("--lexicon")
            lex = args[i+1]
        ask(q, nt, lex)

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
