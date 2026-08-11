# Factory Ontology Kit：工厂本体驱动的数据问答框架

> 把任意结构化数据（CSV）自动转成“实体-关系-属性”语义本体，再提供自然语言问答。换任何工厂或领域，只换数据，代码不动。

[![Version](https://img.shields.io/badge/version-0.1.4-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue)](https://www.python.org/)
[![CI](https://github.com/zhengjinjun1975/factory-ontology/actions/workflows/ci.yml/badge.svg)](https://github.com/zhengjinjun1975/factory-ontology/actions)

## 它解决什么问题

工厂里的数据多半躺着，没人会用。MES、SCADA、台账堆在系统里，业务人员不会 SQL，IT 人员忙不过来。想查“哪台设备要维护”“报警的空压机有几台”，要么翻系统、要么找 IT、要么靠老师傅口口相传。数据不是没有，是答不出来。

这个项目把数据变成会说人话的智能体：业务人员用自然语言提问，立刻得到答案，还能看到答案从哪条记录来。

```
用户: "哪台设备要优先维护?" "报警的空压机有几台"
              │
              ▼
    问答引擎 (ontology_qa_v3 + graph_rag)
      ① 自动建模(本体+词典)
      ② 规则引擎(确定性, 快准)
      ③ LLM兜底(语义泛化, 灵活)
              │
              ▼
    现场数据: 设备台账/传感器/工单 (CSV)
```

## 一个关键的决定

大多数同类工具靠 LLM 生成 SQL 或者直接向量检索。这个项目的起点不一样：**先建本体**，把裸表变成有业务含义的“实体-关系-属性”结构，再在这个结构上做问答。本体起到两重作用，一是把查询空间约束住，二是让答案可以溯源。规则能答的问题，走规则，确定性 100%、零 token、零幻觉；规则答不了的模糊问题，才交给 LLM 兜底。

这套做法的代价是，不如 Text-to-SQL 那样直接落到原库，多了一步建模的认知负担。换来的是可解释和可复现。定位不在“最强”，在“中小工厂结构化数据场景够用、能落地、敢承诺确定性”。

## 快速开始

```bash
# 1. 克隆后, 用公共示例数据集建模 (UCI AI4I 预测性维护数据集, 已含)
cd codes
python run.py setup data/ai4i.csv ai4i

# 2. 自检
python run.py test

# 3. 自然语言问答
python run.py ask "有多少台设备在运行"
python run.py ask "哪台设备要优先维护"
```

### 换你自己的数据（3 步，零手写）

```bash
# 1. 转本体: 任何 CSV → 本体
python csv_to_owl.py 你的数据.csv output.nt

# 2. 自动词典: LLM 推断字段语义 → 中文词典 (lexicon_agent)
# 3. 直接问: 通用模板引擎 (ontology_qa_v3) 回答
python run.py setup 你的数据.csv
```

问答逻辑不绑定具体词表，靠词典驱动。换数据源只换词典，代码不动。

### 多表自动关联建本体

`multi_table.py` 从多个关联 CSV 表自动检测外键并生成统一跨表本体，无需手写 relations.json：```bash
python multi_table.py output/multi.nt data/equipment.csv data/line.csv data/supplier.csv
```

外键自动检测规则（任一命中即判为外键，生成 owl:ObjectProperty）：1. 列名 = `<目标表名>_id` / `<目标表名>Id`（如 `line_id` → line 表）
2. 列名去掉 `_id` 后 == 目标表的 id 列名
3. 目标表 id 列与当前列同名

每个表一个类，每行一个实例，普通列是数据属性，外键列是对象属性跨表链接。示例表在 `data/line.csv`、`equipment.csv`、`supplier.csv`（合成数据，便于复现）。

### 多数据源

`data_loader.py` 统一读取 CSV / JSON / SQLite / Excel，`csv_to_owl` 与 `multi_table` 均支持：```bash
python csv_to_owl.py data/equipment.json output/equipment.nt      # JSON
python csv_to_owl.py data/equipment.db  output/equipment.nt       # SQLite(取第一个表)
python csv_to_owl.py data/equipment.xlsx output/equipment.nt      # Excel(需 pip install openpyxl)
```

CSV / JSON / SQLite 用标准库实现，零依赖。Excel 可选 `openpyxl`。SQLite 取库中第一个表。

## 核心概念

**本体在这里是“受限的 schema”，不是 W3C 标准本体。** 立项时就刻意避开 RDF/SHACL/SPARQL 那一套完整语义网标准。代价是少了标准互操作和推理能力，收益是业务人员能看懂、零门槛上手、换领域只换数据。这个取舍是刻意的，不藏着。

三条核心原则：1. **建模是桥不是终点**： 建本体是为了让大模型“懂领域”，别为建模而建模
2. **词典外置**： 问答逻辑不绑具体词表，换领域只换词典，代码不动
3. **规则优先，LLM 兜底**： 确定性走规则（快准零成本、可复现），模糊问题才走 LLM

## 架构

README 源文件图在 `docs/diagrams/`，GitHub 自动渲染。链路一句话讲清楚：企业台账 → 数据接入（data_loader + 质量校验）→ 本体构建（multi_table）→ 问答检索（规则 → GraphRAG 兜底）→ API → APP / 语音 / 证据导出。

- **系统级架构**：`docs/diagrams/architecture.svg`
- **数据走向**：`docs/diagrams/dataflow.svg`
- **工厂落地路线**：`docs/diagrams/roadmap.svg`

分层：交付层（APP/语音/管理后台）→ API 层（FastAPI）→ 问答推理层（规则→逻辑桥→GraphRAG，确定性优先）→ 本体层（本体图 + 词典外置）→ 数据层（多格式）→ 模型层（云 DeepSeek / 本地 Ollama 可切换）。

## 核心组件

| 模块 | 作用 |
|------|------|
| `data_loader.py` | 统一读取 CSV/JSON/SQLite/Excel |
| `multi_table.py` | 多表自动关联建本体（自动外键检测 → 跨表对象属性） |
| `schema_ontology.py` | schema 驱动统一建模 + `suggest_schema` 自动推断 |
| `ontology_qa_v3.py` | canonical 通用问答引擎（规则优先 + 词典驱动） |
| `graph_rag.py` | GraphRAG-lite：建图 + 图遍历检索 + LLM 生成 |
| `graph_store.py` | SQLite 图持久化（10万-100万实体过渡） |
| `model_llm.py` | 模型调用薄封装（云/本地可切换） |
| `api_server.py` | REST API（FastAPI）：问答 + 正/反向溯源 + 扫码 + 统计 |
| `benchmark.py` | 对照评测（规则引擎 vs 纯 LLM） |
| `benchmark_graphrag.py` | GraphRAG 开放式问题命中率评测 |
| `data_import.py` | 数据接入自动化（Excel/DB → 知识库，定时同步） |
| `data_quality.py` | 数据质量自动校验 |
| `monitor.py` | 服务监控看门狗 |
| `agents/lexicon_agent.py` | 自动词典生成（LLM 推断字段语义） |
| `config/` | 模型配置 + 词典 + 关系 + 多租户注册表 |

问答引擎以 `ontology_qa_v3.py` 为唯一 canonical 核心，兜底走 GraphRAG（与 API 层一致）。

### REST API（api_server.py）

FastAPI 服务，APP / 语音 / Web 的统一入口：```bash
cd codes && pip install fastapi uvicorn
python api_server.py          # http://localhost:8000
```

| 端点 | 说明 |
|------|------|
| `POST /api/ask` | 自然语言问答（规则引擎 → GraphRAG 兜底） |
| `GET /api/trace/forward?batch=B001` | 正向溯源：批次 → 产品 + 原料 |
| `GET /api/trace/reverse?raw=RM008` | 反向溯源：原料 → 受影响批次 → 产品 |
| `GET /api/scan?code=P003-B005` | 扫码溯源（识别产品批次） |
| `GET /api/stats` | 知识库统计 |
| `POST /api/admin/rebuild` | 接新数据后强制重建本体（需 admin Key） |
| `GET /metrics` | 指标：各端点请求计数 |

### Web 前端（web/）

Svelte5 + Vite + Node，实现 CSV 上传 → 本体建模 → 自然语言问答 → 知识图谱/分析看板全流程：```bash
cd web
npm install          # 装依赖
npm run build        # 构建前端
npm start            # 启动服务 http://localhost:3001
```

后端 `server/` 通过 child_process 调用 `codes/` 套件（已去除硬编码私有路径，全相对）。上传文本数据（CSV/JSON）→ `run.py setup` 建模 → `ontology_qa_v3` 问答；二进制 SQLite/Excel 走命令行。

## 示例场景（合成数据，非真实）

`data_valve/` 是**合成示例数据**，用来实证“换领域即用”。它对设备/阀门台账类结构化业务数据的适用性，和一个真实阀门厂的数据形态一致：```bash
cd codes
python valve_demo.py
```

输出示例：
- **规则问答**：`一共有多少个阀门` → 6；`价格最贵的阀门` → 安全阀A42Y（3980 元）
- **逻辑桥**：`有多少种球阀` → 1（LLM 转逻辑 → 确定性执行）
- **反向溯源**：`密封圈RM03(不合格) → VB02 → V02 球阀`
- **benchmark**：13/13 = 100%（规则引擎确定性）

数据：`data_valve/*.csv`（合成）+ `config/lexicon_valve.json`（阀门词典）。

## 定位与横向对比（诚实）

它不是通用 AI 平台（Dify/RAGFlow），不是大规模图 RAG（GraphRAG/LightRAG），也不是完整 Text-to-SQL 方案（Chat2DB/Vanna）。它的位置在“KAG 的轻量确定版 + 垂直溯源场景”，补的是“轻量 + 确定性 + 可解释 + 中小厂台账问答”这一格。

| 产品 | 本体建模 | 确定性(结构化) | 溯源 | 部署 |
|------|:---:|:---:|:---:|:---:|
| Dify / RAGFlow / FastGPT | ❌ | ❌ | ❌ | 重 |
| LightRAG / GraphRAG(微软) | ❌ | ❌ | ❌ | 重 |
| Text-to-SQL(Chat2DB/Vanna) | ❌ | 中 | 中 | 中 |
| KAG(蚂蚁) | ✅ | ❌ | ❌ | 重 |
| **factory-ontology** | ✅ | **✅** | **✅** | **极轻** |

三条真实差异，不夸大：1. **确定性**：规则引擎对结构化查询 100% 命中、零 token、零幻觉，这部分全社区独有。但只覆盖结构化查询（数量/极值/平均/过滤/范围），开放式/模糊问题要靠 GraphRAG 或 LLM。
2. **可溯源**：正/反向溯源是召回场景的核心，答案能指出支撑它的记录。
3. **落地轻**：纯标准库核心、本地化部署、数据不出厂。代价是规模受限，中小台账级，不适合百万级实体图。

诚实短板也摆明：社区和生态极小、无分布式、无大规模生产部署故事、非结构化文本（工艺手册/ISO 文档）暂不处理。它是可运行的演示方法 + 可落地的对照实现，不是开箱即用的工业级生产平台。

## 实测验证

用 UCI AI4I 2020 预测性维护数据集（1 万条真实制造设备传感器 + 故障）验证：
| 指标 | 结果 |
|------|------|
| 数据规模 | 10,000 条 / 14 列 |
| 自动建模时间 | <2 分钟 |
| 全自动词典 | 12 个属性中文名自动生成 |
| 本体规则命中率 | 61/61 = 100%（结构化查询） |
| 问答能力 | 数量/极值/范围/统计/组合/区域全类型 |
| 语义泛化 | 规则未命中自动降级 LLM |

## 工程化

- **鉴权**：设 `FOOD_ADMIN_KEY` / `FOOD_READ_KEY` 后，/api/* 需 `X-API-Key` 头（管理 vs 只读双角色）；不设则内网开放
- **增量重建**：数据文件 hash 检测，数据未变复用缓存本体，变了才重建
- **Docker 部署**：`docker compose up -d`（`Dockerfile` + `docker-compose.yml`），HTTPS 反代见 `nginx.conf`
- **数据接入**：`python data_import.py <Excel/DB/CSV> --schedule N` 自动同步台账
- **数据质量**：`python data_quality.py` 自动校验异常
- **多知识库**：`FOOD_DATA_DIR` / `kbs.json` 切换多企业知识库
- **图存储**：`graph_store.py` SQLite 图持久化（20 万实体实测）

### 添加你的工厂数据（新企业落地）

```bash
cd codes
python new_kb.py <知识库名> --name "企业显示名" --icon "🏭"
```

之后三步：把数据放进 `data_<知识库名>/`（按表结构），编辑 `config/lexicon_<知识库名>.json` 设中文字段名，设 `FOOD_KB=<知识库名>` 启动。APP 的品牌/图标/示例问题自动从 kbs.json 读取，无需改代码。验证用 `data_quality.py` + `benchmark_graphrag.py`。

## 设计原则

1. **建模是桥不是终点**： 让大模型“懂领域”，别为建模而建模
2. **原子性是积木**： 模块单一职责、可组合、可替换
3. **编排器是组织者**： 轻量调度器按任务类型路由，零依赖可部署现场
4. **泛化靠外置**： 词典/字段映射外置成配置，换领域只换配置
5. **规则兜底 + LLM 泛化**： 确定性走规则，模糊走 LLM
6. **零依赖可部署**： 纯标准库，能带到任何现场

## 文档

- `docs/方法论文-本体vs裸LLM.md`： 实证论文：本体规则引擎 100% vs 裸 LLM 78%（+22pp）
- `docs/泛化方法论.md`： 领域无关泛化方法论 + 多领域 benchmark 实证
- `docs/交付方法论.md` / `docs/交付白皮书.md`： 现场落地方法
- `docs/开源调研.md`： 本体/知识图谱开源生态调研
- `codes/food_demo.py`： 食品企业溯源可复现案例（一条命令运行）
- `docs/部署.md`： 小型企业部署指南（API + APP + 语音，换数据即用）

## 版本

完整变更见 [CHANGELOG.md](CHANGELOG.md)。

## License

[Apache License 2.0](LICENSE)
