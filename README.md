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

已接入 OpenAI Agents SDK + DeepSeek，提供 SQLite 状态服务、商品工具、命令行和本地网页。额外硬条件核验覆盖 12 品类的明确属性，包括单位比较、证据未知与冲突；主推荐依据核验结果生成。已编制并实际执行 48 个完整任务，保留所有失败、修复与复杂子集重跑结果。

- [最新验收报告与剩余问题](docs/evaluation-results.md)
- [各品类可核验属性及数据覆盖](docs/attribute-coverage.md)
- [3 个真实故障的复现与修复](docs/reproducible-fixes.md)
- [评估输入、独立标注和重跑方法](eval/README.md)

本仓库不包含旧项目代码或旧 Git 历史。设计文档中的完整决策状态写入、推断依赖传播等未实现能力仍明确列为限制。

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

主推荐会检查预算、排除、支持范围内的数值/枚举硬条件及引用；未知或冲突不能当作满足。格式合法但尚不能核验的条件仍保存为硬要求。自然语言理解与非结构化解释不具有普遍正确性保证，实际失败见验收报告。
