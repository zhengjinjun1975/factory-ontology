# Changelog

## [0.3.0] - 2026-08-13

### 生态插件基础框架（第三方可开发插件扩展系统）

> 不改主程序，第三方即可通过「插件」为系统新增能力。核心框架纯标准库零依赖，完全离线可跑。

- **插件加载器**（`codes/plugin_framework.py`）：扫描 `codes/plugins/` 目录 → 解析 `manifest.json`（`name/kind/version/entry/provides`）→ 按 `load → register → run → unload` 生命周期调度。清单缺字段/kind 非法/name 与目录名不符/入口缺失时逐个容错报告，不中断整体扫描
- **扩展点注册表**（`ExtensionRegistry`）：四类扩展点 `decision`（决策规则）/ `data_source`（数据源）/ `push`（推送通道）/ `template`（模板渲染），按 `(kind, id)` 注册、调用、注销，重复占用抛冲突；卸载插件自动注销其扩展点
- **CLI**（`run.py plugin`）：`plugin list [kind]` / `plugin run <名> ['<json>']` / `plugin ext <kind> <id> ['<json>']` / `plugin install <目录|zip|tar.gz> [--name 别名] [--force]` / `plugin remove <名>`；安装支持本地目录、zip、tar.gz 归档，别名安装自动改写 manifest
- **示例插件**（`codes/plugins/example_decision/`）：决策类插件，按温度/磨损/转速阈值输出设备维护优先级（正常/关注/预警/紧急），登记 `decision/maintenance_priority` 与 `decision/failure_alert` 两个扩展点；提供独立运行自测（`python plugin.py`）
- **测试**：`tests/test_plugin_framework.py` 6 项（扫描/生命周期/注册表/冲突/安装移除/zip 安装）；全量 pytest **35 passed**
- **文档**：`docs/插件框架.md` 第三方开发指南（目录结构/manifest 字段/生命周期/扩展点/CLI/写插件步骤）


## [0.2.0] - 2026-08-13

### 功能累积升级（检索/评测/上传/前端全面增强）

- **多租户企业绑定修复**：`getCurrentKb` 移除 `keys[0]`/`food` 兜底（A 企业不再被 B 企业数据污染）、`resetKb` 不再拦 `food`、onboarding 企业名/行业正确贯穿、顶部标签按企业行业识别
- **检索增强**：咨询/建议型开放问题（"有什么安全问题/风险/注意"）走专业 LLM 兜底生成建议；极值语义陷阱拦截（"最大的安全问题"不再误答"容量最大"）；评测路径友好兜底 + hit 判定补非答案词（不虚高命中率）
- **文档上传增强**：超长章节按 size 二次切分（修 PDF embed 超 token 失败）、入库时间 `ingested_at` 字段 + 前端格式化、会话持久化（node 重启不掉线）
- **前端**：资产面板空态也显示"创建快照"入口（修死锁）、企业行业识别、UI 审美调优
- **验证**：pytest 29 passed、前端 vite build 通过

## [0.1.5] - 2026-08-11

### 综合方案：9 大行业泛化建模 + 向量混合检索（命中率 100%）

> 基于 9 大行业（阀门/机械/食品/化工/地震/精加工/波纹管/环保/造船）横向验证，落地"两阶段泛化建模"方法论：建库自动生成查询映射 + 向量语义混合检索，实现"换任何行业数据即用、命中率 100%"。

- **基础重构（建库自动生成映射，替代硬编码）**：`_build_lexicon` 自动生成 `entity_cn2en`（实体计数映射，词干+中文label双源）+ `numeric_fields`（极值字段，data profiling 数值列识别），任意新行业实体（测线/炮点/项目/船/船坞/订单）自动可计数、极值查询自动命中
- **向量语义混合检索**（`vector_retrieval.py`，本地 nomic-embed-text 768维 纯标准库）：BM25 稀疏 + 向量语义 融合，接入 run.py 主链路 + api_server，语义模糊查询（"最贵的产品"/"油轮有几艘"）命中；embedding 失败回落不阻塞
- **极值词映射**：`_EXTREME_WORD_FIELDS` + `_extreme_field`，"最贵/最便宜"→price、"大/小"→容量/"高/低"→温度功率，通用极值词自动推断字段
- **模型配置增加向量模型**：model_config.json 加 `embedding` 配置（默认本地 nomic-embed-text），`get_embedding_config()` + vector_retrieval 读配置
- **9 行业数据**（`data_valve/data_machining/data_food_co/data_chem/data_seismic/data_precision/data_bellows/data_eco/data_ship`）
- **验证**：9 行业 40/40 = 100% 命中（实体计数/极值/类型/材质全泛化）；pytest 29 passed；CI verify ok:True
- **方法论**（`docs/方法论-两阶段泛化建模.md`）：两阶段（规则+LLM 自动建模 → 人工辅助精细化）+ 分层架构（结构层通用 + 映射层随行业）+ 混合检索

## [0.1.4] - 2026-08-11

### 厂区数据真实化 + 检索容错

> 检索全网真实阀门制造数据特征，重构示例数据为接近真实（产品BOM/工艺/传感器/噪声），并增强检索容错。

- **真实化示例数据**（`data_valve/`，基于研究《阀门制造工厂数据特征-真实化.md》）：产品用 GB/T 32808 型号编码（Z41H-16C/Q641F-40P）+ 材料牌号（WCB/CF8/CF8M）+ 标准号/温度范围；设备含传感器（振动/温度/电流）；质检用 API 598 试压矩阵（壳体/密封压力、保压、泄漏率气泡/min）；含真实噪声（材质别名 1Cr18Ni9Ti≈304≈CF8、缺失值、泄漏超标异常）
- **schema 更新**（`ontology_schema.json`）：匹配真实 BOM 字段（model_code/pressure_grade/connection/seal_material/body_material/standard_no/temp_range）+ 设备传感器 + 质检试压字段
- **检索容错**（`graph_rag.py`）：材质/单位/类型同义词扩展，`_expand_synonyms` + `_SYNONYM_GROUPS`，查询"不锈钢"能命中 CF8/CF8M/304/1Cr18Ni9Ti（子串匹配兼容 CF8(304) 带括号格式）；缺失值/异常值检索不崩
- **验证**：真实化数据建模 1066 行 NT（142 节点/173 边）；检索容错 8/8（"不锈钢"命中 CF8 产品 P004）；pytest 29 passed；CI verify ok:True
- **修复（同日）**：过滤计数模板优先级：属性名含"故障"时被状态模板劫持（"机器故障标签=0 的数量"答成"有 339 故障的"），模板前置后 ai4i benchmark 82% → **61/61 = 100%**（四领域全 100% 复现）；极值回答显示修正（"最扭矩的记录" → "扭矩最大的记录"）；移除 run.py 对已删除 ontology_depth.py 的失效调用

## [0.1.3] - 2026-08-11

### 安全边界 + 核心程序加固 + 编码正确性

> 审查重构后本体建模的安全边界、加固核心程序、确保检索无编码错误。

- **本体建模失败报告**（`schema_ontology.py` / `run.py setup-schema`）：建模各步骤失败时报告清晰原因（`[建模失败] 数据目录不存在` / `schema JSON 非法` / `实体id重复` / `关系引用不存在`），不裸抛异常。`load_schema` 的 assert 改为显式 ValueError（报告具体错误项）
- **SQL 注入加固**（`db_loader.py`）：表名白名单校验（`re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*")`），拦截 `products; DROP TABLE x` 类注入，与 data_loader 一致
- **编码正确性验证**：核心文件全 UTF-8；中文 label 无乱码、BM25 中文检索正常、find_seeds 中文匹配、parse_nt 中文 label 正确保留
- **验证**：错误报告 6/6 + 编码正确性 7/7 + pytest 29 passed + CI verify ok:True

## [0.1.2] - 2026-08-11

### 本体驱动增强（基于 2025-2026 最新技术研究）

> 检索全网企业本体建模/知识图谱最新技术讨论（OntoRAG、OG-RAG、本体约束减幻觉、LLM驱动本体构建），方法论升级为「schema 驱动建模 + 本体驱动混合检索 + 本体约束减幻觉」。

- **本体引导 GraphRAG 种子**（`graph_rag.find_seeds(ontology=...)`）：问题匹配本体关系 label 时，沿关系路径扩展种子，提升多跳/关系查询召回（依据 OG-RAG EMNLP2025 / ORT ACL2025）
- **本体约束减幻觉**（`graph_rag`/`logical_qa` LLM prompt 注入）：只依据子图事实回答，不编造图中不存在的关系/实体（依据临床 QA 98% vs GPT-4 37%）
- **schema 自动推断**（`schema_ontology.suggest_schema(data)`）：从多表数据自动推断实体/关系/约束，无手写 schema 也能 schema 驱动建模（schema-free 范式，依据 LLM/规则驱动本体构建）
- **方法论升级**：`docs/泛化方法论.md` 加入 2026 本体驱动增强表，引用三篇研究笔记
- **研究笔记**：`knowledge/AI/articles/` 新增《企业本体建模最新技术讨论-2025》《知识图谱最新技术演进-2025》《本体建模输入输出与作用-2025》
- **验证**：本体增强 9/9（suggest_schema 推断 8 实体 8 关系建 403 行 NT + find_seeds 本体引导 + prompt 本体约束）；pytest 29 passed；valve_demo 溯源命中 + benchmark 13/13

## [0.1.1] - 2026-08-11

### 激进重构：schema 驱动统一建模 + 融入 sme 本体重构精髓

> 从 v0.1.0 起点，按「复用优先·极简落地」方法论全面重构本体建模路径。

- **版本降维**：v2.9.6 → **v0.1.0 → v0.1.1**，从 0.1 重新开始（融入 sme 本体重构精髓后作为新起点）
- **schema 驱动统一建模**（`schema_ontology.py`）：移植 sme-decision-ontology 本体重构精髓，schema 驱动（ontology.json 显式声明实体/关系/约束）、属性语义角色（identifier/reference/measure/category/timestamp）、类型体系（Enterprise→BusinessObject→域类→实体）、validate 约束校验、traverse 跨域图遍历、build_graph 跨表统一实例图
- **`to_nt()` N-Triples 统一输出**（激进重构核心）：schema 驱动建模结果输出标准 N-Triples，替代 csv_to_owl/multi_table 的多表建本体职责；类名表名风格（Valve_products）+ 对象属性英文 id（usesRawMaterial），下游 ontology_qa_v3/graph_rag 无缝消费。已拆 4 子函数（类声明/属性声明/类别层级/实例）降低复杂度
- **`run.py setup-schema` 命令**：多表数据目录 + ontology_schema.json → 统一本体（约束校验 + 类型体系 + 语义域）
- **调用点统一**：valve_demo/mcp_server 建本体改为 schema 驱动优先（无 schema 回退 multi_table，向后兼容）；单表 benchmark 保留 csv_to_owl（正确工具）
- **`config/ontology_schema.json`**：阀门工厂示例 schema（8 实体 / 6 关系 / 溯源链 usesRawMaterial+belongsToBatch+checkedBy）
- **验证**：valve_demo 反向溯源命中（RM03→VB02）+ benchmark 13/13=100%；pytest 28 passed；setup-schema 端到端 8 表→383 行 N-Triples→43 节点/36 边；CodeAgent 审查通过

## [2.9.6] - 2026-08-07

### 本体深化：类别类层级(Is-A) + 企业与客户关系 + 企业本体大图（参考 sme-decision-ontology）
- **本体层次深入**：multi_table.py 新增类别类层级，自动检测 `type/category` 列，生成 `<表名>Category_值 rdfs:subClassOf <表名>`（产品 Is-A 类别、设备 Is-A 类别）+ 实例 `hasType` 链接
- **FK 检测增强**：支持领域前缀（valve_/food_/factory_）+ 单复数匹配（product_id → products），自动识别跨表关系
- **企业与客户关系**：新增 `valve_customers.csv` + `valve_sales.csv`，产品 --销售--> 客户（hasValve_products / hasValve_customers）
- **企业本体大图**：`docs/diagrams/ontology-大图.svg` 展示企业与客户关系 + 本体层次 Is-A
- **跨行业验证**：阀门 8 subClassOf + 食品 11 subClassOf，FK 前缀/单复数均适配
- 版本 2.9.5 → 2.9.6

## [2.9.5] - 2026-08-07

### 优化：lexicon_agent._build_full_lexicon（方案A）
- 圈复杂度 **46 → 2**（数据驱动查表 + 抽子方法，行为不变）
- 抽：`_build_attr_mapping`/`_build_enum_mapping`/`_build_field_aliases`/`_build_relations_cn2en`/`_is_single_letter_grade`/`_is_binary_flag`/`_camelize`
- 关键词查表：STATUS_KEYWORDS/TYPE_KEYWORDS/ZONE_KEYWORDS
- **修复潜在 bug**：`_infer_cn_from_name(f, {})` 传2参但函数只收1参（attr_map 空时崩溃）→ `(f)`
- 验证：pytest 29 + e2e 17/17 全过，行为一致

## [2.9.4] - 2026-08-07

### BM25 混合检索 + MCP server（AI 原生）
- **BM25 混合检索**（`bm25_retrieval.py`，纯标准库零依赖）：中文 unigram+bigram 分词、倒排索引、BM25 打分；接入 API 问答链路（规则→逻辑桥→GraphRAG→BM25→miss），提升模糊/自然语言查询召回，零 token；`min_score` 阈值过滤噪音
- **MCP server**（`mcp_server.py`，纯标准库 stdio JSON-RPC）：暴露知识库给任意 MCP-native AI agent，工具=ask/trace_forward/trace_reverse/stats；AI 原生，agent 可调用问答/溯源/统计
- 测试：`tests/test_bm25_mcp.py` 5 项（BM25 检索/排序 + MCP 握手/工具/溯源）；pytest 29 项全过
- 鲁棒性：`benchmark_logical.py` 自动构建缺失的 NT（干净检出也可跑，e2e 17/17）

## [2.9.3] - 2026-08-06

### 内部使用方案（示例）
- `docs/内部使用方案.md`（示例）：设备/合同知识库场景 + 落地步骤 + ROI

## [2.9.2] - 2026-08-06

### 阀门行业示例（交叉佐证）
- **阀门行业 demo**（`valve_demo.py` + `data_valve/*.csv` 合成示例）：实证框架对石油/阀门领域"换领域即用"
  - 规则问答（数量/极值）、逻辑桥（自然语言）、反向溯源（不合格密封圈→批次→阀门，质量召回）、benchmark 13/13=100%
- `config/lexicon_valve.json` 阀门词典；README 加"阀门行业示例"章节
- 实证框架对设备/阀门台账类结构化数据"换领域即用"

## [2.9.1] - 2026-08-06

### 文档/图表
- **系统级架构设计图**、**数据走向逻辑图**、**工厂落地路线图**（`docs/diagrams/*.svg`，暗色玻璃拟态）
- README 加"系统架构与落地路线"章节（嵌入 3 图，GitHub 自动渲染）+ 过程说明
- **端到端测试** `e2e_test.py`：CodeAgent 驱动，问答/溯源/导出/管理/多源/一致性 17 项全过

## [2.9.0] - 2026-08-06

### 平台化 + 多源 + 定位（A+B+D1）
- **逻辑桥评测**（`benchmark_logical.py`）：命中率 5/5=100%；`logical_qa` 补实体名解析（极值/排序返回中文名）
- **ERP 多源接入**（`db_loader.py`）：直连 MySQL/PostgreSQL，缺驱动清晰报错
- **Web 管理后台**：`/admin` 页面 + `POST /api/admin/upload`（CSV 上传 → 重建本体）+ 统计/词典/审计视图
- **溯源导出**：`GET /api/export/reverse`（CSV/TXT，可读名：原料→批次→产品→日期）
- **定位与横向对比**：README 加生态定位表（vs Dify/RAGFlow/GraphRAG/KAG）

## [2.8.0] - 2026-08-06

### 逻辑推理 + 可解释 + 全本地化（A+B+C，多 Agent 实现）
- **逻辑推理桥**（`logical_qa.py`）：LLM 转逻辑查询 → 确定性执行器（借鉴 KAG logical-form 模式）。规则引擎 miss 后先走逻辑桥，覆盖更多开放式问题而不失确定性
- **答案溯源/可解释**（`evidence.py`）：提取命中实体/属性/值证据，`/api/ask` 返回 `evidence`，APP 展示"答为什么"
- **全本地化**（Ollama）：`model_config` 加 `local_ollama`，`model_llm` 支持 base_url 离线，数据不出厂
- ask 流程：规则 → 逻辑桥 → GraphRAG → 引导（pytest 10→24 项）

## [2.7.2] - 2026-08-06

### 交付测试修复
- **多 Agent 交付测试**：3 Agent 并行测 L1-L5（数据/问答/API/交付/一致性），全过
- **修复增量缓存 bug**：`_ensure_food_ontology` 复用缓存前校验跨表对象属性完整（`_has_required_relations`），缺失强制重建，避免污染本体致溯源静默失效
- 新增 `docs/测试方案.md` + `docs/交付测试报告.md`

## [2.7.1] - 2026-08-06

### 落地问题修复（用户视角）
- **APP/API 通用化**：`GET /api/app-config` 返回当前 KB 品牌/图标/示例；APP 动态加载（去食品硬编码），miss 引导读 KB 示例，任何工厂换数据即换 APP 文案
- **新知识库引导**（`new_kb.py`）：一键搭建企业知识库骨架（kbs.json 注册 + 数据目录 + 词典模板 + 表结构说明）
- **README** 加"添加你的工厂数据（新企业落地）"快速指南

## [2.7.0] - 2026-08-06

### 精炼化（去除冗余能力）
- **删除早期研究框架的冗余层**（10 个模块）：`pipeline` / `factory_agent` / `aggregate` / `analysis` / `model_schema` / `ontology_depth` + 4 个死 agent（enhance/ingest/ops/query）
- 保留 `agents/lexicon_agent`（run.py 自动词典用）+ `core/base_agent`（其依赖）+ `csv_to_owl`（单表建本体）
- 根目录收敛为**精炼核心路径**：data_loader → multi_table → ontology_qa_v3 + graph_rag → api_server
- README 核心组件表更新，定位为"本体在中小工厂的具体实施"参考

## [2.6.2] - 2026-08-06

### 短板推进（测试加固 + 实证）
- **测试加固**：`tests/test_api.py` 新增 API/多租户/graph_store/data_import 测试，pytest 6→10 项
- **LLM 兜底评测**（`benchmark_graphrag.py`）：GraphRAG 开放式问题命中率 **8/8 = 100%**（实证）
- **规模实证**：20 万实体合成图，SQLite 图持久化 1.58s / 加载 1.76s / 67MB（规模化路径实测可行）

## [2.6.1] - 2026-08-06

### 规模化/产品（T-D 剩余）
- **多租户隔离**：`config/kbs.json` 注册多知识库，每企业独立数据/词典；`FOOD_KB` 切换；`GET /api/admin/kbs`
- **实时数据同步**：`POST /api/admin/sync`（admin）重读数据 + 可选外部源导入 + 重建
- **图数据库路径**：`graph_store.py`（SQLite 图持久化，10万-100万实体过渡）+ `docs/规模化.md`（内存图→SQLite→Neo4j 三档 + 迁移要点）

## [2.6.0] - 2026-08-06

### 重构 + 能力增强 + 规模化
- **删除 4 个弃用 QA 引擎**（ontology_qa/v2/query/relation_qa，~771 行，含 eval/exec 隐患），全部迁移到 canonical v3 + GraphRAG 兜底，run.py/factory_agent/query_agent 一致
- **GraphRAG 实体链接增强**（T-C）：`find_seeds` 支持词典引导，问题提到属性/类型时加权有该字段的实体（归一化下划线匹配驼峰）
- **多知识库支持**（T-D）：`FOOD_DATA_DIR` / `FOOD_KB` 环境变量切换，一套部署服务多个企业知识库

## [2.5.0] - 2026-08-06

### 规模化/合规（T3）
- **审计日志**：每次 API 请求记录(时间/方法/路径/来源IP/状态码/耗时)落盘，`GET /api/admin/audit` 可查
- **监控告警**（`monitor.py`）：健康检查 + 指标看门狗，异常告警
- **食品合规文档**（`docs/合规.md`）：GB 溯源标准对齐、一物一码、召回场景、诚实边界

## [2.4.0] - 2026-08-06

### 可用性提升（T2）
- **数据接入自动化**（`data_import.py`）：Excel/DB/CSV → 知识库，列映射 + 定时同步（`--schedule`）
- **数据质量反馈环**（`data_quality.py`）：自动校验空值/重复ID/悬空引用/数值越界 + 报告
- **APP 升级 PWA**：manifest + service worker + 图标，可安装、离线缓存核心页面

## [2.3.0] - 2026-08-06

### 工程化硬化（M1 + 三跳板）
- **角色化鉴权**：`FOOD_ADMIN_KEY`（管理）/ `FOOD_READ_KEY`（只读），/api/* 需 `X-API-Key` 头；未配置则内网开放
- **增量重建**：数据文件 hash 检测，数据未变复用缓存本体，变了才重建（接新数据自动生效）
- **管理端点** `POST /api/admin/rebuild`（admin 权限），接真实数据后强制重建
- **结构化日志 + 指标** `/metrics`：请求计数 + 统一日志格式
- **Docker 一键部署**：`Dockerfile` + `docker-compose.yml` + `nginx.conf`（HTTPS 反代示例）

## [2.2.0] - 2026-08-06

### 新增
- **REST API 层**（`api_server.py`）：FastAPI 服务，统一入口供 APP/语音/Web 调用，自然语言问答 + 正/反向溯源 + 扫码溯源 + 统计
- **食品企业知识库示例**：`data/food_*.csv`（产品/原料/批次/质检/设备 + 溯源 join 表），可跑规则问答 + GraphRAG 溯源
- **品类计数模板**：规则引擎支持"X 的数量"（食品品类计数场景）

### 修复（multi_table 图一致性命中）
- **join 表实例 ID 去重**：id 列非唯一时追加行号，避免 URI 碰撞丢关系
- **id 列同时是外键**：id_col 若在 relations 里则作为对象属性发出（否则 join 表丢失跨表关系）

## [2.1.0] - 2026-08-06

### 新增
- **GraphRAG-lite 层**（`graph_rag.py`）：本体建图 + 图遍历检索（BFS 正反向邻域）+ LLM 生成，补开放式/关系问题路径
- **GitHub Actions CI**（`ci.yml`）：自动跑 pytest 单测 + 对照评测
- **持久化 pytest 单测**（`tests/test_core.py`）：数据加载/本体生成/规则问答/GraphRAG 检索，5 项全过
- **图查询接口**：graph_rag 提供 build_graph / find_seeds / extract_subgraph / serialize（图查询即接口）

### 重构
- 定 `ontology_qa_v3.py` 为唯一 canonical 问答引擎；`ontology_qa.py` / v2 / ontology_query / relation_qa 标注 DEPRECATED（保留供回退，不硬删）

## [2.0.2] - 2026-08-06

### 新增
- **多数据源支持**：`data_loader.py` 统一读取 CSV / JSON / SQLite / Excel（前三种标准库零依赖，Excel 可选 openpyxl）；`csv_to_owl.py` 与 `multi_table.py` 均支持

### 修复（CodeAgent 代码审查发现）
- **data_loader.py**：SQLite 表名拼接前加合法标识符校验，消除 SQL 注入风险（表名来自库内，校验后安全）
- **multi_table.py**：移除未使用的 `csv` / `json` import（改 data_loader 后的残留）
- **csv_to_owl.py**：移除未使用的 `os` import

## [2.0.1] - 2026-08-06

### 新增
- **Web 前端**（`web/`）：Svelte5 + Vite + Node 的完整问答应用，CSV 上传 → 建模 → 自然语言问答 → 知识图谱/分析看板。已移除硬编码私有路径，指向仓库内 `codes/` 套件

## [2.0.0] - 2026-08-06

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

## [1.0.0] - 2026-08-06（初始开源发布）

- CSV → 本体（N-Triples，类型自动推断）
- 词典驱动通用问答引擎（规则 + LLM 兜底）
- 交付方法论 / 白皮书 / 开源调研文档
