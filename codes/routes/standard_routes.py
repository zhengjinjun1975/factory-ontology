# -*- coding: utf-8 -*-
"""标准/外部本体 路由族 — 从 api_server.py 抽出的 APIRouter 试点（代码审查 P1-1）。

拆分纪律：函数体逐字搬运；对 api_server 模块级名字改写成 ``core.X``（9 个），
fastapi/pydantic 的导入名在新模块显式重新导入，路由路径/鉴权依赖/返回结构一律不变。
回归门：codes/tests 单测 + codes/e2e_test.py + 路由清单逐条比对（改前 67 条）。
"""

from fastapi import Depends, Query
from fastapi.responses import FileResponse

from fastapi import APIRouter

import api_server as core

router = APIRouter()


@router.get("/api/standard/compliance", dependencies=[Depends(core.require_key)])
def standard_compliance():
    """本体标准合规度（GB/T 48000.3 描述项齐备率 + 命名空间 + SHACL + 类层次 + 导出物）。

    A2(2026-09-25): 按**当前激活 KB 的本体**计算；找不到该库本体时明确报错，
    绝不静默回落到全局 config/ontology_schema.json。
    """
    kb = core._active_kb()
    schema_path, err = core._kb_ontology_schema(kb)
    if err:
        return {"ok": False, "kb": kb, "error": f"按当前激活库计算合规度失败: {err}"}
    try:
        import ontology_check as oc
        root = core.os.path.dirname(core.os.path.abspath(__file__))
        r = oc._check_standard(root, schema_path=schema_path)
        st = r.get("state") or {}
        return {
            "ok": True,
            "kb": kb,
            "ontology": core.os.path.relpath(schema_path, root).replace("\\", "/"),
            "standard_rate": st.get("standard_rate"),
            "ent_core_rate": st.get("ent_core_rate"),
            "ent_rate": st.get("ent_rate"),
            "attr_rate": st.get("attr_rate"),
            "ns_ok": st.get("ns_ok"),
            "subclass_count": st.get("subclass_count"),
            "has_export": st.get("has_export"),
            "issues": [{"severity": s, "message": m} for s, m in r.get("issues", [])],
            "standards": ["GB/T 48000.3-2026", "GB/T 42131-2022", "GB/T 41472.2-2022",
                          "ISO/IEC 21838", "IEEE 知识图谱评估标准"],
        }
    except Exception as e:
        return {"ok": False, "kb": kb, "error": f"合规度计算失败: {e}"}


@router.post("/api/standard/export", dependencies=[Depends(core.require_key)])
def standard_export():
    """生成标准导出物（ontology.ttl / shapes.ttl / ontology.jsonld），返回文件名与大小。

    A3(2026-09-25): 按**当前激活 KB 的本体**导出；找不到该库本体时明确报错，
    绝不静默回落到全局 config/ontology_schema.json。
    """
    kb = core._active_kb()
    schema_path, err = core._kb_ontology_schema(kb)
    if err:
        return {"ok": False, "kb": kb, "error": f"按当前激活库导出失败: {err}"}
    try:
        import ontology_export as ox
        root = core.os.path.dirname(core.os.path.abspath(__file__))
        outs = ox.export(schema_path, core.os.path.join(root, "export"))
        return {"ok": True, "kb": kb,
                "schema": core.os.path.relpath(schema_path, root).replace("\\", "/"),
                "files": [{"name": k, "size": core.os.path.getsize(v)}
                          for k, v in sorted(outs.items())]}
    except Exception as e:
        return {"ok": False, "kb": kb, "error": f"导出失败: {e}"}


@router.get("/api/standard/export/{fname}", dependencies=[Depends(core.require_key)])
def standard_export_download(fname: str):
    """下载标准导出物（白名单文件名，防路径穿越）。"""
    from fastapi.responses import FileResponse
    if fname not in core._EXPORT_ALLOW:
        return {"ok": False, "error": "不允许的文件名"}
    p = core.os.path.join(core.os.path.dirname(core.os.path.abspath(__file__)), "export", fname)
    if not core.os.path.exists(p):
        return {"ok": False, "error": "导出物不存在，请先生成"}
    return FileResponse(p, filename=fname,
                        media_type="text/turtle" if fname.endswith(".ttl") else "application/ld+json")


@router.get("/api/standard/quality", dependencies=[Depends(core.require_key)])
def standard_quality(kb: str = Query("")):
    """本体建模质量门：标签/定义/外键关系/结构 体检 + 阈值判定。

    kb 配了 schema 就用配置的；没配则从该 kb 数据自动推断(FDE 现场主场景：
    CSV 丢进来就能体检, 不用先手写 schema)。
    """
    kb = (kb or "").strip() or core._active_kb()   # A2 同口径: 缺省跟随当前激活 kb
    core._kb_guard(kb)  # 多租户: 越权 KB → 403(拒绝)
    try:
        import ontology_quality as oq
        kbc = core.KBS.get(kb) or {}
        root = core.os.path.dirname(core.os.path.abspath(__file__))
        data_dir = kbc.get("data_dir", "data")
        data_dir = data_dir if core.os.path.isabs(data_dir) else core.os.path.join(root, data_dir)
        data = core.so.load_all(data_dir) if core.os.path.isdir(data_dir) else {}
        schema_rel = kbc.get("schema")
        if schema_rel and core.os.path.exists(core.os.path.join(root, schema_rel)):
            schema, source = core.so.load_schema(core.os.path.join(root, schema_rel)), "configured"
        else:
            # 快速模式: 质量门只需结构体检, 不调 LLM(否则每次 19s)
            schema, source = core.so.suggest_schema(data, use_llm=False), "auto-inferred"
        rep = oq.inspect(schema, data)
        return {"ok": True, "kb": kb, "source": source, **rep, "verdict": oq.judge(rep)}
    except Exception as e:
        return {"ok": False, "error": f"质量门执行失败: {e}"}


@router.get("/api/standard/roundtrip", dependencies=[Depends(core.require_key)])
def standard_roundtrip():
    """导入层自检：把导出的 ontology.ttl 读回来，与 schema 核对是否无损往返。

    这是导出物质量的可重跑门 —— 导出/导入任一侧退化都会立刻暴露。
    外部本体对齐（--align）走 CLI：python ontology_import.py --in <外部文件> --schema ... --align
    """
    try:
        import ontology_import as oim
        root = core.os.path.dirname(core.os.path.abspath(__file__))
        # A3 同口径: 往返自检对的是**当前激活 kb** 导出的 ttl ↔ 该 kb 的 schema
        kb = core._active_kb()
        schema_path, _err = core._kb_ontology_schema(kb)
        if _err:
            return {"ok": False, "kb": kb, "error": "按当前激活库往返自检失败: " + _err}
        ttl = core.os.path.join(root, "export", "ontology.ttl")
        if not core.os.path.exists(ttl):
            return {"ok": False, "error": "导出物不存在，请先生成标准导出物"}
        fmt, data = oim.parse_input(ttl)
        model = oim.graph_to_model(data[1])
        rt = oim.roundtrip(model, schema_path)
        return {"ok": bool(rt.get("ok")), "kb": kb, **rt,
                "classes_note": f"{rt.get('classes_imported')}/{rt.get('classes_expected')}",
                "props_note": f"{rt.get('dataprops_imported')}/{rt.get('props_expected')}"}
    except Exception as e:
        return {"ok": False, "error": f"往返自检失败: {e}"}


@router.post("/api/standard/import-align", dependencies=[Depends(core.require_key)])
def standard_import_align(payload: dict | None = None):
    """外部本体对齐：入参 {"path": "外部 .ttl"} 或 {"content": "Turtle 文本"}，
    与 schema 做对齐报告（同名类匹配、外部独有类），供"对标国标"落到可核对清单。"""
    try:
        import ontology_import as oim
        import tempfile
        root = core.os.path.dirname(core.os.path.abspath(__file__))
        payload = payload or {}
        path = payload.get("path")
        tmp = None
        if not path and payload.get("content"):
            # 按内容探测后缀：JSON-LD 原文是 JSON，若写成 .ttl 会被按 Turtle 解析(曾静默出 0 类)
            content = payload["content"]
            suffix = ".jsonld" if content.lstrip().startswith(("{", "[")) else ".ttl"
            fd, tmp = tempfile.mkstemp(suffix=suffix, text=True)
            with core.os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            path = tmp
        if not path or not core.os.path.exists(path):
            return {"ok": False, "error": "需提供存在的 path 或 content"}
        try:
            fmt, data = oim.parse_input(path)
            triples = oim.graph_from_jsonld(data)[0] if fmt == "jsonld" else data[1]
            model = oim.graph_to_model(triples)
            al = oim.align_report(model, core.os.path.join(root, "config", "ontology_schema.json"))
            out = {"ok": True, "format": fmt, "external_classes": len(model["classes"]), **al}
            # 无类可对齐时给出可操作提示（常见误操作：喂了 SHACL 约束文件而非本体文件）
            if not model["classes"]:
                n_shapes = sum(1 for t in triples if "shacl#" in str(t[1]))
                out["hint"] = ("该文件未含 owl:Class，无法对齐。"
                               + ("看起来是 SHACL 约束文件（shapes.ttl），请改喂本体文件（ontology.ttl / .jsonld）。"
                                  if n_shapes else "请确认是本体文件（含 owl:Class 的 .ttl / .jsonld）。"))
                out["ok"] = False
            return out
        finally:
            if tmp and core.os.path.exists(tmp):
                core.os.remove(tmp)
    except Exception as e:
        return {"ok": False, "error": f"对齐失败: {e}"}
