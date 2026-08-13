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
import re

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

        # ---- 3. 混合检索(BM25 稀疏 + 向量语义, RRF 融合; 权重见 bm25_retrieval.HYBRID_CFG) ----
        try:
            graph, _, _, _ = build_graph(nt)
            from bm25_retrieval import BM25Index, HYBRID_CFG, rrf_fuse
            from vector_retrieval import VectorIndex
            bm = BM25Index.from_graph(graph)
            _b = HYBRID_CFG["bm25"]
            bm_hits = bm.search(q, top_k=_b["top_k"], min_score=_b["min_score"])
            # 向量语义: 阈值见 HYBRID_CFG(已放宽至 0.55, 提升复杂/口语问题语义召回)
            _v = HYBRID_CFG["vector"]
            vec_hits = []
            vx = VectorIndex.from_graph(graph, lexicon=D)
            vec_hits = vx.search(q, top_k=_v["top_k"], min_score=_v["min_score"])
            fused = rrf_fuse(bm_hits, vec_hits, HYBRID_CFG)
            if fused:
                ents = "、".join(f["entity"] for f in fused)
                # 评测路径(use_llm=False): 不输出"找到相关实体"占位(用户不可读)。
                # 咨询/建议型开放问题(有什么安全问题/风险/建议等)即使评测也走 LLM 兜底生成可读建议,
                # 而非僵硬返回"未找到确切答案"——这类问题本来就不该罗列实体。
                # 明确数据查询(极值/计数/列出)才返回友好提示(诚实告知未找到确切答案)。
                if not task.get("use_llm", True):
                    import re as _re
                    _ADVICE = _re.compile(
                        r"有什么需要|建议|应当注意|应该注意|需要警惕|"
                        r"如何(才能|有效|更好|避免|预防|防范|降低|减少|提高|确保)|"
                        r"风险|安全隐患|合规|规范要求|需要注意|怎么办|意义|作用|影响|"
                        r"问题|措施|方案|注意", _re.I)
                    if _ADVICE.search(q):
                        lans = self._advice_answer(q, nt, D)
                        if lans:
                            return self._ok(_pack(q, lans,
                                                 engines=["rule", "graphrag", "hybrid", "llm"],
                                                 evidence={"answer": lans}), "query")
                    return self._ok(_pack(q, "未能在当前知识库中找到确切答案（可换一种问法，或补充相关字段数据）",
                                         engines=["rule", "graphrag", "hybrid"],
                                         evidence={"hits": fused[:5], "no_basis": True}), "query")
                return self._ok(_pack(q, f"（混合检索）找到相关实体: {ents}",
                                     engines=["rule", "graphrag", "hybrid"],
                                     evidence={"hits": fused[:5], "bm25_hits": bm_hits[:6],
                                               "vector_hits": vec_hits[:8], "answer": ents}), "query")
        except Exception:
            pass

        # ---- 4. LLM 兜底(开放式问题, 借鉴 ontology_qa_v2 code_answer/llm_answer) ----
        # 通用跨域校验(P1, 取代原白名单词表): 若问题是一条明确的数据查询, 且其中引用的
        # 实体概念不在该 kb 本体任何实体类/词典(kb_vocab: entity/type/status/zone/attr/
        # numeric_fields), 且规则/图/混合全 miss, 则禁止 LLM 兜底编造, 强制返回"无相关数据"。
        # 横向覆盖所有跨域问题(书/船/测线/冲床/图纸…), 不靠具体词表。
        try:
            from ontology_qa_v3 import is_cross_domain_data_query
            # 咨询/建议型开放问题即使含"哪些/多少"也非数据查询, 跳过跨域校验, 交给 LLM 兜底生成建议。
            if is_cross_domain_data_query(q, D) and not re.search(
                    r"需要注意|注意事项|建议|注意什么|注意哪些|应当注意|应该注意|风险|隐患|"
                    r"怎么办|措施|方案|如何|怎么(才能|有效|避免|预防)|意义|作用|影响|经验", q):
                return self._ok(_pack(q, "无相关数据（该知识库不含该实体概念）", engines=["miss"],
                                     evidence={"cross_domain": True, "no_basis": True}), "query")
        except Exception:
            pass
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
            "严格规则: 仅可依据上面给出的本体 schema 中实际存在的实体/属性/概念作答。\n"
            "若问题中的实体/概念【不在】该 schema(如问书/船/测线等本库没有的概念), 必须回答\"无相关数据\"。\n"
            "严禁编造数量/记录/实体名。若本库有该数据但未给出具体值, 说明\"数据中未找到\", 不得猜测数字。\n"
        )
        try:
            ans = llm_generate(prompt, temperature=0.4, max_tokens=400)
            if not ans or ans.startswith("[模型"):
                return None
            return ans.strip()
        except Exception:
            return None

    def _advice_answer(self, q, nt, D):
        """咨询/建议型问题专用兜底: 基于行业常识生成建议, 明确声明非具体数据结论。
        区别于 _llm_fallback(数据问答导向, 实体不在schema答'无相关数据')。
        模型不可用/输出非法返回 None。"""
        try:
            from model_llm import llm_generate
        except Exception:
            return None
        prompt = (
            f"用户问题: {q}\n"
            "这是关于化工/制造企业的咨询型问题, 用户在寻求通用建议/知识, 不是查询具体数据。\n"
            "请基于行业通用常识给出简洁、务实的建议。\n"
            "注意: 若涉及本知识库可能有的具体数据(设备/产品/批次数量等), 可提及但不编造具体数字; "
            "纯咨询部分按通用知识作答。回答控制在150字内。"
        )
        try:
            ans = llm_generate(prompt, temperature=0.5, max_tokens=300)
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
