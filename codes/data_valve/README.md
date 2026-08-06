# 阀门行业示例知识库（合成示例数据，非真实数据）
# 展示 factory-ontology 框架对石油/阀门行业的适用性（换领域即用）

## 数据表
- valve_products.csv — 阀门产品（型号/口径/压力等级/材质/密封/价格）
- valve_raw_materials.csv — 零部件（阀体/阀芯/密封圈/螺栓，供应商/材质/质检）
- valve_batches.csv — 生产批次
- valve_batch_ingredient.csv — 批次-部件关联（溯源关键）
- valve_qc.csv — 质检（密封/压力/泄漏率）
- valve_equipment.csv — 生产设备

## 运行 demo
python valve_demo.py
