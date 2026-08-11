# 需求：向量混合检索接入 run.py ask 主链路（Web 端生效）

> 背景：向量检索已实现（vector_retrieval.py）并接入 api_server.py，但 Web 实际问答链路（run.py ask）未接入——web/server 调 run.py ask，用户通过 Web 端问答时向量混合检索不生效。需接入主链路。

## 目标文件
- `E:\open-source\factory-ontology-kit\codes\run.py`（ask 主链路加向量混合检索）

## 现状
- run.py ask：规则引擎 → logical → GraphRAG → BM25 → miss（无向量混合）
- api_server.py：已接入向量混合（BM25 + VectorIndex 融合），但 web 不走 api_server
- vector_retrieval.py：VectorIndex.from_graph + search，纯标准库，失败回落

## 要求
1. 把向量混合检索接入 run.py ask 主链路（与 api_server 一致）：
   - 查询 → 规则 → GraphRAG → BM25(稀疏) → 向量语义(embedding) → 融合 → miss
   - 复用 vector_retrieval.VectorIndex（from_graph + search, min_score=0.60）
   - 融合：BM25 + 向量取并集（BM25 优先）
2. 惰性构建向量索引（首次查询构建，embedding 失败回落空索引不阻塞）
3. 与 api_server 逻辑一致，保证 Web 端和命令行行为一致
4. 不破坏现有命中

## 约束
- 极简，复用 vector_retrieval
- embedding 失败回落（不阻塞）
- 不破坏现有命中（data_valve '有多少台设备'→10）
- 中文正确

## 验收
1. 命令行 python run.py ask '最贵的产品'（data_precision）→ 命中价格相关
2. 命令行 python run.py ask '油轮有几艘'（data_ship）→ 命中船型
3. data_valve '有多少台设备'→10（不破坏）
4. pytest 29 passed
