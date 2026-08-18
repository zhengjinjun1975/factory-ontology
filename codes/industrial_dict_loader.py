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

# 合并优先级键位（只合并这两个映射层；attr/entity 数值等仍以 KB 为准）
_MERGE_KEYS = ("type_cn2en", "status_cn2en", "synonym_map", "entity_cn2en")
# 兜底键位：KB 完全没有时用公共层（attr/numeric 不做合并，防误伤工厂字段）
_FALLBACK_KEYS = ("entity_cn2en",)


def _load_public(files=None):
    """加载公共词典 JSON 文件, 合并为一份公共层字典。
    files: 指定要合并的文件名列表; 默认只合并基础层(00_basis.json, 跨行业通用)。"""
    merged = {}
    if not os.path.isdir(_DICT_DIR):
        return merged
    if files is None:
        # 默认只合并基础层(00_basis), 行业词典按需通过 files 指定
        files = ["00_basis.json"]
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


def merge_industrial_dict(kb_dict, files=None):
    """把公共工业词典合并进 KB 词典，返回合并结果（不修改入参）。

    合并规则：
      * type/status/synonym/entity 四类：KB 有则用 KB（覆盖公共），KB 无则用公共（兜底）。
      * 其余键（attr_cn2en/numeric_fields/field_aliases 等）保持 KB 原样，不动。
    files: 指定要合并的公共词典文件列表(如 ["valve_public_dict.json"])。
           默认合并所有(device_types.json + valve_public_dict.json 等)。
    """
    pub = _load_public(files)
    if not pub:
        return kb_dict
    out = dict(kb_dict) if kb_dict else {}
    for key in _MERGE_KEYS:
        pub_sub = pub.get(key) or {}
        kb_sub = out.get(key) or {}
        # KB 覆盖公共：公共项仅当 KB 无此中文键时才补入
        for cn, en in pub_sub.items():
            if cn not in kb_sub:
                kb_sub[cn] = en
        out[key] = kb_sub
    return out


def public_dict_size():
    """返回公共层统计（用于验证/调试）。"""
    pub = _load_public()
    return {k: len(v) for k, v in pub.items()}


if __name__ == "__main__":
    import sys
    print("公共词典目录:", _DICT_DIR)
    print("公共层规模:", public_dict_size())
    # 自检：合并一个空 KB 词典，验证公共层兜底生效
    merged = merge_industrial_dict({})
    print("合并空KB后 type_cn2en 条数:", len(merged.get("type_cn2en", {})))
    print("  样例:", {k: v for k, v in list(merged.get("type_cn2en", {}).items())[:5]})
