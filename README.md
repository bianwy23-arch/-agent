# Shopping Agent v1

独立重建项目，创建于 2026-09-08。只迁入本轮讨论形成的方案文档与已冻结的商品数据，不继承旧项目源码、测试、构建配置或 Git 历史。

## 已确定

- Agent 框架：OpenAI Agents SDK，单 Agent。
- 模型提供方：DeepSeek；当前已验证 `deepseek-v4-flash`，型号可配置。
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

已接入 OpenAI Agents SDK + DeepSeek，提供 SQLite 状态服务、商品工具、命令行和本地网页。38 项程序级检查通过；真实集成检查初次 4/5 通过，币种停止分类失败已修正并单项复验通过。完整 40～60 个任务 eval 尚未完成。详见 [接入记录与能力限制](docs/deepseek-integration.md)。文档中提到的旧代码、脚本、接口、测试和历史运行结果仅为讨论背景，不代表本仓库包含或验证过这些实现。后续以本仓库重新实现，不复用旧项目作为实现起点。

API 密钥仅放在本地环境或被忽略的 `.env`，不提交到仓库。

## 安装与运行

需要 Python 3.11 或更新版本及 uv。在项目根目录执行：

```bash
uv sync --frozen
uv run --frozen python -m unittest discover -s tests -v
uv run --frozen shopping-agent
```

程序测试使用显式模拟模型，不调用收费 API。命令行及网页的真实对话需要本地 `.env` 中的 `DEEPSEEK_API_KEY`；缺失时明确报错，无脚本回退。配置名参见 `.env.example`，不要覆盖已有密钥。

启动网页：

```bash
uv run --frozen python -m shopping_agent.web
```

打开 http://127.0.0.1:8765 。仅绑定本地地址，刷新可恢复会话消息。日志和数据库保存在被 Git 忽略的 `.runtime/`。

当前自动主推荐核验覆盖预算、用户排除和原文引用；其他明确硬要求保留为未核实，不伪装成已满足。自然语言解析正确性及语义推荐理由仍需完整验收。
