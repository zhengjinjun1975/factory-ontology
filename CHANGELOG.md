# Changelog

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
