# -*- coding: utf-8 -*-
"""industry 路由族 — 从 api_server.py 抽出的 APIRouter（代码审查 P1-1）。

函数体逐字搬运；对 api_server 模块级名字改写成 ``core.X``（5 个），
fastapi/pydantic 导入名在本模块显式重新导入；路由路径/鉴权依赖/返回结构不变。
回归门：codes/tests 单测 + codes/e2e_test.py + 路由清单逐条比对。
"""

from fastapi import Depends, Query, Request
from fastapi.responses import FileResponse

from fastapi import APIRouter

import api_server as core

router = APIRouter()


@router.get("/api/industry/list", dependencies=[Depends(core.require_key)])
def industry_dict_list():
    """列出公共工业本体词典集(00基础+01泵阀+02化工+03地质)及各规模。"""
    try:
        from industrial_dict_loader import _DICT_DIR
        items = []
        for fn in sorted(core.os.listdir(_DICT_DIR)):
            if not fn.endswith(".json") or fn == "index.json":
                continue
            fp = core.os.path.join(_DICT_DIR, fn)
            d = core.json.load(open(fp, encoding="utf-8"))
            items.append({
                "file": fn,
                "description": d.get("description", ""),
                "type": len(d.get("type_cn2en", {})),
                "status": len(d.get("status_cn2en", {})),
                "synonym": len(d.get("synonym_map", {})),
                "entity": len(d.get("entity_cn2en", {})),
            })
        return {"ok": True, "items": items}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/api/industry/absorb", dependencies=[Depends(core.require_key)])
async def industry_dict_absorb(req: Request):
    """吸收企业词典 → 候选池 → 行业层（按「独立来源数」判定，默认阈值 3）。

    body: {lexicon: 企业词典路径, industry: 行业名, threshold?: int}
    行业名: 泵阀/精细化工/地球物理/基础。
    单一企业来源通常不足以升级进行业层，词会先进候选池等待后续企业确认。
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    lexicon = body.get("lexicon", "")
    industry = body.get("industry", "基础")
    if not lexicon or not core.os.path.exists(lexicon):
        return {"ok": False, "error": f"企业词典不存在: {lexicon}"}
    try:
        from absorb_public_dict import learn_from_kb, load_candidates, CROSS_KB_THRESHOLD
        res = learn_from_kb(lexicon, industry=industry,
                            threshold=int(body.get("threshold", CROSS_KB_THRESHOLD)), verbose=False)
        if res.get("ok"):
            res["candidates"] = len(load_candidates())
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/industry/export", dependencies=[Depends(core.require_key)])
def industry_dict_export(industry: str = Query("泵阀"), download: bool = Query(False)):
    """导出行业词典。industry: 泵阀/精细化工/地球物理/基础。
    download=true 返回文件下载, 否则返回 JSON。"""
    try:
        from absorb_public_dict import load_public, INDUSTRY_FILES
        fn = INDUSTRY_FILES.get(industry, "00_basis.json")
        pub = load_public(industry)
        export_dir = core.os.path.join(core.ROOT, "..", "dict_export")
        core.os.makedirs(export_dir, exist_ok=True)
        out = core.os.path.join(export_dir, fn)
        with open(out, "w", encoding="utf-8") as f:
            core.json.dump(pub, f, ensure_ascii=False, indent=2)
        if download:
            return FileResponse(out, filename=fn, media_type="application/core.json")
        return {"ok": True, "file": out, "industry": industry, "type": len(pub.get("type_cn2en", {}))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/industry/candidates", dependencies=[Depends(core.require_key)])
def industry_dict_candidates(limit: int = Query(50)):
    """候选池：服务过的企业里出现、但独立来源数尚未达阈值的概念。

    返回 {threshold, similarity, file, total, items:[{word, sources, n, key, first_seen, last_seen}]}
    """
    try:
        from absorb_public_dict import load_candidates, CAND_PATH, CROSS_KB_THRESHOLD, SAME_SOURCE_JACCARD
        cand = load_candidates()
        items = sorted(cand.items(), key=lambda x: -len(x[1].get("sources", [])))
        return {
            "ok": True,
            "threshold": CROSS_KB_THRESHOLD,
            "similarity": SAME_SOURCE_JACCARD,
            "file": CAND_PATH,
            "total": len(cand),
            "items": [{
                "word": w,
                "sources": e.get("sources", []),
                "n": len(e.get("sources", [])),
                "key": e.get("key", ""),
                "first_seen": e.get("first_seen", ""),
                "last_seen": e.get("last_seen", ""),
            } for w, e in items[:limit]],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
