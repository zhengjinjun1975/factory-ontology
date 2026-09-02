# -*- coding: utf-8 -*-
"""test_schema_contract.py — factory-ontology 版本契约(schema 演化破坏性检测) pytest。

阶段2 迁入：把 ontology-analysis 的 schema_contract(删=破坏/加=兼容/去噪) 重新适配到
factory 的 ontology_schema.json 结构(entities/relations/constraints/version)。

用法(CI): cd codes && python -m pytest tests/test_schema_contract.py -q
"""
import os
import sys
import json

_CODES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _CODES)
from schema_contract import diff_schema, format_report  # noqa: E402


def _schema(**over):
    base = {
        "version": "1.0",
        "entities": [
            {"id": "Product", "label": "产品", "table": "valve_products", "key": "id",
             "attributes": [{"name": "id", "type": "string", "required": True},
                            {"name": "size_mm", "type": "number", "required": False}]},
            {"id": "Order", "label": "订单", "table": "valve_orders", "key": "id",
             "attributes": [{"name": "id", "type": "string", "required": True}]},
        ],
        "relations": [{"id": "sells", "from": "Product", "to": "Order",
                       "fk": "valve_orders.product_id", "cardinality": "1:N"}],
        "constraints": [{"type": "unique", "on": "Product.id", "msg": "产品唯一"}],
    }
    base.update(over)
    return base


def _del_entity(s, eid):
    s = json.loads(json.dumps(s))
    s["entities"] = [e for e in s["entities"] if e["id"] != eid]
    return s


def _del_relation(s, rid):
    s = json.loads(json.dumps(s))
    s["relations"] = [r for r in s["relations"] if r["id"] != rid]
    return s


def _del_attr(s, eid, an):
    s = json.loads(json.dumps(s))
    for e in s["entities"]:
        if e["id"] == eid:
            e["attributes"] = [a for a in e["attributes"] if a["name"] != an]
    return s


def test_delete_entity_breaking():
    prev, curr = _schema(), _del_entity(_schema(), "Product")
    breaks, compats = diff_schema(prev, curr)
    assert any("删除实体: Product" in b for b in breaks)
    assert breaks  # 有破坏
    assert not compats  # 纯删除无兼容


def test_delete_relation_breaking():
    breaks, _ = diff_schema(_schema(), _del_relation(_schema(), "sells"))
    assert any("删除关系: sells" in b for b in breaks)


def test_delete_attribute_breaking():
    breaks, _ = diff_schema(_schema(), _del_attr(_schema(), "Product", "size_mm"))
    assert any("删除字段: Product.size_mm" in b for b in breaks)


def test_require_field_breaking():
    curr = _schema()
    for e in curr["entities"]:
        if e["id"] == "Product":
            for a in e["attributes"]:
                if a["name"] == "size_mm":
                    a["required"] = True
    breaks, _ = diff_schema(_schema(), curr)
    assert any("字段收紧" in b and "size_mm" in b for b in breaks)


def test_add_entity_is_compatible():
    curr = _schema()
    curr["entities"].append({"id": "Warehouse", "label": "仓库", "table": "wh", "key": "id",
                             "attributes": [{"name": "id", "type": "string"}]})
    curr["version"] = "1.1"
    breaks, compats = diff_schema(_schema(), curr)
    assert not breaks  # 纯新增 = 兼容
    assert any("新增实体: Warehouse" in c for c in compats)


def test_delete_entity_denoises_fields():
    # 删实体不应连带报它的 N 个字段(去噪)
    breaks, _ = diff_schema(_schema(), _del_entity(_schema(), "Product"))
    assert sum(1 for b in breaks if b.startswith("删除字段: Product.")) == 0


def test_format_report_present():
    breaks, compats = diff_schema(_schema(), _del_entity(_schema(), "Product"))
    report = format_report(breaks, compats)
    assert "破坏性变更" in report and "删除实体: Product" in report
