#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lexicon.py — 开源问答侧「中文→英文」静态词典唯一数据源（P1-7 词典收敛）。

背景：graph_rag.py / ontology_qa_v3.py 原先各自内嵌维护一份静态中文词典，
存在重复与漂移风险。本模块把以下静态词典收敛为单一数据源：
  - 同义词组（材质/类型，如 不锈钢→CF8/CF8M/304）
  - 单位别名（psi/MPa/bar 统一归一）
  - 高频中文属性别名（保质期→expiry_days 等）
  - 通用中文状态词→英文值兜底（运行中→running 等）
  - 中文实体名→表名/URI 子串兜底（设备→equipment 等）

设计原则：
  * 只读：对外仅暴露 get_*() 函数，数据为模块私有常量，调用方不可修改。
  * 中文 docstring；不引入任何第三方依赖（仅标准库）。
  * 与 graph_rag/ontology_qa_v3 原常量语义完全一致（键值逐一保留，不新增不删除）。

明确不并入本模块（语义不同，保留原处）：
  * ontology_qa_v3._EXTREME_WORD_FIELDS —— 中文极值词→中文字段名提示，非「中→英」映射。
  * schema_ontology._REF/_MEASURE/_DATE/_CATEGORY_HINTS —— 英文列名语义角色分类器。
  * 闭源 review.py/assets.py 运行时词典、multi_model 生成路径、lexicon_agent.EN_CN。
"""

# ------------------------------------------------------------------ 材质/类型同义词组
# 材质/单位/类型的别名→规范组，检索时把查询词展开成同义组扩大匹配。
_SYNONYM_GROUPS = {
    "不锈钢": ["不锈钢", "304", "316", "cf8", "cf8m", "cf3", "1cr18ni9ti"],
    "碳钢": ["碳钢", "wcb", "a105", "20钢", "20"],
    "合金钢": ["合金钢", "wc6", "wc9", "15crmo", "10cr2mo1"],
    "球墨铸铁": ["球墨铸铁", "qt450", "qt400"],
    "灰铸铁": ["灰铸铁", "ht200", "ht250"],
    "铜": ["铜", "铜合金", "h62", "h59"],
    "法兰": ["法兰", "flange"],
    "电动": ["电动", "电装", "z9"],
    "气动": ["气动", "q6"],
    "不锈钢304": ["不锈钢304", "304", "cf8"],
    "不锈钢316": ["不锈钢316", "316", "cf8m"],
}

# ------------------------------------------------------------------ 单位归一
# 同一物理量多单位（psi/MPa/bar），统一到规范单位再检索。
_UNIT_ALIASES = {
    "mpa": ["mpa", "兆帕"], "bar": ["bar", "巴"],
    "psi": ["psi", "磅"],
}

# ------------------------------------------------------------------ 高频中文属性别名
# 常见数值/极值字段的中文别名 -> 规范英文字段名（词典 attr_cn2en 缺失时兜底）。
_ATTR_CN_ALIASES = {
    "保质期": "expiry_days", "保质": "expiry_days", "保质天数": "expiry_days",
    "shelf": "expiry_days", "shelflife": "expiry_days",
    "质保": "warranty", "质保期": "warranty",
    "价格": "price", "售价": "price", "单价": "price",
    "功率": "power", "库存": "stock", "数量": "quantity",
    "温度": "temperature", "振动": "vibration", "压力": "pressure",
}

# ------------------------------------------------------------------ 通用中文状态词
# 中文运维状态词 -> 英文值兜底（当词典缺失中文映射时）。
_COMMON_ZH_STATUS = {
    "运行中": "running", "运行": "running", "正常": "running", "工作中": "running",
    "停止": "stopped", "停机": "stopped", "空闲": "idle", "待机": "idle",
    "故障": "alarm", "报警": "alarm", "异常": "alarm",
    "维护": "maintenance", "保养": "maintenance", "维修": "maintenance",
    "离线": "offline",
}

# ------------------------------------------------------------------ 中文实体名 → URI 子串
# 中文实体类名 -> 表名/URI 子串（计数"有多少台设备"等）。
_ENTITY_CN2URI = {
    "设备": "equipment", "产品": "product", "客户": "customer",
    "批次": "batch", "原料": "raw_material", "原材料": "raw_material",
    "销售": "sale", "质检": "qc",
}


def get_synonym_groups():
    """返回材质/类型同义词组 {中文规范词: [同义词...]}（只读副本）。"""
    return dict(_SYNONYM_GROUPS)


def get_unit_aliases():
    """返回单位别名映射 {规范单位: [别名...]}（只读副本）。"""
    return dict(_UNIT_ALIASES)


def get_attr_cn_aliases():
    """返回高频中文属性别名 {中文属性: 英文字段}（只读副本）。"""
    return dict(_ATTR_CN_ALIASES)


def get_common_zh_status():
    """返回通用中文状态词 {中文: 英文值}（只读副本）。"""
    return dict(_COMMON_ZH_STATUS)


def get_entity_cn2uri():
    """返回中文实体名 -> URI 子串映射（只读副本）。"""
    return dict(_ENTITY_CN2URI)


if __name__ == "__main__":
    # 自检：打印各词典条目数，确认与收敛前一致。
    print("synonym_groups:", len(get_synonym_groups()))
    print("unit_aliases:", len(get_unit_aliases()))
    print("attr_cn_aliases:", len(get_attr_cn_aliases()))
    print("common_zh_status:", len(get_common_zh_status()))
    print("entity_cn2uri:", len(get_entity_cn2uri()))
