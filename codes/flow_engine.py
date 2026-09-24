#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flow_engine.py — 通用流程引擎（编排上半截：流程定义 + 控制流 + 预设）。

**纪律：不新增执行体系。** 本模块不引入任务队列 / 调度器 / worker 进程，
只做三件事：
  1. 读流程定义（`codes/flows/*.json`，风格对齐 plugin_framework 的 manifest）；
  2. 按定义走控制流，**每一步的能力调用都走 `plugin_framework` 的
     `ExtensionRegistry.call(kind, id, params)`**（复用既有能力与插件扩展点）；
  3. 每步结果写 `event_bus`，全过程写 `audit_chain`（可回放）。

只做**通用流程引擎**，不含任何行业口径与甲方业务规则。

## 流程定义 JSON

    {
      "flow_id": "demo",              # 必须与文件名一致（同 plugin: name==目录名）
      "name": "演示流程",             # 人读名
      "version": "1.0.0",
      "preset": true,                  # 可选；true 表示可作为预设卡（配合 presets.json）
      "entry": "s1",                   # 可选；缺省=无入边的唯一步
      "steps": [
        # 普通步：引用已有能力/插件扩展点
        {"id": "s1", "name": "取数", "kind": "data_source", "ref": "const_rows",
         "params": {"n": 4}, "requires": ["up_x"]},
        # 控制流四件（见下）
        {"id": "p1", "control": "parallel", "members": [
            {"id": "a", "kind": "decision", "ref": "inventory", "params": {...}}]},
        {"id": "b1", "control": "branch",
         "cases": [{"when": "steps.s1.count >= 4", "goto": "s2"}], "default": "s5"},
        {"id": "l1", "control": "loop", "body": [ {...} ],
         "until": "iter >= 3", "max_iterations": 5},
        {"id": "g1", "control": "gate", "evidence_from": ["s2"],
         "pass_when": "steps.s2.passed == true", "on_pass": "s5", "on_reject": "s2",
         "reject_reason": "上游证据不足"}
      ],
      "edges": [{"from": "s1", "to": "s2"}, {"from": "s2", "to": "g1"}, ...]
    }

`kind` ∈ plugin_framework.KINDS（decision/data_source/push/template）。

## 控制流四件

  parallel  同列并行：`members` 内子步并发执行（threading），全部完成后汇合。
  branch    条件分支：按 `cases[].when` 表达式选 `goto`，都不中走 `default`。
  loop      循环：重复跑 `body` 直到 `until` 为真或到 `max_iterations`。
  gate      门控：按 `evidence_from` 的上游证据用 `pass_when` 判放行/打回；
            **打回回到哪一步由 `on_reject` 明写在定义里（可读）**。

## 结果状态（每步）

  success   能力正常返回
  no_input  上游无输入 → reason="没有输入"，**不判整条失败**
  failed    能力抛异常或返回 `{"ok": false}` → 记录真实原因，流程**停在该步**
  rejected  门控判打回（回到 on_reject 指定上游步）

能力返回约定（引擎据此判定 no_input）：
  · 返回 `{"no_input": true, ...}`  或 `None`  → 视为「没有输入」
  · 返回 `{"ok": false, "error": "..."}`       → 视为失败（真实原因进 error）

## 表达式（branch/loop/gate 的 when/until/pass_when）

受限安全求值（AST 白名单，无 eval 内建、无函数调用）：
  可用 `steps.<id>.<字段>` / `steps["<id>"]["字段"]`、`params.<字段>`、
  `attempts.<id>`、`iter`，以及比较/布尔/算术运算。例：`steps.s2.total >= 4`。

  注意：字段名为 Python 关键字时（如 `pass`）不能点号访问，须用下标
  `steps.s2["pass"]`（引擎内 review/threshold 输出统一用 `passed` 规避此坑）。

## 事件（写 event_bus）

  flow.started / flow.step / flow.gate / flow.finished

## 回放（audit_chain）

每一步经 `AuditChain.record_decision(category="flow", ...)` 落链；
`replay(audit, flow_id)` 按时间线取回该流程的全部步骤记录。
"""
from __future__ import annotations

import ast
import json
import os
import re
import threading
import traceback
from datetime import datetime

# 复用既有框架（不重造）：扩展点注册表 + 事件总线 + 审计链
from plugin_framework import KINDS, ExtensionRegistry, PluginManager, PluginError
from event_bus import Event, EventBus

# ── 流程事件类型 ───────────────────────────────────────
FLOW_STARTED = "flow.started"
FLOW_STEP = "flow.step"
FLOW_GATE = "flow.gate"
FLOW_FINISHED = "flow.finished"

CONTROLS = ("parallel", "branch", "loop", "gate")

# 每步允许出现的状态
STATUS_OK = "success"
STATUS_NO_INPUT = "no_input"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"


class FlowError(Exception):
    """流程定义/加载错误（携带真实原因，供接口如实输出）。"""


# ═══════════════════════════════════════════════════════════════════
# 1. 受限安全表达式求值
# ═══════════════════════════════════════════════════════════════════
class _Box(dict):
    """dict 的属性访问包装（steps.s2.total）。缺键返回 _MISSING 语义：空 dict。"""

    def __getattr__(self, k):
        if k.startswith("__"):
            raise AttributeError(k)
        v = self.get(k)
        return _boxify(v)

    def __getitem__(self, k):
        return _boxify(dict.get(self, k))


def _boxify(v):
    if isinstance(v, dict) and not isinstance(v, _Box):
        return _Box(v)
    if isinstance(v, list):
        return [_boxify(x) for x in v]
    return v


_BIN_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
            ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
            ast.Mod: lambda a, b: a % b}
_CMP_OPS = {ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
            ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
            ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b}


def safe_eval(expr, ctx):
    """受限求值表达式。只支持白名单语法；非法语法抛 FlowError（不静默）。

    兼容 JSON 字面量：`true/false/null` 自动归一为 `True/False/None`。
    """
    if isinstance(expr, bool):
        return expr
    if not isinstance(expr, str):
        raise FlowError(f"表达式须为 bool 或字符串，实得 {type(expr).__name__}")
    src = re.sub(r"\btrue\b", "True", expr)
    src = re.sub(r"\bfalse\b", "False", src)
    src = re.sub(r"\bnull\b", "None", src)
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as e:
        raise FlowError(f"表达式语法错误: {expr!r} ({e})")
    return _eval_node(tree.body, ctx)


def _eval_node(node, ctx):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in ctx:
            raise FlowError(f"表达式引用了未知名称: {node.id}")
        return ctx[node.id]
    if isinstance(node, ast.Attribute):
        base = _eval_node(node.value, ctx)
        if isinstance(base, dict):
            return base.get(node.attr)
        raise FlowError(f"表达式中 {node.attr} 的基不是对象: {type(base).__name__}")
    if isinstance(node, ast.Subscript):
        base = _eval_node(node.value, ctx)
        key = _eval_node(node.slice, ctx)
        try:
            return base[key] if isinstance(base, (dict, list)) else None
        except (KeyError, IndexError, TypeError):
            return None
    if isinstance(node, ast.BoolOp):
        vals = [_eval_node(v, ctx) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_node(node.operand, ctx)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, ctx)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval_node(comp, ctx)
            fn = _CMP_OPS.get(type(op))
            if fn is None:
                raise FlowError(f"不支持的比较运算: {type(op).__name__}")
            if not fn(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BinOp):
        fn = _BIN_OPS.get(type(node.op))
        if fn is None:
            raise FlowError(f"不支持的算术运算: {type(node.op).__name__}")
        return fn(_eval_node(node.left, ctx), _eval_node(node.right, ctx))
    raise FlowError(f"表达式含不支持的语法: {type(node).__name__}")


# ═══════════════════════════════════════════════════════════════════
# 2. 流程注册表（扫描 / 校验 / 预设）
# ═══════════════════════════════════════════════════════════════════
def default_flows_dir():
    """流程目录：`FLOWS_DIR` 环境变量优先，否则 codes/flows。"""
    env = os.environ.get("FLOWS_DIR")
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "flows")


class FlowRegistry:
    """扫描 flows/ 目录，解析并校验流程定义；收集加载失败的真实原因（不抛）。"""

    PRESETS_FILE = "presets.json"

    def __init__(self, flows_dir=None, presets_file=None):
        self.flows_dir = flows_dir or default_flows_dir()
        self.presets_file = presets_file or os.path.join(
            self.flows_dir, self.PRESETS_FILE)
        self._flows = {}        # flow_id -> 规范化定义
        self._presets = []      # 配置里的预设卡列表
        self._preset_error = None
        self.errors = []        # 加载失败的真实原因
        self.scan()

    # ── 扫描 ──────────────────────────────────────────
    def scan(self):
        self._flows = {}
        self.errors = []
        if not os.path.isdir(self.flows_dir):
            self.errors.append(f"流程目录不存在: {self.flows_dir}")
            self._presets = []
            return self._flows
        for entry in sorted(os.listdir(self.flows_dir)):
            if not entry.endswith(".json") or entry.startswith((".", "_")):
                continue
            if entry == self.PRESETS_FILE:
                continue
            path = os.path.join(self.flows_dir, entry)
            stem = entry[:-5]
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                flow = self._validate(raw, stem, path)
                self._flows[flow["flow_id"]] = flow
            except FlowError as e:
                self.errors.append(f"{entry}: {e}")
            except json.JSONDecodeError as e:
                self.errors.append(f"{entry}: JSON 解析失败: {e}")
            except OSError as e:
                self.errors.append(f"{entry}: 读取失败: {e}")
        self._load_presets()
        return self._flows

    def _load_presets(self):
        """预设卡配置（放配置不放代码）。缺失不算错误，只标记。"""
        self._presets = []
        self._preset_error = None
        if not os.path.exists(self.presets_file):
            self._preset_error = f"预设配置缺失: {self.presets_file}"
            return
        try:
            with open(self.presets_file, encoding="utf-8") as f:
                cfg = json.load(f)
            items = cfg.get("presets")
            if not isinstance(items, list):
                raise FlowError("presets.json 缺 'presets' 列表")
            for it in items:
                if not isinstance(it, dict) or not it.get("flow_id"):
                    raise FlowError(f"预设项非法（需含 flow_id）: {it!r}")
                self._presets.append(it)
        except (FlowError, json.JSONDecodeError, OSError) as e:
            self._presets = []
            self._preset_error = f"预设配置解析失败: {e}"

    # ── 校验 ──────────────────────────────────────────
    def _validate(self, raw, stem, path):
        if not isinstance(raw, dict):
            raise FlowError("流程定义必须是 JSON 对象")
        fid = raw.get("flow_id")
        if not fid or not isinstance(fid, str):
            raise FlowError("缺必需字段 'flow_id'")
        if fid != stem:
            raise FlowError(f"flow_id '{fid}' 与文件名 '{stem}' 不一致")
        if not raw.get("name"):
            raise FlowError("缺必需字段 'name'")
        steps = raw.get("steps")
        if not isinstance(steps, list) or not steps:
            raise FlowError("缺必需字段 'steps'（非空列表）")
        edges = raw.get("edges", [])
        if not isinstance(edges, list):
            raise FlowError("'edges' 必须是列表")

        norm_steps = {}
        for s in steps:
            sid = self._validate_step(s, parent="")
            if sid in norm_steps:
                raise FlowError(f"步骤 id 重复: '{sid}'")
            norm_steps[sid] = s
        if not norm_steps:
            raise FlowError("steps 为空")

        for e in edges:
            if not isinstance(e, dict):
                raise FlowError(f"边非法（需对象）: {e!r}")
            f_, t_ = e.get("from"), e.get("to")
            if f_ not in norm_steps:
                raise FlowError(f"边 {f_}->{t_} 的 from 步不存在")
            if t_ not in norm_steps:
                raise FlowError(f"边 {f_}->{t_} 的 to 步不存在")

        # 入口：显式 entry，否则取无入边的唯一步
        entry = raw.get("entry")
        if entry is not None:
            if entry not in norm_steps:
                raise FlowError(f"entry '{entry}' 不存在")
        else:
            targets = {e["to"] for e in edges}
            roots = [sid for sid in norm_steps if sid not in targets]
            if len(roots) != 1:
                raise FlowError(
                    f"无法确定唯一入口（无入边步: {roots}）；请显式给 'entry'")
            entry = roots[0]

        # 控制步的 goto/members/body 已在上面的 _validate_step 内校验 target 存在性
        for sid, s in norm_steps.items():
            for tgt in self._goto_targets(s):
                if tgt not in norm_steps:
                    raise FlowError(f"步骤 '{sid}' 的跳转目标 '{tgt}' 不存在")

        out = dict(raw)
        out["entry"] = entry
        out.setdefault("preset", False)
        out["_source"] = path
        out["steps"] = norm_steps
        out["_order"] = [s["id"] for s in steps]
        return out

    def _validate_step(self, s, parent):
        """校验单步并返回其 id（不递归校验 goto 存在性，由 _validate 统一做）。"""
        if not isinstance(s, dict):
            raise FlowError(f"步骤必须是对象: {s!r}")
        sid = s.get("id")
        if not sid or not isinstance(sid, str):
            raise FlowError(f"步骤缺 'id': {s!r}")
        ctl = s.get("control")
        if ctl is None:
            kind, ref = s.get("kind"), s.get("ref")
            if not kind or not ref:
                raise FlowError(f"步骤 '{sid}' 需给 'kind'+'ref' 或 'control'")
            if kind not in KINDS:
                raise FlowError(f"步骤 '{sid}' 的 kind '{kind}' 非法，允许: {KINDS}")
        else:
            if ctl not in CONTROLS:
                raise FlowError(f"步骤 '{sid}' 的 control '{ctl}' 非法，允许: {CONTROLS}")
            if ctl == "parallel":
                members = s.get("members")
                if not isinstance(members, list) or not members:
                    raise FlowError(f"parallel 步 '{sid}' 需非空 'members'")
                seen = set()
                for m in members:
                    mid = self._validate_step(m, parent=sid)
                    if mid in seen:
                        raise FlowError(f"parallel '{sid}' 的成员 id 重复: '{mid}'")
                    seen.add(mid)
            elif ctl == "branch":
                cases = s.get("cases")
                if not isinstance(cases, list) or not cases:
                    raise FlowError(f"branch 步 '{sid}' 需非空 'cases'")
                for c in cases:
                    if not isinstance(c, dict) or "when" not in c or "goto" not in c:
                        raise FlowError(f"branch '{sid}' 的 case 需含 'when'+'goto'")
                if not s.get("default"):
                    raise FlowError(f"branch 步 '{sid}' 需 'default'（兜底跳转）")
            elif ctl == "loop":
                body = s.get("body")
                if not isinstance(body, list) or not body:
                    raise FlowError(f"loop 步 '{sid}' 需非空 'body'")
                seen = set()
                for b in body:
                    bid = self._validate_step(b, parent=sid)
                    if bid in seen:
                        raise FlowError(f"loop '{sid}' 的体步 id 重复: '{bid}'")
                    seen.add(bid)
                if not s.get("until"):
                    raise FlowError(f"loop 步 '{sid}' 需 'until'（出口条件）")
                mi = s.get("max_iterations", 1)
                if not isinstance(mi, int) or mi < 1:
                    raise FlowError(f"loop '{sid}' 的 max_iterations 须为正整数")
            elif ctl == "gate":
                if not s.get("on_pass") or not s.get("on_reject"):
                    raise FlowError(f"gate 步 '{sid}' 需 'on_pass' 与 'on_reject'")
                if not s.get("pass_when"):
                    raise FlowError(f"gate 步 '{sid}' 需 'pass_when'（放行条件）")
                if not s.get("evidence_from"):
                    raise FlowError(f"gate 步 '{sid}' 需 'evidence_from'（上游证据来源）")
        return sid

    @staticmethod
    def _goto_targets(step):
        ctl = step.get("control")
        if ctl == "gate":
            return [step.get("on_pass"), step.get("on_reject")]
        if ctl == "branch":
            return [c.get("goto") for c in step.get("cases", [])] + [step.get("default")]
        return []

    # ── 查询 ──────────────────────────────────────────
    def list(self):
        out = []
        for fid, f in self._flows.items():
            out.append({
                "flow_id": fid, "name": f.get("name"),
                "version": f.get("version", "0.0.0"),
                "preset": bool(f.get("preset")),
                "steps": len(f["_order"]), "entry": f["entry"],
                "description": f.get("description", ""),
            })
        out.sort(key=lambda x: x["flow_id"])
        return out

    def get(self, flow_id):
        f = self._flows.get(flow_id)
        if f is None:
            raise FlowError(f"流程不存在: {flow_id}")
        return f

    def presets(self):
        """预设卡列表：与流程定义一一对应（缺定义/未标 preset 都如实标注）。"""
        out = []
        for p in self._presets:
            fid = p["flow_id"]
            f = self._flows.get(fid)
            item = dict(p)
            item["available"] = bool(f and f.get("preset"))
            if f is None:
                item["error"] = f"预设指向的流程不存在: {fid}"
            elif not f.get("preset"):
                item["error"] = f"流程 '{fid}' 未标 preset:true，不可作预设卡"
            else:
                item["name"] = f.get("name")
                item["version"] = f.get("version", "0.0.0")
                item["steps"] = len(f["_order"])
            out.append(item)
        return out

    def status(self):
        """注册表概况（供接口只读输出，含加载失败的真实原因）。"""
        return {
            "flows_dir": self.flows_dir,
            "flows": self.list(),
            "presets": self.presets(),
            "preset_config": self.presets_file,
            "preset_error": self._preset_error,
            "load_errors": list(self.errors),
        }


# ═══════════════════════════════════════════════════════════════════
# 3. 流程引擎
# ═══════════════════════════════════════════════════════════════════
class FlowEngine:
    """按流程定义执行控制流；能力调用一律走 reg.call（不另写调度器）。

    registry : plugin_framework.ExtensionRegistry（已登记插件扩展点/能力）
    bus      : event_bus.EventBus（每步写事件）
    audit    : audit_chain.AuditChain（全过程落链，可回放），None 则跳过
    """

    MAX_STEPS = 200          # 走步上限（防定义成环导致死循环）
    DEFAULT_MAX_REJECT = 3   # 门控默认最多打回次数

    def __init__(self, registry, bus=None, audit=None, flow_registry=None):
        self.registry = registry
        self.bus = bus or EventBus()
        self.audit = audit
        self.flow_registry = flow_registry
        self._lock = threading.Lock()

    # ── 对外入口 ──────────────────────────────────────
    def run_flow_id(self, flow_id, params=None):
        if self.flow_registry is None:
            raise FlowError("未提供 flow_registry，无法按 flow_id 触发")
        return self.run(self.flow_registry.get(flow_id), params)

    def run(self, flow, params=None):
        """执行一条流程定义，返回结果 dict（含每步状态、事件、被打回次数、真实错误）。"""
        flow = self._normalize(flow)
        fid = flow["flow_id"]
        params = params or {}
        records = {}           # step_id -> 记录
        timeline = []          # 有序时间线
        attempts = {}          # step_id -> 已执行次数
        gate_rejects = {}      # gate_id -> 打回次数
        seq = [0]

        def emit(ev_type, payload):
            ev = Event(ev_type, f"flow:{fid}", payload)
            res = self.bus.publish(ev)
            return res

        self._audit("flow_start", fid, f"流程开始 name={flow.get('name')}",
                    {"flow_id": fid, "params": params, "steps": flow["_order"]})

        emit(FLOW_STARTED, {"flow_id": fid, "name": flow.get("name"),
                            "steps": flow["_order"], "preset": bool(flow.get("preset")),
                            "params": params})

        cursor = flow["entry"]
        stopped_at = None
        flow_error = None
        status = STATUS_OK
        walk = 0

        while cursor is not None:
            walk += 1
            if walk > self.MAX_STEPS:
                status = STATUS_FAILED
                stopped_at = cursor
                flow_error = f"走步超过上限 {self.MAX_STEPS}（疑似定义成环）"
                break
            step = flow["steps"].get(cursor)
            if step is None:
                status = STATUS_FAILED
                stopped_at = cursor
                flow_error = f"跳转到不存在的步骤: {cursor}"
                break

            ctl = step.get("control")
            if ctl == "parallel":
                rec = self._run_parallel(step, flow, records, attempts)
                nxt = self._next_of(flow, cursor)
            elif ctl == "loop":
                rec = self._run_loop(step, flow, records, attempts)
                nxt = self._next_of(flow, cursor)
            elif ctl == "branch":
                rec, nxt = self._run_branch(step, flow, records)
            elif ctl == "gate":
                rec, nxt = self._run_gate(step, flow, records, gate_rejects)
            else:
                rec = self._run_leaf(step, flow, records, attempts, parent="")
                nxt = self._next_of(flow, cursor)

            records[cursor] = rec
            seq[0] += 1
            rec["seq"] = seq[0]
            rec["ts"] = datetime.now().isoformat(timespec="seconds")
            timeline.append({"seq": seq[0], "step_id": cursor, "name": step.get("name", ""),
                             "control": ctl or "step", "status": rec["status"],
                             "next": nxt, "reason": rec.get("reason", "")})
            # 每步写事件总线
            emit(FLOW_STEP, {"flow_id": fid, "step_id": cursor,
                             "control": ctl or "step", "status": rec["status"],
                             "reason": rec.get("reason", ""),
                             "result": _jsonable(rec.get("result"))})
            # 全过程落审计链（可回放）
            self._audit("flow_step", fid,
                        f"{cursor} [{ctl or 'step'}] -> {rec['status']}",
                        {"flow_id": fid, "step_id": cursor, "control": ctl or "step",
                         "status": rec["status"], "reason": rec.get("reason", ""),
                         "next": nxt, "attempt": rec.get("attempt", 1)})

            if rec["status"] == STATUS_FAILED:
                status = STATUS_FAILED
                stopped_at = cursor
                flow_error = rec.get("error") or rec.get("reason") or "步骤失败"
                break
            cursor = nxt

        finished = {"flow_id": fid, "status": status, "stopped_at": stopped_at,
                    "error": flow_error,
                    "gate_rejects": dict(gate_rejects),
                    "steps_executed": len(timeline)}
        emit(FLOW_FINISHED, finished)
        self._audit("flow_end", fid,
                    f"流程结束 status={status}" + (f" 停在 {stopped_at}" if stopped_at else ""),
                    {"flow_id": fid, "status": status, "stopped_at": stopped_at,
                     "error": flow_error, "gate_rejects": dict(gate_rejects)})

        result = {
            "ok": status != STATUS_FAILED,
            "flow_id": fid,
            "name": flow.get("name"),
            "preset": bool(flow.get("preset")),
            "status": status,
            "stopped_at": stopped_at,
            "error": flow_error,
            "gate_rejects": dict(gate_rejects),
            "entry": flow["entry"],
            "order": flow["_order"],
            "timeline": timeline,
            "steps": {k: _jsonable(v) for k, v in records.items()},
            "no_input_steps": [k for k, r in records.items() if r["status"] == STATUS_NO_INPUT],
        }
        return result

    # ── 控制流实现 ────────────────────────────────────
    def _next_of(self, flow, step_id):
        """普通/parallel/loop 步之后的下一步：取其唯出边目标。"""
        targets = [e["to"] for e in flow.get("edges", []) if e.get("from") == step_id]
        if not targets:
            return None
        if len(targets) > 1:
            raise FlowError(
                f"步骤 '{step_id}' 有多条出边 {targets}；分支请用 control=branch")
        return targets[0]

    def _run_leaf(self, step, flow, records, attempts, parent, iter_no=None):
        sid = step["id"]
        full_id = f"{parent}.{sid}" if parent else sid
        attempts[sid] = attempts.get(sid, 0) + 1
        attempt = attempts[sid]

        # 上游无输入判定：requires 里的步若为 no_input/不存在 → 本步「没有输入」
        requires = step.get("requires") or []
        missing = [r for r in requires
                   if r not in records or records[r]["status"] == STATUS_NO_INPUT]
        if missing:
            return {"step_id": full_id, "control": "step", "status": STATUS_NO_INPUT,
                    "attempt": attempt, "reason": "没有输入",
                    "result": None,
                    "detail": f"上游无输入: {missing}"}

        params = dict(step.get("params") or {})
        params["_flow_id"] = flow["flow_id"]
        params["_step_id"] = full_id
        params["_attempt"] = attempt
        if requires:
            params["_upstream"] = {r: records[r].get("result") for r in requires}
        if iter_no is not None:
            params["_iter"] = iter_no

        try:
            result = self.registry.call(step["kind"], step["ref"], params)
        except PluginError as e:
            return {"step_id": full_id, "control": "step", "status": STATUS_FAILED,
                    "attempt": attempt, "error": str(e),
                    "reason": f"扩展点调用失败: {e}", "result": None,
                    "ref": f"{step['kind']}/{step['ref']}"}
        except Exception as e:
            return {"step_id": full_id, "control": "step", "status": STATUS_FAILED,
                    "attempt": attempt,
                    "error": f"{type(e).__name__}: {e}",
                    "reason": f"能力执行异常: {type(e).__name__}: {e}",
                    "result": None, "ref": f"{step['kind']}/{step['ref']}",
                    "trace": traceback.format_exc(limit=3)}

        status, reason = self._classify(result)
        rec = {"step_id": full_id, "control": "step", "status": status,
               "attempt": attempt, "reason": reason,
               "result": result, "ref": f"{step['kind']}/{step['ref']}"}
        if status == STATUS_NO_INPUT:
            # 统一口径：无输入一律写「没有输入」，附加上游/能力给的细节
            rec["reason"] = "没有输入"
            if reason and reason != "没有输入":
                rec["detail"] = reason
        return rec

    @staticmethod
    def _classify(result):
        """把能力返回值归类到 success/no_input/failed（失败如实给原因）。"""
        if result is None:
            return STATUS_NO_INPUT, "没有输入"
        if isinstance(result, dict):
            if result.get("no_input") is True:
                return STATUS_NO_INPUT, result.get("reason") or "没有输入"
            if result.get("ok") is False:
                return STATUS_FAILED, (result.get("error") or result.get("reason")
                                       or "能力返回 ok=false")
        return STATUS_OK, ""

    def _run_parallel(self, step, flow, records, attempts):
        sid = step["id"]
        members = step["members"]
        results = {}

        def work(m):
            results[m["id"]] = self._run_leaf(m, flow, records, attempts, parent=sid)

        threads = [threading.Thread(target=work, args=(m,)) for m in members]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ordered = [results[m["id"]] for m in members]
        # 任何成员失败 → 汇合点判失败（停在该步）；no_input 不判失败
        failed = [r for r in ordered if r["status"] == STATUS_FAILED]
        no_in = [r["step_id"] for r in ordered if r["status"] == STATUS_NO_INPUT]
        if failed:
            status = STATUS_FAILED
            reason = "并行成员失败: " + "; ".join(
                f"{r['step_id']}: {r.get('error') or r.get('reason')}" for r in failed)
            err = failed[0].get("error") or failed[0].get("reason")
        else:
            status = STATUS_OK
            reason = ("部分成员没有输入: " + ", ".join(no_in)) if no_in else ""
            err = None
        return {"step_id": sid, "control": "parallel", "status": status,
                "members": ordered, "reason": reason, "error": err,
                "result": {"members": {r["step_id"]: _jsonable(r.get("result"))
                                       for r in ordered}}}

    def _run_loop(self, step, flow, records, attempts):
        sid = step["id"]
        max_it = step.get("max_iterations", 1)
        until = step["until"]
        iterations = []
        until_met = False
        final = None
        status = STATUS_OK
        err = None
        reason = ""
        for i in range(1, max_it + 1):
            body_recs = []
            for b in step["body"]:
                r = self._run_leaf(b, flow, records, attempts, parent=sid, iter_no=i)
                body_recs.append(r)
                if r["status"] == STATUS_FAILED:
                    status = STATUS_FAILED
                    err = r.get("error") or r.get("reason")
                    reason = f"循环体第 {i} 轮失败: {reason or err}"
                    break
            iterations.append({"iter": i, "steps": body_recs})
            final = body_recs
            if status == STATUS_FAILED:
                break
            ctx = self._expr_ctx(records, attempts, iter_no=i)
            try:
                if safe_eval(until, ctx):
                    until_met = True
                    break
            except FlowError as e:
                status = STATUS_FAILED
                err = f"退出条件求值失败: {e}"
                reason = err
                break
        if status != STATUS_FAILED and not until_met:
            reason = f"达到 max_iterations={max_it} 仍未满足出口条件（未判失败）"
        return {"step_id": sid, "control": "loop", "status": status,
                "iterations": len(iterations), "until_met": until_met,
                "until": until, "reason": reason, "error": err,
                "result": {"iterations": iterations}}

    def _run_branch(self, step, flow, records):
        sid = step["id"]
        ctx = self._expr_ctx(records, {}, iter_no=None)
        chosen, matched = step.get("default"), None
        for c in step["cases"]:
            try:
                if safe_eval(c["when"], ctx):
                    chosen, matched = c["goto"], c["when"]
                    break
            except FlowError as e:
                return ({"step_id": sid, "control": "branch", "status": STATUS_FAILED,
                         "error": f"分支条件求值失败: {e}",
                         "reason": f"分支条件求值失败: {e}", "result": None},
                        None)
        # 分支目标若是在同流程外（不在 records），返回 chosen 交给主循环
        nxt = self._resolve_local_or_flow(chosen, flow)
        return ({"step_id": sid, "control": "branch", "status": STATUS_OK,
                 "chosen": chosen, "matched_when": matched,
                 "reason": f"命中分支 -> {chosen}" if matched else f"走 default -> {chosen}",
                 "result": {"goto": chosen, "matched_when": matched}}, nxt)

    def _resolve_local_or_flow(self, target, flow):
        """分支目标必须是本流程步骤（供主循环跳转）。"""
        if target not in flow["steps"]:
            raise FlowError(f"分支目标 '{target}' 不在本流程内")
        return target

    def _run_gate(self, step, flow, records, gate_rejects):
        sid = step["id"]
        evidence = step.get("evidence_from") or []
        ev_status = {}
        for e_ in evidence:
            r = records.get(e_)
            ev_status[e_] = r["status"] if r else "absent"
        # 上游全无输入 → 门控写「没有输入」，不判失败，按放行侧继续
        if evidence and all(ev_status.get(e_) == STATUS_NO_INPUT for e_ in evidence):
            return ({"step_id": sid, "control": "gate", "status": STATUS_NO_INPUT,
                     "reason": "没有输入", "evidence": ev_status,
                     "passed": None, "on_pass": step["on_pass"],
                     "on_reject": step["on_reject"], "result": None},
                    step["on_pass"])
        ctx = self._expr_ctx(records, {}, iter_no=None)
        try:
            passed = bool(safe_eval(step["pass_when"], ctx))
        except FlowError as e:
            return ({"step_id": sid, "control": "gate", "status": STATUS_FAILED,
                     "error": f"门控条件求值失败: {e}",
                     "reason": f"门控条件求值失败: {e}", "evidence": ev_status,
                     "on_pass": step["on_pass"], "on_reject": step["on_reject"],
                     "result": None},
                    None)
        if passed:
            return ({"step_id": sid, "control": "gate", "status": STATUS_OK,
                     "passed": True, "evidence": ev_status,
                     "pass_when": step["pass_when"],
                     "on_pass": step["on_pass"], "on_reject": step["on_reject"],
                     "reason": "门控放行", "result": {"passed": True}},
                    step["on_pass"])
        # 打回：回到 on_reject（定义里可读）；超上限则判失败
        gate_rejects[sid] = gate_rejects.get(sid, 0) + 1
        limit = step.get("max_reject", self.DEFAULT_MAX_REJECT)
        if gate_rejects[sid] > limit:
            return ({"step_id": sid, "control": "gate", "status": STATUS_FAILED,
                     "passed": False, "evidence": ev_status,
                     "error": f"门控打回超过上限 {limit}",
                     "reason": f"门控打回超过上限 {limit}（目标 {step['on_reject']}）",
                     "on_pass": step["on_pass"], "on_reject": step["on_reject"],
                     "result": {"passed": False}},
                    None)
        return ({"step_id": sid, "control": "gate", "status": STATUS_REJECTED,
                 "passed": False, "evidence": ev_status,
                 "pass_when": step["pass_when"],
                 "reject_count": gate_rejects[sid],
                 "reason": step.get("reject_reason") or "门控打回",
                 "on_pass": step["on_pass"], "on_reject": step["on_reject"],
                 "result": {"passed": False, "goto": step["on_reject"]}},
                step["on_reject"])

    def _expr_ctx(self, records, attempts, iter_no):
        steps = {}
        for sid, r in records.items():
            base = {"status": r["status"], "ok": r["status"] == STATUS_OK}
            res = r.get("result")
            if isinstance(res, dict):
                base.update(res)
            base["result"] = res
            steps[sid] = base
        ctx = {"steps": _Box(steps), "attempts": _Box(attempts), "params": _Box({})}
        if iter_no is not None:
            ctx["iter"] = iter_no
        return ctx

    # ── 审计 ──────────────────────────────────────────
    def _audit(self, op, flow_id, summary, metadata):
        if not self.audit:
            return
        try:
            self.audit.record_decision(
                scenario=f"{flow_id}",
                reasoning=f"[{op}] {summary}",
                outcome=metadata.get("status", op),
                category="flow",
                entities=[flow_id, metadata.get("step_id", "")],
                confidence=1.0 if metadata.get("status") == STATUS_OK else 0.5,
                metadata=metadata)
        except Exception as e:  # 审计失败不阻断流程，但如实写日志
            try:
                print(f"[flow_engine] 审计写入失败: {type(e).__name__}: {e}")
            except Exception:
                pass

    # ── 规范化 ────────────────────────────────────────
    @staticmethod
    def _normalize(flow):
        if "steps" in flow and isinstance(flow["steps"], dict) and "_order" in flow:
            return flow
        # 传入了原始定义（未过 registry）：就地规范化
        steps = flow["steps"]
        norm = {s["id"]: s for s in steps}
        edges = flow.get("edges", [])
        if "entry" not in flow:
            targets = {e["to"] for e in edges}
            roots = [s["id"] for s in steps if s["id"] not in targets]
            flow = dict(flow)
            flow["entry"] = roots[0] if len(roots) == 1 else (steps[0]["id"] if steps else None)
        out = dict(flow)
        out["steps"] = norm
        out.setdefault("_order", [s["id"] for s in steps])
        return out


def _jsonable(v):
    try:
        json.dumps(v, ensure_ascii=False)
        return v
    except (TypeError, ValueError):
        return repr(v)


# ═══════════════════════════════════════════════════════════════════
# 3b. 通用能力桩（generic builtins —— 域无关的确定性积木）
# ═══════════════════════════════════════════════════════════════════
# 说明：这些是**通用、无行业口径**的最小能力桩，经既有 ExtensionRegistry 登记后
# 供流程步骤 reg.call 调用，使流程定义可离线一键跑通。真实部署里由第三方插件
# 覆盖/扩展同名扩展点（卸载桩 + 注册插件即可），**不是新的执行体系**。
def _cap_records(params):
    """data_source/records — 产出 n 条确定性记录。"""
    n = int((params or {}).get("n", 3))
    rows = [{"i": i + 1, "value": i + 1} for i in range(max(0, n))]
    return {"ok": True, "count": len(rows), "rows": rows}


def _cap_empty(params):
    """data_source/empty — 空数据源：如实返回「没有输入」。"""
    return {"no_input": True, "reason": "没有输入（数据源为空）"}


def _cap_threshold(params):
    """decision/threshold — 确定性阈值判定。"""
    p = params or {}
    val = p.get("value", 1)
    th = p.get("threshold", 1)
    op = p.get("op", ">=")
    ops = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
           "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
           "==": lambda a, b: a == b}
    if op not in ops:
        return {"ok": False, "error": f"不支持的比较符: {op}"}
    return {"ok": True, "passed": bool(ops[op](val, th)), "value": val,
            "threshold": th, "op": op}


def _cap_aggregate(params):
    """decision/aggregate — 对上游/参数里的记录做确定性汇总。"""
    p = params or {}
    rows = p.get("rows")
    if rows is None:
        up = p.get("_upstream") or {}
        for v in up.values():
            if isinstance(v, dict) and isinstance(v.get("rows"), list):
                rows = v["rows"]
                break
    rows = rows or []
    total = sum(float(r.get("value", 0)) for r in rows if isinstance(r, dict))
    return {"ok": True, "count": len(rows), "total": total}


def _cap_render(params):
    """template/render — 按变量渲染一段文本（无变量则原样返回模板）。"""
    p = params or {}
    tpl = p.get("template", "")
    variables = p.get("vars") or {}
    try:
        text = tpl.format(**variables) if variables else tpl
    except (KeyError, IndexError, ValueError) as e:
        return {"ok": False, "error": f"模板渲染失败: {e}"}
    return {"ok": True, "text": text}


def _cap_review(params):
    """decision/review — 模拟复核：首次不通过，达到 pass_from_attempt 次后通过。"""
    p = params or {}
    attempt = int(p.get("_attempt", 1))
    need = int(p.get("pass_from_attempt", 2))
    return {"ok": True, "passed": attempt >= need, "attempt": attempt, "need": need}


def _cap_collect(params):
    """push/collect — 收集清单（模拟推送/落库收尾），如实回条数。"""
    p = params or {}
    items = p.get("items") or []
    return {"ok": True, "collected": len(items), "items": items}


# (kind, id, handler) 通用桩清单
BUILTIN_CAPS = (
    ("data_source", "records", _cap_records),
    ("data_source", "empty", _cap_empty),
    ("decision", "threshold", _cap_threshold),
    ("decision", "aggregate", _cap_aggregate),
    ("decision", "review", _cap_review),
    ("template", "render", _cap_render),
    ("push", "collect", _cap_collect),
)


def register_builtins(reg, plugin="flow_builtins"):
    """把通用能力桩登记进既有扩展点注册表（已存在的键跳过，不覆盖插件）。"""
    n = 0
    for kind, ext_id, fn in BUILTIN_CAPS:
        if not reg.has(kind, ext_id):
            reg.register(kind, ext_id, fn, plugin=plugin,
                         meta={"builtin": True, "generic": True})
            n += 1
    return n


# ═══════════════════════════════════════════════════════════════════
# 4. 便捷装配 + 回放
# ═══════════════════════════════════════════════════════════════════
def build_engine(plugins_dir=None, flows_dir=None, audit=None, load_plugins=True,
                 with_builtins=True):
    """按项目约定装配：PluginManager 扫描 plugins/ 并登记扩展点 + FlowRegistry。

    返回 (engine, registry, plugin_manager)。**不新增执行体系**：就是复用
    plugin_framework 的注册表与 event_bus 的默认总线。
    with_builtins=True 时额外登记通用能力桩（域无关积木），使预设可离线一键跑通。
    """
    codes_dir = os.path.dirname(os.path.abspath(__file__))
    plugins_dir = plugins_dir or os.path.join(codes_dir, "plugins")
    pm = PluginManager(plugins_dir)
    if load_plugins:
        pm.load_all()
    if with_builtins:
        register_builtins(pm.registry)
    fr = FlowRegistry(flows_dir)
    eng = FlowEngine(pm.registry, bus=None, audit=audit, flow_registry=fr)
    return eng, pm.registry, pm


def replay(audit, flow_id, limit=200):
    """从审计链按时间线回放某流程的步骤记录（可回放）。"""
    if not audit:
        return {"flow_id": flow_id, "entries": [], "error": "审计链未启用"}
    try:
        rows = audit.decisions(category="flow", limit=limit)
    except Exception as e:
        return {"flow_id": flow_id, "entries": [], "error": f"读取审计链失败: {e}"}
    entries = []
    for r in rows:
        if r.get("metadata", {}).get("flow_id") == flow_id:
            entries.append({
                "sequence_id": r.get("sequence_id"),
                "ts": r.get("ts"),
                "op": r.get("metadata", {}).get("control", "") or r.get("outcome"),
                "step_id": r.get("metadata", {}).get("step_id", ""),
                "status": r.get("outcome"),
                "reason": r.get("metadata", {}).get("reason", ""),
                "notice": r.get("reasoning", ""),
            })
    entries.sort(key=lambda x: x["sequence_id"] or 0)
    return {"flow_id": flow_id, "entries": entries, "count": len(entries)}


if __name__ == "__main__":
    # 最小自检：装配引擎 → 列出流程与预设
    eng, reg, pm = build_engine()
    print("=== 流程引擎自检 ===")
    print(f"插件扩展点: {len(reg.list())} 个")
    print(f"流程目录: {eng.flow_registry.flows_dir}")
    st = eng.flow_registry.status()
    for f in st["flows"]:
        print(f"  · {f['flow_id']:<28} preset={f['preset']} steps={f['steps']}")
    if st["load_errors"]:
        print("加载失败:")
        for e in st["load_errors"]:
            print(f"  ⚠️ {e}")
    print("预设卡:")
    for p in st["presets"]:
        print(f"  · {p['flow_id']} available={p['available']} "
              f"{p.get('error', '')}")
