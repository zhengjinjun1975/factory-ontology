# 贡献指南

欢迎参与 factory-ontology-kit 的改进。这个仓库是本体驱动的数据问答开源实现，贡献方式和注意事项如下。

## 快速链接

- **简介与用法**：见 [README.md](README.md)
- **贡献要求（精简版）**：README 的 [贡献指南](README.md#贡献指南) 一节（Bug 报告、PR 要求、示例数据、词典驱动、模板顺序）
- **开源合规**：见 [开源发布合规流程](../docs/开源调研.md) 与 [开源发布方法论](docs/交付方法论.md)
- **版本记录**：见 [CHANGELOG.md](CHANGELOG.md)

## 提交 Issue

### Bug 报告

请尽量附上**可复现命令**：

```bash
cd codes
python run.py setup <数据文件> [表名]
python run.py ask "<问题>"
```

并说明：数据文件、问题、期望输出、实际输出。规则引擎是确定性的，可复现性对定位问题至关重要。

### 功能建议

说明场景、期望行为、以及它在本体的「诚实边界」内的位置（结构化台账问答？还是超出当前能力范围）。不接收与本体定位（受限 schema）冲突的泛 AI 平台需求。

## 提交 PR

### 前置检查

```bash
cd codes && python -m pytest tests/ -q     # 单测必须全绿（当前基线 29 项）
```

涉及问答能力或检索链路的改动，请附 **benchmark 复现结果**：

```bash
cd codes
python csv_to_owl.py data/<你的数据>.csv output/<你的数据>.nt
python benchmark.py data/<你的数据>.csv
```

### 应遵循的约束

- **示例数据一律用虚构数据**：仓库不接收任何真实工厂数据，任何含真实企业/现场数据的改动会被拒绝。
- **保持词典驱动**：不要在规则引擎里硬编码具体中文词，字段语义一律走 lexicon（`config/lexicon_*.json`）。
- **模板顺序**：改动提问模板前先看 `codes/ontology_qa_v3.py` 的模板顺序注释，避免同类问题被先行模板劫持的回归。
- **路径全相对**：代码与文档一律用仓库相对路径，禁止硬编码本机绝对路径（如 `E:\...`）或引用仓库外私有库。
- **不引入核心依赖**：核心问答路径保持纯标准库，第三方依赖只进 `requirements.txt` 并在注释里写明 `import` 出处。

### 代码风格

- 中文注释，模块顶部写清职责。
- 新增能力保持「单一职责 + 可替换」，外置配置优于硬编码。

## License

[Apache License 2.0](LICENSE)
