# DeepSeek 接入与第二阶段验证

日期：2026-09-09。使用独立新仓库，不继承旧项目实现。

## 已交付

- OpenAI Agents SDK 单 Agent 工具循环，模型通过 `process_turn`、`search_products`、`inspect_product`、`finish_turn` 选择动作；没有固定工具执行序列冒充模型决策。
- DeepSeek `deepseek-v4-flash`，通过 `OpenAIChatCompletionsModel` 和 DeepSeek 官方 Chat Completions 接口连接，非思考模式。型号可由 `DEEPSEEK_MODEL` 修改；切换后需要重新验证，不视为已验证其他型号。
- Pydantic 类型校验、未知字段拒绝及生成的 `schemas/turn-plan.schema.json`、`schemas/final-answer.schema.json`。服务端绑定任务和轮次，不接受模型任意重写状态。
- SQLite 持久化会话、任务、轮次和状态历史。工具使用异步函数保持与 SQLite 连接在同一执行线程；本地请求串行处理。
- CLI 与 FastAPI 本地网页；刷新网页可加载会话消息，进程重启后可继续原会话。
- 本地 `.runtime/trace.jsonl` 保存用户输入、模型/运行配置、工具参数与结果、错误类型、最终结果和调用用量。禁用 OpenAI tracing；不保存模型隐藏思维内容、不记录凭据或 HTTP 请求头。
- 依赖在 `pyproject.toml` 直接固定，完整解析结果保存在 `uv.lock`。本次实际使用 openai-agents 0.22.1、openai 3.10.0、pydantic 2.13.5、FastAPI 0.141.1、uvicorn 0.52.4、python-dotenv 1.2.3。

## 运行上限

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| AGENT_MAX_TURNS | 12 | SDK 单轮用户请求的模型迭代上限 |
| AGENT_MAX_TOOL_CALLS | 24 | 业务工具调用上限 |
| AGENT_REQUEST_TIMEOUT | 45 秒 | 单次模型请求超时 |
| AGENT_TURN_TIMEOUT | 150 秒 | 整轮执行超时 |
| AGENT_MAX_OUTPUT_TOKENS | 4096 | 单次模型输出 token 上限 |
| AGENT_API_RETRIES | 1 | SDK HTTP 重试上限，最多一次重试 |
| AGENT_REPEATED_CALL_LIMIT | 3 | 同工具、同参数、同需求版本第三次调用终止 |

工具 schema 错误允许两次内部修正，第三次终止。以上是执行后备限制，不是“搜三次就成功”的业务标准；异常、超时或触发上限输出 execution_limited 并保留已生效修改，不静默回退脚本。

`execution_limited` 由运行时生成，模型不能把币种澄清等用户信息缺口标成执行失败。实际工具校验错误会回传给模型修正；所有调用仍受整轮上限约束。

## 已运行的验证与实际结果

- 38 项程序级检查通过，包括先前 21 项，以及新工具边界、SDK 模拟模型集成、HTTP 往返、JSON 输出和错误分类检查。模拟模型检查属于程序测试，不属于真实 Agent eval。
- 真正使用 DeepSeek 执行一组 5 轮集成检查：初次 4 轮通过，1 轮失败。失败为人民币预算场景停止原因被模型错标成 execution_limited，未发生隐式币种转换。
- 修正停止分类并为人民币预算增加结构化信息缺口后，单独重跑失败场景，返回 needs_user，复验通过。不把单项复验包装成最后代码重新执行全套 5/5。
- 四轮已通过场景分别为：100 USD 耳机推荐；预算提高到 150 USD 且排除实际展示商品；只恢复预算到 100 USD 并保留排除；临时 200 USD 方案不改正式 100 USD。
- 具体输入、结果、调用数与耗时保存于 [集成检查记录](validation/deepseek-integration-2026-09-09.json)。原始本地轨迹留在 `.runtime/validation/`，不将日常对话轨迹默认提交到 Git。

此前接入调试的失败也保留在该记录中：同步 SDK 工具进入工作线程导致 SQLite 线程检查失败；第一次完成真实推荐后 CLI 无法序列化 SDK 的嵌套 token 用量对象。前者改为异步工具，后者只输出显式标量用量字段，两者都有程序回归检查。不能把接入失败轮次算作成功导购任务。

## 当前能力边界

- 搜索自动使用服务端正式或临时预算和用户排除项。模型不能通过 search 参数另填一个宽松预算。
- 主推荐要求当前轮取得商品原文证据、价格符合预算、商品未被用户排除、引用真实存在。搜索卡不算完整规格核验。
- **自动全约束判断目前仅实现预算和商品排除。** 防水、兼容性、佩戴形式等额外硬要求会保存，但尚未实现其确定性核验，资格保持 unknown；可交付明确受限的讨论，不能提交为全部条件已核实的主推荐。
- 证据引用校验能确认原文片段存在，不能自动证明所有自然语言推荐理由都准确。这一层仍需要后续语义验收；当前检查不是完整商品事实验证。
- 模型是否完整、正确理解用户的全部语义，仍需后续多轮案例验证；结构校验不能证明语义解析必然正确。
- 当前无进展检查主要针对重复调用，尚未完成跨不同措辞的整段决策循环识别。
- 待澄清项的跨轮自动消解、推断来源和依赖失效重算、复杂任务切换语义、用户选择状态更新仍需补齐和验收。
- 这里只完成小范围真实集成检查，没有完成 40～60 个任务 eval，也不能据此宣称通过率或相对提升。

## 使用方式

在项目根目录安装和运行：

```bash
uv sync --frozen
uv run --frozen python -m unittest discover -s tests -v
uv run --frozen shopping-agent
uv run --frozen python -m shopping_agent.web
```

网页默认仅绑定 `http://127.0.0.1:8765`。`/health` 只说明本地服务及目录已加载，不证明 DeepSeek 当前网络或余额可用。未配置密钥时报错，不自动走假数据。

CLI 可使用 `--conversation <ID>` 继续既有会话，`--message` 运行单轮，`--json` 查看结构化结果。`.env.example` 给出配置名，实际密钥只写入被忽略的 `.env`。

再次执行真实检查会调用收费 API，运行：

```bash
uv run --frozen python scripts/live_smoke.py --output .runtime/smoke
```

## 接入资料

- [OpenAI Agents SDK：模型与提供方](https://developers.openai.com/api/docs/guides/agents/models)
- [OpenAI Agents SDK：执行循环](https://developers.openai.com/api/docs/guides/agents/running-agents)
- [DeepSeek：工具调用](https://api-docs.deepseek.com/guides/tool_calls/)
- [DeepSeek：API 入口与型号](https://api-docs.deepseek.com/)

采用非 strict 的提供方工具协议及本地 Pydantic 校验，不使用 DeepSeek beta strict 端点；依赖 API 不保证 JSON 必然合规，因此保留格式错误处理。
