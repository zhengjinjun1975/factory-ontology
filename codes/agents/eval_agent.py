#!/usr/bin/env python3
"""eval_agent.py — 原子智能体：评测隔离打分。

PenguinHarness 纪律 1：打分与优化隔离。
本智能体只负责“执行问答并记录可答性”，**绝不接收/暴露 Gold 与评分规则**，
评分(Rubric)由外部闭源持有，优化者看不到，防止评测泄漏。

task 结构:
  {"questions": [...], "nt_file": "...", "lexicon": "...", "mode": "baseline|isolate"}

两种模式:
  - baseline(默认): 对每个问题跑问答, 判 hit/miss, 汇总命中率 score。
                    用于交付报告/基线对比。
  - isolate: 只执行问答返回答案, 不打分(Gold 与 rubric 由外部闭源持有)。

返回 AgentResult:
  ok=True, data={"score"?: 0.0-1.0, "per_question": [{"q":..., "answer"?:..., "hit"?: bool}],
                 "mode": ..., "questions_n": N}
"""

import os
import sys

# 路径修正(与 lexicon_agent/query_agent 一致)
_APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APP)
sys.path.insert(0, os.path.join(_APP, ".."))
sys.path.insert(0, os.path.join(_APP, "..", "src"))

from core.base_agent import BaseAgent

# 规则引擎兜底哨兵(与 run.py/query_agent 一致)
_MISS_SENTINEL = "暂不支持该问题"


class EvalAgent(BaseAgent):
    """评测隔离打分原子智能体：只问答不评分，打分规则由外部闭源持有。"""

    name = "eval"

    def run(self, task: dict):
        return self._timed(self._run, task)

    def _run(self, task):
        questions = task.get("questions") or []
        nt = task.get("nt_file")
        lex = task.get("lexicon")
        mode = task.get("mode", "baseline")
        if not questions:
            return self._err("缺少 questions")
        if not nt or not os.path.exists(nt):
            return self._err(f"nt_file 不存在: {nt}")
        if mode not in ("baseline", "isolate"):
            return self._err(f"未知 mode: {mode}")

        # 复用检索融合问答原子智能体(懒加载, 保持本模块 import 轻量)
        from agents.query_agent import QueryAgent
        qa = QueryAgent()

        per, hit = [], 0
        for q in questions:
            q = str(q).strip()
            if not q:
                continue
            r = qa.run({"question": q, "nt_file": nt,
                        "lexicon": lex, "use_llm": False})
            if not r.ok:
                per.append({"q": q, "answer": None, "hit": False})
                continue
            ans = (r.data or {}).get("answer", "")
            is_hit = bool(ans) and ans != _MISS_SENTINEL
            if is_hit:
                hit += 1
            per.append({"q": q, "answer": ans, "hit": is_hit})

        data = {"per_question": per, "mode": mode, "questions_n": len(per)}
        # 仅 baseline 模式打分；isolate 不产出 score(不接收 Gold, 无法/不应打分)
        if mode == "baseline":
            data["score"] = (hit / len(per)) if per else 0.0
        return self._ok(data, "eval")


def main():
    import json
    codes = os.path.join(_APP, "..")
    task = {
        "questions": json.loads(sys.argv[1]) if len(sys.argv) > 1
                     else ["有多少台运行中的设备", "哪台泵压力最高"],
        "nt_file": os.path.join(codes, "data", "equipment.nt"),
        "lexicon": os.path.join(codes, "config", "lexicon.json"),
        "mode": "baseline",
    }
    r = EvalAgent().run(task)
    print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
