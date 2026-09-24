#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""model_advisory.py — 批 3：模型建议层（可选适配）。

一句话：**模型只出候选与依据，绝不充当结论。**

硬纪律（本模块的存在理由，任何改动不得违反）
------------------------------------------------
1. **默认不启用**：必须由「环境变量 或 codes/config/model_advisory.json」显式打开，
   且存在可用的适配器，才可能产出 advisory。未启用时 `attach_if_enabled()` 原样返回
   入参对象 —— 调用方（ask_service.envelope_from_result）逐字段与批 2 完全一致。
2. **模型产物只能进 `advisory`**：恒为 `{"candidates": [...], "basis": [...], "model": "..."}`，
   恰好这三个键，别的一律不放（不做"顺手把模型话术塞进其它字段"这种口子）。
3. **`hit` / `answer` 绝不因模型改变**：本模块从不读也不写这两个字段；`attach_if_enabled()`
   只允许新增 `advisory` 一个键。若发现 answer/hit 与入参不一致，丢弃 advisory 并
   如实写 `advisory_error`（保守失败，不静默）。
4. **advisory 内容不得混进 answer**：运行期校验候选/依据文本是否出现在 answer 中，
   命中即丢弃 advisory（红线是代码里的一道闸，不只是自检脚本里的一句断言）。
5. 纯标准库；**不 import model_llm**（它依赖第三方 requests）。真实模型适配器用
   `urllib.request` 自带实现；云端适配器由使用方经 `register_adapter()` 注入。

适配器协议
----------
    class MyAdapter:
        name = "my-adapter"
        requires_key = False          # 需要 API Key 的适配器置 True（无 key 则整体不启用）
        def available(self) -> bool: ...          # 可选；缺省视为 True
        def advise(self, question, result, kb_name) -> dict | None:
            # 返回 build_advisory(...) 的结果，或 None 表示"没给出候选"（不编造）

启用方式（示例）
----------------
  # 1) 显式开一个本地 Ollama 建议模型
  FACTORY_ADVISORY_MODEL=writer-agent:latest
  FACTORY_ADVISORY_BASE=http://127.0.0.1:11434/api/generate
  # 2) 或只打开开关并自己注册适配器（进程内）
  FACTORY_ADVISORY_ENABLE=1
  import model_advisory as ma
  ma.set_active_adapter(ma.StaticAdapter([...], [...]))

关闭（默认态）：以上全部不设 → `is_enabled()` 为 False，一切照旧。
"""
import json
import os
import re
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config", "model_advisory.json")

# advisory 契约：恰好这三个键
ADVISORY_KEYS = ("candidates", "basis", "model")

ENV_ENABLE = "FACTORY_ADVISORY_ENABLE"        # "1"/"true"/"yes"/"on" 视为打开
ENV_MODEL = "FACTORY_ADVISORY_MODEL"          # 建议模型名（设置即视为显式配置）
ENV_BASE = "FACTORY_ADVISORY_BASE"            # 模型端点（默认本地 Ollama /api/generate）
ENV_KEY = "FACTORY_ADVISORY_API_KEY"          # 需要 key 的适配器用
ENV_TIMEOUT = "FACTORY_ADVISORY_TIMEOUT"      # 秒

DEFAULT_BASE = "http://127.0.0.1:11434/api/generate"

# 上一次 advise() 失败/被丢弃的原因（供自检与排查读取，不往上抛）
LAST_ERROR = None


# ────────────────────────────────────────────────────────── 适配器协议与注册表
class AdvisoryAdapter(object):
    """适配器基类。子类实现 advise()；必须只返回候选与依据，不得返回结论。"""
    name = "base"
    requires_key = False

    def available(self):
        return True

    def advise(self, question, result, kb_name):  # pragma: no cover - 抽象
        raise NotImplementedError


_ADAPTERS = {}          # name -> instance（进程内注册表）
_ACTIVE = None          # name（显式指定的生效适配器）


def register_adapter(adapter, name=None):
    """注册一个适配器实例。name 缺省取 adapter.name。"""
    nm = name or getattr(adapter, "name", None)
    if not nm:
        raise ValueError("适配器必须有 name")
    _ADAPTERS[nm] = adapter
    return nm


def unregister_adapter(name):
    _ADAPTERS.pop(name, None)
    global _ACTIVE
    if _ACTIVE == name:
        _ACTIVE = None


def set_active_adapter(adapter, name=None):
    """注册并指定为生效适配器（自检/装配注入点）。"""
    nm = register_adapter(adapter, name)
    global _ACTIVE
    _ACTIVE = nm
    return nm


def clear_active_adapter():
    global _ACTIVE
    _ACTIVE = None


def get_active_adapter():
    if _ACTIVE and _ACTIVE in _ADAPTERS:
        return _ADAPTERS[_ACTIVE]
    return None


# ────────────────────────────────────────────────────────── 配置
def _load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            # 配置坏了按"未启用"处理（保守），但不静默到无迹可查：记 LAST_ERROR
            _set_error("读取 %s 失败，按未启用处理" % CONFIG_PATH)
            return {}
    return {}


def _set_error(msg):
    global LAST_ERROR
    LAST_ERROR = msg


def _cfg_get(cfg, key, env_key):
    v = os.environ.get(env_key)
    if v is not None and str(v).strip() != "":
        return str(v).strip()
    return cfg.get(key)


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def _opt_in(cfg):
    """是否被**显式**打开。默认（不设 env、无配置文件）一律 False。"""
    if _truthy(os.environ.get(ENV_ENABLE)):
        return True
    if str(os.environ.get(ENV_MODEL) or "").strip():
        return True
    return bool(_truthy(cfg.get("enabled")))


def _api_key(cfg):
    v = os.environ.get(ENV_KEY)
    if v is not None and str(v).strip():
        return str(v).strip()
    return str(cfg.get("api_key") or "").strip()


def resolve_adapter():
    """按「显式生效适配器 → 已注册的 ollama → 由配置构造 OllamaAdapter」的顺序解析。"""
    ad = get_active_adapter()
    if ad is not None:
        return ad
    cfg = _load_config()
    want = _cfg_get(cfg, "adapter", "FACTORY_ADVISORY_ADAPTER")
    if want and want in _ADAPTERS:
        return _ADAPTERS[want]
    model = _cfg_get(cfg, "model", ENV_MODEL)
    if model:
        base = _cfg_get(cfg, "base_url", ENV_BASE) or DEFAULT_BASE
        try:
            timeout = float(_cfg_get(cfg, "timeout", ENV_TIMEOUT) or 20)
        except Exception:
            timeout = 20.0
        return OllamaAdvisoryAdapter(model, base=base, timeout=timeout)
    if want:
        _set_error("配置指定的适配器 %r 未注册" % want)
    return None


def is_enabled():
    """是否启用。未显式配置 / 无适配器 / 需要 key 而无 key / 适配器自报不可用 → False。"""
    cfg = _load_config()
    if not _opt_in(cfg):
        return False
    ad = resolve_adapter()
    if ad is None:
        return False
    if getattr(ad, "requires_key", False) and not _api_key(cfg):
        _set_error("适配器 %s 需要 API Key，未配置 → 不启用" % getattr(ad, "name", "?"))
        return False
    try:
        return bool(ad.available())
    except Exception as e:
        _set_error("适配器 %s.available() 异常：%s" % (getattr(ad, "name", "?"), e))
        return False


# ────────────────────────────────────────────────────────── advisory 构造（形状收口）
def _clean_list(v):
    """去空、去重、转字符串（保持原顺序）。"""
    out = []
    seen = set()
    for x in (v or []):
        s = str(x).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def build_advisory(candidates, basis, model):
    """把模型产物收口成**恰好三键**的 advisory；候选与依据都为空时返回 None（不产空壳）。"""
    cands = _clean_list(candidates)
    bas = _clean_list(basis)
    if not cands and not bas:
        return None
    return {"candidates": cands, "basis": bas, "model": str(model or "unknown")}


# ────────────────────────────────────────────────────────── 内置适配器
class StaticAdapter(AdvisoryAdapter):
    """固定候选适配器 —— 自检/演示/装配用。**不参与默认启用**（无配置不会被选中）。"""
    name = "static-advisory"
    requires_key = False

    def __init__(self, candidates, basis=None, model="static-advisory"):
        self._c = list(candidates or [])
        self._b = list(basis or [])
        self._m = model

    def advise(self, question, result, kb_name):
        return build_advisory(self._c, self._b, self._m)


_PROMPT = (
    "你是知识库问答的**建议层**。你只产候选与依据，绝不充当结论。\n"
    "已知信息（检索到的答案与证据，可能为空）：\n"
    "问题：{q}\n"
    "现有答案：{a}\n"
    "现有证据：{e}\n"
    "要求：只输出一个 JSON 对象，恰好两个键：\n"
    '  "candidates": 你认为可能相关的**候选**（要点/实体/可能想问的问题），数组，元素为短字符串；\n'
    '  "basis": 每条候选的**依据**（为什么这么建议），数组，元素为短字符串。\n'
    "不要下结论，不要改写现有答案，不要输出 JSON 之外的任何文字。"
)


class OllamaAdvisoryAdapter(AdvisoryAdapter):
    """本地 Ollama 建议模型（纯标准库 urllib，不依赖 requests）。失败返回 None，绝不编造。"""
    name = "ollama-advisory"
    requires_key = False

    def __init__(self, model, base=DEFAULT_BASE, timeout=20.0):
        self.model = model
        self.base = base or DEFAULT_BASE
        self.timeout = timeout

    def available(self):
        return bool(self.model)

    def advise(self, question, result, kb_name):
        ev = result.get("evidence") or []
        prompt = _PROMPT.format(q=question or "", a=str(result.get("answer") or "")[:300],
                                e=json.dumps(ev[:5], ensure_ascii=False)[:800])
        try:
            body = json.dumps({"model": self.model, "prompt": prompt,
                               "stream": False, "format": "json",
                               "options": {"temperature": 0.3}}).encode("utf-8")
            req = urllib.request.Request(self.base, data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            text = (data or {}).get("response") or ""
        except Exception as e:
            _set_error("ollama 建议模型调用失败：%s" % e)
            return None
        obj = _parse_json_obj(text)
        if not isinstance(obj, dict):
            _set_error("ollama 建议模型未返回可解析 JSON")
            return None
        return build_advisory(obj.get("candidates"), obj.get("basis"),
                              "ollama:%s" % self.model)


def _parse_json_obj(text):
    """从模型输出里抠出 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    s = str(text or "").strip()
    if not s:
        return None
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.MULTILINE).strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


# ────────────────────────────────────────────────────────── 主入口
def advise(question, result, kb_name=""):
    """调生效适配器拿 advisory。未启用/适配器异常/无产物 → None（**不编造**）。"""
    if not is_enabled():
        return None
    ad = resolve_adapter()
    try:
        adv = ad.advise(question, result, kb_name)
    except Exception as e:
        _set_error("适配器 %s.advise() 异常：%s" % (getattr(ad, "name", "?"), e))
        return None
    if adv is None:
        return None
    # 形状收口：任何适配器都不得越出三键契约
    return build_advisory(adv.get("candidates"), adv.get("basis"),
                          adv.get("model") or getattr(ad, "name", "unknown"))


def _leaks_into_answer(adv, answer):
    """红线检查：候选/依据文本不得出现在 answer 里。"""
    a = str(answer or "")
    if not a:
        return False
    for s in list(adv.get("candidates") or []) + list(adv.get("basis") or []):
        s = str(s).strip()
        if len(s) >= 4 and s in a:      # 太短的串（如"设备"）不作为泄漏判据，避免误杀
            return True
    return False


def attach_if_enabled(result, question=None, kb_name=""):
    """把 advisory 挂到问答返回 dict 上。**只新增 `advisory` 一个键**。

    未启用（默认态）→ 原样返回入参对象，逐字段零变化。
    启用但模型没给出候选 → 原样返回（宁可没有建议，也不编造）。
    """
    if not isinstance(result, dict):
        return result
    if not is_enabled():
        return result
    q = question if question is not None else result.get("question") or ""
    adv = advise(q, result, kb_name)
    if adv is None:
        return result
    out = dict(result)
    out["advisory"] = adv
    # ── 运行期红线闸（不是只在自检脚本里断言）──
    if out.get("answer") != result.get("answer") or out.get("hit") != result.get("hit"):
        out.pop("advisory", None)
        out["advisory_error"] = "advisory 层篡改了 answer/hit，已丢弃 advisory"
        _set_error(out["advisory_error"])
        return out
    if _leaks_into_answer(adv, out.get("answer")):
        out.pop("advisory", None)
        out["advisory_error"] = "advisory 内容出现在 answer 中，已丢弃 advisory（红线）"
        _set_error(out["advisory_error"])
        return out
    return out


def model_advisory_status():
    """自检/运维用状态快照（只读，不改任何东西）。"""
    cfg = _load_config()
    ad = resolve_adapter()
    return {
        "enabled": is_enabled(),
        "opt_in": _opt_in(cfg),
        "adapter": getattr(ad, "name", None),
        "registered": sorted(_ADAPTERS.keys()),
        "active": _ACTIVE,
        "config_path": CONFIG_PATH,
        "config_exists": os.path.exists(CONFIG_PATH),
        "last_error": LAST_ERROR,
    }


if __name__ == "__main__":
    print(json.dumps(model_advisory_status(), ensure_ascii=False, indent=2))
