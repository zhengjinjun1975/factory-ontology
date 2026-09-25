#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_registry.py — 多知识库/多行业统一注册表入口（纯标准库，不改问答引擎）。

把「多知识库/多行业」从『一库一改代码』升级为『KB 注册表 + 词典分层』：

  ① KR 注册表 = codes/config/kbs.json（单一真相源）。
     新增/删除一个 KB 只动这份 JSON —— 问答引擎（api_server._load_kbs / _nt_path_for /
     _ensure_ontology）读同一份文件即可感知，**引擎代码零改动、零重启**。

  ② 词典分层（dict layers）：
       · 公共工业本体层（PUBLIC_LAYER，只读）: codes/industrial_dict/*.json
         跨行业稳定骨架（设备/状态/材质/实体/故障/工艺），开源算法资产，所有 KB 共享。
       · 工厂专属层（FACTORY_LAYER，可写）: codes/config/lexicon_<kb>.json
         企业私有词（列名/批次/型号特有字段），各 KB 隔离，互不污染。
       · 两层对同一中文词给出**不同规范名** → 抛 DictLayerConflictError，绝不静默覆盖。

  ③ 数据就绪 = **真实探测**（os.stat 文件系统），数据缺失即 not_ready，不假设、不编造。

每 KB 详情字段（get_kb 返回）：
    kb_id          知识库标识（kbs.json 的 key）
    name           展示名（缺失回退 kb_id）
    data_dir       数据目录（绝对路径）
    nt_file        本体 .nt 绝对路径
    lexicon_file   工厂专属词典绝对路径
    industry       行业（泵阀/精细化工/地球物理/基础）
    schema_layer   schema 层标记（shared）
    dict_layer     词典层描述 {public:{只读}, factory:{可写}}   ← 词典层
    data_ready     数据是否就绪（bool，真实探测）              ← 数据是否就绪
    status         ready / not_ready
    probe          探测细节（data_dir_exists/data_file_count/nt_exists/lexicon_exists/missing）

用法：
    from kb_registry import list_kbs, get_kb, probe_kb, dict_layers, merge_dict_layers
    for k in list_kbs():            # 全部 KB 摘要
        print(k["kb_id"], k["status"], k["data_dir"])
    get_kb("valve")                 # 单 KB 详情
    probe_kb("valve")               # 真实探测
    merge_dict_layers(kb_id="valve")  # 公共层 ∪ 工厂层（冲突报错，on_conflict="error" 默认）

自检脚本见 scripts/verify_kb_registry.py（用 %TEMP% 临时副本，不动真实数据）。
"""
import os
import sys
import json
import glob

# 本模块目录 = codes/（与 api_server.py 同根，路径解析口径一致）
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(ROOT, "config")
PUBLIC_DICT_DIR = os.path.join(ROOT, "industrial_dict")
DEFAULT_REGISTRY = os.path.join(CONFIG_DIR, "kbs.json")

# 词典分层标记（与 industrial_dict_loader / dict_asset / absorb_public_dict 保持一致）
PUBLIC_LAYER = "public"
FACTORY_LAYER = "factory"

# 清理项备案（第③项要求，本次不删数据，只登记）—— 测试 / onboarding / 重复项
CLEANUP_PATTERNS = ("valve2", "valve3", "valve9", "e2ecust", "factory_multi_", "onb_")

# 把 codes/ 加入 sys.path，复用工厂层公共层既有实现（不复制逻辑）
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    import industrial_dict_loader as _idl
    _DictLayerConflictError = _idl.DictLayerConflictError
except Exception:  # pragma: no cover - 降级：分层仍可用，只是冲突类型退化为 ValueError
    _idl = None

    class _DictLayerConflictError(ValueError):
        pass


DictLayerConflictError = _DictLayerConflictError

# 多租户（2026-09-24）：KB 可见范围按租户过滤。租户注册表来自 tenant.py（配置驱动）。
try:
    import tenant as _tenant_mod
except Exception:  # pragma: no cover - 降级：无 tenant 模块时不做租户过滤（等于老行为）
    _tenant_mod = None


def _owner_visible(entry, tenant_id):
    """KB 侧声明的属主可见性：entry["tenant"] 单属主 / entry["tenants"] 多租户共享。

    未声明属主的 KB 视为公共（受租户注册表的 kbs 白名单约束）。
    """
    owner = entry.get("tenant")
    if owner and str(tenant_id) != str(owner):
        return False
    shared = entry.get("tenants")
    if shared and str(tenant_id) not in {str(x) for x in shared}:
        return False
    return True


def kb_visible_for(kb_id, tenant_id, path=None):
    """该租户是否可见某 KB：租户注册表白名单 ∩ KB 侧属主声明。未注册 KB → False。"""
    if _tenant_mod is None:
        return True
    entry = load_registry(path).get(kb_id)
    if entry is None:
        return False
    try:
        if not _tenant_mod.kb_visible(tenant_id, kb_id, path):
            return False
    except Exception:
        return False
    return _owner_visible(entry, tenant_id)


def list_kb_ids(tenant=None, path=None, include_cleanup=True):
    """按租户过滤的 KB id 列表（不触发真实探测，纯注册表 + 可见性）。"""
    out = []
    for kb_id in load_registry(path):
        if not include_cleanup and is_cleanup_candidate(kb_id):
            continue
        if tenant is not None and not kb_visible_for(kb_id, tenant, path):
            continue
        out.append(kb_id)
    return out


# ── 注册表读写 ────────────────────────────────────────────────────────────────
def registry_path(path=None):
    """解析注册表文件路径：显式入参 > 环境变量 KB_REGISTRY_FILE > 默认 config/kbs.json。

    环境变量是给自检脚本指向 %TEMP% 临时副本用的，生产路径不变。
    """
    if path:
        return os.path.abspath(path)
    return os.path.abspath(os.environ.get("KB_REGISTRY_FILE") or DEFAULT_REGISTRY)


def load_registry(path=None):
    """读取注册表 → {kb_id: entry}。文件缺失/损坏返回 {}（不抛，调用方可判空）。"""
    try:
        with open(registry_path(path), encoding="utf-8") as f:
            data = json.load(f)
        kbs = data.get("kbs")
        return kbs if isinstance(kbs, dict) else {}
    except Exception:
        return {}


def save_registry(kbs, path=None):
    """写回注册表（保留顶层其它字段）。**只对给定路径生效**，默认写真实 config/kbs.json。"""
    p = registry_path(path)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data["kbs"] = kbs
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return p


# ── 路径解析（与引擎 api_server 口径一致：data_dir/nt 相对 codes/，lexicon 相对 config/）
def _resolve_data_dir(entry):
    return os.path.join(ROOT, entry.get("data_dir") or "data")


def _resolve_nt(entry, kb_id):
    return os.path.join(ROOT, entry.get("nt") or ("output/%s.nt" % kb_id))


def _resolve_lexicon(entry, kb_id):
    rel = entry.get("lexicon") or ("lexicon_%s.json" % kb_id)
    # kbs.json 里 lexicon 有的写 "lexicon_x.json"（相对 config/），有的写 "config/lexicon_x.json"
    if rel.replace("\\", "/").startswith("config/"):
        return os.path.join(ROOT, rel)
    cand = os.path.join(ROOT, rel)
    return cand if os.path.exists(cand) else os.path.join(CONFIG_DIR, rel)


def _industry_for(kb_id, entry):
    """行业解析：kbs.json 的 industry 字段 > 关键词兜底。与 industrial_dict_loader 同口径。"""
    ind = str(entry.get("industry") or "").strip()
    if ind:
        return ind
    if _idl is not None:
        try:
            return _idl.industry_for_kb(kb_id) or "基础"
        except Exception:
            pass
    return "基础"


# ── 真实探测 ──────────────────────────────────────────────────────────────────
def probe_kb(kb_id, path=None):
    """**真实探测**单 KB 数据就绪：stat 文件系统，不假设。

    返回 {kb_id, data_dir, data_dir_exists, data_file_count, nt_file, nt_exists,
          lexicon_file, lexicon_exists, data_ready, servable, ontology_source,
          status, missing}。
    data_ready = 数据目录存在且有 ≥1 个数据文件（← 字面意义的「数据是否就绪」）。
    servable   = data_ready 且（本体 .nt 已生成 或 注册表声明 schema 可自动构建）。
    status     = ready / not_ready（跟随 data_ready）。
    """
    entry = load_registry(path).get(kb_id)
    if entry is None:
        return {"kb_id": kb_id, "data_ready": False, "servable": False,
                "status": "not_found", "missing": ["kb 未注册于注册表"]}

    data_dir = _resolve_data_dir(entry)
    nt_file = _resolve_nt(entry, kb_id)
    lex_file = _resolve_lexicon(entry, kb_id)

    data_dir_exists = os.path.isdir(data_dir)
    data_file_count = 0
    if data_dir_exists:
        for dp, _dn, fns in os.walk(data_dir):
            data_file_count += sum(1 for fn in fns if not fn.startswith("."))
    nt_exists = os.path.isfile(nt_file) and os.path.getsize(nt_file) > 0
    lex_exists = os.path.isfile(lex_file)
    schema_declared = bool(entry.get("schema"))

    if nt_exists:
        ontology_source = "nt"
    elif schema_declared:
        ontology_source = "schema(可自动构建)"
    else:
        ontology_source = "missing"

    missing = []
    if not data_dir_exists:
        missing.append("数据目录不存在: %s" % data_dir)
    elif data_file_count == 0:
        missing.append("数据目录无数据文件: %s" % data_dir)
    if not nt_exists and not schema_declared:
        missing.append("本体文件缺失且未声明 schema: %s" % nt_file)
    if not lex_exists:
        missing.append("工厂词典缺失: %s" % lex_file)

    data_ready = data_dir_exists and data_file_count > 0
    servable = data_ready and (nt_exists or schema_declared)
    return {
        "kb_id": kb_id,
        "data_dir": data_dir,
        "data_dir_exists": data_dir_exists,
        "data_file_count": data_file_count,
        "nt_file": nt_file,
        "nt_exists": nt_exists,
        "ontology_source": ontology_source,
        "lexicon_file": lex_file,
        "lexicon_exists": lex_exists,
        "data_ready": data_ready,
        "servable": servable,
        "status": "ready" if data_ready else "not_ready",
        "missing": missing,
    }


# ── 词典层 ────────────────────────────────────────────────────────────────────
def dict_layers(kb_id, path=None):
    """返回该 KB 的词典层描述：公共工业本体层（只读）+ 工厂专属层（可写）。"""
    entry = load_registry(path).get(kb_id) or {}
    industry = _industry_for(kb_id, entry)
    pub_files = ["00_basis.json"]
    if _idl is not None:
        try:
            pub_files = _idl.load_industry_files(industry)
        except Exception:
            pass
    lex_file = _resolve_lexicon(entry, kb_id)
    return {
        "public": {
            "layer": PUBLIC_LAYER,
            "read_only": True,
            "dir": PUBLIC_DICT_DIR,
            "industry": industry,
            "files": pub_files,
            "files_present": [f for f in pub_files
                              if os.path.isfile(os.path.join(PUBLIC_DICT_DIR, f))],
        },
        "factory": {
            "layer": FACTORY_LAYER,
            "read_only": False,
            "file": lex_file,
            "exists": os.path.isfile(lex_file),
        },
    }


def _load_factory_lexicon(kb_id, path=None):
    entry = load_registry(path).get(kb_id) or {}
    try:
        with open(_resolve_lexicon(entry, kb_id), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def detect_layer_conflicts(kb_id, path=None):
    """探测该 KB 工厂层与公共层的不一致项（同词不同规范名）。返回列表（空=完全一致）。

    注意语义：工厂层按既有 kb-wins 规则**覆盖**公共层是设计内行为（例如工厂层把
    「设备」映射到自己的表名 valve_equipment，公共层映射到通用名 equipment）。这些
    不一致项默认被静默覆盖 —— 本函数把它们**显式暴露**出来；需要强制报错时用
    merge_dict_layers(on_conflict="error")。
    """
    if _idl is None:
        return []
    entry = load_registry(path).get(kb_id) or {}
    industry = _industry_for(kb_id, entry)
    return _idl.detect_layer_conflicts(_load_factory_lexicon(kb_id, path), industry=industry)


def dict_layer_report(kb_id, path=None):
    """词典分层报告：把「工厂层覆盖公共层」的不一致项可见化 + 标注严格模式是否会报错。"""
    overrides = detect_layer_conflicts(kb_id, path)
    by_key = {}
    for c in overrides:
        by_key.setdefault(c["key"], 0)
        by_key[c["key"]] += 1
    return {
        "kb_id": kb_id,
        "layers": dict_layers(kb_id, path),
        "overrides_count": len(overrides),
        "overrides_by_key": by_key,
        "overrides_sample": overrides[:8],
        "strict_mode_would_error": len(overrides) > 0,
        "note": ("工厂层覆盖公共层=既有 kb-wins 语义（默认静默）。本报告使其可见；"
                 "merge_dict_layers(on_conflict='error') 可把覆盖升级为报错。"),
    }


def merge_dict_layers(kb_id=None, kb_dict=None, industry=None, path=None,
                      on_conflict="error"):
    """合并「公共工业本体层 ∪ 工厂专属层」，返回合并词典（不修改入参、不落盘）。

    默认 on_conflict="error"：两层冲突时抛 DictLayerConflictError，不静默覆盖。
    on_conflict="kb_wins" 保留旧行为（工厂层覆盖公共层），供兼容调用。
    """
    if _idl is None:
        raise RuntimeError("industrial_dict_loader 不可用，无法合并词典层")
    entry = load_registry(path).get(kb_id) or {} if kb_id else {}
    if industry is None:
        industry = _industry_for(kb_id, entry) if kb_id else None
    if kb_dict is None:
        kb_dict = _load_factory_lexicon(kb_id, path) if kb_id else {}
    return _idl.merge_industrial_dict(kb_dict, industry=industry, on_conflict=on_conflict)


# ── KB 详情 / 列表 ────────────────────────────────────────────────────────────
def get_kb(kb_id, path=None, tenant=None):
    """单 KB 详情（含真实探测的 data_ready）。

    未注册 → 返回 None（老行为）。
    带 tenant → **未知/越权一律拒**（抛 tenant.TenantDenied，绝不返回空/None 掩盖）：
      · KB 未注册 → TenantDenied（未知 KB）
      · KB 不在该租户可见范围 → TenantDenied（越权）
    """
    kbs = load_registry(path)
    entry = kbs.get(kb_id)
    if entry is None:
        if tenant is not None:
            raise _tenant_denied("未知 KB: %r（不在注册表中）" % kb_id)
        return None
    if tenant is not None and not kb_visible_for(kb_id, tenant, path):
        raise _tenant_denied("越权: 租户 %r 不可见 KB %r" % (tenant, kb_id))
    probe = probe_kb(kb_id, path)
    return {
        "kb_id": kb_id,
        "name": entry.get("name") or kb_id,
        "icon": entry.get("icon") or "",
        "data_dir": probe["data_dir"],
        "nt_file": probe["nt_file"],
        "lexicon_file": probe["lexicon_file"],
        "industry": _industry_for(kb_id, entry),
        "schema_layer": "shared",
        "dict_layer": dict_layers(kb_id, path),
        "data_ready": probe["data_ready"],
        "servable": probe["servable"],
        "ontology_source": probe["ontology_source"],
        "status": probe["status"],
        "probe": probe,
        "is_cleanup_candidate": is_cleanup_candidate(kb_id),
    }


def _tenant_denied(msg):
    """构造 TenantDenied（tenant 模块缺失时降级为 PermissionError，仍是拒绝）。"""
    if _tenant_mod is not None and hasattr(_tenant_mod, "TenantDenied"):
        return _tenant_mod.TenantDenied(msg)
    return PermissionError("[多租户] " + msg)


def list_kbs(path=None, include_cleanup=True, tenant=None):
    """KB 详情列表。include_cleanup=False 时剔除测试/onboarding/重复项备案项。

    tenant 非 None → 只返回该租户可见的 KB（按租户过滤）；越权/未知 KB 不在列表里。
    tenant 为 None（老调用）→ 不过滤，行为与改前一致。
    """
    out = []
    for kb_id in load_registry(path):
        if not include_cleanup and is_cleanup_candidate(kb_id):
            continue
        if tenant is not None and not kb_visible_for(kb_id, tenant, path):
            continue
        out.append(get_kb(kb_id, path))
    return out


def summary(path=None):
    """注册表概览：总数 / 就绪数 / 备案清理项。"""
    kbs = load_registry(path)
    details = list_kbs(path)
    ready = [d for d in details if d["data_ready"]]
    servable = [d for d in details if d["servable"]]
    return {
        "registry": registry_path(path),
        "total": len(kbs),
        "ready": len(ready),
        "servable": len(servable),
        "not_ready": len(details) - len(ready),
        "cleanup_candidates": cleanup_candidates(path),
    }


# ── 清理项备案 ────────────────────────────────────────────────────────────────
def is_cleanup_candidate(kb_id):
    s = str(kb_id)
    return any(s == p or s.startswith(p) for p in CLEANUP_PATTERNS)


def cleanup_candidates(path=None):
    """返回备案的清理候选（测试/onboarding/重复项）。本次不删数据，仅登记。"""
    return sorted(k for k in load_registry(path) if is_cleanup_candidate(k))


# ── 注册 / 注销（默认写真实注册表；自检脚本用临时副本）────────────────────────
def register_kb(kb_id, entry, path=None, overwrite=True):
    """注册一个新 KB 到注册表（幂等 upsert）。返回 detail dict。

    只动 kbs.json —— 问答引擎读同一份注册表即可感知，**无需改引擎代码、无需重启**。
    """
    kbs = load_registry(path)
    if kb_id in kbs and not overwrite:
        raise ValueError("KB 已存在且 overwrite=False: %s" % kb_id)
    kbs[kb_id] = dict(entry)
    save_registry(kbs, path)
    return get_kb(kb_id, path)


def unregister_kb(kb_id, path=None):
    """从注册表注销 KB，返回 {kb_id, removed, residue}。

    residue = 仍存在于磁盘、但已不在注册表中的关联文件（词典/本体/数据目录），
    **只报告不删除**（数据安全红线）。
    """
    kbs = load_registry(path)
    if kb_id not in kbs:
        return {"kb_id": kb_id, "removed": False, "residue": []}
    entry = kbs.pop(kb_id)
    save_registry(kbs, path)
    residue = []
    for label, p in (
        ("lexicon", _resolve_lexicon(entry, kb_id)),
        ("nt", _resolve_nt(entry, kb_id)),
        ("data_dir", _resolve_data_dir(entry)),
    ):
        if os.path.exists(p):
            residue.append({"kind": label, "path": p})
    return {"kb_id": kb_id, "removed": True, "residue": residue}


def _main():
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        s = summary()
        print("注册表: %s" % s["registry"])
        print("KB 总数 %d  数据就绪 %d  可服务(有本体) %d  备案清理项 %d"
              % (s["total"], s["ready"], s["servable"], len(s["cleanup_candidates"])))
        print("备案清理项:", ", ".join(s["cleanup_candidates"]))
        print("  %-3s %-32s %-9s %-9s %-14s %s"
              % ("", "kb_id", "数据就绪", "可服务", "本体来源", "data_dir"))
        for d in list_kbs():
            print("  %s   %-32s %-9s %-9s %-14s %s"
                  % ("✓" if d["servable"] else "·", d["kb_id"],
                     "ready" if d["data_ready"] else "not_ready",
                     "yes" if d["servable"] else "no",
                     d["ontology_source"], d["data_dir"]))
        return
    print(__doc__)


if __name__ == "__main__":
    _main()
