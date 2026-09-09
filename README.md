# Shopping Agent v1

独立重建项目，创建于 2026-09-08。只迁入本轮讨论形成的方案文档与已冻结的商品数据，不继承旧项目源码、测试、构建配置或 Git 历史。

## 已确定

- Agent 框架：OpenAI Agents SDK，单 Agent。
- 模型提供方：DeepSeek，具体模型 ID 与 API 凭据在接入时配置。
- 数据：Amazon 商品元数据，12 个日常品类 × 50 条，共 600 条；历史 USD 价格，不含用户评论。
- 评估：40～60 个完整任务，固定测试用户输入/简单分支，Agent 自主决策；不要求基础 Agent 对照。

## 文档入口

- [第一版实现规格](docs/shopping-agent-v1-spec.md)
- [任务、需求、候选与决策状态](docs/decision-state-design-notes.md)
- [追问、搜索、查看证据与停止](docs/action-selection-design-notes.md)
- [评估与简历约定](docs/evaluation-and-resume-scope.md)
- [商品数据说明](docs/amazon-data.md)
- [KuaiSearch 数据调研](docs/research-kuaisearch-shopping-fit.md)
- [替代数据来源调研](docs/research-shopping-dataset-alternatives.md)

## 当前交付状态

已实现第一阶段的 SQLite 状态服务和只读商品服务，21 项程序级检查通过；尚未接入 Agent 框架、自然语言解析或真实模型调用。详见 [实现进度与限制](docs/implementation-progress.md)。文档中提到的旧代码、脚本、接口、测试和历史运行结果仅为讨论背景，不代表本仓库包含或验证过这些实现。后续以本仓库重新实现，不复用旧项目作为实现起点。

API 密钥仅放在本地环境或被忽略的 `.env`，不提交到仓库。

## 运行程序级检查

需要 Python 3.11 或更新版本，无需 API Key 或第三方运行依赖。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
