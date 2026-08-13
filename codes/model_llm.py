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
import json
import requests

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "model_config.json")


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


if __name__ == "__main__":
    import sys
    prompt = sys.argv[1] if len(sys.argv) > 1 else "说一句话"
    print(llm_generate(prompt))
