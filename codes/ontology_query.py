#!/usr/bin/env python3
"""ontology_query.py — 团队 Agent 可调用的本体查询入口。

给 团队 Agent 用的极简 CLI：输入自然语言问题，返回本体问答结果。
规则引擎 + LLM 兜底，封装成单命令。

用法:
  python ontology_query.py "<问题>"                    # 用默认本体
  python ontology_query.py --file <nt文件> "<问题>"     # 指定本体
  python ontology_query.py --lex <词典.json> "<问题>"   # 指定词典

团队 Agent 集成: 可把它注册为工具, 工人/管理层直接问设备问题。

v2 修复：
- 移除硬编码 本仓库 绝对路径（违反"零依赖可迁移"宣称）
- 改用套件自己的 ontology_qa_v3.parse_nt/build_data/answer（原先引用的外部 ontology_qa.py 在本套件中不存在）
"""

import sys
import os
import importlib.util

_CODES = os.path.dirname(os.path.abspath(__file__))
# 默认本体：基于套件根自适应（可迁移）
DEFAULT_NT = os.path.join(_CODES, "output", "equipment.nt")
DEFAULT_LEX = os.path.join(_CODES, "config", "lexicon.json")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_CODES, filename))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    args = sys.argv[1:]
    nt_file = DEFAULT_NT
    lex_file = None
    question = None

    if "--file" in args:
        i = args.index("--file")
        nt_file = args[i + 1]
        args = args[:i] + args[i + 2:]
    if "--lex" in args:
        i = args.index("--lex")
        lex_file = args[i + 1]
        args = args[:i] + args[i + 2:]

    if args:
        question = args[0]

    if not question:
        print("用法: python ontology_query.py [--file <nt>] [--lex <词典>] '<问题>'")
        sys.exit(1)

    v3 = _load("ontology_qa_v3", "ontology_qa_v3.py")
    v2 = _load("ontology_qa_v2", "ontology_qa_v2.py")

    D = {}
    lex = lex_file or DEFAULT_LEX
    if lex and os.path.exists(lex):
        D = v3.load_dict(lex)

    triples = v3.parse_nt(nt_file)
    data = v3.build_data(triples, D)
    if not data:
        print("本体解析失败或无实例")
        sys.exit(1)

    # 规则优先
    rule_ans = v3.answer(question, data, D)
    if rule_ans != "暂不支持该问题":
        print(rule_ans)
        return

    # LLM 兜底（Text-to-Query 优先）
    try:
        code_ans, _mode = v2.code_answer(question, data)
        if not code_ans.startswith("[LLM") and not code_ans.startswith("[查询"):
            print(code_ans)
        else:
            print(v2.llm_answer(question, data))
    except Exception as e:
        print(f"暂不支持(LLM兜底失败: {e})")


if __name__ == "__main__":
    main()
