# 工厂本体问答 Web 应用

Svelte5 + Vite 前端 + Node 后端，调用仓库 `codes/` 套件实现 **CSV 上传 → 本体建模 → 自然语言问答 → 知识图谱/分析看板**。

## 快速开始

```bash
cd web
npm install          # 安装依赖
npm run build        # 构建前端(生成 public/)
npm start            # 启动服务 http://localhost:3001
```

## 功能

| 端点 | 说明 |
|------|------|
| `POST /api/ontology/setup` | 上传 CSV → `run.py setup` 建模（本体+词典） |
| `POST /api/ontology/ask` | 自然语言问答（`ontology_qa_v3` 规则 + LLM 兜底） |
| `GET /api/ontology/schema` | 本体结构图（类/数据属性/对象属性） |
| `GET /api/ontology/stats` | 聚合统计看板（类型/状态/产线分布） |
| `POST /api/ontology/analyze` | 智能分析（统计摘要 + LLM 洞察） |
| `GET/POST /api/ontology/model` | 模型配置读取/切换 |

## 说明

- 后端通过 `child_process` 调用仓库 `codes/` 下的 Python 套件（路径全相对，无硬编码私有路径）
- **上传→建模→问答** 主流程适用于任意 CSV；分析/看板面板按"工厂设备 + 产线"场景设计，读 `data/equipment.csv` + `line.csv`
- 模型在 `codes/config/model_config.json` 配置，前端顶部下拉可切换
- 依赖：Node 18+、Python 3.9+（模型调用可选）

## License

[Apache License 2.0](../LICENSE)
