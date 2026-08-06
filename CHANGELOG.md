# Changelog

## [2.9.1] — 2026-08-06

### 文档/图表
- **系统级架构设计图**、**数据走向逻辑图**、**工厂落地路线图**（`docs/diagrams/*.svg`，暗色玻璃拟态）
- README 加"系统架构与落地路线"章节（嵌入 3 图，GitHub 自动渲染）+ 过程说明
- **端到端测试** `e2e_test.py`：CodeAgent 驱动，问答/溯源/导出/管理/多源/一致性 17 项全过

## [2.9.0] — 2026-08-06

### 平台化 + 多源 + 定位（A+B+D1）
- **逻辑桥评测**（`benchmark_logical.py`）：命中率 5/5=100%；`logical_qa` 补实体名解析（极值/排序返回中文名）
- **ERP 多源接入**（`db_loader.py`）：直连 MySQL/PostgreSQL，缺驱动清晰报错
- **Web 管理后台**：`/admin` 页面 + `POST /api/admin/upload`（CSV 上传 → 重建本体）+ 统计/词典/审计视图
- **溯源导出**：`GET /api/export/reverse`（CSV/TXT，可读名：原料→批次→产品→日期）
- **定位与横向对比**：README 加生态定位表（vs Dify/RAGFlow/GraphRAG/KAG）

## [2.8.0] — 2026-08-06

### 逻辑推理 + 可解释 + 全本地化（A+B+C，多 Agent 实现）
- **逻辑推理桥**（`logical_qa.py`）：LLM 转逻辑查询 → 确定性执行器（借鉴 KAG logical-form 模式）。规则引擎 miss 后先走逻辑桥，覆盖更多开放式问题而不失确定性
- **答案溯源/可解释**（`evidence.py`）：提取命中实体/属性/值证据，`/api/ask` 返回 `evidence`，APP 展示"答为什么"
- **全本地化**（Ollama）：`model_config` 加 `local_ollama`，`model_llm` 支持 base_url 离线——数据不出厂
- ask 流程：规则 → 逻辑桥 → GraphRAG → 引导（pytest 10→24 项）

## [2.7.2] — 2026-08-06

### 交付测试修复
- **多 Agent 交付测试**：3 Agent 并行测 L1-L5（数据/问答/API/交付/一致性），全过
- **修复增量缓存 bug**：`_ensure_food_ontology` 复用缓存前校验跨表对象属性完整（`_has_required_relations`），缺失强制重建——避免污染本体致溯源静默失效
- 新增 `docs/测试方案.md` + `docs/交付测试报告.md`

## [2.7.1] — 2026-08-06

### 落地问题修复（用户视角）
- **APP/API 通用化**：`GET /api/app-config` 返回当前 KB 品牌/图标/示例；APP 动态加载（去食品硬编码），miss 引导读 KB 示例——任何工厂换数据即换 APP 文案
- **新知识库引导**（`new_kb.py`）：一键搭建企业知识库骨架（kbs.json 注册 + 数据目录 + 词典模板 + 表结构说明）
- **README** 加"添加你的工厂数据（新企业落地）"快速指南

## [2.7.0] — 2026-08-06

### 精炼化（去除冗余能力）
- **删除早期研究框架的冗余层**（10 个模块）：`pipeline` / `factory_agent` / `aggregate` / `analysis` / `model_schema` / `ontology_depth` + 4 个死 agent（enhance/ingest/ops/query）
- 保留 `agents/lexicon_agent`（run.py 自动词典用）+ `core/base_agent`（其依赖）+ `csv_to_owl`（单表建本体）
- 根目录收敛为**精炼核心路径**：data_loader → multi_table → ontology_qa_v3 + graph_rag → api_server
- README 核心组件表更新，定位为"本体在中小工厂的具体实施"参考

## [2.6.2] — 2026-08-06

### 短板推进（测试加固 + 实证）
- **测试加固**：`tests/test_api.py` 新增 API/多租户/graph_store/data_import 测试，pytest 6→10 项
- **LLM 兜底评测**（`benchmark_graphrag.py`）：GraphRAG 开放式问题命中率 **8/8 = 100%**（实证）
- **规模实证**：20 万实体合成图，SQLite 图持久化 1.58s / 加载 1.76s / 67MB（规模化路径实测可行）

## [2.6.1] — 2026-08-06

### 规模化/产品（T-D 剩余）
- **多租户隔离**：`config/kbs.json` 注册多知识库，每企业独立数据/词典；`FOOD_KB` 切换；`GET /api/admin/kbs`
- **实时数据同步**：`POST /api/admin/sync`（admin）重读数据 + 可选外部源导入 + 重建
- **图数据库路径**：`graph_store.py`（SQLite 图持久化，10万-100万实体过渡）+ `docs/规模化.md`（内存图→SQLite→Neo4j 三档 + 迁移要点）

## [2.6.0] — 2026-08-06

### 重构 + 能力增强 + 规模化
- **删除 4 个弃用 QA 引擎**（ontology_qa/v2/query/relation_qa，~771 行，含 eval/exec 隐患）——全部迁移到 canonical v3 + GraphRAG 兜底，run.py/factory_agent/query_agent 一致
- **GraphRAG 实体链接增强**（T-C）：`find_seeds` 支持词典引导，问题提到属性/类型时加权有该字段的实体（归一化下划线匹配驼峰）
- **多知识库支持**（T-D）：`FOOD_DATA_DIR` / `FOOD_KB` 环境变量切换，一套部署服务多个企业知识库

## [2.5.0] — 2026-08-06

### 规模化/合规（T3）
- **审计日志**：每次 API 请求记录(时间/方法/路径/来源IP/状态码/耗时)落盘，`GET /api/admin/audit` 可查
- **监控告警**（`monitor.py`）：健康检查 + 指标看门狗，异常告警
- **食品合规文档**（`docs/合规.md`）：GB 溯源标准对齐、一物一码、召回场景、诚实边界

## [2.4.0] — 2026-08-06

### 可用性提升（T2）
- **数据接入自动化**（`data_import.py`）：Excel/DB/CSV → 知识库，列映射 + 定时同步（`--schedule`）
- **数据质量反馈环**（`data_quality.py`）：自动校验空值/重复ID/悬空引用/数值越界 + 报告
- **APP 升级 PWA**：manifest + service worker + 图标，可安装、离线缓存核心页面

## [2.3.0] — 2026-08-06

### 工程化硬化（M1 + 三跳板）
- **角色化鉴权**：`FOOD_ADMIN_KEY`（管理）/ `FOOD_READ_KEY`（只读），/api/* 需 `X-API-Key` 头；未配置则内网开放
- **增量重建**：数据文件 hash 检测，数据未变复用缓存本体，变了才重建（接新数据自动生效）
- **管理端点** `POST /api/admin/rebuild`（admin 权限）——接真实数据后强制重建
- **结构化日志 + 指标** `/metrics`：请求计数 + 统一日志格式
- **Docker 一键部署**：`Dockerfile` + `docker-compose.yml` + `nginx.conf`（HTTPS 反代示例）

## [2.2.0] — 2026-08-06

### 新增
- **REST API 层**（`api_server.py`）：FastAPI 服务，统一入口供 APP/语音/Web 调用——自然语言问答 + 正/反向溯源 + 扫码溯源 + 统计
- **食品企业知识库示例**：`data/food_*.csv`（产品/原料/批次/质检/设备 + 溯源 join 表），可跑规则问答 + GraphRAG 溯源
- **品类计数模板**：规则引擎支持"X 的数量"（食品品类计数场景）

### 修复（multi_table 图一致性命中）
- **join 表实例 ID 去重**：id 列非唯一时追加行号，避免 URI 碰撞丢关系
- **id 列同时是外键**：id_col 若在 relations 里则作为对象属性发出（否则 join 表丢失跨表关系）

## [2.1.0] — 2026-08-06

### 新增
- **GraphRAG-lite 层**（`graph_rag.py`）：本体建图 + 图遍历检索（BFS 正反向邻域）+ LLM 生成，补开放式/关系问题路径
- **GitHub Actions CI**（`ci.yml`）：自动跑 pytest 单测 + 对照评测
- **持久化 pytest 单测**（`tests/test_core.py`）：数据加载/本体生成/规则问答/GraphRAG 检索，5 项全过
- **图查询接口**：graph_rag 提供 build_graph / find_seeds / extract_subgraph / serialize（图查询即接口）

### 重构
- 定 `ontology_qa_v3.py` 为唯一 canonical 问答引擎；`ontology_qa.py` / v2 / ontology_query / relation_qa 标注 DEPRECATED（保留供回退，不硬删）

## [2.0.2] — 2026-08-06

### 新增
- **多数据源支持**：`data_loader.py` 统一读取 CSV / JSON / SQLite / Excel（前三种标准库零依赖，Excel 可选 openpyxl）；`csv_to_owl.py` 与 `multi_table.py` 均支持

### 修复（CodeAgent 代码审查发现）
- **data_loader.py**：SQLite 表名拼接前加合法标识符校验，消除 SQL 注入风险（表名来自库内，校验后安全）
- **multi_table.py**：移除未使用的 `csv` / `json` import（改 data_loader 后的残留）
- **csv_to_owl.py**：移除未使用的 `os` import

## [2.0.1] — 2026-08-06

### 新增
- **Web 前端**（`web/`）：Svelte5 + Vite + Node 的完整问答应用——CSV 上传 → 建模 → 自然语言问答 → 知识图谱/分析看板。已移除硬编码私有路径，指向仓库内 `codes/` 套件

## [2.0.0] — 2026-08-06

### 新增
- **benchmark.py**：本体问答 vs 纯 LLM 命中率对照评测（可复现，标准答案从源数据确定性计算）
- **多领域泛化验证**：新增图书库存、能源电站 2 个不同领域示例数据集，三个领域 benchmark 均 **100%** 命中
- **multi_table.py**：多表自动关联建本体（自动外键检测 + 跨表对象属性），无需手写 relations.json
- **新问答模板**：过滤计数（`属性=N 的数量`）、总数（`一共有多少条记录`）
- **方法论文档升级**：`docs/泛化方法论.md`（含 benchmark 实证）

### 修复
- 本体问答引擎补全过滤计数、总数模板，结构化查询命中率 74% → **100%**

### 基础设施
- 引入 `__version__`（主入口 run.py）

## [1.0.0] — 2026-08-06（初始开源发布）

- CSV → 本体（N-Triples，类型自动推断）
- 词典驱动通用问答引擎（规则 + LLM 兜底）
- 交付方法论 / 白皮书 / 开源调研文档
