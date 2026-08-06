# Factory Ontology Kit — 工厂本体驱动的数据问答框架

> 本体建模 → 大模型落地的开源实现：把任意结构化数据（CSV）自动转成"实体-关系-属性"语义本体，再提供自然语言问答。**换任何工厂/领域，只换数据，代码不动。**

[![Version](https://img.shields.io/badge/version-2.7.1-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue)](https://www.python.org/)
[![CI](https://github.com/zhengjinjun1975/factory-ontology/actions/workflows/ci.yml/badge.svg)](https://github.com/zhengjinjun1975/factory-ontology/actions)

## 它解决什么问题

工厂现场的三座大山：

| 痛点 | 现状 | 代价 |
|------|------|------|
| 数据看不懂 | MES/SCADA/台账数据躺在系统里，业务/运维人员不会 SQL | 决策靠老师傅经验 |
| 信息难找 | 想查"哪台设备要维护/报警"要翻系统、找 IT 人 | 效率低、响应慢 |
| 知识断层 | 老师傅退休带走经验，新人上手慢 | 知识流失 |

**核心价值**：把任意结构化数据变成"会说人话的智能体"——业务人员用自然语言提问，立即得到答案。

```
用户: "哪台设备要优先维护?" "报警的空压机有几台"
              │
              ▼
    工厂智能体 (factory_agent)
      ① 自动建模(本体+词典)
      ② 规则引擎(确定性, 快准)
      ③ LLM兜底(语义泛化, 灵活)
              │
              ▼
    现场数据: 设备台账/传感器/工单 (CSV)
```

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
python factory_agent.py setup 你的数据.csv
```

**问答逻辑不绑定具体词表，靠词典驱动。换数据源只换词典，代码不动。**

### 多表自动关联建本体（#3）

`multi_table.py` 从多个关联 CSV 表**自动检测外键**并生成统一跨表本体，无需手写 relations.json：

```bash
# 自动检测 equipment.line_id -> line, 生成 hasLine 对象属性链接
python multi_table.py output/multi.nt data/equipment.csv data/line.csv data/supplier.csv
```

外键自动检测规则（任一命中即判为外键，生成 owl:ObjectProperty）：
1. 列名 = `<目标表名>_id` / `<目标表名>Id`（如 `line_id` → line 表）
2. 列名去掉 `_id` 后 == 目标表的 id 列名
3. 目标表 id 列与当前列同名

每个表 → 一个类，每行 → 一个实例，普通列 → 数据属性，外键列 → 对象属性跨表链接。示例表在 `data/line.csv`、`equipment.csv`、`supplier.csv`（合成数据，便于复现）。

### 多数据源（#4）

`data_loader.py` 统一读取 CSV / JSON / SQLite / Excel，`csv_to_owl` 与 `multi_table` 均支持：

```bash
python csv_to_owl.py data/equipment.json output/equipment.nt      # JSON
python csv_to_owl.py data/equipment.db  output/equipment.nt       # SQLite(取第一个表)
python csv_to_owl.py data/equipment.xlsx output/equipment.nt      # Excel(需 pip install openpyxl)
```

- CSV / JSON / SQLite 用标准库（零依赖）；Excel 可选 `openpyxl`
- JSON 格式：`[{"col":值,...}]` 或 `{"rows":[...]}` / `{"data":[...]}`
- SQLite 取库中第一个表

## Web 前端（web/）

仓库自带完整 Web 应用（Svelte5 + Vite + Node），实现 **CSV 上传 → 本体建模 → 自然语言问答 → 知识图谱/分析看板** 全流程：

```bash
cd web
npm install          # 装依赖
npm run build        # 构建前端
npm start            # 启动服务 http://localhost:3001
```

- 后端 `server/` 通过 child_process 调用仓库 `codes/` 套件（已去除硬编码私有路径，全相对）
- 上传文本数据（CSV/JSON）→ `run.py setup` 建模 → `ontology_qa_v3` 问答；二进制 SQLite/Excel 走命令行
- 含模型切换、知识图谱结构图、分析看板
- 注意：分析面板目前按"工厂设备 + 产线"场景设计（读 `data/equipment.csv` + `line.csv`）；用自定义数据时，**上传→建模→问答**主流程不受影响

## 核心组件（精炼核心路径）

| 模块 | 作用 |
|------|------|
| `data_loader.py` | 统一读取 CSV/JSON/SQLite/Excel |
| `multi_table.py` | 多表自动关联建本体（自动外键检测 → 跨表对象属性） |
| `ontology_qa_v3.py` | **canonical 通用问答引擎**（规则优先 + 词典驱动） |
| `graph_rag.py` | **GraphRAG-lite**：建图 + 图遍历检索 + LLM 生成（开放式/关系问题） |
| `graph_store.py` | SQLite 图持久化（10万-100万实体过渡） |
| `model_llm.py` | 模型调用薄封装 |
| `api_server.py` | **REST API**（FastAPI）：问答 + 正/反向溯源 + 扫码 + 统计 |
| `benchmark.py` | 对照评测（规则引擎 vs 纯 LLM） |
| `benchmark_graphrag.py` | GraphRAG 开放式问题命中率评测 |
| `data_import.py` | 数据接入自动化（Excel/DB → 知识库，定时同步） |
| `data_quality.py` | 数据质量自动校验 |
| `monitor.py` | 服务监控看门狗 |
| `agents/lexicon_agent.py` | 自动词典生成（LLM 推断字段语义） |
| `config/` | 模型配置 + 词典 + 关系 + 多租户注册表 |

> 问答引擎以 `ontology_qa_v3.py` 为唯一 canonical 核心，兜底走 GraphRAG（与 API 层一致）。早期研究框架的 agent 编排层已精炼移除。

### REST API（api_server.py）

FastAPI 服务，APP / 语音 / Web 的统一入口（以食品企业知识库为例）：

```bash
cd codes && pip install fastapi uvicorn
python api_server.py          # http://localhost:8000
```

| 端点 | 说明 |
|------|------|
| `POST /api/ask` | 自然语言问答（规则引擎 → GraphRAG 兜底） |
| `GET /api/trace/forward?batch=B001` | 正向溯源：批次 → 产品 + 原料 |
| `GET /api/trace/reverse?raw=RM008` | **反向溯源**（食品安全）：原料 → 受影响批次 → 产品 |
| `GET /api/scan?code=P003-B005` | 扫码溯源（识别产品批次） |
| `GET /api/stats` | 知识库统计 |
| `POST /api/admin/rebuild` | **管理**：接新数据后强制重建本体（需 admin Key） |
| `GET /metrics` | 指标：各端点请求计数 |

### 工程化（v2.3.0）

- **鉴权**：设 `FOOD_ADMIN_KEY` / `FOOD_READ_KEY` 后，/api/* 需 `X-API-Key` 头（管理 vs 只读双角色）；不设则内网开放
- **增量重建**：数据文件 hash 检测，数据未变复用缓存本体，变了才重建——接新数据自动生效
- **Docker 一键部署**：`docker compose up -d`（`Dockerfile` + `docker-compose.yml`），HTTPS 反代见 `nginx.conf`
- **数据接入**（v2.4.0）：`python data_import.py <Excel/DB/CSV> --schedule N` 自动同步台账
- **数据质量**（v2.4.0）：`python data_quality.py` 自动校验异常
- **PWA**（v2.4.0）：APP 可安装、离线可用
- **审计 + 监控 + 合规**（v2.5.0）：审计日志、`monitor.py` 看门狗、`docs/合规.md`（一物一码/召回/标准对齐）
- **实体链接增强 + 多知识库**（v2.6.0）：GraphRAG 词典引导种子定位；`FOOD_DATA_DIR` 切换多企业知识库
- **多租户 + 实时同步 + 图存储**（v2.6.1）：`kbs.json` 多企业隔离、`/api/admin/sync`、`graph_store.py` SQLite 图持久化
- **测试加固 + 双实证**（v2.6.2）：pytest 10 项；GraphRAG 开放式 100%；20 万实体图存储实测

### 移动端 APP + 语音助手

一套部署，三层消费：API / 移动端 APP / 语音助手。

- **移动端 APP**：`GET /`（API 同端口托管）→ 手机浏览器打开即用，含智能问答 / 扫码溯源 / 反向追溯三个面板
- **语音助手**：`python voice_assistant.py "乳制品的数量"` → edge-tts 中文朗读；`--voice` 可选语音输入（需装 faster-whisper）
- **部署**：`docs/部署.md` 面向无专职工程师的小型企业，换数据即用

### 添加你的工厂数据（新企业落地）

```bash
cd codes
python new_kb.py <知识库名> --name "企业显示名" --icon "🏭"
# 例: python new_kb.py valve --name "阀门厂" --icon "🔧"
```
自动搭建：kbs.json 注册 + 数据目录 + 词典模板 + 表结构说明。

之后三步：
1. 把企业数据放进 `data_<知识库名>/`（按 README 的表结构：产品/原料/批次/质检/设备 + 批次-原料关联表）
2. 编辑 `config/lexicon_<知识库名>.json` 设中文字段名
3. 设 `FOOD_KB=<知识库名>` 启动：`python api_server.py`

验证：`python data_quality.py`（数据质量）+ `python benchmark_graphrag.py`（问答命中率）。
APP 的品牌/图标/示例问题自动从 kbs.json 读取，无需改代码。

## 模型配置

`codes/config/model_config.json` 统一管理，`active` 切换即可：

```json
{
  "active": "cloud",
  "models": {
    "local": { "type": "ollama", "base_url": "http://127.0.0.1:11434/api/generate", "model": "ornith:latest", "api_key": "" },
    "cloud": { "type": "openai", "base_url": "https://api.deepseek.com/v1/chat/completions", "model": "deepseek-chat", "api_key": "" }
  }
}
```

- local = 本地 Ollama；cloud = DeepSeek（OpenAI 兼容）
- `api_key` 留空时从环境变量读取
- 规则能答的走规则（省 token、快、准），规则未命中才走 LLM 兜底

## 设计原则（六条通用经验）

1. **建模是桥不是终点** — 让大模型"懂领域"，别为建模而建模
2. **原子智能体是积木** — 单一职责、可组合、可替换
3. **编排器是组织者** — 轻量调度器按任务类型路由，零依赖可部署现场
4. **泛化靠外置** — 词典/字段映射外置成配置，换领域只换配置
5. **规则兜底 + LLM 泛化** — 确定性走规则，模糊走 LLM
6. **零依赖可部署** — 纯标准库，能带到任何现场

## 诚实边界（不夸大）

**定位**：这是"方法论 + 最小可运行对照实现"，**不是** GraphRAG / Neo4j / LlamaIndex 的替代品。能力上它远弱于工业级平台，不追求大规模、不追求语义检索的完备性。

- **能力边界**：规则引擎只覆盖结构化查询（数量/极值/平均/过滤/范围）；开放式/关系/模糊问题走 GraphRAG 或 LLM 兜底，命中率不保证
- **数据形态**：最适配"结构化台账/单表/多表"；不处理非结构化文本、不涉及大规模语义检索（无向量索引、无实体链接，种子定位是子串匹配）
- **规模**：内存图，中小规模台账级；不适合百万级实体图
- **语义校对**：自动词典偶有误判，需人确认关键字段
- **LLM 稳定性**：本地模型偶发空响应；生产建议用更强模型
- **基准**：`docs/方法论文-本体vs裸LLM.md` 实证规则引擎结构化查询 100%（vs 裸 LLM 78%，+22pp），但仅覆盖结构化查询
- **工程化程度**：有 REST API / CI / pytest，但无 Docker、无分布式、无生产级部署故事——它是可跑的演示+方法论，不是开箱即用的生产平台

## 文档

- `docs/方法论文-本体vs裸LLM.md` — **实证论文**：本体规则引擎 100% vs 裸 LLM 78%（+22pp）
- `docs/泛化方法论.md` — 领域无关泛化方法论 + 多领域 benchmark 实证
- `docs/交付方法论.md` / `docs/交付白皮书.md` — 现场落地方法
- `docs/开源调研.md` — 本体/知识图谱开源生态调研
- `codes/food_demo.py` — 食品企业溯源可复现案例（一键运行）
- `docs/部署.md` — 小型企业部署指南（API + APP + 语音，换数据即用）

## 实测验证

用 UCI AI4I 2020 预测性维护数据集（1 万条真实制造设备传感器 + 故障）验证：

| 指标 | 结果 |
|------|------|
| 数据规模 | 10,000 条 / 14 列 |
| 自动建模时间 | <2 分钟 |
| 全自动词典 | 12 个属性中文名自动生成 |
| 问答能力 | 数量/极值/范围/统计/组合/区域全类型 |
| 语义泛化 | 规则未命中自动降级 LLM |

## License

[Apache License 2.0](LICENSE)
