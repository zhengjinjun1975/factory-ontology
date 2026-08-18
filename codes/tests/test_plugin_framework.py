# -*- coding: utf-8 -*-
"""test_plugin_framework.py — 生态插件基础框架测试。

覆盖：加载器扫描/清单解析、生命周期、扩展点注册与调用、冲突检测、安装/移除。
运行：cd codes && python -m pytest tests/test_plugin_framework.py -q
"""
import json
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


# ─────────────────────────────────────────────────────────────
# 遗留补修：CLI 传参 JSON 引号转义兼容（run.py plugin ext 传参）
# ─────────────────────────────────────────────────────────────

def test_parse_json_standard():
    assert pf._parse_json('{"industry": "manufacturing"}') == \
        {"industry": "manufacturing"}


def test_parse_json_single_quoted():
    """单引号 JSON（用户手写/Python 字面量）应被解析。"""
    assert pf._parse_json("{'industry': 'manufacturing'}") == \
        {"industry": "manufacturing"}


def test_parse_json_bare_shell_stripped():
    """外壳剥离双引号后的裸 JSON（CMD/bash）应被修复解析。"""
    assert pf._parse_json("{industry:manufacturing}") == \
        {"industry": "manufacturing"}
    assert pf._parse_json("{industry:manufacturing, stock:5, ok:true}") == \
        {"industry": "manufacturing", "stock": 5, "ok": True}


def test_parse_json_backslash_escaped():
    """CMD 保留反斜杠转义 `\\\"` 应还原为双引号。"""
    assert pf._parse_json('{\\"industry\\":\\"manufacturing\\"}') == \
        {"industry": "manufacturing"}


def test_parse_json_nested_bare():
    """嵌套裸 JSON 也应被修复。"""
    assert pf._parse_json("{lead_time_days:14, nested:{safety_stock:20}}") == \
        {"lead_time_days": 14, "nested": {"safety_stock": 20}}


def test_parse_json_invalid_raises():
    with pytest.raises(pf.PluginError):
        pf._parse_json("not json at all")
    with pytest.raises(pf.PluginError):
        pf._parse_json("")


def test_repair_bare_json_preserves_quoted_and_numbers():
    r = pf._repair_bare_json('{"industry": "manufacturing", stock:5}')
    assert json.loads(r) == {"industry": "manufacturing", "stock": 5}


def test_inventory_decision_ext_runs_with_params():
    """run.py plugin ext 调 inventory 扩展点并传参数——真实跑通。"""
    import subprocess
    codes = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    run_py = os.path.join(codes, "run.py")
    # 裸 JSON（模拟 CMD 剥离双引号后经 run.py 传参）
    proc = subprocess.run(
        [sys.executable, run_py, "plugin", "ext",
         "decision", "inventory", "{industry:manufacturing, stock:5}"],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert '"ok": true' in out
    assert "补货" in out
    assert "V01" in out


def test_inventory_decision_standalone_runs():
    """插件可 python plugin.py 独立运行（遗留路径 bug 修复）。"""
    import subprocess
    entry = os.path.join(PLUGINS_DIR, "inventory_decision", "plugin.py")
    proc = subprocess.run([sys.executable, entry],
                          capture_output=True, text=True,
                          encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert '"ok": true' in proc.stdout
