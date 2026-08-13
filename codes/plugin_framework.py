#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""plugin_framework.py — 生态插件基础框架（第三方可开发插件扩展系统）。

设计目标：让第三方开发者不改主程序，就能通过「插件」为系统新增能力。
对齐本项目「纯标准库 · 零依赖 · 确定性优先」的工程纪律：核心框架零第三方依赖，
可完全离线运行。

## 插件 = 一个目录

    plugins/
      <插件名>/
        manifest.json      # 插件清单：name/kind/version/entry/provides
        <entry>.py         # 入口模块，实现 BasePlugin 子类或约定接口

### manifest.json 字段

    {
      "name": "example_decision",      # 唯一插件名，同时是目录名
      "kind": "decision",              # 主类型：decision/data_source/push/template
      "version": "1.0.0",              # 插件版本号
      "entry": "plugin.py",            # 入口文件名（相对插件目录）
      "description": "…",              # 一句话说明
      "author": "…",                   # 可选
      "provides": ["threshold_check"]  # 提供的扩展点 id 列表（可选）
    }

## 扩展点类型（ExtensionRegistry）

    decision     — 决策规则：输入事实/参数，输出决策结论
    data_source  — 数据源：按查询产出结构化数据行
    push         — 推送通道：把消息推送到外部系统（钉钉/微信/Webhook 等）
    template     — 模板渲染：按变量渲染文本

## 生命周期

插件入口模块定义一个 `Plugin` 类（继承 BasePlugin 或实现相同接口），
按 load → register → run → unload 四个阶段被框架调度。

    class Plugin(BasePlugin):
        name = "example_decision"
        kind = "decision"
        version = "1.0.0"

        def load(self, ctx):        # 初始化，可注册扩展点
            pass

        def register(self, reg):    # 把本插件提供的扩展点登记进注册表
            reg.register("decision", "threshold_check", self.check)

        def run(self, params):      # 执行插件主逻辑
            return {"ok": True}

        def unload(self, ctx):      # 释放资源
            pass

扩展点 handler 签名统一为 `fn(params: dict) -> 任意可 JSON 序列化结果`。
"""

import importlib.util
import json
import os
import shutil
import sys
import zipfile

# 允许的扩展点类型
KINDS = ("decision", "data_source", "push", "template")

# 插件清单必需字段
_MANIFEST_REQUIRED = ("name", "kind", "version", "entry")


class PluginError(Exception):
    """插件框架统一异常。"""


# ─────────────────────────────────────────────────────────────
# 扩展点注册表
# ─────────────────────────────────────────────────────────────
class ExtensionRegistry:
    """扩展点注册表：按 (kind, id) 登记可被外部调用的能力。

    第三方插件通过它对外暴露自己的函数；系统核心或其他插件通过
    `reg.call(kind, id, params)` 调用这些能力。多实例的注册表是安全的：
    注册/调用/注销都以 (kind, id) 为键，互不污染。
    """

    def __init__(self):
        # {(kind, id): {"handler": fn, "meta": dict, "plugin": name}}
        self._points = {}

    def register(self, kind, ext_id, handler, plugin="", meta=None):
        """登记一个扩展点。kind 必须是 KINDS 之一，ext_id 在同类下唯一。

        handler 必须是可调用对象，统一签名 handler(params: dict) -> result。
        同一 (kind, id) 重复注册视为冲突（覆盖需先 unregister）。
        """
        if kind not in KINDS:
            raise PluginError(f"非法扩展点类型 '{kind}'，允许: {KINDS}")
        if not callable(handler):
            raise PluginError(f"扩展点 {kind}/{ext_id} 的 handler 不可调用")
        key = (kind, ext_id)
        if key in self._points:
            raise PluginError(
                f"扩展点 {kind}/{ext_id} 已被插件 '{self._points[key]['plugin']}' 占用"
            )
        self._points[key] = {"handler": handler, "meta": meta or {},
                             "plugin": plugin}

    def unregister(self, kind, ext_id):
        """按 (kind, id) 注销扩展点。"""
        self._points.pop((kind, ext_id), None)

    def unregister_plugin(self, plugin):
        """注销某插件登记的全部扩展点（卸载插件时调用）。"""
        for key in [k for k, v in self._points.items() if v["plugin"] == plugin]:
            del self._points[key]

    def get(self, kind, ext_id):
        """取扩展点元信息，不存在返回 None。"""
        return self._points.get((kind, ext_id))

    def has(self, kind, ext_id):
        return (kind, ext_id) in self._points

    def call(self, kind, ext_id, params=None):
        """调用扩展点，返回 handler 的执行结果。未登记则抛 PluginError。"""
        point = self._points.get((kind, ext_id))
        if point is None:
            raise PluginError(f"扩展点不存在: {kind}/{ext_id}")
        return point["handler"](params)

    def list(self, kind=None):
        """列出扩展点。kind 传 None 则全部。返回按 (kind,id) 排序的列表。"""
        items = [
            {"kind": k, "id": i, "plugin": v["plugin"], "meta": v["meta"]}
            for (k, i), v in self._points.items()
            if kind is None or k == kind
        ]
        items.sort(key=lambda x: (x["kind"], x["id"]))
        return items


# ─────────────────────────────────────────────────────────────
# 插件基类
# ─────────────────────────────────────────────────────────────
class BasePlugin:
    """插件基类。第三方插件入口模块的 Plugin 类继承它，或实现相同接口。

    接口约定：
      name / kind / version  类属性，必须给出（与 manifest 一致最佳）
      load(ctx)              [可选] 初始化
      register(reg)          [可选] 向注册表登记扩展点
      run(params)            [必须] 执行插件主逻辑
      unload(ctx)            [可选] 释放资源
    """

    name = "plugin"
    kind = "decision"
    version = "0.0.0"

    def load(self, ctx):
        """初始化钩子。ctx 提供 plugins_dir/registry/plugin_dir/log 等。"""

    def register(self, reg):
        """登记扩展点钩子。reg 是 ExtensionRegistry。"""

    def run(self, params):
        """执行插件主逻辑，返回可 JSON 序列化结果。"""
        raise NotImplementedError(f"插件 {self.name} 未实现 run()")

    def unload(self, ctx):
        """释放资源钩子。"""


# ─────────────────────────────────────────────────────────────
# 插件加载器
# ─────────────────────────────────────────────────────────────
class PluginManager:
    """插件管理器：扫描 plugins/ 目录、解析 manifest、加载/卸载插件。

    用法：
        pm = PluginManager(plugins_dir)
        pm.scan()                 # 扫描目录，发现可用插件
        pm.load_all()             # 加载全部插件（load + register）
        pm.run("example_decision", params)   # 执行某插件
        pm.unload("example_decision")
    """

    def __init__(self, plugins_dir, registry=None, auto_scan=True):
        self.plugins_dir = plugins_dir
        self.registry = registry or ExtensionRegistry()
        self._loaded = {}          # name -> 插件实例
        self._manifests = {}       # name -> manifest dict
        self._modules = {}         # name -> 已加载的模块对象
        if auto_scan:
            self.scan()

    # ── 扫描 ─────────────────────────────────────────────
    def scan(self):
        """扫描 plugins/ 目录，解析每个子目录的 manifest.json。

        返回 {name: manifest}。跳过无 manifest 或 manifest 非法的目录，
        并把错误记录到 self.errors（不抛异常，保证扫描健壮）。
        """
        self._manifests = {}
        self.errors = []
        if not os.path.isdir(self.plugins_dir):
            return self._manifests
        for entry in sorted(os.listdir(self.plugins_dir)):
            pdir = os.path.join(self.plugins_dir, entry)
            if not os.path.isdir(pdir) or entry.startswith((".", "_")):
                continue
            mpath = os.path.join(pdir, "manifest.json")
            if not os.path.exists(mpath):
                self.errors.append(f"{entry}: 缺 manifest.json，跳过")
                continue
            try:
                with open(mpath, encoding="utf-8") as f:
                    manifest = json.load(f)
                self._validate_manifest(manifest, pdir)
                self._manifests[manifest["name"]] = manifest
            except PluginError as e:
                self.errors.append(f"{entry}: {e}")
            except (json.JSONDecodeError, OSError) as e:
                self.errors.append(f"{entry}: manifest 解析失败: {e}")
        return self._manifests

    def _validate_manifest(self, m, pdir):
        for field in _MANIFEST_REQUIRED:
            if not m.get(field):
                raise PluginError(f"manifest 缺必需字段 '{field}'")
        if m["kind"] not in KINDS:
            raise PluginError(f"kind '{m['kind']}' 非法，允许: {KINDS}")
        # name 必须与目录名一致，防止混乱
        if m["name"] != os.path.basename(os.path.normpath(pdir)):
            raise PluginError(
                f"插件名 '{m['name']}' 与目录名 '{os.path.basename(pdir)}' 不一致"
            )
        entry = os.path.join(pdir, m["entry"])
        if not os.path.exists(entry):
            raise PluginError(f"入口文件不存在: {m['entry']}")

    # ── 加载 ─────────────────────────────────────────────
    def load(self, name):
        """加载单个插件：解析清单 → 动态导入入口模块 → 实例化 → load → register。

        返回插件实例。已加载则直接返回。失败抛 PluginError（附详细原因）。
        """
        if name in self._loaded:
            return self._loaded[name]
        if name not in self._manifests:
            raise PluginError(f"插件未发现: {name}（先 scan 或 install）")
        m = self._manifests[name]
        pdir = os.path.join(self.plugins_dir, name)
        entry = os.path.join(pdir, m["entry"])

        # 动态导入入口模块（可跨平台，避免依赖 sys.path 污染）
        try:
            spec = importlib.util.spec_from_file_location(f"plugin_{name}", entry)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            raise PluginError(f"插件 {name} 入口导入失败: {e}") from e
        self._modules[name] = module

        # 实例化 Plugin 类（继承 BasePlugin 或实现同名接口）
        cls = getattr(module, "Plugin", None)
        if cls is None:
            raise PluginError(
                f"插件 {name} 入口缺少 'Plugin' 类（需定义 Plugin 或继承 BasePlugin）"
            )
        try:
            inst = cls()
        except Exception as e:
            raise PluginError(f"插件 {name} 实例化失败: {e}") from e

        ctx = PluginContext(self.plugins_dir, self.registry, pdir, m, module)
        try:
            if hasattr(inst, "load"):
                inst.load(ctx)
            if hasattr(inst, "register"):
                inst.register(self.registry)
        except PluginError:
            raise
        except Exception as e:
            raise PluginError(f"插件 {name} 初始化失败: {e}") from e

        self._loaded[name] = inst
        return inst

    def load_all(self):
        """加载全部已发现插件。逐个容错：单个失败记 errors，不中断其余。"""
        for name in list(self._manifests):
            try:
                self.load(name)
            except PluginError as e:
                self.errors.append(f"{name}: {e}")
        return list(self._loaded)

    # ── 运行 ─────────────────────────────────────────────
    def run(self, name, params=None):
        """加载并执行插件主逻辑 run(params)。返回结果。"""
        inst = self.load(name)
        try:
            result = inst.run(params or {})
        except Exception as e:
            raise PluginError(f"插件 {name} 运行失败: {e}") from e
        return result

    # ── 卸载 ─────────────────────────────────────────────
    def unload(self, name):
        """卸载插件：unload → 注销其扩展点 → 从已加载表移除。"""
        inst = self._loaded.pop(name, None)
        module = self._modules.pop(name, None)
        if inst is not None:
            ctx = PluginContext(self.plugins_dir, self.registry,
                                os.path.join(self.plugins_dir, name),
                                self._manifests.get(name, {}), module)
            try:
                if hasattr(inst, "unload"):
                    inst.unload(ctx)
            except Exception:
                pass
        self.registry.unregister_plugin(name)

    # ── 列表 / 安装 / 移除 ─────────────────────────────
    def list(self):
        """返回所有已发现插件的清单信息列表（含是否已加载）。"""
        out = []
        for name, m in self._manifests.items():
            out.append({
                "name": m.get("name"), "kind": m.get("kind"),
                "version": m.get("version"), "entry": m.get("entry"),
                "description": m.get("description", ""),
                "author": m.get("author", ""),
                "provides": m.get("provides", []),
                "loaded": name in self._loaded,
            })
        out.sort(key=lambda x: x["name"])
        return out

    def install(self, source, name=None, force=False):
        """安装插件：把 source（本地目录 或 zip/tar.gz 归档）复制进 plugins/。

        - source 为目录：整目录复制，以 manifest 的 name 命名目标目录。
        - source 为归档：解压到临时目录再校验 manifest，按 name 放置。
        - name 覆盖：显式指定安装后的插件名（目录名）。
        返回安装后的插件名。manifest 非法或同名已存在（非 force）则抛 PluginError。
        """
        tmp = None
        try:
            if os.path.isdir(source):
                src_dir = os.path.abspath(source)
            elif os.path.isfile(source) and (source.endswith(".zip")
                                             or source.endswith(".tar.gz")
                                             or source.endswith(".tgz")):
                tmp = os.path.join(self.plugins_dir, f"._install_tmp_{os.getpid()}")
                os.makedirs(tmp, exist_ok=True)
                if source.endswith(".zip"):
                    with zipfile.ZipFile(source) as z:
                        z.extractall(tmp)
                else:
                    import tarfile
                    with tarfile.open(source, "r:gz") as t:
                        t.extractall(tmp)
                # 归档可能解出单目录或散落文件，找到含 manifest.json 的顶层
                candidates = []
                for root, dirs, files in os.walk(tmp):
                    if "manifest.json" in files:
                        candidates.append(root)
                if not candidates:
                    raise PluginError(f"归档 {source} 中未找到 manifest.json")
                # 取最短路径 = 最外层含 manifest 的目录
                src_dir = min(candidates, key=lambda p: len(p.split(os.sep)))
            else:
                raise PluginError(
                    f"无法识别的插件来源（需为目录或 .zip/.tar.gz）: {source}"
                )

            mpath = os.path.join(src_dir, "manifest.json")
            if not os.path.exists(mpath):
                raise PluginError(f"源目录缺 manifest.json: {src_dir}")
            with open(mpath, encoding="utf-8") as f:
                manifest = json.load(f)
            pname = name or manifest.get("name")
            if not pname:
                raise PluginError("无法确定插件名（manifest 缺 name 且未指定 --name）")
            # 校验：kind/必需字段/entry 存在（entry 相对真实源目录校验）
            for field in _MANIFEST_REQUIRED:
                if not manifest.get(field):
                    raise PluginError(f"manifest 缺必需字段 '{field}'")
            if manifest["kind"] not in KINDS:
                raise PluginError(f"kind '{manifest['kind']}' 非法，允许: {KINDS}")
            entry_path = os.path.join(src_dir, manifest["entry"])
            if not os.path.exists(entry_path):
                raise PluginError(f"入口文件不存在: {manifest['entry']}")
            if not pname or os.path.basename(pname) != pname \
               or pname in (".", ".."):
                raise PluginError(f"非法插件名: {pname!r}")

            dest = os.path.join(self.plugins_dir, pname)
            if os.path.exists(dest):
                if not force:
                    raise PluginError(f"插件 '{pname}' 已存在（用 --force 覆盖）")
                shutil.rmtree(dest)

            # 复制：跳过源目录内的 __pycache__
            shutil.copytree(src_dir, dest,
                            ignore=shutil.ignore_patterns("__pycache__"))
            # 若以别名安装，同步改写目标 manifest 的 name 字段（保持 name==目录名）
            if manifest.get("name") != pname:
                _m = dict(manifest)
                _m["name"] = pname
                with open(os.path.join(dest, "manifest.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(_m, f, ensure_ascii=False, indent=2)
            self.scan()  # 重新扫描
            return pname
        finally:
            if tmp and os.path.isdir(tmp):
                shutil.rmtree(tmp, ignore_errors=True)

    def remove(self, name):
        """移除（卸载并删除）插件目录。返回是否删除成功。"""
        self.unload(name)
        pdir = os.path.join(self.plugins_dir, name)
        if not os.path.isdir(pdir):
            return False
        shutil.rmtree(pdir)
        self.scan()
        return True


class PluginContext:
    """插件运行上下文：把目录、注册表、清单、模块句柄传给插件。"""

    def __init__(self, plugins_dir, registry, plugin_dir, manifest, module):
        self.plugins_dir = plugins_dir
        self.registry = registry
        self.plugin_dir = plugin_dir
        self.manifest = manifest
        self.module = module


# ─────────────────────────────────────────────────────────────
# CLI 处理函数（由 run.py 的 `plugin` 子命令调用）
# ─────────────────────────────────────────────────────────────
def make_manager(plugins_dir):
    """构造 PluginManager（供 CLI 统一入口）。"""
    return PluginManager(plugins_dir)


def cmd_plugin(args):
    """run.py plugin 子命令的派发入口。

    用法：
      python run.py plugin list [kind]
      python run.py plugin run <插件名> ['<json 参数>']
      python run.py plugin install <目录|归档> [--name 别名] [--force]
      python run.py plugin remove <插件名>
      python run.py plugin ext <kind> <id> ['<json 参数>']   # 调已登记扩展点
    """
    plugins_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "plugins")
    if not args:
        print(_PLUGIN_USAGE)
        return 0
    cmd = args[0]

    if cmd == "list":
        pm = make_manager(plugins_dir)
        kind = args[1] if len(args) > 1 else None
        if kind and kind not in KINDS:
            print(f"非法类型 '{kind}'，允许: {KINDS}")
            return 1
        rows = pm.list()
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        print(f"\n[插件] plugins 目录: {plugins_dir}")
        if pm.errors:
            for e in pm.errors:
                print(f"  ⚠️ {e}")
        if not rows:
            print("  (无已发现插件)")
        else:
            for r in rows:
                state = "●" if r["loaded"] else "○"
                print(f"  {state} {r['name']:<24} v{r['version']:<8} "
                      f"[{r['kind']}]  {r['description']}")
        # 扩展点总览
        exts = pm.registry.list()
        if exts:
            print(f"\n  已登记扩展点 {len(exts)} 个:")
            for e in exts:
                print(f"    {e['kind']}/{e['id']:<24} <- {e['plugin']}")
        return 0

    if cmd == "run":
        if len(args) < 2:
            print("用法: python run.py plugin run <插件名> ['<json 参数>']")
            return 1
        pm = make_manager(plugins_dir)
        params = _parse_json(args[2]) if len(args) > 2 else {}
        try:
            result = pm.run(args[1], params)
        except PluginError as e:
            print(f"❌ {e}")
            return 1
        print(f"\n[插件] {args[1]} 运行结果:")
        print(_fmt_result(result))
        return 0

    if cmd == "ext":
        if len(args) < 3:
            print("用法: python run.py plugin ext <kind> <id> ['<json 参数>']")
            return 1
        kind, ext_id = args[1], args[2]
        if kind not in KINDS:
            print(f"非法类型 '{kind}'，允许: {KINDS}")
            return 1
        # 先加载全部插件，确保扩展点已登记
        pm = make_manager(plugins_dir)
        pm.load_all()
        params = _parse_json(args[3]) if len(args) > 3 else {}
        try:
            result = pm.registry.call(kind, ext_id, params)
        except PluginError as e:
            print(f"❌ {e}")
            return 1
        print(f"\n[插件] 调用扩展点 {kind}/{ext_id} 结果:")
        print(_fmt_result(result))
        return 0

    if cmd == "install":
        if len(args) < 2:
            print("用法: python run.py plugin install <目录|归档> "
                  "[--name 别名] [--force]")
            return 1
        source = args[1]
        name = None
        force = False
        i = 2
        while i < len(args):
            if args[i] == "--name" and i + 1 < len(args):
                name = args[i + 1]; i += 2
            elif args[i] == "--force":
                force = True; i += 1
            else:
                print(f"未知参数: {args[i]}"); return 1
        pm = make_manager(plugins_dir)
        try:
            installed = pm.install(source, name=name, force=force)
        except PluginError as e:
            print(f"❌ 安装失败: {e}")
            return 1
        print(f"✅ 插件已安装: {installed}")
        # 安装后立即加载验证
        try:
            pm.load(installed)
            print(f"  加载成功: {installed} v{pm.list() and next(r for r in pm.list() if r['name']==installed)['version']}")
        except PluginError as e:
            print(f"  ⚠️ 安装完成但加载失败: {e}")
        return 0

    if cmd == "remove":
        if len(args) < 2:
            print("用法: python run.py plugin remove <插件名>")
            return 1
        pm = make_manager(plugins_dir)
        if not os.path.isdir(os.path.join(plugins_dir, args[1])):
            print(f"❌ 插件不存在: {args[1]}")
            return 1
        pm.remove(args[1])
        print(f"🗑️ 插件已移除: {args[1]}")
        return 0

    print(_PLUGIN_USAGE)
    return 0


def _parse_json(text):
    """宽松解析 JSON 参数：失败时报出原因并退出 CLI。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise PluginError(f"参数不是合法 JSON: {e}") from e


def _fmt_result(result, indent=2):
    """格式化插件运行结果（dict 转 JSON 缩进，否则 str）。"""
    if isinstance(result, (dict, list)):
        try:
            return json.dumps(result, ensure_ascii=False, indent=indent)
        except (TypeError, ValueError):
            pass
    return str(result)


_PLUGIN_USAGE = """\
[插件] 生态插件扩展 — 用法:
  python run.py plugin list [kind]                 # 列出插件 (kind: decision/data_source/push/template)
  python run.py plugin run <插件名> ['<json>']      # 加载并运行插件主逻辑
  python run.py plugin ext <kind> <id> ['<json>']   # 调用已登记扩展点
  python run.py plugin install <目录|归档> [--name 别名] [--force]
  python run.py plugin remove <插件名>
"""
