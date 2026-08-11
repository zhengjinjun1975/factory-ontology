# 需求：修复 statsOntology 悬空依赖（aggregate.py 已删）+ 看板空态

> 背景：DashboardPanel 看板调 /api/ontology/stats，后端 statsOntology 硬编码调 `codes/aggregate.py`，但该文件在精炼化时已删除 → 看板一直报错（"没输入之前不该报错"）。
> 修复：用 schema_ontology 从本体图计算统计，替换 aggregate.py；无数据时返回空态。

## 目标文件
- `E:\open-source\factory-ontology-kit\web\server\ontology.js`（statsOntology）

## 现状
statsOntology 调 `aggregate.py`（不存在）→ 报错。DashboardPanel 需要字段：
- `device_type_dist`: [{type, count}]（设备类型分布）
- `status_dist`: [{status, count}]（状态分布）
- `line_stats`: [{line, device_count}]（产线统计）
- `total_devices`: int
- `fault_rate`: float (0-1)

## 要求
改 statsOntology，用 schema_ontology 从当前建模的本体（web_state 或 current.json 指向的 .nt）计算统计：
1. 若无建模数据（无 web_state/current.json 或 .nt 不存在/为空）→ 返回 `{ ok: true, stats: null, empty: true }`（友好空态，非报错）
2. 有数据时，用 Python 调 schema_ontology 或直接解析 .nt 计算：
   - 设备类型分布：从 Equipment 实体的 deviceType 属性聚合
   - 状态分布：从 Equipment 实体的 status 属性聚合
   - 总数：Equipment 实例数
   - fault_rate：alarm/maintenance/offline 占比
   - line_stats：可从 location/workshop 聚合（无则空数组）
3. 返回 `{ ok: true, stats }`

## 极简实现建议
新增一个 Python 统计脚本 `codes/ontology_stats.py`（纯标准库，解析 .nt 聚合设备分布），statsOntology 调它替代 aggregate.py：
- 输入：.nt 文件路径
- 输出：JSON {total_devices, device_type_dist, status_dist, line_stats, fault_rate}
- 复用 schema_ontology.parse/build 思路，或直接用 ontology_qa_v3.parse_nt

或者更极简：statsOntology 直接读 .nt 用 JS 正则解析（不额外 Python），但需注意中文/URI 解析。优先用 Python 脚本（复用现有 parse_nt）。

## 约束
- 极简：一个统计脚本 + 改 statsOntology
- 不破坏现有 setup/ask/schema 功能
- 空态返回 empty:true（非 error），前端已支持
- 数据源优先读 web_state（Web 建模的），fallback current.json

## 验收
1. 无建模数据时 /api/ontology/stats 返回 { ok:true, stats:null, empty:true }
2. 有建模数据时返回完整 stats（device_type_dist/status_dist/total_devices/fault_rate）
3. 看板不再报"统计加载失败"（未建模显示"尚未建模"空态）
4. node server/index.js 启动正常
