# -*- coding: utf-8 -*-
"""event_bus 事件总线测试（开源侧：事件模型/订阅/发布 + monitor 接入适配器）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))  # codes/

import event_bus  # noqa: E402


def test_publish_subscribe_dispatch():
    bus = event_bus.EventBus()
    got = []
    bus.subscribe(event_bus.METRIC_ANOMALY, lambda ev: got.append(("anomaly", ev.payload)))
    bus.subscribe(event_bus.THRESHOLD_EXCEEDED, lambda ev: got.append(("threshold", ev.payload)))
    bus.subscribe("*", lambda ev: got.append(("all", ev.payload)))

    r = bus.publish(event_bus.Event(event_bus.THRESHOLD_EXCEEDED, "monitor",
                                    {"device_id": "d1", "metric": "temperature",
                                     "value": 95, "op": ">", "threshold": 80}))
    assert r["handlers"] == 2  # threshold + wildcard
    types = [t for t, _ in got]
    assert "threshold" in types and "all" in types


def test_event_helpers_and_log():
    bus = event_bus.EventBus()
    event_bus.publish_threshold_exceeded(bus, "d1", "temperature", 95, ">", 80, "high")
    event_bus.publish_metric_anomaly(bus, "d2", "vibration", 4.2, "突变检测")
    event_bus.publish_data_added(bus, "设备台账", {"设备名称": "机床A", "状态": "运行中"})

    cnt = bus.count_by_type()
    assert cnt["threshold.exceeded"] == 1
    assert cnt["metric.anomaly"] == 1
    assert cnt["data.added"] == 1
    assert len(bus.events()) == 3
    assert bus.events(event_type="data.added")[0]["payload"]["entity"] == "设备台账"


def test_monitor_adapter_publishes_events():
    """结合 monitor：指标写入 → 数据新增/阈值超限/工单事件。"""
    solo = os.path.normpath(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "..", "solo-agent-kit"))
    if not os.path.exists(solo):
        import pytest
        pytest.skip("solo-agent-kit 未找到")
    os.environ["SOLO_MONITOR_DIR"] = tempfile.mkdtemp()
    sys.path.insert(0, solo)
    import solo.factory.monitor as mon  # noqa: E402

    bus = event_bus.EventBus()
    ad = event_bus.MonitorEventAdapter(bus, mon)
    ad.store.set_rule("d1", "temperature", ">", 80, level="high")
    r = ad.ingest("d1", "temperature", 95.0, auto_ticket=True)

    assert len(r["alerts"]) >= 1
    assert len(r["tickets"]) >= 1
    cnt = bus.count_by_type()
    assert cnt["data.added"] >= 1
    assert cnt["threshold.exceeded"] == 1
    assert cnt["ticket.created"] == 1
    # 工单状态机已推进
    assert ad.store.alerts("firing")
