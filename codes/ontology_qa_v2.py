#!/usr/bin/env python3
"""ontology_qa_v2.py — 进阶版：规则优先 + LLM 语义兜底的中文问答。

在 v3 规则引擎基础上，当规则匹配不到时，把"问题 + 本体数据摘要"发给
本地 Ollama 模型 (ornith)，让它基于本体数据理解意图并回答。混合策略：
  规则引擎(快、准、零成本) → 未命中时 LLM(泛化、理解隐含语义)

用法: python ontology_qa_v2.py <nt文件> "<问题>" [词典.json]
依赖: requests (LLM 兜底时用)

v2 修复：
- 改为复用套件自己的 ontology_qa_v3（原先引用的 ontology_qa.py 在本套件中不存在）
- LLM 兜底输入更紧凑（截断到合理长度），输出更稳
"""

import sys
import os
import importlib.util

_this_dir = os.path.dirname(os.path.abspath(__file__))

# 统一模型调用（从 model_config.json 读取，可切 ornith/deepseek）
from model_llm import llm_generate


def _load_v3():
    spec = importlib.util.spec_from_file_location("ontology_qa_v3", os.path.join(_this_dir, "ontology_qa_v3.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def code_answer(question, data, model_key=None):
    """Text-to-Query 兜底：让 LLM 生成可执行查询代码，在数据上跑出确定性结果。

    比"让 LLM 直接答"更可靠——数字由代码算出，不靠模型猜。返回 (answer, mode)。
    model_key 可选 local/cloud。
    """
    # 摘要数据 schema + 前几行示例
    if not data:
        return "[LLM空响应] 无数据", "llm"
    sample_keys = list(next(iter(data.values())).keys())
    sample_rows = []
    for name, d in list(data.items())[:5]:
        sample_rows.append(str({**{"_id": name}, **d}))
    cols_desc = ", ".join(sample_keys)
    context = "\n".join(sample_rows)

    prompt = (
        "你是数据分析助手。数据在 dict `data` 里，每个值是 {列名: 值} 的一行。\n"
        f"列: {cols_desc}\n"
        f"示例行:\n{context}\n\n"
        f"用户问题: {question}\n"
        "输出一段可执行 Python 代码（可用 collections.Counter、内置函数、推导式），"
        "把答案算出来赋给变量 result，用 print(result) 输出。只输出代码，不要解释。"
        "注意：如果问题是统计数量/分组/极值/平均/求和/范围过滤，用代码精确计算。"
    )
    code = llm_generate(prompt, temperature=0.1, max_tokens=500, model_key=model_key)
    if not code or code.startswith("[模型"):
        return (code or "[LLM空响应] 模型未生成代码"), "llm"
    # 去围栏
    import re
    code = re.sub(r'```\w*\s*|\s*```', '', code).strip()
    if not code:
        return "[LLM空响应] 模型未生成代码", "llm"
    # 受限执行：只给 data + 标准库，收集 print 输出
    ns = {"data": data, "Counter": __import__("collections").Counter}
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            exec(code, {"__builtins__": {**__import__("builtins").__dict__, "Counter": __import__("collections").Counter}}, ns)
        except Exception as e:
            return f"[查询执行失败] {e}\n代码: {code[:200]}", "llm"
    out = buf.getvalue().strip()
    return (out if out else "[查询无输出]"), "llm"


def llm_answer(question, data, model_key=None):
    """把问题+本体数据摘要发给模型，让它基于数据回答。model_key 可选 local/cloud。"""
    # 本体数据摘要：紧凑的实例-属性列表（限制条数，避免超长）
    lines = []
    for name, d in list(data.items())[:80]:
        parts = [f"{k}={v}" for k, v in d.items()]
        lines.append(f"{name}: {', '.join(parts)}")
    context = "\n".join(lines)
    total = len(data)

    prompt = (
        "你是一个工厂设备数据查询助手。下面是设备本体数据的部分实例(实例:属性)。\n"
        f"数据共 {total} 条，以下仅展示前 80 条。回答用户的问题，只基于这些数据，"
        "用中文，简洁，给数字结果。如果数据不足以回答，明确说明。\n\n"
        f"【数据】\n{context}\n\n"
        f"【问题】{question}\n"
        "【回答】"
    )

    ans = llm_generate(prompt, temperature=0.2, max_tokens=300, model_key=model_key)
    if not ans:
        return "[LLM空响应] 模型未生成内容，请稍后重试或换更具体的问题"
    return ans


def main():
    if len(sys.argv) < 3:
        print("用法: python ontology_qa_v2.py <nt文件> '<问题>' [词典.json]")
        sys.exit(1)
    nt_file, question = sys.argv[1], sys.argv[2]
    lex = sys.argv[3] if len(sys.argv) > 3 else os.path.join(_this_dir, "config", "lexicon.json")

    v3 = _load_v3()
    D = {}
    if lex and os.path.exists(lex):
        D = v3.load_dict(lex)
    triples = v3.parse_nt(nt_file)
    data = v3.build_data(triples, D)
    if not data:
        print("本体解析失败或无实例")
        sys.exit(1)

    # 先走规则引擎
    rule_answer = v3.answer(question, data, D)
    if rule_answer != "暂不支持该问题":
        print("[规则命中]")
        print(rule_answer)
        return

    # 规则未命中 -> LLM 语义兜底（Text-to-Query 优先）
    print("[LLM语义推理]")
    try:
        import requests
    except ImportError:
        print("未安装 requests，无法调用 LLM 兜底。仅规则引擎可用。")
        return
    code_ans, _mode = code_answer(question, data)
    if not code_ans.startswith("[LLM") and not code_ans.startswith("[查询"):
        print(code_ans)
    else:
        print(llm_answer(question, data))


if __name__ == "__main__":
    main()
