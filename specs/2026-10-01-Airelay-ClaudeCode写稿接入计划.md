# Airelay Claude Code 写稿接入计划

> 使用 writing-plans 整理执行与验收；用户已选择该路线，按当前会话授权直接实现。

**目标：** 使用本机真实 Claude Code，通过 Airelay 请求 `claude-sonnet-5-5` 写稿及返工；仍由 Qwen 独立审核，三篇全部通过后展示。

**架构：** 内容主控、任务和费用台账保留现有服务。新增 Claude Code 运输层，只替换 writer/repair 的文本执行方式，planner/reviewer 继续普通 Qwen API。原生 CLI 运行于独立临时目录，禁工具、文件访问、MCP、hooks 和会话续写；凭据只传子进程环境，不修改用户全局 Claude Code 设置。

## 执行任务

- [ ] 原生运输层：`content/providers/claude_code.py`。本地一次代理接收真实 CLI 请求并转发至固定服务根；核对模型、输出上限和空工具清单，不重定向，每任务最多一条付费 Messages 请求。完整 JSON/SSE 响应保留模型、usage、请求 ID；没有确定响应时保留未知状态。
- [ ] 内容集成：`providers/text.py` 支持显式 `http_api/claude_code`。仅 writer/repair 且 Anthropic 可用 CLI；预检可执行文件，保留现有统一返回值、模型和 JSON 审核。CLI 默认等待 600 秒，租约覆盖等待并额外保留 60 秒；价格预留包含客户端开销，实际费用按完整 usage 结算。
- [ ] 前端恢复：内容状态返回非敏感 `writer_runtime`。生成与返工等待按配置加 90 秒，后台未完成继续轮询；未知结果不释放 request_id，重复点击不产生新收费请求。流程仍保持三篇全审门槛。
- [ ] 配置与验收：项目 `.env` 切回原 Airelay 凭据并设置 CLI 可执行文件，不改共享密钥文件、全局 Claude Code 配置、PostgreSQL 迁移或媒体线路。离线测试、独立审查之后做一次受控真实 CLI 小请求；不自动重提此前失败任务。

## 验收要求

测试先红后绿。覆盖真实 CLI 参数/隔离、内部重试被代理拦截、超时后的费用未知、缓存用量、响应已收到但进程失败、模型身份和输出上限、费用台账重放，以及长任务 UI 反馈。业务结果和原参考版本保留，失败不虚记零费用。

## 已落地

运输层、文本路由、父任务执行配置冻结、前端长任务反馈/刷新后幂等恢复及项目配置均已实现。实际能力接口返回 writer/repair 的 `claude-code-cli / claude_code`、600 秒；reviewer 保持 Qwen-plus。后端已加载新代码，原主参考和写作提示保留。

原生 CLI 使用独立目录、禁用工具/MCP/hooks，真实请求经过本地单次代理。写稿规则用 `--append-system-prompt` 追加，保留 Claude Code 原生身份和内置系统规则。项目启用与正常客户端配置一致的 1M 上下文选项，CLI 参数使用 `[1m]`，实际 HTTP 模型仍精确为 `claude-sonnet-5-5`；没有手工伪造客户端身份或专有 beta 请求头。

写稿预留增加 4096 token 的客户端输入开销估计。请求前冻结真实可执行文件及上下文配置；父任务只在首次创建时冻结执行配置，部署后同 request_id 重放仍返回原记录。已发送旧任务不能因换线路产生新的重复收费请求。

前端后台长任务最终失败/未知都恢复明确反馈。刷新后已确认终态允许新的人工生成；未知请求仍取回原 request_id。

## 实际验收尚未通过

三个有明确输入差异的小请求分别为 `job_2f1f6ad1ad08`、`job_652169a0e64e`、`job_3b254453f305`；每个只发送一次，全部收到 HTTP 502 `Upstream access forbidden`，没有返回稿件或用量。没有自动重试。费用保留待核对，不虚记 0 元。

本机正常 Claude Code 的既有成功记录停在 15:00，不能代表当前服务状态。当前需用户确认常用客户端是否仍能用同一 Airelay 账户/模型回复，以区分项目隔离调用路径和外部状态变化。不能将单次代理路径的失败泛化为整个 Airelay 不可用，也不能声称三篇候选已经生成。

验证证据：前端 90 项、类型检查和构建通过；CLI 运输层 29 项真实本机进程+本地假上游检查通过；执行身份/文本接线回归 32 项通过。最终记录在 `production/runtime/claude-code-writer-20261001/verification.json`，源码备份在同目录 `source-before`。这些通过项不替代真实收费生成验收。
