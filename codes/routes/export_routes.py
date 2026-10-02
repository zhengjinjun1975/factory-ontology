# -*- coding: utf-8 -*-
"""export 路由族 — 从 api_server.py 抽出的 APIRouter（代码审查 P1-1）。

函数体逐字搬运；对 api_server 模块级名字改写成 ``core.X``（4 个），
fastapi/pydantic 导入名在本模块显式重新导入；路由路径/鉴权依赖/返回结构不变。
回归门：codes/tests 单测 + codes/e2e_test.py + 路由清单逐条比对。
"""

from fastapi import Depends, Query
from fastapi.responses import PlainTextResponse

from fastapi import APIRouter

import api_server as core

router = APIRouter()


@router.get("/api/export/reverse", dependencies=[Depends(core.require_key)])
def export_reverse(raw: str = Query(..., description="原料编号，如 RM008"), fmt: str = Query("csv", pattern="^(csv|txt)$")):
    """溯源报告导出: 原料 → 受影响批次 → 产品(食品召回/合规)。"""
    data = core._reverse_trace(raw)
    raw_name = core._resolve_readable(data["raw_material"])
    lines = [["原料", raw_name], [], ["受影响批次", "产品", "生产日期"]]
    for ab in data["affected_batches"]:
        prod = core._resolve_readable(ab["product"]) if ab.get("product") else ""
        lines.append([ab["batch"], prod, ab.get("produce_date", "")])
    if fmt == "txt":
        body = "\n".join("\t".join(map(str, r)) for r in lines)
        return PlainTextResponse(body, media_type="text/plain",
                                 headers={"Content-Disposition": f"attachment; filename=trace_{raw}.txt"})
    import io, csv as _csv
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerows(lines)
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=trace_{raw}.csv"})
