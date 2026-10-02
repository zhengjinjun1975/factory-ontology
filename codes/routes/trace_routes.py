# -*- coding: utf-8 -*-
"""trace 路由族 — 从 api_server.py 抽出的 APIRouter（代码审查 P1-1）。

函数体逐字搬运；对 api_server 模块级名字改写成 ``core.X``（5 个），
fastapi/pydantic 导入名在本模块显式重新导入；路由路径/鉴权依赖/返回结构不变。
回归门：codes/tests 单测 + codes/e2e_test.py + 路由清单逐条比对。
"""

from fastapi import Depends, Query

from fastapi import APIRouter

import api_server as core

router = APIRouter()


@router.get("/api/trace/forward", dependencies=[Depends(core.require_key)])
def trace_forward(batch: str = Query(..., description="生产批次号，如 B001")):
    res = core._forward_trace(batch)
    core._audit_trace("forward", batch, res)
    return {"ok": True, "direction": "forward", **res}


@router.get("/api/trace/reverse", dependencies=[Depends(core.require_key)])
def trace_reverse(raw: str = Query(..., description="原料编号，如 RM008")):
    res = core._reverse_trace(raw)
    core._audit_trace("reverse", raw, res)
    return {"ok": True, "direction": "reverse", **res}
