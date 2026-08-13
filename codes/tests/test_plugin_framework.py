# -*- coding: utf-8 -*-
"""test_plugin_framework.py — 生态插件基础框架测试。

覆盖：加载器扫描/清单解析、生命周期、扩展点注册与调用、冲突检测、安装/移除。
运行：cd codes && python -m pytest tests/test_plugin_framework.py -q
"""
import os
import sys
import tempfile
import shutil

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import plugin_framework as pf  # noqa: E402

# 指向真实示例插件目录
PLUGINS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "plugins")


def test_scan_discovers_example():
    pm = pf.PluginManager(PLUGINS_DIR)
    assert "example_decision" in pm._manifests
    m = pm._manifests["example_decision"]
    assert m["kind"] == "decision"
    assert m["version"] == "1.0.0"
    assert m["entry"] == "plugin.py"


def test_load_register_run_lifecycle():
    pm = pf.PluginManager(PLUGINS_DIR)
    inst = pm.load("example_decision")
    # 扩展点已登记
    assert pm.registry.has("decision", "maintenance_priority")
    assert pm.registry.has("decision", "failure_alert")
    # run
    result = pm.run("example_decision", {
        "records": [{"device_id": "D1", "air_temperature": 310,
                     "tool_wear": 40, "rotational_speed": 1500}]})
    assert result["ok"] is True
    assert result["decisions"][0]["priority"] == "预警"
    # 卸载后扩展点被注销
    pm.unload("example_decision")
    assert not pm.registry.has("decision", "maintenance_priority")


def test_extension_registry_call():
    reg = pf.ExtensionRegistry()
    reg.register("decision", "d1", lambda p: {"n": (p or {}).get("x")},
                 plugin="t")
    assert reg.call("decision", "d1", {"x": 3}) == {"n": 3}
    with pytest.raises(pf.PluginError):
        reg.call("push", "d1", None)   # 未登记类型
    # 冲突检测
    with pytest.raises(pf.PluginError):
        reg.register("decision", "d1", lambda p: None, plugin="t2")
    # 非法 kind
    with pytest.raises(pf.PluginError):
        reg.register("bogus", "x", lambda p: None, plugin="t")
    reg.unregister("decision", "d1")
    assert not reg.has("decision", "d1")


def test_install_remove(tmp_path):
    pm = pf.PluginManager(str(tmp_path))
    # 安装自示例插件目录
    installed = pm.install(PLUGINS_DIR + "/example_decision",
                           name="my_decision")
    assert installed == "my_decision"
    assert "my_decision" in pm._manifests
    # 重复安装拒绝
    with pytest.raises(pf.PluginError):
        pm.install(PLUGINS_DIR + "/example_decision", name="my_decision")
    # 移除
    assert pm.remove("my_decision") is True
    assert "my_decision" not in pm._manifests


def test_install_from_zip(tmp_path):
    import zipfile
    zpath = os.path.join(str(tmp_path), "plug.zip")
    with zipfile.ZipFile(zpath, "w") as z:
        base = os.path.join(str(tmp_path), "src", "zplug")
        for f in ("manifest.json", "plugin.py"):
            os.makedirs(base, exist_ok=True)
            shutil.copy2(os.path.join(PLUGINS_DIR, "example_decision", f),
                         os.path.join(base, f))
            z.write(os.path.join(base, f), f"zplug/{f}")
    pm = pf.PluginManager(str(tmp_path))
    installed = pm.install(zpath)
    # 未指定 --name 时，采用 manifest 的 name 作为插件名
    assert installed == "example_decision"
    assert "example_decision" in pm._manifests


def test_run_without_params_reports_thresholds():
    pm = pf.PluginManager(PLUGINS_DIR)
    result = pm.run("example_decision", {})
    assert result["ok"] is True
    assert "thresholds" in result
