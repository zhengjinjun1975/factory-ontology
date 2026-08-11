# 需求：动态调温 + schema→prompt（第4、5项，依据调研）

> 依据调研《工厂本体检索优化调研-2026.md》：
> - 动态调温：按查询类型动态调整 LLM 温度（精确数值查询低温 0.0-0.3，开放解释 0.6-0.8），替代固定 0.6
> - schema→prompt：把词典/schema 结构化为 LLM 上下文，提升未见行业查询准确率（ShEx/OntoSCPrompt）

## 目标文件
- `E:\open-source\factory-ontology-kit\codes\graph_rag.py`（answer_graph 动态调温 + schema→prompt）

## 要求
### 1. 动态调温（graph_rag.answer_graph）
- 按问题类型动态选温度：
  - 精确数值/状态/极值查询（含 多少/几/最/数量/最大值 等）→ 低温 0.2（确定性）
  - 开放/解释/建议查询（含 分析/怎么样/如何/状况 等）→ 高温 0.7（多样性）
- 复用现有 llm_generate 的温度参数，按问题内容动态传入

### 2. schema→prompt（graph_rag.answer_graph）
- 把当前词典/schema 结构化为 prompt 上下文（实体类型/属性中文名/枚举值），注入 LLM 生成 prompt
- 让 LLM 生成时"知道有哪些实体/属性/取值"，提升未见行业查询准确率（ShEx 思路）
- 复用 current.json 的 lexicon，或从图 labels 提取

## 约束
- 极简
- LLM 失败回落（不阻塞）
- 不破坏现有命中
- 中文正确，泛化

## 验收
1. 精确查询（有多少台设备/功率最大的设备）走低温，开放查询（设备整体状况分析）走高温
2. prompt 含 schema 上下文（实体/属性/枚举）
3. 现有查询不破坏（有多少台设备→10）
4. pytest 29 passed
