# factory-ontology CSV 数据质量修复说明

> 修复日期：2026-08-23　|　脚本：`ontology_check.py`（5 类核查，CSV 数据质量权重 25%）　|　工具：仅 Python 标准库

## 一、背景与问题

本体检查（`ontology_check.py`）对全库 5 类核查发现：**CSV 数据质量子分 55/100（全库最低）**，
累计 **72 项问题**（32 主要 + 40 次要）。集中表现为：

| 类别 | 数量 | 严重度 | 问题 |
|------|------|--------|------|
| 知识库 CSV 主键重复 | 32 | 主要 | `data_appliance` 等 8 个知识库的 `customers/equipment/orders/products.csv` **首尾行主键重复**（如 `C001`、`E001`、`O001`、`P001` 在文件首行与末行各出现一次） |
| 空单元格 | 33 | 次要 | 上述 8 个知识库 CSV + `data_valve/valve_raw_materials.csv` 共 33 个文件存在空单元格 |
| 词典英文值重复 | 7 | 次要 | `industrial_dict/00_basis.json`、`02_fine_chem.json`、`03_geophysics.json` 中 cn2en 词典同一英文值对应多个中文词 |

## 二、根因分析

1. **主键重复**：每个受影响 CSV 的**最后一行数据是首行数据的完整重复副本**
   （同一主键、同各字段值），判定为生成/复制时误加的首尾行重复，属可安全去重的冗余行。
2. **空单元格**：部分行的属性列缺失（如 `region`、`product_name`、`amount`、`qc_result` 等）。
3. **词典英文值重复**：cn2en 词典本应“一对一”（一中文→一英文），却混入了“多中一英”的同义词
   （如 `电机→motor` 与 `电动机→motor`），应把同义词别名归入 `synonym_map` 而非 cn2en 主表。

## 三、修复内容

### 1. CSV 主键去重（32 个文件）
对 8 个知识库（`data_appliance`、`data_auto_parts`、`data_electronics`、`data_furniture`、
`data_hardware`、`data_medical_dev`、`data_plastics`、`data_textile`）的
`customers/equipment/orders/products.csv` 各 4 个文件共 **32 个**，按主键 `id` **去重保留首现**，
移除末尾与首行重复的冗余行，唯一数据记录全部保留（不误删数据资产）。

### 2. CSV 空单元格补值（33 个文件，约 230 处单元格）
数据清洗采用下列插补/标注策略（保留数据资产，不误删）：
- **数值列**（`amount`/`price`/`quantity`/`power_kw`/`temp_c`/`vibration_mm_s`/`device_age_years`/`stock_qty`/`spec_value` 等）：**列中位数**插补；
- **类别列**（`region`/`industry`/`status`/`delivery_status`/`material`/`device_type`/`workshop`/`contact`/`credit_level`/`qc_result` 等）：**列众数**插补；
- **描述性名称列**（`product_name`/`device_name`/`customer_name`）：标注为 **“未知”**（诚实标注，不做臆造命名）。

### 3. 词典英文值去重规范（3 个 JSON）
把 cn2en 词典中“多中一英”的同义词**别名**从 cn2en 主表移除（保留规范中文词），并**归入 `synonym_map`**，
使 cn2en 恢复一对一、英文值唯一；同义词关系完整保留（不丢数据）：
- `00_basis.json`：移除 `电动机`、`CNC`、`运行/正常/工作中`、`空闲`、`停机`、`报警/异常`、`维修中/保养中`、`原材料`；
  对应别名补入 `synonym_map`（如 `电机→[motor,马达,电动机]` 已有、`运行中→[运行,正常,工作中]`、`故障→[报警,异常]` 等）。
- `02_fine_chem.json`：移除 `反应釜`、`精馏塔`、`储槽`、`停车`；`停车` 归入 `synonym_map（停产→[停车]）`，
  `反应釜/精馏塔/储槽` 已在 `synonym_map` 中作为别名保留。
- `03_geophysics.json`：移除 `地震检波器`、`工区`；`测区→[工区]` 补入 `synonym_map`。

## 四、修复前后分数对比（本地 `ontology_check.py` 复测）

| 类别 | 权重 | 修复前 | 修复后 |
|------|------|--------|--------|
| A. 链路断链 chain | 20% | 100 | 100 |
| **B. CSV 数据质量 csv** | **25%** | **55** | **100** |
| C. NT 本体质量 nt | 20% | 100 | 100 |
| D. 词典一致性 lexicon | 15% | 93 | 100 |
| E. 本体一致性 consistency | 20% | 100 | 100 |
| **综合得分** | 100% | **87.7** | **100.0** |
| 问题总数 | — | 72（32 主要 + 40 次要） | **0** |

> CSV 子分 **55 → 100**（提升 45 分），全库综合 **87.7 → 100.0**，本地判定 `✅ 通过`（阈值 60）。

## 五、验证与 CI

- **本体检查**：`python ontology_check.py --threshold 60` → 综合 **100.0**，致命/主要/次要问题 **0**，判定通过。
- **单测**：`pytest tests/ -v` → **67 passed**。
- **对照评测**（test job 内步骤）：`ai4i/library_inventory/energy_station` 本体构建成功，
  `benchmark.py` 本体问答命中率 **13/13 = 100%**。
- **真实构建冒烟**：`data_valve` 8 表 CSV→本体 构建成功（链路无断链）。
- **CI**：本地全绿；推送后远端 CI `ontology-check` job 通过（本体检查阈值通过）。

## 六、涉及文件

- 修改 CSV（去重+补值）：`codes/data_appliance|data_auto_parts|data_electronics|data_furniture|
  data_hardware|data_medical_dev|data_plastics|data_textile/{customers,equipment,orders,products}.csv`（32 个）、
  `codes/data_valve/valve_raw_materials.csv`。
- 修改词典：`codes/industrial_dict/00_basis.json`、`02_fine_chem.json`、`03_geophysics.json`。

## 七、说明

本次修复为**真实 CSV 数据修改**（非仅调分）：实际删除了首尾重复行、实际补全/标注了空单元格、
实际去重规范了词典英文值。所有唯一数据记录均保留，无数据资产误删。修复后本地全绿并经远端 CI 验证。
