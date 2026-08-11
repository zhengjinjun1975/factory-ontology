# 需求：本体建模逻辑升级到 sme 水平（关系推断修正 + LLM 中文 label）

> 用户反馈："认真学习 sme 库的本体建模和 FDE/OPC 工具套件的本体建模技术，需要逻辑自洽，你这差得有点远。"
> 诊断当前 suggest_schema 与 sme 的三个差距：
> 1. 关系推断有错误/重复：`auto_valve_batch_ingredient_batch_id` 有两个 to（batch_ingredient 和 batches）——FK 推断需修正（参考 sme 单复数词干匹配）
> 2. 属性中文 label 不全：词典生成 `batch编号`/`product编号`（丑陋）、`produce_date`/`raw_parts` 保持英文
> 3. 实体 label 是英文表名（Valve_batches），无中文名（sme 用 LLM 生成中文 label）

## 参考
- sme 库：`E:\open-source\sme-decision-ontology\codes\core\modeling.py`（先读）
  - `suggest_schema`: 单复数词干匹配关系（products↔product）、类型推断
  - `llm_enhance`: LLM 给实体生成中文 label（本地优先，失败回落规则）

## 目标文件
- `E:\open-source\factory-ontology-kit\codes\schema_ontology.py`（suggest_schema 升级）
- `E:\open-source\factory-ontology-kit\codes\multi_model.py`（_build_lexicon 中文优化）

## 要求
### 1. 关系推断修正（suggest_schema 的 _infer_relations）
- 修正重复/错向：一个 FK 列只对应一个目标实体（单复数词干匹配：valve_batches↔valve_batch_ingredient 的 batch 应匹配 Valve_batches 而非自身）
- 参考 sme 的 `_match_target`（词干匹配 + 排除自身）
- 关系 label 用中文（如 product_id→"生产产品"，避免 auto_ 前缀的英文）

### 2. LLM 中文 label 增强（实体 + 属性）
- 给 suggest_schema 加 `llm_enhance(schema, use_llm=True)`（参考 sme modeling.llm_enhance）：
  - 实体：生成中文 label（Valve_batches→批次、Valve_equipment→设备）
  - 属性：生成中文 label（produce_date→生产日期、raw_parts→原料、batch_id→批次编号）
  - 复用 codes/model_llm.py 的 llm_generate（若 key 可用）；不可用时回落规则映射
- 生成的 label 写入 schema 实体/属性，并被 to_nt 用于 RDFS label（中文展示）

### 3. 词典中文优化（multi_model._build_lexicon）
- 用上述中文 label 生成更自然的 attr_cn2en（生产日期、原料、批次编号），而非 `batch编号`/`produce_date`
- 保留英文兜底

## 约束
- 极简：复用 model_llm.py + sme 思路，改 suggest_schema/_infer_relations/_build_lexicon
- LLM 失败回落规则（不阻塞建模）
- 不破坏现有：build_graph/validate/to_nt/问答
- 中文正确

## 验收
1. suggest_schema 的关系无重复/错向（每个 FK 一个目标）
2. 建模后实体/属性有中文 label（批次/设备/生产日期等），to_nt 输出中文
3. 词典 attr_cn2en 更自然（生产日期→produce_date 而非 produce_date→produce_date）
4. 多文件建模后问答正常（设备总数等）
5. pytest 29 passed
