# 开源社区"本体/知识图谱 + LLM 问答"竞品生态调研
### 对标：factory-ontology（CSV/多表→轻本体→规则引擎确定性问答→逻辑推理桥→本体引导GraphRAG→BM25+向量RRF→证据溯源→多租户；面向中小企业制造业；零依赖纯标准库+数据不出厂+换行业只换配置）
**调研日期：2026-09-03　数据源：GitHub REST API 实测（star 为 API 实时值，非文档值）　环境：GFW**

---

## 〇、一句话结论
主流开源"图谱问答"全部站在**两条你反对的路**上：要么拿 LLM 从非结构化文档里**抽图**（不可控、费算力、难溯源），要么把表格**交给 LLM 写 SQL**（Text-to-SQL，有幻觉与越权风险）。**显式本体 schema + 确定性规则引擎 + 换行业只换配置 + 零依赖纯标准库**这条路的开源产品几乎是真空，最接近的两家（Semantica、OpenSPG/KAG）都很重，且都面向云端/监管/大型语料，不是轻量本地化路线。

---

## 一、Semantica 详查（最强概念对标，非最强产品对标）
- **semantica-agi/semantica**　star **11,746**（实测）／fork 1,320／Python／MIT／创建 2025-06-25／最近 push **2026-09-02**（活跃）／https://getsemantica.ai
- 自封 **"The Open Source Palantir for AI Agents"**，是社区里唯一把"本体治理 + 图 + 决策溯源 + 审计"串成产品叙事的项目。口号几乎踩中你全部卖点：Ontology Management、Deterministic Reasoning、Decision Intelligence、End-to-End Traceability、W3C 标准、RDF+LPG 双图、OWL/SHACL/SKOS 受控词表。
- **verify_chain 哈希链（题主关心的点，实读源码确认）**：
  - `semantica/provenance/integrity.py`：用 **SHA-256** 做 `compute_checksum/verify_checksum`，对 provenance 记录逐条算确定性校验和。
  - 关键设计（issue #825）：每条记录 `previous_checksum` **链式指向前一条**，`ProvenanceStorage.get_chain_head()` 取链头，`ProvenanceManager.verify_chain()` 验链——**整行被删会断后续某条的链，从而"删行"这种抹除可被探测**（单纯逐行校验和只能证明幸存行没被就地篡改）。
  - `compute_checksum` **刻意排除 entity_id**（因为版本化重标号会把主键 X→X:v:…，若哈希会误报断链）；但纳入 agent_id/lineage 字段（parent_entity_id、previous_version_id、derived_from_id）防止篡改归因。
  - 删除走 **tombstone 作废追加**而非硬删（`invalidated*` 映射 PROV 的 Invalidation），保持链不裂。
  - 语义层：**W3C PROV-O 合规**，Unified `ProvenanceManager` 汇聚 KG/分块/来源三类 tracker，支持 InMemory/SQLite 后端，可导出 RDF/PROV（`export_prov()`）。合规瞄准 FDA 21 CFR Part 11、SOX、HIPAA。
- **决策记录**：`semantica/context/decision_*`（decision_recorder/query/models/methods）把"为什么给这个结论"作为一等实体建模，另有 provenance.md / decision-intelligence.md 文档与 MCP/工具集成。
- **对本项目的意义**：Semantica 证明"确定性 + 决策溯源"叙事在 2026 社区有真需求、能拿 star。但它是 **Python 包 + 可插拔图存储/向量库的分布式重型平台**，**非零依赖、非纯标准库、不面向换行业只换配置**，且定位监管/溯源大平台而非中小企业本地可交付。它是你的"叙事参照物"，不是"贴身竞品"。

---

## 三、GraphRAG 微软家族最新形态
- **microsoft/graphrag**　star **35,802**（实测）／MIT／README 首行 WARNING（2026 实测）：**"largely in maintenance mode, won't be accepting new PRs or implementing new features. We'll perform bug fixes and dependency updates as appropriate"**。
- 这是关键信号：**微软官方 GraphRAG 已进入维护冻结**。其自述原因是"frontier 模型能力变化 + 微软研究组合已多元化"，即团队重心转移。
- 家族现状分化成三股：
  1. **官方原版**：GraphRAG 2.x 定型能力（实体/关系/社区检测抽取、局部全局检索、DRIFT 搜索）冻结，靠社区维护。
  2. **轻量复刻/改进**：HKUDS/LightRAG（star **39,349**，EMNLP 2025，更快更省），gusye1234/nano-graphrag（**3,981**，可 hack 的教学实现）。这些主打"轻"，但轻的是**工程栈**，仍是 LLM 抽图路线。
  3. **"可解释后起者"叙事被 Semantica 等接走**。
- 对本项目：GraphRAG 家族本质是**非结构化文档抽图**，数据源是文本不是 CSV 表格，且 LLM 抽图天然不可控、无 schema 约束、难证据溯源——恰是你"本体引导 + 确定性 + 证据"要纠正的病灶。family 里没有一家做"本体 schema 约束下生成图"的中小企业表格问答。

---

## 四、竞品横向盘点
### 4.1 直接竞品与概念对标（本体驱动数据问答 / Palantir 类）
| 项目 | star实测 | 思路 | 与本项目差异 |
|---|---|---|---|
| semantica-agi/semantica | 11,746 | 开源 Palantir：本体治理+双图+确定性推理+决策溯源(W3C PROV-O) | 重型 Python 平台；监管/溯源定位；非零依赖非换行业换配置 |
| OpenSPG/openspg | 2,227 | 蚂蚁+OpenKG，SPG 语义增强可编程图谱引擎，Java，领域建模约束 | Java 重引擎，面向专业图谱平台；不轻量 |
| OpenSPG/KAG | 9,035 | 基于 OpenSPG 的逻辑形式引导推理检索框架（LGKR），专业域 KB | 需配套 OpenSPG Java 底座，重；强在推理形式化，非零依赖交付 |
| apache/jena | 1,429 | 经典 RDF/SPARQL/OWL 语义网框架 | 底层引擎库，非"开箱问答产品"，重 Java |
| LightRAG | 39,349 | LLM 抽图的轻量 GraphRAG | 非结构化文本抽图；不可控；非表格/本体 |
| cognee / graphiti / llm-graph-builder | 30,426/30,536/5,205 | agent 记忆图 / 实时 KG / Neo4j LLM 构图 | 面向 agent 长期记忆与文档，LLM 构图，非确定性问答 |

### 4.2 文档 RAG vs 结构化本体问答的分野
- **文档 RAG 产品（你并非同赛道，但用户常拿来替代）**：infiniflow/ragflow（star **89,939**，Go，全球最热 RAG）、netease-youdao/QAnything（**14,084**，AGPL，2025-03 后近乎停更）、labring/FastGPT（**29,548**）。共同点：解析 PDF/网页 → 切块 → 向量/混合检索 → LLM 生成。**对"要准确数字/多表 join/可解释"的制造业问数是错配**——只能召回相关片段，不能算出"3 号产线本月 A 料用量超阈值"这类确定性答案，也无字段级溯源。
- **结构化本体问答**恰是文档 RAG 的补位，但社区几乎只有 Semantica/OpenSPG 这类重方案在讲，**没有轻量开箱件**。

### 4.3 国内中文本体 / 工业知识图谱
- 开源国产几乎全押"大平台 + 云 + 大语料"：OpenSPG/KAG（蚂蚁-OpenKG，本体/语义网味最正）、RAGFlow、FastGPT、DB-GPT（蚂蚁系）都是**需要起服务、吃显存/云 API** 的重方案。
- **中小企业制造业"零依赖、Excel 落本地、不出厂、换行业换配置"的国产可交付件未发现成熟开源项目**——这块通常被内包/外包（收费软件、项目制）或纯 Excel+规则脚本吃掉，不沉淀为可复用开源。这是空白的国产化佐证。

### 4.4 结构化表格问数据替代：Text-to-SQL vs 确定性本体
- **Vanna（vanna-ai/vanna）**　star **23,816**／**已 archived（实测 archived=true，push 止 2026-02）**。曾被视作"用 SQL 数据库做 NL→SQL"标杆（agentic retrieval 训练 DDL/文档）。**归档**本身说明"纯 LLM Text-to-SQL 当通用问答引擎"这条路商业上没走通/转向——对你是利好。
- **eosphoros-ai/DB-GPT**　star **19,852**／活跃：Agentic AI 数据助手，含 NL2SQL，要部署服务。
- **Canner/WrenAI**　star **17,462**／push 2026-09-03（最活跃）：GenBI，语义层(Semantic Layer)+受控 Text-to-SQL，"治理"(governed)是卖点，最接近"用 schema 约束 LLM"的思考，但仍是"LLM 写 SQL"范式。
- **dataease/SQLBot**　star **6,729**／活跃：中文项目，RAG+NL2SQL 对话分析。
- **zylon-ai/private-gpt**　star **57,489**：本地私有大 API 层，含 text-to-sql 模块。
- **对本项目**：Text-to-SQL 的痛点是**生成不可保证正确、无真证据链、跨表口径易错**。确定性本体路线的价值正来自把"口径"固化进 schema/规则而非每次让 LLM 现编。两者可互补（本体兜底确定性问答 + LLM 兜语义/闲聊），这是社区尚未产品化的组合。

---

## 五、竞品对比总表
| 项目 | star实测 | 方向 | 强项 | 弱项 | vs 本项目 |
|---|---|---|---|---|---|
| semantica-agi/semantica | 11,746 | 开源Palantir/本体治理+溯源 | 决策溯源哈希链、PROV-O、叙事完整 | 重、Python+图库+向量、云/监管向 | 叙事近，工程与交付定位远；可借其溯源设计 |
| microsoft/graphrag | 35,802 | LLM抽图文档GraphRAG | 方法论文献地位高 | 已维护冻结、抽图贵不可控 | 反面对照：抽图非表格确定性 |
| LightRAG | 39,349 | 轻量GraphRAG | 快省、论文背书 | 非结构化抽图、无schema | 不同数据源与可信度路线 |
| nano-graphrag | 3,981 | 教学GraphRAG | 轻、可hack | 单点demo级 | 可参考其轻工程取舍 |
| cognee | 30,426 | agent记忆KG | 记忆+图、活跃 | LLM构图、面向agent | 不同需求 |
| graphiti | 30,536 | 实时agent知识图 | 时序/实时增量 | 非表格确定性问答 | 不同需求 |
| ragflow | 89,939 | 文档RAG引擎 | 生态最热、产品化 | 文档切块检索、非算数 | 补位而非替代，可协作 |
| QAnything | 14,084 | 文档RAG | 中文场景成熟 | AGPL、近停更 | 同上 |
| FastGPT | 29,548 | 知识库+RAG工作流 | 开箱工作流 | 非结构化、要服务 | 同上 |
| OpenSPG/openspg | 2,227 | SPG本体图谱引擎(Java) | 领域建模强、蚂蚁背书 | 重、要配套 | 本体思想可借鉴，太重 |
| OpenSPG/KAG | 9,035 | 逻辑形式引导检索推理 | 推理形式化、专业域 | 依赖OpenSPG重底座 | 形式化推理可比；非轻量 |
| apache/jena | 1,429 | RDF/SPARQL引擎 | 标准、稳定 | 引擎库非产品、Java | 底层参考非竞品 |
| Vanna | 23,816 | NL→SQL | 曾标杆、SQL生态 | **已archived**、生成不可保证 | 证明纯Text2SQL走不通 |
| DB-GPT | 19,852 | Agentic数据助手 | 全栈、中英 | 重部署 | 大而全非轻本体 |
| WrenAI | 17,462 | 受控Text-to-SQL | 语义层治理、活跃 | 仍LLM写SQL | 治理思路接近，范式不同 |
| SQLBot | 6,729 | 中文RAG+NL2SQL | 中文、仪表盘 | 无确定性/证据链 | 同为问数，方法不同 |

---

## 六、社区空白判断（题主核心问题）
**"零依赖 + 确定性优先 + 换行业只换配置"的轻本体表格问答，在开源社区是少见中的少见，几乎没有真竞品。**

依据：
1. **确定性/溯源叙事虽火，但都在"重"一侧**。唯一高举"确定性+本体+决策溯源"大旗的 Semantica 是一个 Python 全家桶平台（要图库、要向量库、要跑 pipeline），它证明了**需求真实**，却反证了**"轻量零依赖可交付"没人做**——因为它和你会拿同一批理由营销，但它交付的是重型基建。
2. **大star图谱项目全走两条反路线**：LLM 抽文档图（GraphRAG 系，微软官方已冻结）或 Text-to-SQL（Vanna 已归档）。这两条路都**不可保证正确、难字段级溯源**，恰是本项目"确定性规则 + 证据溯源"要解决的问题。没人站在"显式本体 schema 固化口径 + 规则引擎算数 + 换行业换配置"这一侧。
3. **国产全是"平台/云/服务化"**（OpenSPG/KAG、RAGFlow、FastGPT、DB-GPT），无轻量本地、零依赖、纯标准库的中小制造问答开源件；这块被外包与 Excel+脚本吃掉，未沉淀为可复用项目。
4. **真正的"伪竞品"威胁不在开源**，而在：(a) 客户自购 RDBMS 后"直接写 SQL 视图/报表 + 确定性模板"（不需要你的本体层——对策：把本体价值做成"改表不返工 + 口径单一事实源 + 跨表溯源"）；(b) 小团队用自家脚本。（a）是你要主动论证"为什么需要一层本体"的地方。

**结论**：factory-ontology 占据的"确定性优先 + 轻本体 + 零依赖 + 配置化换行业"定位，在 2026 开源图谱问答生态中**未见成熟同款开源项目**，属真空白。最大的借力点是 Semantica（溯源/哈希链/PROV-O 设计可低成本借鉴）、LightRAG/nano-graphrag（轻工程取舍）、OpenSPG（本体建模理念）。最大的定位风险是需向市场讲清"RDBMS+确定性视图为何不够、需要本体层"。

---

## 附：方法与来源说明
- star/fork/push/archived/license 均为 2026-09-03 经 api.github.com/repos/{owner}/{repo} 实测（Vanna archived=true、microsoft/graphrag README maintenance 警告为源码实读）；未发现即可信度存疑处已标注。
- 待核实（未深读源码，仅 README 层面）：Semantica 实际部署资源门槛、KAG 在多租户下的工程成熟度。
- 仓储：`C:\Users\zjjem\_res_ont_kg\`（gh.py 采集脚本 + 本报告）；原 E:/ 目录本机未挂载，已就近落盘。
