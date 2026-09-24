#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""industrial_dict.py — 公共工业本体词典合并加载器。

设计原则（精而泛化、不庞杂）：
  * 公共词典存跨行业稳定的"领域骨架"（设备大类/材质同义词/通用状态），
    不存易变的"工厂实例"（具体型号/批次/企业特有字段）——那属于 per-KB 词典。
  * 问答时把 公共词典 ∪ KB词典 合并为一个 dict：KB 覆盖公共（工厂有特殊定义时优先），
    公共兜底 KB（KB 没有时用公共层）。一个文件、一次合并，不增加问答复杂度。
  * 公共词典是开源侧的算法资产（纯领域知识，无任何企业数据）。

用法：
  from industrial_dict import merge_industrial_dict
  D = merge_industrial_dict(kb_dict)   # 返回合并后的词典
"""
import os
import json

# 公共词典目录（本文件同级 industrial_dict/）
_DICT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "industrial_dict")

# 合并优先级键位（只合并"中文词→规范名"的 flat 映射；attr_cn2en/numeric_fields/field_aliases
# 等属工厂字段，不合并，防误伤 KB 自己的列名语义）
_MERGE_KEYS = ("type_cn2en", "status_cn2en", "synonym_map", "entity_cn2en",
               "fault_cn2en", "material_synonyms", "pump_cn2en", "part_cn2en",
               "process_cn2en", "product_type_cn2en", "safety_cn2en", "method_cn2en")
# 兜底键位：KB 完全没有时用公共层（attr/numeric 不做合并，防误伤工厂字段）
_FALLBACK_KEYS = ("entity_cn2en",)

# ── 词典分层标记（KB 注册表 + 词典分层 2026-09-24）─────────────────────────────
# public  = 公共工业本体层（industrial_dict/*.json）：跨行业稳定骨架，只读，开源算法资产
# factory = 工厂专属层（config/lexicon_<kb>.json）：企业私有词，可写，各 KB 隔离
PUBLIC_LAYER = "public"
FACTORY_LAYER = "factory"
# 对外公开的合并键位别名（kb_registry / 自检脚本消费，避免依赖私有名）
MERGE_KEYS = _MERGE_KEYS


class DictLayerConflictError(ValueError):
    """公共层与工厂层对同一中文词给出不同规范名。

    分层纪律：冲突时**报错、不静默覆盖**（调用方需显式决定谁赢）。
    """

    def __init__(self, conflicts):
        self.conflicts = list(conflicts or [])
        sample = self.conflicts[:5]
        msg = ("词典层冲突: 公共层与工厂层对 %d 个中文词给出不同规范名，拒绝静默覆盖。样例: %s"
               % (len(self.conflicts), "; ".join(
                   "%s/%s 公共=%r 工厂=%r" % (c.get("key"), c.get("cn"), c.get("public"), c.get("factory"))
                   for c in sample)))
        super().__init__(msg)

# 行业 → 公共词典文件（与 absorb_public_dict.INDUSTRY_FILES 保持一致）
INDUSTRY_FILES = {
    "基础": "00_basis.json",
    "泵阀": "01_valve_pump.json",
    "精细化工": "02_fine_chem.json",
    "地球物理": "03_geophysics.json",
}
# kb → 行业兜底：kbs.json 未声明 industry 时按 kb 关键词推断
_KB_INDUSTRY_HINTS = (("valve", "泵阀"), ("pump", "泵阀"),
                      ("chem", "精细化工"),
                      ("seis", "地球物理"), ("geo", "地球物理"))


def industry_for_kb(kb):
    """解析 kb 所属行业：先读 config/kbs.json 的 industry 字段，再按 kb 关键词兜底。

    返回行业名（基础/泵阀/精细化工/地球物理）或 None（无法判定 → 只合并基础层）。
    """
    if not kb:
        return None
    kb_name = str(kb).strip()
    # 允许直接传行业名
    if kb_name in INDUSTRY_FILES:
        return kb_name
    try:
        cfg_path = os.path.join(os.path.dirname(_DICT_DIR), "config", "kbs.json")
        with open(cfg_path, encoding="utf-8") as f:
            kbcfg = (json.load(f).get("kbs") or {}).get(kb_name) or {}
        ind = str(kbcfg.get("industry") or "").strip()
        if ind in INDUSTRY_FILES:
            return ind
    except Exception:
        pass
    low = kb_name.lower()
    for kw, ind in _KB_INDUSTRY_HINTS:
        if kw in low:
            return ind
    return None


def load_industry_files(industry=None):
    """行业 → 要合并的公共词典文件列表。

    industry 为 None/基础/未知 → 只基础层（与旧行为一致，向后兼容）。
    """
    if industry and industry in INDUSTRY_FILES and industry != "基础":
        return ["00_basis.json", INDUSTRY_FILES[industry]]
    return ["00_basis.json"]


def _load_public(files=None, industry=None):
    """加载公共词典 JSON 文件, 合并为一份公共层字典。
    files: 显式指定要合并的文件名列表;
    industry: 指定行业时 = [00_basis.json, 该行业词典]（行业层从此真正被消费）。
    默认（两者都未给）只合并基础层 00_basis.json。"""
    merged = {}
    if not os.path.isdir(_DICT_DIR):
        return merged
    if files is None:
        files = load_industry_files(industry)
    for fn in sorted(files):
        fp = os.path.join(_DICT_DIR, fn)
        if not os.path.exists(fp):
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
            for key in _MERGE_KEYS:
                sub = d.get(key) or {}
                merged.setdefault(key, {}).update(sub)
        except Exception:
            continue
    return merged


def load_public_layer(files=None, industry=None):
    """公开入口：加载公共工业本体层（只读）并返回合并后的扁平词典。

    返回 {合并键: {中文: 规范名}}，只含 _MERGE_KEYS 收录的跨行业键位
    （attr_cn2en/numeric_fields 等工厂字段不属公共层，永不返回）。
    """
    return _load_public(files, industry)


def detect_layer_conflicts(kb_dict, files=None, industry=None):
    """探测工厂层与公共层冲突：同一中文词 → 不同规范名。

    返回 [{"key","cn","public","factory"}...]（空 = 无冲突）。
    不修改任何入参。
    """
    pub = _load_public(files, industry)
    kb_dict = kb_dict if isinstance(kb_dict, dict) else {}
    conflicts = []
    for key in _MERGE_KEYS:
        pub_sub = pub.get(key) or {}
        kb_sub = kb_dict.get(key) or {}
        if not isinstance(pub_sub, dict) or not isinstance(kb_sub, dict):
            continue
        for cn, pen in pub_sub.items():
            if cn in kb_sub and kb_sub[cn] != pen:
                conflicts.append({"key": key, "cn": cn, "public": pen, "factory": kb_sub[cn]})
    return conflicts


def merge_industrial_dict(kb_dict, files=None, industry=None, on_conflict="kb_wins"):
    """把公共（基础层 + 行业层）词典合并进 KB 词典，返回合并结果（不修改入参）。

    合并规则：
      * _MERGE_KEYS 各类：KB 有则用 KB（覆盖公共），KB 无则用公共（兜底）。
      * 其余键（attr_cn2en/numeric_fields/field_aliases 等）保持 KB 原样，不动。
    files: 显式指定要合并的公共词典文件列表。
    industry: 指定行业（基础/泵阀/精细化工/地球物理）→ 合并 [00_basis, 行业词典]。
              为 None 时只合并基础层，与旧行为完全一致（向后兼容）。
    on_conflict: "kb_wins"（默认，旧行为：工厂层覆盖公共层）；
                 "error" → 存在公共/工厂冲突时抛 DictLayerConflictError，不静默覆盖。
    """
    if on_conflict == "error":
        conflicts = detect_layer_conflicts(kb_dict, files, industry)
        if conflicts:
            raise DictLayerConflictError(conflicts)
    pub = _load_public(files, industry)
    if not pub:
        return kb_dict
    out = dict(kb_dict) if kb_dict else {}
    for key in _MERGE_KEYS:
        pub_sub = pub.get(key) or {}
        kb_sub = out.get(key) or {}
        if not isinstance(kb_sub, dict):
            continue
        kb_sub = dict(kb_sub)   # 值层拷贝: 防兜底补入改动调用方嵌套 dict(兑现"不修改入参")
        # KB 覆盖公共：公共项仅当 KB 无此中文键时才补入
        for cn, en in pub_sub.items():
            if cn not in kb_sub:
                kb_sub[cn] = en
        out[key] = kb_sub
    return out


def public_dict_size(industry=None):
    """返回公共层统计（用于验证/调试）。传 industry 时含该行业层。"""
    pub = _load_public(industry=industry)
    return {k: len(v) for k, v in pub.items()}


if __name__ == "__main__":
    import sys
    print("公共词典目录:", _DICT_DIR)
    print("公共层规模:", public_dict_size())
    # 自检：合并一个空 KB 词典，验证公共层兜底生效
    merged = merge_industrial_dict({})
    print("合并空KB后 type_cn2en 条数:", len(merged.get("type_cn2en", {})))
    print("  样例:", {k: v for k, v in list(merged.get("type_cn2en", {}).items())[:5]})
