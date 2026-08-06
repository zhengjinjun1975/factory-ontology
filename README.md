# Factory Ontology Kit — 工厂本体驱动的数据问答框架

> 本体建模 → 大模型落地的开源实现：把任意结构化数据（CSV）自动转成"实体-关系-属性"语义本体，再提供自然语言问答。**换任何工厂/领域，只换数据，代码不动。**

[![Version](https://img.shields.io/badge/version-2.0.2-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-blue)](https://www.python.org/)

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
- 上传任意 CSV → `run.py setup` 建模 → `ontology_qa_v3` 问答
- 含模型切换、知识图谱结构图、分析看板
- 注意：分析面板目前按"工厂设备 + 产线"场景设计（读 `data/equipment.csv` + `line.csv`）；用自定义数据时，**上传→建模→问答**主流程不受影响

## 核心组件

| 模块 | 作用 |
|------|------|
| `csv_to_owl.py` | 数据→本体（零依赖，类型自动推断，N-Triples） |
| `factory_agent.py` | 一站式入口：自动建模 + 问答 |
| `ontology_qa_v3.py` | 通用问答引擎（规则优先 + 词典驱动） |
| `ontology_depth.py` | 深度增强（时序观测、状态/优先级推断） |
| `analysis.py` | 智能分析（统计摘要 + LLM 洞察） |
| `agents/lexicon_agent.py` | 全自动词典生成（LLM 推断字段语义） |
| `config/` | 模型配置 + 词典 + 关系外置（换领域只换配置） |

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

- 数据形态：最适配"结构化台账/单表"，时序/多表需扩展
- 语义校对：自动词典偶有误判（如产品等级被当设备类型），需人确认关键字段
- LLM 稳定性：本地模型偶发空响应，生产建议用更强模型
- 只支持 CSV：Excel/DB 需先导出 CSV

## 文档

- [交付方法论：本体建模 → 大模型落地](docs/交付方法论.md)
- [工厂智能体交付白皮书](docs/交付白皮书.md)
- [本体建模开源全景调研](docs/开源调研.md)

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
