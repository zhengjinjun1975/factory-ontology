# -*- coding: utf-8 -*-
"""audit 路由族 — 从 api_server.py 抽出的 APIRouter（代码审查 P1-1）。

函数体逐字搬运；对 api_server 模块级名字改写成 ``core.X``（4 个），
fastapi/pydantic 导入名在本模块显式重新导入；路由路径/鉴权依赖/返回结构不变。
回归门：codes/tests 单测 + codes/e2e_test.py + 路由清单逐条比对。
"""

from fastapi import Depends, Query

from fastapi import APIRouter

import api_server as core

router = APIRouter()


@router.get("/api/audit/chain", dependencies=[Depends(core.require_key)])
def audit_chain_status():
    """校验溯源审计链完整性(防篡改/防删行)。"""
    ac = core._audit_chain()
    if not ac:
        return {"ok": False, "error": "审计链未启用(需 audit_chain.py 可用)"}
    try:
        ok, issues = ac.verify_chain()
        return {"ok": True, "chain_integrity": "PASS" if ok else "FAIL",
                "integrity_issues": issues, **ac.audit_report()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/audit/decisions", dependencies=[Depends(core.require_key)])
def audit_decisions(category: str = Query("", description="决策类别过滤")):
    """列出审计链中的决策记录(可选 category 过滤)。"""
    ac = core._audit_chain()
    if not ac:
        return {"ok": False, "error": "审计链未启用"}
    try:
        decs = ac.decisions(category=category or None, limit=200)
        return {"ok": True, "count": len(decs), "decisions": decs}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/audit/export", dependencies=[Depends(core.require_key)])
def audit_export(fmt: str = Query("json", description="json/csv/prov-o")):
    """导出审计报告到 temp, 返回文件路径与校验状态。"""
    ac = core._audit_chain()
    if not ac:
        return {"ok": False, "error": "审计链未启用"}
    try:
        import tempfile
        out = core.os.path.join(tempfile.gettempdir(), f"factory_audit.{fmt}"
                           if fmt != "prov-o" else "factory_audit.prov-o.json")
        r = ac.export_audit(out, fmt=fmt)
        return {"ok": True, **r}
    except Exception as e:
        return {"ok": False, "error": str(e)}
