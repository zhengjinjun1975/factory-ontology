#!/usr/bin/env python3
"""query_agent.py — 原子智能体：检索融合问答。

把 run.py ask 的多级检索链路封装成单一原子智能体，可被编排器调度：
  规则(v3 确定性) → 图检索(graph_rag, LLM) → BM25+向量(混合检索) → LLM兜底
逐级回退，命中即返回；每级都记录实际用到的引擎与证据(可解释)。

task 结构:
  {"question": "...", "nt_file": "...", "lexicon": "...", "use_llm": true}

返回 AgentResult:
  ok=True, data={"answer": "...", "evidence": {...}, "engines": ["rule", ...]}
  evidence 尽力而为记录命中的实体/来源(至少记录 answer)。
"""

import os
import sys

# 路径修正(与 lexicon_agent 一致)
_APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP)
sys.path.insert(0, os.path.join(_APP, ".."))
sys.path.insert(0, os.path.join(_APP, "..", "src"))

from core.base_agent import BaseAgent

# 规则引擎兜底答案哨兵(与 run.py/api_server 一致)
_MISS_SENTINEL = "暂不支持该问题"
_GRAPH_MISS_PREFIX = "[图检索]"


class QueryAgent(BaseAgent):
    """检索融合问答原子智能体。"""

    name = "query"

    def run(self, task: dict):
        return self._timed(self._run, task)

    def _run(self, task):
        q = (task.get("question") or "").strip()
        nt = task.get("nt_file")
        lex = task.get("lexicon")
        if not q:
            return self._err("缺少 question")
        if not nt or not os.path.exists(nt):
            return self._err(f"nt_file 不存在: {nt}")

        # ---- 0. 加载词典 / 解析本体(供各引擎复用) ----
        from ontology_qa_v3 import load_dict, parse_nt, build_data
        D = None
        if lex and os.path.exists(lex):
            try:
                D = load_dict(lex)
            except Exception:
                D = None

        # ---- 1. 规则引擎(确定性, 结构化查询) ----
        try:
            data = build_data(parse_nt(nt), D) if D else {}
            ans = _answer_rule(q, data, D)
            if ans and ans != _MISS_SENTINEL:
                return self._ok(_pack(q, ans, engines=["rule"],
                                     evidence=self._rule_evidence(ans)), "query")
        except Exception:
            pass

        # ---- 2. 图检索(GraphRAG, LLM 兜底) ----
        from graph_rag import answer_graph, build_graph
        try:
            gans, gctx = answer_graph(q, nt, depth=2, max_nodes=40, lexicon=D)
            if gans and not gans.startswith(_GRAPH_MISS_PREFIX):
                return self._ok(_pack(q, gans, engines=["rule", "graphrag"],
                                     evidence={"context": gctx[:2000], "answer": gans}), "query")
        except Exception:
            gans = None

        # ---- 3. 混合检索(BM25 稀疏 + 向量语义) ----
        try:
            graph, _, _, _ = build_graph(nt)
            from bm25_retrieval import BM25Index
            from vector_retrieval import VectorIndex
            bm = BM25Index.from_graph(graph)
            bm_hits = bm.search(q, top_k=3, min_score=4.0)
            # 向量语义: 仅强语义信号(min_score 0.60)才触发, 避免对无关问题误召回
            vec_hits = []
            vx = VectorIndex.from_graph(graph, lexicon=D)
            vec_hits = vx.search(q, top_k=5, min_score=0.60)
            seen, fused = set(), []
            for h in (bm_hits + vec_hits):
                e = h["entity"]
                if e not in seen:
                    seen.add(e)
                    fused.append(h)
            if fused:
                ents = "、".join(h["entity"] for h in fused)
                return self._ok(_pack(q, f"（混合检索）找到相关实体: {ents}",
                                     engines=["rule", "graphrag", "hybrid"],
                                     evidence={"hits": fused[:5], "bm25_hits": bm_hits[:3],
                                               "vector_hits": vec_hits[:5], "answer": ents}), "query")
        except Exception:
            pass

        # ---- 4. LLM 兜底(开放式问题, 借鉴 ontology_qa_v2 code_answer/llm_answer) ----
        if task.get("use_llm", True):
            lans = self._llm_fallback(q, nt, D)
            if lans:
                return self._ok(_pack(q, lans,
                                     engines=["rule", "graphrag", "hybrid", "llm"],
                                     evidence={"answer": lans}), "query")

        # ---- 5. 彻底答不上 ----
        return self._ok(_pack(q, _MISS_SENTINEL,
                             engines=["rule", "graphrag", "hybrid"],
                             evidence={"answer": _MISS_SENTINEL}), "query")

    # ---------------- 各引擎 ----------------

    def _llm_fallback(self, q, nt, D):
        """纯 LLM 兜底: 注入本体 schema 上下文(尽力而为), 让模型直接作答。
        模型不可用 / 输出非法时返回 None, 绝不阻塞。"""
        try:
            from model_llm import llm_generate
        except Exception:
            return None
        schema_ctx = ""
        try:
            from graph_rag import _schema_context
            schema_ctx = _schema_context(nt, D)
        except Exception:
            schema_ctx = ""
        head = ("你是数据问答助手。\n" + schema_ctx + "\n\n") if schema_ctx else "你是数据问答助手。\n"
        prompt = (
            head +
            f"请回答以下关于工厂/本体知识库的问题: {q}\n"
            "依据已有知识如实回答; 信息不足时如实说明, 不要编造。"
        )
        try:
            ans = llm_generate(prompt, temperature=0.4, max_tokens=400)
            if not ans or ans.startswith("[模型"):
                return None
            return ans.strip()
        except Exception:
            return None

    @staticmethod
    def _rule_evidence(ans):
        """规则引擎命中的证据(尽力而为)。"""
        return {"answer": ans}


# ---------------- 模块级辅助(便于独立复用) ----------------

def _answer_rule(q, data, D):
    """调用 v3 规则问答; 词典不可用/无实例时返回哨兵。"""
    if not D or not data:
        return _MISS_SENTINEL
    from ontology_qa_v3 import answer
    return answer(q, data, D)


def _pack(question, answer, engines, evidence=None):
    """组装 AgentResult.data。evidence 至少记录 answer。"""
    ev = dict(evidence or {})
    ev.setdefault("answer", answer)
    return {
        "question": question,
        "answer": answer,
        "evidence": ev,
        "engines": engines,
    }


def main():
    import json
    task = {
        "question": sys.argv[1] if len(sys.argv) > 1 else "有多少台运行中的设备",
        "nt_file": os.path.join(_APP, "..", "data", "equipment.nt"),
        "lexicon": os.path.join(_APP, "..", "config", "lexicon.json"),
        "use_llm": False,
    }
    r = QueryAgent().run(task)
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
