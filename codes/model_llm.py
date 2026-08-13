#!/usr/bin/env python3
"""model_llm.py — 统一模型调用工具。

从 config/model_config.json 读取模型配置，封装 Ollama 和 OpenAI(DeepSeek) 两种调用。
所有环节（建模/查询/分析）复用此工具，改配置即可切换模型，无需改代码。

用法:
  from model_llm import llm_generate, get_model_config
  text = llm_generate(prompt, temperature=0.3, max_tokens=800)

支持切换模型:
  - 改 config/model_config.json 的 "active": "local" | "cloud"
  - 或在代码里 llm_generate(prompt, model_key="cloud") 临时指定
"""
import os
import re
import json
import requests

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "model_config.json")

# 模型错误/失败描述前缀：llm_generate 在模型不可用(离线/无key/HTTP错误/异常)时统一返回
# "[模型错误]..." / "[模型调用失败]..."，路由层据此识别并降级，绝不抛异常。
_ERR_PREFIXES = ("[模型错误]", "[模型调用失败]")


def _load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"active": "local", "models": {}}


def get_embedding_config():
    """返回向量检索(embedding)模型配置 dict。默认本地 nomic-embed-text + Ollama。"""
    cfg = _load_config()
    emb = cfg.get("embedding")
    if not isinstance(emb, dict) or not emb.get("model"):
        # 兜底默认本地向量模型
        return {"name": "本地向量模型", "type": "ollama",
                "base_url": "http://127.0.0.1:11434", "model": "nomic-embed-text", "api_key": ""}
    return {
        "name": emb.get("name", "本地向量模型"),
        "type": emb.get("type", "ollama"),
        "base_url": emb.get("base_url", "http://127.0.0.1:11434"),
        "model": emb.get("model", "nomic-embed-text"),
        "api_key": emb.get("api_key", ""),
    }


def get_model_config(model_key=None):
    """返回当前生效的模型配置 dict。model_key 可选 'local'/'cloud'，默认读 active。"""
    cfg = _load_config()
    # 优先命令行/环境变量 FOOD_MODEL 覆盖，其次显式 model_key，最后读配置 active
    key = model_key or os.environ.get("FOOD_MODEL") or cfg.get("active", "local")
    m = cfg.get("models", {}).get(key)
    if not m:
        # 回退到 local
        key = "local"
        m = cfg.get("models", {}).get(key, {})
    return {**m, "key": key}


def _load_env_key(name):
    """从 环境变量 或 团队 Agent .env 文件 读取 API key。"""
    v = os.environ.get(name)
    if v:
        return v
    # 尝试读 团队 Agent .env 文件
    env_candidates = [
        os.path.join(os.path.expanduser("~"), ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for env_path in env_candidates:
        if os.path.exists(env_path):
            try:
                with open(env_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith(name + "="):
                            return line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass
    return ""


def llm_generate(prompt, temperature=0.3, max_tokens=800, model_key=None):
    """调用模型生成文本。兼容 Ollama 和 OpenAI(DeepSeek/智谱)。返回响应文本或错误描述。"""
    cfg = get_model_config(model_key)
    mtype = cfg.get("type", "ollama")
    base = cfg.get("base_url", "")
    model = cfg.get("model", "ornith:latest")
    api_key = cfg.get("api_key", "")

    # 云端：api_key 为空时从环境变量 / 团队 Agent .env 读取
    if mtype == "openai" and not api_key:
        api_key = _load_env_key("DEEPSEEK_API_KEY") or _load_env_key("ZHIPU_API_KEY")

    try:
        if mtype == "openai":
            # 智谱 / DeepSeek / OpenAI 兼容（含本地 Ollama OpenAI 端点）
            if not api_key:
                return "[模型错误] 云端模型未配置 API Key（model_config.json 或环境变量 ZHIPU_API_KEY/DEEPSEEK_API_KEY）"
            # 若 base_url 只给到 /v1（Ollama OpenAI 兼容端点），自动补全 /chat/completions
            url = base if base.rstrip("/").endswith("/chat/completions") else base.rstrip("/") + "/chat/completions"
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=180,
            )
            if resp.status_code != 200:
                return f"[模型错误] HTTP {resp.status_code}: {resp.text[:200]}"
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            # Ollama
            resp = requests.post(
                base,
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                },
                timeout=180,
            )
            if resp.status_code != 200:
                return f"[模型错误] HTTP {resp.status_code}"
            return resp.json().get("response", "").strip()
    except Exception as e:
        return f"[模型调用失败] {e}"


# =====================================================================================
# 模型智能路由（简单→本地 ornith / 复杂→云端 DeepSeek / 离线→降级本地）
# =====================================================================================

def is_model_error(text):
    """判断 llm_generate 返回值是否为模型不可用/失败的错误描述串。"""
    return isinstance(text, str) and text.startswith(_ERR_PREFIXES)


def classify_question(q):
    """把问题分为 simple / complex。

    simple：明确点查/计数(有多少、是什么、状态、是否存在、列出实体)。
    complex：多条件(且/并且/在…条件下/当…时/高于/低于等)、开放式(如何/怎样/什么原因)、
            或需要推理(对比/差异/影响/风险/建议/分析)。分数 >=2 判 complex。
    """
    q = (q or "").strip()
    if not q:
        return "simple"
    score = 0
    # 开放式 / 推理 / 建议型 → 复杂(+2)
    if re.search(
            r"如何|怎样|怎么|为什么|为何|原因|分析|评估|建议|对比|比较|区别|差异|"
            r"影响|风险|隐患|措施|方案|趋势|分布|占比|比例|汇总|统计|展望|规划|"
            r"什么(危害|后果|问题)|注意事项|需要(注意|警惕)", q):
        score += 2
    # 多条件 / 约束型 → 复杂(+2)
    if re.search(
            r"并且|同时|以及|且|在[^，。；,;]{1,10}(情况|条件|状态|场景|时候|情形)"
            r"[^，。；,;]{0,4}(下|时)|当[^，。；,;]{1,8}时|如果|若|满足|处于|"
            r"高于|低于|超过|大于|小于|不低于|不超过", q):
        score += 2
    # 多个枚举/并列实体 → 复杂(轻微)
    score += min(q.count("、") + q.count(",") + q.count("，") + q.count("和"), 2)
    # 长句倾向复杂
    if len(q) >= 20:
        score += 1
    # 明确点查/计数 → 简单(减分)
    if re.search(r"有多少|数量|总数|总共|是什么|叫什么|哪个|哪些实体|状态|"
                 r"是否存在|有没有|在不在|有.{0,3}个", q):
        score -= 1
    return "complex" if score >= 2 else "simple"


def _candidates_for(question, force_key=None):
    """返回尝试顺序的 model_key 列表。复杂→[cloud, local]，简单→[local]，
    force_key 显式指定时用它优先，其后追加 local 作降级。离线时 cloud 失败自动落到 local。"""
    if force_key:
        return [force_key] + (["local"] if force_key != "local" else [])
    if question:
        return ["cloud", "local"] if classify_question(question) == "complex" else ["local"]
    # 无问题上下文时退化为配置 active
    return [get_model_config()["key"]]


def llm_generate_auto(prompt, question=None, temperature=0.3, max_tokens=800, force_key=None):
    """带智能路由的模型调用。返回 (text, route)。

    route 说明实际命中的模型/尝试链/是否降级，供上层记录到 evidence。
    路由策略：
      简单问题 → 本地 ornith；复杂问题 → 云端 DeepSeek，云端不可用(离线/无key/HTTP错)
      → 自动降级本地；force_key 显式指定时优先并追加 local 兜底。
    绝不抛异常；所有模型均不可用时返回错误描述串(调用方按空处理即可，走规则/检索结果)。
    """
    route = {"question": question, "class": None, "tried": [], "model_key": None,
             "type": None, "fallback": False}
    try:
        candidates = _candidates_for(question, force_key)
        route["class"] = "forced" if force_key else (
            classify_question(question) if question else "auto")
        last = None
        for key in candidates:
            route["tried"].append(key)
            ans = llm_generate(prompt, temperature=temperature, max_tokens=max_tokens,
                               model_key=key)
            if ans and not is_model_error(ans):
                route["model_key"] = key
                route["type"] = get_model_config(key).get("type")
                route["fallback"] = len(route["tried"]) > 1
                return ans, route
            last = ans
        route["model_key"] = candidates[-1]
        return last, route
    except Exception as e:
        route["tried"].append("error")
        return f"[模型调用失败] {e}", route


if __name__ == "__main__":
    import sys
    prompt = sys.argv[1] if len(sys.argv) > 1 else "说一句话"
    print(llm_generate(prompt))
