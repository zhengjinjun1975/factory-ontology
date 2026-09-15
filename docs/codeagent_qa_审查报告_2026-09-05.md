# factory-ontology 本体问答核心能力审查报告

- **审查日期**: 2026-09-05
- **审查对象**: `D:/factory-ontology`（开源本体问答项目）
- **审查性质**: 只读 + 真实数据跑测（未改动任何源码）
- **审查人**: leaf 子代理（deepseek-v4-flash）
- **结论速览**: 主问答链「CSV → csv_to_owl/multi_table/schema_ontology → .nt 本体 → ontology_qa_v3 词典规则问答」在**词表与本配套齐全的行业（food / electronics）下工作正常**，数量/列出/极值/TopN/平均/总和/范围/分组等绝大部分模板实测正确；**但存在 3 类真 bug**（out-of-KB 实体计数被静默答成全库总数、多实体裸“类型”枚举跨实体污染、并列极值漏报）与 **词表为空/键不配套的数据问题**（valve 空词表导致类型级问答几乎失效）。已定位可执行修复建议。

---

## 一、审查范围与方法

1. **未改任何代码**；所有结论来自 `read_file` + `python` 实跑 + `grep` 行号证据。
2. 用项目自带入口跑真实数据：
   - `food`：用现成 `output/food.nt`（本体已存在，68 个个体）+ `data/` 下真实 CSV 交叉核验。
   - `valve`：跑项目生产构建路径 `schema_ontology.load_all(data_valve)+to_nt` 生成 `valve.nt`（142 个体，测后已删除生成物）。
   - `electronics`：按生产路径 `csv_to_owl.build_nt` 逐表生成再合并（Products12/Equipment10/Orders20/Customers6，共 48 个体，测后已删除生成物）。
3. 每题同时用 CSV 原生数据人工算答案做“应得值”，对比引擎返回值，防“答了但答错”。
4. 审查产物（临时 .py / .nt）已清理，`git status` 无本审查新增痕迹。

### 问答核心链数据流（摸清结论）

```
CSV 源数据
  codes/data/           （食物：food_*.csv，demo 与 config 实际引用目录）
  codes/data_valve/     （阀门：valve_*.csv）
  codes/data_electronics/（电子：customers/equipment/orders/products.csv）
  codes/data_food_co/、data_chem/… 等 data_* 多为相同 CSV 的冗余副本（仅 food 产出现成本体）
        │ ① 本体构建（多选一）
        ▼
codes/csv_to_owl.py   —— 单表 CSV→N-Triples（零依赖，每表一个类）
codes/multi_table.py  —— 多表 + 溯源/对象关系建本体（food_demo 用它）
codes/schema_ontology.py —— schema 驱动建本体（valve_demo 用它，读 config/ontology_schema.json）
        │ ② 产物
        ▼
codes/output/*.nt     —— N-Triples 本体（现仅 food.nt 存在）
        │ ③ 词典加载（三层兜底）
        ▼
ontology_qa_v3.load_dict(lexicon)：
   config/lexicon_*.json（attr_cn2en/attr_en2cn/type_cn2en/status_cn2en/entity_cn2en/
                         numeric_fields/field_aliases/synonym_map）
   ← merge_industrial_dict（公共工业本体层，跨行业通用概念）
   ← lexicon.py 内置兜底（get_attr_cn_aliases / get_common_zh_status / get_entity_cn2uri）
        │ ④ 问答
        ▼
ontology_qa_v3.parse_nt → build_data → answer(question, data, D)
```

**可跑通一次真实问答的入口（3 个）**：
- CLI：`python ontology_qa_v3.py <nt文件> '<问题>' <lexicon.json>`（本项目 README/文档主用法，**实测可用**）
- Demo：`python food_demo.py` / `python valve_demo.py`
- REST：`python api_server.py`（另有 agent 层跨域守卫，见下文 bug-A）

**逐环节实测结论先行**：`ontology_qa_v3.py`（词典驱动规则引擎）核心链无断点；`lexicon.py`、`industrial_dict_loader.py`、`data_loader.py`、`csv_to_owl.py` 均能 import 并正常执行。真正的断点 / 失效集中在**词表内容缺失**与**少数规则模板的多实体健壮性**，而非链路本身崩溃。

---

## 二、真实数据验证结果

### 行业 1：food（食品溯源，现成本体 output/food.nt）

本体：`data/food_*.csv` → 68 个个体（产品8/原料10/批次9/质检10/设备7/溯源join…，经 multi_table 建）。词表 `config/lexicon_food_products.json`（与 lexicon_food.json **逐字节相同**，`diff` 验证）。

CSV 应得值（data/food_products.csv 真算）：
- 产品 8 行，品类 = 乳制品3 / 烘焙食品2 / 速冻食品2 / 果汁饮品1
- 价格 max=18（手工水饺）；avg=10.06；sum=80.50；价格>5 共 6 个、<4 共 1 个（原味酸奶）
- 保质期 max=180（**鸡蛋灌饼 与 手工水饺 并列**）
- 储存方式 4 种：-18℃冷冻 / 2-6℃冷藏 / 常温避光 / 常温阴凉

| # | 问题 | 引擎返回 | 应得值 | 判定 |
|---|------|---------|--------|------|
| 1 | 乳制品的数量 | 有 3 乳制品 | 3 | ✅ 正确 |
| 2 | 速冻食品的数量 | 有 2 速冻食品 | 2 | ✅ 正确 |
| 3 | 果汁饮品有多少个 | 有 1 果汁饮品 | 1 | ✅ 正确 |
| 4 | 一共有多少种产品 | 共有 4 种类型 | 4 种品类 | ✅ 正确 |
| 5 | 一共有多少产品 | 有 8 个产品 | 8 | ✅ 正确 |
| 6 | 产品总数 | 产品总数 8 | 8 | ✅ 正确 |
| 7 | 列出所有乳制品 | 鲜牛奶/草莓酸奶/原味酸奶 | 同 | ✅ 正确 |
| 8 | 价格最高的产品 | 手工水饺 (价格=18.0) | 手工水饺 | ✅ 正确 |
| 9 | 价格最高的3个产品 | 手工水饺/鸡蛋灌饼/鲜牛奶 | 同 | ✅ 正确 |
| 10 | 平均价格 | 价格平均值 10.06 (8条) | 10.06 | ✅ 正确 |
| 11 | 价格的总和 | 价格总和 80.50 | 80.50 | ✅ 正确 |
| 12 | 价格大于5的产品 | 6 个 | 6 | ✅ 正确 |
| 13 | 价格小于4的产品 | 1（原味酸奶） | 1 | ✅ 正确 |
| 14 | 各品类的产品数量 / 品类分布 | 乳3 烘2 速2 果1 | 同 | ✅ 正确 |
| 15 | **保质期最长的产品** | 手工水饺 (180) | 鸡蛋灌饼 **与** 手工水饺均 180 | ⚠️ 漏并列（见 bug-C） |
| 16 | **列出所有食品类型** | **暂不支持该问题** | 应枚举 4 种品类 | ❌ 失效（模板+词表缺口，非假答） |
| 17 | **类型分布** | 11 个类型（产品品类 **+ 设备类型混排**） | 应只枚举品类 | ❌ 跨实体污染（见 bug-B） |
| 18 | **储存方式分布** | 暂不支持该问题 | 应分 4 组 | ❌ 失效（词表键“储存”无“储存方式”） |
| 19 | 有哪些食品类型 | 逐行列出 产品→品类 | 期望去重枚举 | ⚠️ 勉强列出、非去重枚举 |

### 行业 2：valve（阀门，schema 驱动构建）

本体：`schema_ontology.load_all(data_valve)+to_nt` 生成 142 个体：Valve_products8 / customers8 / sales30 / equipment10 / raw_materials10 / batches15 / batch_ingredient37 / qc24。
词表：`config/lexicon_valve.json` —— **全空 stub（所有映射 dict 均为 0，仅 description/review 有值）**；valve_demo 实际靠内置通用词典（价格→price 等）+ schema 提供有限能力。

CSV 应得值（data_valve/valve_products.csv）：产品 8 行；价格 max=2560（对夹蝶阀 D371X-10）；type=闸阀 共 2 行。

| # | 问题 | 引擎返回 | 应得值 | 判定 |
|---|------|---------|--------|------|
| 1 | **一共有多少个阀门** | **一共有 142 条记录** | 产品仅 8 个 | ❌ **静默答错**（见 bug-A，核心发现） |
| 2 | 价格最贵的阀门 | 对夹蝶阀 D371X-10 (价格=2560) | 2560 | ✅ 数值正确（仅产品有 price，故全局 max=产品 max，巧合等价） |
| 3 | **最贵的闸阀** | 暂不支持该问题 | 闸阀 2 行中 max | ⚠️ 词表空 → 无法识别“闸阀”为 type（诚实拒绝，能力缺口） |
| 4 | 有多少种球阀 | （逻辑桥）未命中 | — | ⚠️ 词表空 → 类型级问答失效 |
| 5 | 溯源 R007 螺栓→受影响批次→产品 | 命中 8 批（产品列为空） | 溯源通路在 | ✅ 溯源链工作（产品 join 因 demo 简化未取到，非规则问答范畴） |

### 行业 3：electronics（电子制造，生产路径逐表构建）

本体：`csv_to_owl.build_nt` 对 data_electronics 4 表各建一次再合并 → 48 个体（Products12 / Equipment10 / Orders20 / Customers6）。
词表：`config/lexicon_electronics.json` —— **丰富完整**（attr_cn2en 30 / type_cn2en 7 / status_cn2en 13 / entity_cn2en / numeric_fields 7 / synonym_map）。

CSV 应得值：产品 12 行，product_type = 被动元件6 / 基板3 / 半导体2 / 结构件1；设备 device_type = SMT6 / 组装3 / 检测1；价格 max=**38260（陶瓷电容，数值复核非字符串序）**、avg=7257.58；stock_qty<100 → PCB板。

| # | 问题 | 引擎返回 | 应得值 | 判定 |
|---|------|---------|--------|------|
| 1 | 一共有多少个产品 | 有 12 个产品 | 12 | ✅ 正确 |
| 2 | 结构件的数量 / 半导体的数量 | 1 / 2 | 1 / 2 | ✅ 正确 |
| 3 | 有多少台SMT设备 / 组装设备 | 6 / 3 | 6 / 3 | ✅ 正确（跨实体计数不串） |
| 4 | 列出所有半导体 / 有哪些结构件 | 贴片电阻/PCB板 / 连接器 | 同 | ✅ 正确 |
| 5 | 各设备类型的设备数量 / 设备类型分布 | SMT6 组装3 检测1 | 同 | ✅ 正确（**实体消歧生效，未与产品串**） |
| 6 | 价格最高的产品 | 陶瓷电容 (38260) | 陶瓷电容 | ✅ 正确 |
| 7 | 平均价格 | 价格平均值 7257.58 (12条) | 7257.58 | ✅ 正确 |
| 8 | 库存小于100的产品 | 1（PCB板） | PCB板 | ✅ 正确 |
| 9 | 产品总数 | 产品总数 12 | 12 | ✅ 正确 |

**electronics 全链 0 静默失效** —— 证明当词表与本配套完整时，数量/列出/分组/极值/范围/平均全部正确，含跨实体“设备 vs 产品”消歧。

---

## 三、发现的静默失效点（带文件行号证据）

> 行号均为当前 `codes/` 源码实际行号，`grep`/`read_file` 复核。

### ❌ bug-A【真 bug · 健壮性 — 最高优先】out-of-KB 实体计数被静默答成全库总数

- 位置：`ontology_qa_v3.py:901-903`
  ```python
  # ---- 总数: 一共有多少条记录 ----
  if ("一共" in q or "总共有" in q or "总共" in q) and ("记录" in q or "多少" in q):
      return "一共有 %d 条记录" % len(data)
  ```
- 表现：当“多少X”中的名词 X 不在该 KB 任何实体/类型词表时，前面所有模板（实体总数 L710-721、状态/类型计数 L738-747）全部落空，最终落到该 catch-all，返回**整个 KB 所有实体类个体总数**——即便用户问的实体根本不在库里。
- 实测：`valve.nt`（8 个类 142 个体）“一共有多少个阀门”→ **142**（应约 8 个产品）。阀门在 `lexicon_valve.json`（空）与内置 `get_entity_cn2uri()`（仅 设备/产品/客户/批次/原料/销售/质检，`lexicon.py`）中都不存在。
- 关键证据：跨域守卫 `is_cross_domain_data_query`（L446）与 `kb_vocab`（L423）**只在 `api_server.py:720-723` 被调用**；`ontology_qa_v3.answer()`（L577-905）主路径**内部从未调用**。即规则引擎 miss 后若前序 catch-all 命中（返回总数），连 api_server 的守卫也到不了 → CLI 与 API 都会静默给出错误总数。
- 判定：**真 bug（代码健壮性）**，与具体行业词表无关；任何行业问 out-of-KB 的计数都会被误导成“全库条数”。

### ❌ bug-B【真 bug · 多实体】裸“类型 / XX类型”枚举跨实体污染

- 位置：`ontology_qa_v3.py:759-764`
  ```python
  if "类型" in q and "哪些" not in q and "列出" not in q and "的" not in q:
      ty_vals = sorted({str(d.get("deviceType") or d.get("category") or "") for d in data.values()} - {""})
      if ty_vals:
          return "类型有：%s" % "、".join(ty_vals)
  ```
- 表现：该分支用 `deviceType or category` 遍历**全部 `data` 个体**取去重，隐含“单一实体类”假设。多实体 KB（产品用 category、设备用 deviceType）时会把**不同实体类的类型枚举混在一起**。
- 实测：food.nt “类型分布”返回 11 个：乳制品/烘焙食品/速冻食品/果汁饮品（产品品类）**＋** 制冷/包装/搅拌/杀菌/灌装/烘烤/速冻设备（设备类型）混排。
- 判定：**真 bug（未按实体类隔离）**。单实体 KB（图书等）无碍；多实体 KB（本项目的常态：产品+设备+批次…）必现。food 把类型字段命名为“品类”又放大了词表侧缺口，但代码不隔离实体类是代码缺陷本身。

### ⚠️ bug-C【真 bug · 边缘】单极值遇并列只返回一个

- 位置：`ontology_qa_v3.py:816-820`（单极值）`max()`/`min()` 取首个；L794-798（TopN）正常取前 N 不受影响。
  ```python
  best = max(items, key=lambda x: x[1]) if is_max else min(items, key=lambda x: x[1])
  return "%s的记录: %s ..." % (...)
  ```
- 表现：并列极值时仅回一个，不提示并列。实测 food“保质期最长的产品”→ 手工水饺(180)，但 CSV 中鸡蛋灌饼同为 180。
- 判定：**真 bug（轻微/边缘）**，返回的是合法极值之一、不算“答错”，但会漏报并列者，评测口径需注意。

### ⚠️ 失效点-D【数据问题 · 词表为空】lexicon_valve.json 为全空 stub

- 位置：`config/lexicon_valve.json`（全 0，仅 description/review）。`config/` 下扫描 40+ 个 lexicon 中**唯一一个全空**。
- 表现：valve 行业的类型/状态/属性级问答（最贵的闸阀、有多少种球阀）无法命中 → 诚实“暂不支持”，非假答案，但类型级能力基本不可用。其 single-table benchmark 用内置通用别名（价格→price）走通（demo 报 9/9=100%），掩盖了词表缺失。
- 判定：**数据问题（词表不配套）**，非引擎代码 bug。修复=补全词表（type_cn2en 加 闸阀/球阀/蝶阀 等，跑 LexiconAgent/data profiling）。

### ⚠️ 失效点-E【数据问题 · 键不配套】属性键粒度与口语不符

- 位置：`config/lexicon_food_products.json` attr_cn2en 键为“储存”→storage、无“储存方式”；`_find_agg_attr`（L512-527）只匹配 `"各"+键` / `键+"分布"`。
- 表现：food“储存方式分布”→ 暂不支持（因“储存方式”不在键，`"储存方式"+"分布"` ≠ 任何键）。
- 判定：**数据问题（词表键粒度）**。可加“储存方式”别名键或对 attr 键做“去 方式/状态/类型 后缀”的归一化匹配。

### ⚠️ 失效点-F【数据问题 + 模板覆盖】“列出所有食品类型”的 distinct-枚举意图无模板可接

- 位置：模板只覆盖 ①`列出所有<具体type值>`（L770-773 列出所有乳制品 ✓）②“有哪些<实体实例>”（L775-786）③裸“XX类型”（L761-764，被 bug-B 污染）。**没有“列出所有某实体字段的类型去重”模板**；“食品类型”既非具体 type 值、也非词表属性名（字段叫“品类”≠“类型”）。
- 判定：**数据/词表 + 模板覆盖缺口**，返回“暂不支持”是诚实拒绝、非假答。可给词表补 `食品类型→category` 别名、并让“哪些/列出 + X类型”走去重枚举。

---

## 四、真 bug vs 数据问题 判定总表

| 失效 | 性质判定 | 一句话 |
|------|---------|--------|
| bug-A 总数 catch-all 答成全库 | **真 bug（健壮性，高优先）** | 守卫 `is_cross_domain_data_query` 未进 `answer()`，CLI/API 均绕过；out-of-KB 计数给错数 |
| bug-B 裸“类型”跨实体污染 | **真 bug（多实体）** | 遍历全 `data` 未按实体类隔离 |
| bug-C 并列极值漏报 | **真 bug（边缘）** | max/min 只取首个 |
| 失效-D valve 空词表 | **数据问题** | 词表为空 → 诚实“暂不支持”，非假答 |
| 失效-E “储存方式分布” | **数据问题（键粒度）** | 词表键“储存”缺“储存方式” |
| 失效-F “列出所有食品类型” | **数据 + 模板缺口** | 无 distinct 类型枚举模板 + 词表字段叫“品类”非“类型” |

**关键区分**：真正会**“答了但答错”的静默失效**只有 **bug-A**（valve 142）与 **bug-B**（food 类型污染）两处；其余多为引擎正确“暂不支持”的**能力缺口**（词表/模板不配套的假象），不算欺骗性错误。

---

## 五、可执行修复建议

1. **【高优先，修 bug-A】** 把跨域/词表存在性守卫下沉进 `ontology_qa_v3.answer()` 主路径：在总数 catch-all（L901-903）命中前，用 `kb_vocab`/`is_cross_domain_data_query` 判断“多少X”的 X 是否在该 KB 实体/类型/状态/属性词表内；不在则返回“无相关数据（该知识库不含该实体概念）”，**不再返回全库条数**。统一 CLI 与 api_server 口径。或至少要求问题中出现“记录”字样才触发该 catch-all。
2. **【高优先，修 bug-B】** L761-764 枚举类型前先做实体类隔离：问题点名实体（有 entity_cn2en 词）时用 `_entity_subset` 限定范围；未点名时对 `deviceType`（设备类）与 `category/type/kind`（产品类）分实体类各出一次，或用 lexicon 属性作用域，避免产品品类与设备类型混排。
3. **【中优先，修 bug-C】** 单极值取并列集合：找出所有等于极值的个体一起列出（“max=180 的有 手工水饺、鸡蛋灌饼”），避免评测漏报。
4. **【中优先，数据】** 为 `lexicon_valve.json` 补全真实词表（闸阀/球阀/蝶阀/截止阀等 type_cn2en，价格/口径/压力等 attr_cn2en），消除其唯一空 stub 状态，恢复阀门类型级问答。
5. **【低优先，数据】** food 等词表补充高频口语别名键（如 attr 加“储存方式”，type 字段别名“食品类型/产品类型→category”），并让 `_find_agg_attr` 对中文属性键做“去 方式/状态/类型 后缀”归一化后再匹配，缓解失效-E/F。
6. **【口径】** 单极值返回并列集合后，food/electronics 类规则 benchmark 预期答案需同步（防“漏报并列”被误判为错）。

---

## 附：已验证但未列入正文的健全项

- 全 3 行业的 数量/列出/TopN/平均/总和/大于小于/范围/分组 模板实测与 CSV 应得值一致（food 15 题、electronics 15 题大部分正确，仅上表列出的 edge 例外）。
- `csv_to_owl.py` / `multi_table.py` / `schema_ontology.py` / `data_loader.py` / `lexicon.py` / `industrial_dict_loader.py` 均 import 成功并正常产出/加载，无链路崩溃。
- 现成本体仅 `output/food.nt` 一份；`data_chem / data_electronics / data_valve` 等原始 CSV 需各自先跑构建路径才产出配套 .nt（本次已代为验证构建路径可用）。`data_food_co/` 与 `data/` 下食物 CSV 内容一致（冗余副本），本体引用的是 `data/`。
- 未核实项：LLM 兜底逻辑桥（`logical_qa`）、GraphRAG/图检索、REST `api_server.py` 的 LLM 兜底分支——本次仅聚焦规则 QA 核心链，故未对 LLM 分支做端到端验证，报告中凡涉及逻辑桥的返回一律标“（逻辑桥）未命中”，不作 LLM 能力结论。
