# -*- coding: utf-8 -*-
"""eval 路由族 — 从 api_server.py 抽出的 APIRouter（代码审查 P1-1）。

函数体逐字搬运；对 api_server 模块级名字改写成 ``core.X``（9 个），
fastapi/pydantic 导入名在本模块显式重新导入；路由路径/鉴权依赖/返回结构不变。
回归门：codes/tests 单测 + codes/e2e_test.py + 路由清单逐条比对。
"""

from fastapi import Depends, Query, Request

from fastapi import APIRouter

import api_server as core

router = APIRouter()


@router.get("/api/eval/benchmark", dependencies=[Depends(core.require_key)])
def eval_benchmark(kb: str = Query("food")):
    """评测基线: 用 kb 配置的示例题目跑 EvalAgent baseline, 返回命中率。"""
    start = core.time.time()
    kbc = core.KBS.get(kb, {})
    questions = kbc.get("examples") or core._kb.get("examples", [])
    if not questions:
        return core._err_env(4001, f"kb '{kb}' 无评测题目(未配置 examples)", start)
    ctx = core._get_kb_ctx(kb)  # 多租户: 按 kb 取本体/词典, 与 /api/ask 对齐
    if ctx is None:
        return core._err_env(4001, f"知识库 '{kb}' 无效或数据缺失", start)
    try:
        from agents.eval_agent import EvalAgent
        r = EvalAgent().run({"questions": questions, "nt_file": ctx["nt_file"],
                             "lexicon": ctx["lex_file"], "mode": "baseline"})
    except Exception as e:
        core.logger.warning(f"API内部错误[评测引擎不可用]: {e}")
        return core._err_env(5001, "评测引擎不可用(内部错误已记录)", start)
    if not r.ok:
        return core._err_env(5001, r.error, start)
    data = r.data or {}
    per = data.get("per_question", [])
    hits = sum(1 for p in per if p.get("hit"))
    return core._ok_env({"kb": kb, "questions_n": data.get("questions_n", len(per)),
                    "hits": hits, "score": data.get("score")}, start)


@router.post("/api/eval/isolate", dependencies=[Depends(core.require_key)])
async def eval_isolate(request: Request):
    """评测隔离: 只问答不打分。字段白名单 {kb, questions}; 出现 gold/rubric/score 返回 4001。"""
    start = core.time.time()
    try:
        body = await request.json()
    except Exception:
        return core._err_env(4001, "请求体不是合法 JSON", start)
    if not isinstance(body, dict):
        return core._err_env(4001, "请求体应为 JSON 对象", start)
    allowed = {"kb", "questions"}
    extra = set(body.keys()) - allowed
    if extra:
        return core._err_env(4001, f"isolate 模式禁止字段: {sorted(extra)} (白名单: {sorted(allowed)})", start)
    questions = body.get("questions")
    if not isinstance(questions, list) or not questions:
        return core._err_env(4001, "缺少非空 questions 列表", start)
    if any(not isinstance(q, str) or not q.strip() for q in questions):
        return core._err_env(4001, "questions 必须全为非空字符串", start)
    kb = body.get("kb", "food")
    ctx = core._get_kb_ctx(kb)  # 多租户: 按 kb 取本体/词典, 与 /api/ask 对齐
    if ctx is None:
        return core._err_env(4001, f"知识库 '{kb}' 无效或数据缺失", start)
    try:
        from agents.eval_agent import EvalAgent
        r = EvalAgent().run({"questions": questions, "nt_file": ctx["nt_file"],
                             "lexicon": ctx["lex_file"], "mode": "isolate"})
    except Exception as e:
        core.logger.warning(f"API内部错误[评测引擎不可用]: {e}")
        return core._err_env(5001, "评测引擎不可用(内部错误已记录)", start)
    if not r.ok:
        return core._err_env(5001, r.error, start)
    data = r.data or {}
    per = data.get("per_question", [])
    answers = [{"q": p.get("q"), "answer": p.get("answer"), "hit": p.get("hit")} for p in per]
    return core._ok_env({"kb": kb, "questions_n": len(answers), "answers": answers}, start)
