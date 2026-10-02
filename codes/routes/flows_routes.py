# -*- coding: utf-8 -*-
"""flows 路由族 — 从 api_server.py 抽出的 APIRouter（代码审查 P1-1）。

函数体逐字搬运；对 api_server 模块级名字改写成 ``core.X``（6 个），
fastapi/pydantic 导入名在本模块显式重新导入；路由路径/鉴权依赖/返回结构不变。
回归门：codes/tests 单测 + codes/e2e_test.py + 路由清单逐条比对。
"""

from fastapi import Depends

from fastapi import APIRouter

import api_server as core

router = APIRouter()


@router.get("/api/flows")
def flows_list():
    """只读：列出可用流程、预设卡与**流程加载失败的真实原因**。"""
    try:
        fr = core._flow_registry()
        return {"ok": True, **fr.status()}
    except Exception as e:
        return {"ok": False, "error": f"流程注册表不可用: {type(e).__name__}: {e}"}


@router.get("/api/flows/presets")
def flows_presets():
    """只读：预设卡列表（放配置不放代码，与流程定义一一对应）。"""
    try:
        fr = core._flow_registry()
        return {"ok": True, "presets": fr.presets(),
                "preset_config": fr.presets_file, "preset_error": fr._preset_error}
    except Exception as e:
        return {"ok": False, "error": f"读取预设失败: {type(e).__name__}: {e}"}


@router.post("/api/flows/{flow_id}/run", dependencies=[Depends(core.require_key)])
def flows_run(flow_id: str, req: core.FlowRunReq = None):
    """触发：按 flow_id 一键运行某流程，返回每步状态与事件/审计结果。"""
    import flow_engine as fe
    try:
        eng = core._flow_engine()
        fr = core._flow_registry()
        params = dict(req.params) if (req and req.params) else {}
    except Exception as e:
        return {"ok": False, "error": f"流程引擎不可用: {type(e).__name__}: {e}"}
    try:
        flow = fr.get(flow_id)
    except fe.FlowError as e:
        # 流程不存在/加载失败 → 如实输出真实原因
        return {"ok": False, "flow_id": flow_id, "error": str(e),
                "load_errors": list(fr.errors)}
    try:
        return {"ok": True, **eng.run(flow, params)}
    except fe.FlowError as e:
        return {"ok": False, "flow_id": flow_id, "error": f"流程执行失败: {e}"}
    except Exception as e:
        core.logger.exception("流程执行异常: %s", flow_id)
        return {"ok": False, "flow_id": flow_id,
                "error": f"流程执行异常: {type(e).__name__}: {e}"}
