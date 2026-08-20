# -*- coding: utf-8 -*-
"""inventory_decision — 库存补货决策插件（决策类扩展点）。

给第三方开发者演示「如何写一个库存决策插件」：
  1. 定义 Plugin 类（继承 BasePlugin）
  2. 在 register() 里用 reg.register(kind, id, handler) 暴露库存补货决策能力
  3. handler 统一签名 fn(params: dict) -> 可 JSON 序列化结果

基于库存台账做确定性补货决策（纯规则，零 LLM）：
  - 当前库存 < 安全库存 → 触发补货
  - 输出补货建议（含物料编号，V01/V02...）
  - 输出结果带 "ok": true，契合本项目「确定性优先」。
"""

import sys
import os

# 允许独立运行调试：python plugin.py（此时能 import 到框架）
if __package__ in (None, ""):
    # 插件在 plugins/<name>/plugin.py，向上三级才是 codes/（框架所在目录）
    _ROOT = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

from plugin_framework import BasePlugin  # noqa: E402


class Plugin(BasePlugin):
    name = "inventory_decision"
    kind = "decision"
    version = "1.0.0"

    def register(self, reg):
        """把库存补货决策登记进扩展点注册表 decision/inventory。"""
        reg.register("decision", "inventory",
                     self._inventory_decision, plugin=self.name)

    # 默认安全库存（可按物料覆盖）
    SAFETY_STOCK = 10

    def _inventory_decision(self, params):
        params = params or {}
        industry = params.get("industry", "unknown")
        stock = params.get("stock", params.get("stock_qty", 0))
        # 支持 records 批量
        records = params.get("records") or []
        if records:
            decisions = [self._decide_single(r) for r in records]
            return {"ok": True, "industry": industry, "decisions": decisions}
        single = self._decide_single({
            "industry": industry,
            "stock": stock,
            "sku": params.get("sku", "V01"),
            "safety_stock": params.get("safety_stock", self.SAFETY_STOCK),
        })
        return {"ok": True, **single}

    def _decide_single(self, r):
        r = r or {}
        stock = r.get("stock", 0)
        sku = r.get("sku", "V01")
        safety = r.get("safety_stock", self.SAFETY_STOCK)
        industry = r.get("industry", "unknown")
        if stock < safety:
            action = "补货"
            reorder = safety - stock
        else:
            action = "无需补货"
            reorder = 0
        return {
            "sku": sku,
            "stock": stock,
            "safety_stock": safety,
            "action": action,
            "reorder_qty": reorder,
            "industry": industry,
        }

    def run(self, params=None):
        """主逻辑：decision/inventory 扩展点的默认调用。"""
        result = self._inventory_decision(params or {})
        if isinstance(result, dict) and "decisions" in result:
            return result
        return {"ok": True, **result}


if __name__ == "__main__":
    # 独立运行自测：python plugin.py
    demo = [
        {"sku": "V01", "stock": 5, "safety_stock": 10, "industry": "manufacturing"},
        {"sku": "V02", "stock": 15, "safety_stock": 10, "industry": "manufacturing"},
    ]
    import json
    print(json.dumps(Plugin().run({"records": demo}),
                     ensure_ascii=False, indent=2))
