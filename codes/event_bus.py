#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""event_bus.py — 工厂数据事件驱动·事件总线（零依赖，纯标准库）。

借鉴 DataBuff「数据采集→事件→系统响应」思想：**数据一旦变更，就发布领域事件**，
让监听方（闭源收拢层的告警/工单/本体刷新/通知/AI 问数）自动响应，而不是各处轮询。

本模块是**开源算法侧**的事件总线，只负责「事件模型 + 订阅/发布 + monitor 接入适配」，
**不包含任何业务响应**——响应（触发告警/刷新本体/更新工单/推送通知/AI 问数）全部由
闭源 `ontology-delivery-tool/src/event_listener.py` 订阅并执行（边界：算法在开源，
编排/响应在闭源）。

数据流（事件驱动骨架）：
    数据变更(MetricStore.record / 台账写入)
        → 发布事件(DATA_ADDED / METRIC_ANOMALY / THRESHOLD_EXCEEDED / TICKET_CREATED)
        → 事件总线分发(EventBus.publish → 已订阅的监听器)
        → 闭源监听器自动响应(告警/工单/本体刷新/通知/AI问数)

零依赖：仅用 `uuid / datetime / threading / typing`，无任何第三方包。
用法（示例）：
    from event_bus import EventBus, DATA_ADDED, METRIC_ANOMALY

    bus = EventBus()
    bus.subscribe(METRIC_ANOMALY, on_anomaly)        # 订阅指标异常事件
    bus.publish({"type": METRIC_ANOMALY, "source": "monitor",
                 "payload": {"device_id": "d1", "metric": "temperature", "value": 95.0}})
    # → 同步调用 on_anomaly(ev)

    # 结合 monitor 设备监测：把开源 solo monitor 的指标写入链路接入事件总线
    import solo.factory.monitor as mon
    adapter = MonitorEventAdapter(bus, monitor_module=mon)
    adapter.ingest("d1", "temperature", 95.0, auto_ticket=True)   # 触发 指标异常/阈值超限 事件
"""
from __future__ import annotations

import os
import uuid
import threading
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════
# 1. 事件类型常量（领域事件）
# ═══════════════════════════════════════════════════════════════════════
DATA_ADDED = "data.added"                # 数据新增（台账/指标写入）
METRIC_ANOMALY = "metric.anomaly"        # 指标异常（突变/状态异常）
THRESHOLD_EXCEEDED = "threshold.exceeded"  # 阈值超限（规则 >/< 命中）
TICKET_CREATED = "ticket.created"        # 工单创建（告警 → 工单）

# 所有已知事件类型
EVENT_TYPES = (DATA_ADDED, METRIC_ANOMALY, THRESHOLD_EXCEEDED, TICKET_CREATED)


# ═══════════════════════════════════════════════════════════════════════
# 2. 事件模型 Event
# ═══════════════════════════════════════════════════════════════════════
class Event:
    """一条领域事件。

    fields:
      id      : 全局唯一事件 id
      type    : 事件类型（EVENT_TYPES 之一）
      source  : 事件来源（如 'monitor' / 'ledger' / 'ask'）
      ts      : 事件发生时间（ISO 字符串）
      payload : 事件载荷 dict（数据变更内容）
    """

    __slots__ = ("id", "type", "source", "ts", "payload")

    def __init__(self, type: str, source: str, payload: dict = None,
                 id: str = None, ts: str = None):
        self.id = id or f"EV-{uuid.uuid4().hex[:12]}"
        self.type = type
        self.source = source
        self.ts = ts or datetime.now().isoformat(timespec="seconds")
        self.payload = payload or {}

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "source": self.source,
                "ts": self.ts, "payload": self.payload}

    def __repr__(self):
        return f"<Event {self.type} {self.id} src={self.source} ts={self.ts}>"


# ═══════════════════════════════════════════════════════════════════════
# 3. 事件总线 EventBus（订阅 / 发布 / 分发）
# ═══════════════════════════════════════════════════════════════════════
class EventBus:
    """同步事件总线。

    - subscribe(type, handler): 订阅某类事件（type='*' 订阅全部）。
    - publish(event): 发布事件，同步分发到所有匹配的监听器，并记录事件日志。
    - 每个 handler 返回 dict 会被聚合进 `publish` 的响应（供上游看到监听结果）。
    线程安全：订阅/发布用锁保护，日志追加 + 监听调用在锁外执行。
    """

    def __init__(self, max_log: int = 500):
        self._subs = {}          # type -> [handler, ...]
        self._log = []           # 事件历史（最新在前）
        self._max_log = max_log
        self._lock = threading.Lock()

    # ---- 订阅 ----
    def subscribe(self, event_type: str, handler) -> None:
        """订阅。event_type='*' 表示订阅全部事件。handler(ev: Event) -> dict。"""
        with self._lock:
            self._subs.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler) -> None:
        with self._lock:
            hs = self._subs.get(event_type, [])
            if handler in hs:
                hs.remove(handler)

    def handlers_for(self, event_type: str) -> list:
        with self._lock:
            out = list(self._subs.get(event_type, []))
            out += list(self._subs.get("*", []))
            return out

    # ---- 发布 ----
    def publish(self, event: Event) -> dict:
        """发布事件：同步分发到监听器，聚合每个监听器返回值，记录日志。

        返回 {"event": {...}, "handlers": N, "results": [...]}。
        """
        results = []
        for h in self.handlers_for(event.type):
            try:
                r = h(event)
                results.append(r if isinstance(r, dict) else {"result": r})
            except Exception as e:  # 监听器异常不阻断其它监听/不抛给发布方
                results.append({"error": f"{type(e).__name__}: {e}"})
        with self._lock:
            self._log.insert(0, event.to_dict())
            self._log = self._log[: self._max_log]
        return {"event": event.to_dict(), "handlers": len(results),
                "results": results}

    # ---- 事件历史 ----
    def events(self, limit: int = 50, event_type: str = None) -> list:
        with self._lock:
            out = self._log
            if event_type:
                out = [e for e in out if e["type"] == event_type]
            return list(out[:limit])

    def count_by_type(self) -> dict:
        with self._lock:
            cnt = {}
            for e in self._log:
                cnt[e["type"]] = cnt.get(e["type"], 0) + 1
            return cnt

    def stats(self) -> dict:
        with self._lock:
            subs = {t: len(hs) for t, hs in self._subs.items()}
        return {"total_events": len(self._log), "by_type": self.count_by_type(),
                "listeners": subs}


# ═══════════════════════════════════════════════════════════════════════
# 4. 便捷发布函数
# ═══════════════════════════════════════════════════════════════════════
def make_event(event_type: str, source: str, payload: dict) -> Event:
    return Event(event_type, source, payload)


def publish_data_added(bus: EventBus, entity: str, data: dict,
                       source: str = "ledger") -> Event:
    """发布「数据新增」事件（台账/指标写入）。"""
    ev = Event(DATA_ADDED, source, {"entity": entity, "data": data})
    bus.publish(ev)
    return ev


def publish_metric_anomaly(bus: EventBus, device_id: str, metric: str,
                           value, reason: str, source: str = "monitor") -> Event:
    """发布「指标异常」事件（突变/状态异常）。"""
    ev = Event(METRIC_ANOMALY, source,
               {"device_id": device_id, "metric": metric, "value": value,
                "reason": reason})
    bus.publish(ev)
    return ev


def publish_threshold_exceeded(bus: EventBus, device_id: str, metric: str,
                               value, op: str, threshold, level: str = "medium",
                               source: str = "monitor") -> Event:
    """发布「阈值超限」事件（规则 >/< 命中）。"""
    ev = Event(THRESHOLD_EXCEEDED, source,
               {"device_id": device_id, "metric": metric, "value": value,
                "op": op, "threshold": threshold, "level": level})
    bus.publish(ev)
    return ev


# ═══════════════════════════════════════════════════════════════════════
# 5. monitor 接入适配器 MonitorEventAdapter
# ═══════════════════════════════════════════════════════════════════════
class MonitorEventAdapter:
    """把开源 solo monitor 的设备指标写入链路接入事件总线。

    边界：本适配器**不 import monitor**（monitor 在独立仓库 solo-agent-kit），
    通过构造参数注入 monitor 模块对象（依赖注入），保持 event_bus 零依赖、可独立复用。

    职责（数据变更 → 事件）：
      - 任何指标写入 → DATA_ADDED 事件
      - 告警触发(type=threshold) → THRESHOLD_EXCEEDED 事件
      - 告警触发(突变/异常)   → METRIC_ANOMALY 事件
      - 告警生成工单           → TICKET_CREATED 事件
    底层仍走 monitor.Source.feed 的「存储→评估告警→自动工单」全链路，
    事件总线在其之上发布领域事件，供闭源监听器自动响应。
    """

    def __init__(self, bus: EventBus, monitor_module):
        self.bus = bus
        self.mon = monitor_module
        self.store = monitor_module.MetricStore()
        self.engine = monitor_module.AlertEngine(self.store)

    def ingest(self, device_id: str, metric: str, value, auto_ticket: bool = True,
               tags: dict = None) -> dict:
        """接入一条设备指标 → 存储 → 评估告警 → 自动工单 → 发布领域事件。

        返回 {"ingested": str, "alerts": [...], "tickets": [...], "events": [...]}。
        """
        src = self.mon.Source(store=self.store, engine=self.engine,
                              auto_ticket=auto_ticket)
        # 规则可能由外部(MetricStore().set_rule/其它实例)写入 rules.json，评估前重载
        self.store._rules = self.store._load(self.store.rules_file, [])
        rec = self.store.record(device_id, metric, float(value),
                                tags={"tags": tags} if tags else None)
        # 数据新增事件
        self.bus.publish(Event(DATA_ADDED, "monitor",
                               {"device_id": device_id, "metric": metric,
                                "value": float(value), "ts": rec["ts"]}))
        # 评估告警 → 发布指标事件 + 工单事件
        raised = self.engine.evaluate_point(device_id, metric, float(value), rec["ts"])
        alert_events, ticket_events = [], []
        for alert in raised:
            if alert["type"] == "threshold":
                self.bus.publish(Event(THRESHOLD_EXCEEDED, "monitor",
                                       {"device_id": device_id, "metric": metric,
                                        "value": float(value),
                                        "op": alert["op"], "threshold": alert["threshold"],
                                        "level": alert["level"], "alert_id": alert["id"]}))
            else:
                self.bus.publish(Event(METRIC_ANOMALY, "monitor",
                                       {"device_id": device_id, "metric": metric,
                                        "value": float(value),
                                        "reason": f"突变检测: {alert['type']}",
                                        "alert_id": alert["id"]}))
            alert_events.append(alert)
            if auto_ticket:
                ticket = self.engine.alert_to_ticket(alert)
                self.bus.publish(Event(TICKET_CREATED, "monitor",
                                       {"ticket_id": ticket["id"],
                                        "device_id": device_id,
                                        "metric": metric, "severity": alert["level"],
                                        "problem": ticket["problem"]}))
                ticket_events.append(ticket)
        return {"ingested": f"{device_id}.{metric}={value}",
                "alerts": alert_events, "tickets": ticket_events}

    def run_demo(self, rounds: int = 12, temp_high: float = 80.0) -> dict:
        """一键端到端演示（模拟数据 → 事件 → 告警/工单）。"""
        self.store.set_rule("d1", "temperature", ">", temp_high,
                            level="high", label="d1 温度过高")
        n_alert, n_ticket = 0, 0
        for step in range(rounds):
            for m, base in (("temperature", 45.0), ("vibration", 2.0),
                            ("power", 30.0)):
                v = base + (2.0 if m == "temperature" and step > 6 else 0.0)
                r = self.ingest("d1", m, round(v, 1), auto_ticket=True)
                n_alert += len(r["alerts"])
                n_ticket += len(r["tickets"])
        return {"rounds": rounds, "alerts": n_alert, "tickets": n_ticket,
                "events": len(self.bus.events())}


# ═══════════════════════════════════════════════════════════════════════
# 6. 全局默认总线（供便捷使用；生产建议显式创建 bus）
# ═══════════════════════════════════════════════════════════════════════
_default_bus = EventBus()


def get_default_bus() -> EventBus:
    return _default_bus


if __name__ == "__main__":
    # 自检：事件总线发布/订阅 + monitor 接入适配器发布事件
    def _on_anomaly(ev):
        p = ev.payload
        return {"received": "anomaly", "device": p.get("device_id")}

    def _on_threshold(ev):
        p = ev.payload
        return {"received": "threshold", "level": p.get("level")}

    bus = EventBus()
    bus.subscribe(METRIC_ANOMALY, _on_anomaly)
    bus.subscribe(THRESHOLD_EXCEEDED, _on_threshold)

    print("=== 事件总线自检 ===")
    publish_threshold_exceeded(bus, "d1", "temperature", 95.0, ">", 80.0, "high")
    publish_metric_anomaly(bus, "d2", "vibration", 4.2, "突变检测")
    publish_data_added(bus, "ledger", {"设备名称": "机床A", "状态": "运行中"})
    print(f"事件总数: {len(bus.events())}")
    print(f"按类型统计: {bus.count_by_type()}")
    print(f"监听器: {bus.stats()['listeners']}")

    print("\n=== monitor 接入适配器自检（需 solo-agent-kit 可 import）===")
    try:
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "..", "..", "solo-agent-kit"))
        import solo.factory.monitor as mon
        adapter = MonitorEventAdapter(EventBus(), mon)
        r = adapter.ingest("ev-demo", "temperature", 95.0, auto_ticket=True)
        print(f"ingest 告警={len(r['alerts'])} 工单={len(r['tickets'])}")
    except Exception as e:
        print(f"(跳过 monitor 自检，未找到 solo-agent-kit: {e})")
