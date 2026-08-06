#!/usr/bin/env python3
"""query_agent.py — 原子智能体：本体问答。

复用本套件 ontology_qa_v3（规则优先）+ v2（LLM 兜底）。单一职责：回答问题。
task 结构:
  {"question": "...", "nt_file": "...", "lexicon": "...", "use_llm": true}

v2 修复：
- 移除硬编码 本仓库 绝对路径（违反"零依赖可迁移"宣称）
- 改用套件自己的 ontology_qa_v3.parse_nt/build_data/answer（原先引用的外部 ontology_qa.py 在本套件中不存在）
"""

import os
import sys
import importlib.util

_APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP)
sys.path.insert(0, os.path.join(_APP, ".."))

from core.base_agent import BaseAgent, AgentResult


def _load_v3():
    v3_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ontology_qa_v3.py")
    spec = importlib.util.spec_from_file_location("ontology_qa_v3", v3_path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class QueryAgent(BaseAgent):
    name = "query"

    def run(self, task: dict) -> AgentResult:
        return self._timed(self._run, task)

    def _run(self, task):
        q = task.get("question", "")
        codes_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        nt = task.get("nt_file") or os.path.join(codes_root, "output", "equipment.nt")
        if not os.path.isabs(nt):
            nt = os.path.join(codes_root, nt)
        if not q:
            return self._err("query 需要 question")
        v3 = _load_v3()
        D = {}
        lex = task.get("lexicon")
        if lex:
            cand = lex if os.path.isabs(lex) else os.path.join(codes_root, lex)
            if os.path.exists(cand):
                lex = cand
            else:
                return self._err(f"词典不存在: {lex}")
        else:
            # 默认：尝试按 nt 文件名匹配套件 config 下的 lexicon_{name}.json
            base = os.path.splitext(os.path.basename(nt))[0]
            auto_lex = os.path.join(codes_root, "config", f"lexicon_{base}.json")
            if os.path.exists(auto_lex):
                lex = auto_lex
        if lex:
            try:
                D = v3.load_dict(lex)
            except Exception as e:
                return self._err(f"词典加载失败: {e}")
        triples = v3.parse_nt(nt)
        data = v3.build_data(triples, D)
        if not data:
            return self._err(f"本体解析失败或空: {nt}")
        rule_ans = v3.answer(q, data, D)
        if rule_ans != "暂不支持该问题":
            return self._ok({"answer": rule_ans, "mode": "rule"}, "query")
        if task.get("use_llm", True):
            try:
                from graph_rag import answer_graph
                gans, _ = answer_graph(q, nt)
                return self._ok({"answer": gans, "mode": "graphrag"}, "query")
            except Exception as e:
                return self._err(f"图检索兜底失败: {e}")
        return self._ok({"answer": "暂不支持该问题", "mode": "unsupported"}, "query")


def main():
    import json, sys
    _codes = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    task = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {
        "question": "有多少台运行中的设备",
        "nt_file": os.path.join(_codes, "output", "equipment.nt"),
        "use_llm": False,
    }
    r = QueryAgent().run(task)
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
