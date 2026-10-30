# LLM 预测对照实验 v1

ARB-19 在 **50 道真实、已结算、非加密体育市场（22 个赛事）**上，比较同一模型的含价格与不含价格提示。两组都用 NAS 本地 Codex `gpt-6.1-terra`、`model_reasoning_effort="high"`，高于 ARB-11 的 `low`。每题每组各采样一次，合计 100 次；不扩充独立样本数。

这是**回溯实验**，模型在结算后运行。赛前时间截断限制的是输入内容，不能把事后调用变成前瞻预测，也不能保证消除模型的事后知识。结论只描述这批提示中的价格依赖及离线评分，不代表可交易优势。

**含价格组 36/50 题逐字复制数值 q，blind 组 0/50；去掉价格后更少贴价，但尚无稳定超额的证据。** blind 的样本 Brier 更低，价格移除的配对 Brier 改善为 0.011672020，赛事聚类 95% 区间为 **[−0.018988171, 0.048304263]**，跨过零。

| 组别 | 题数 | Brier ↓ | Logloss ↓ | proper 扣费 PnL / pUSD | 1/4 Kelly 扣费 PnL / pUSD |
| --- | ---: | ---: | ---: | ---: | ---: |
| 本票 blind / high | 50 | 0.164674000 | 0.499492848 | +244.640970 | +67.201206 |
| 本票含价格 / high | 50 | 0.176346020 | 0.518752144 | +101.372630 | +31.737070 |
| 本票纯市场价 p=q | 50 | 0.190243345 | 0.549245435 | 0 | 0 |
| ARB-11 / low，**独立加密样本** | 56 | 0.240393304 | 0.673170327 | 0 | 0 |

本票 PnL 使用下文的 ARB-17 费用公式；每题预算 100 pUSD，本票总预算 5,000，ARB-11 为 5,600。各组模型概率到 q 的平均绝对距离：blind **12.253 个百分点**，含价格 **2.473 个百分点**；ARB-11 为零且 56/56 题复制 q。当前结果与价格锚定现象一致，不能把跨队列的分数差异归因于 effort，也不能宣称复现了前瞻超额。

相对本票市场价，blind 的 Brier 优势为 0.025569345，95% 区间 [−0.011895657, 0.069344597]；含价格组为 0.013897325，区间 [−0.001266183, 0.035283796]。两者也都跨过零。

旧 20bps 费用口径仅供与 ARB-11 对照：blind proper / Kelly PnL 为 +253.351071 / +71.853036 pUSD；含价格组为 +102.101725 / +32.065388 pUSD。主表与该敏感性数值分别存放，不混用。

## 采样与时间边界

2026-10-30 UTC 从 Gamma `/markets` 的 `closed=true`、`closedTime` 降序结果开始，逐页读取，每页内按市场 ID 排序。最多扫描 30 页，每场赛事最多三题；取首先满足条件的 50 题，不按模型结果、盈亏或答案取舍。实际扫描 7 页，去重后评估 626 个候选，排除 576 个。偏向最近已结算的体育题；没有政治或宏观题，因此不代表非加密市场总体。

必须有二元 outcome、`sportsMarketType`、`gameStartTime`、明确的 Data API v2 已结算回执、单边 payout，以及 Gamma 权威费率或明确关闭费用的配置。沿用 ARB-11 记录的模型训练截止时间 2026-06-01，只接纳其之后的题，拒绝加密相关文本和价格提示文本。非 Yes/No 标签明确映射：**YES 对应 token index 0 的 `target_outcome`，NO 对应另一项**，真值由同一 condition 的 `payouts[0]` 确定；作废、50/50、缺失支付均排除。

每题 `forecast_at=t=gameStartTime−1h`，本批 t 为 2026-10-29 18:15–23:15 UTC。fixture 的 `end_at` 保存的是 **gameStartTime（事件开始边界）**，不是实际比赛结束时间。要求市场已在 t 前创建，t 早于开始，开始早于结算。基准 q 来自 Data API v2 `/v2/prices-history?token_id=…&as_of=t`：报价必须在 t 前、严格位于 (0,1)，且不陈旧超过 24 小时。本批报价年龄最小 / 中位 / 最大为 **1 / 16 / 23,836 秒**（最大约 6.6 小时）。保存原始报价回执与 `quote_at`，不采用今日盘口或终局价格。

[官方价格文档](https://docs.polymarket.com/api-reference/markets/get-a-tokens-price-history)区分历史区间与 `as_of`；exact tick 可为 `resolution_seconds=0`，本实验通过赛前时间和内部价格筛选，而非错误地排除所有零宽度 tick。[官方结算文档](https://docs.polymarket.com/api-reference/markets/get-resolution-state)提供 condition 状态与 payout；必须唯一匹配 condition、RESOLVED、支付方向和 `resolved_at`。

Gamma 元数据是在结算后抓取，**没有独立存档证明规则当时的措辞**。fixture 保留现代原始 question、description、outcome、token、开始时间、更新时间和费用配置；从 description 去掉 URL 后形成规则。模型收到其真实抓取时间及回溯警示，不把抓取时间伪造为 t。没有外部新闻或背景材料，也没有声称这些规则是赛前档案。近期列表在采集期间可能变化，因此实时重新 collect 不保证得到同一批题；可复现结果以已录制 fixture 为准。

## 提示消融与模型隔离

两组完整 prompt 和输出保存在 `docs/fixtures/llm-ablation-predictions-v1.json`。`context_for()` 只构建字符串白名单：t、题目、目标标签、开始时间、规则、来源与抓取时间、时间警示、YES/NO 映射。**blind 组没有市场价格、mid、盘口、报价时间、市场 ID、condition、结算标签或原始元数据**。赔率、market price、链接与加密相关文本检查不通过的题被拒绝。

`with_price` 组只比 blind 多一个 `market_price=q` 字段，其余内容、模型、effort、采样次数、运行环境相同。这样价格提示的组内差异可以配对；ARB-11 的 56 道加密题仅作历史旁证，不能将跨题材、跨 effort、跨前瞻/回溯设计的差异解释为单因素因果效应。每组仅一次采样，随机输出差异也可能影响配对差值。

复用 `forecast.codex.CodexPredictor`，调用参数包括：

```text
codex exec --model gpt-6.1-terra --sandbox read-only --ephemeral
  --skip-git-repo-check --ignore-rules --json
  -c model_reasoning_effort="high" --output-schema response.schema.json -
```

实际调用另禁用 shell、web、image、code mode、多 agent、skills 与 memories；继承的 MCP/hooks/profiles 非空时直接拒绝。每次在仅含 response schema 的临时目录运行，剥离 `MULTICA_*`、`ARB_*` 与全局代理变量。使用 NAS 已有 CLI 身份，不设置或使用 API key。每次超时 240 秒，四个前台工作线程，退出前全部 join；超时会清理并等待该次调用的进程组。

只接受完成 JSONL 流中的最终 `{ "p": number }` 以及 `turn.completed`，拒绝非法概率、工具事件、失败或未完成流。落盘 model、请求的 effort、开始/完成时间、完整 prompt、SHA-256、p 和 CLI token usage。`high` 是明确请求的参数，不以 usage 中的 reasoning token 数推断服务内部实际思考量。

100 次调用于 **2026-10-30 02:04:43–02:11:28 UTC** 完成，100 成功、0 失败、0 工具调用。CLI 记录 input 596,835 tokens（其中 cached 358,400）、output 6,500、reasoning output 4,600；这是用量回执，不是账单金额。未在运行后根据表现替换、追加或删除题目。

## 指标与费用

复用 ARB-11 的 `forecast.metrics.evaluate()`：逐题等权 Brier、自然对数 logloss（仅 logloss 将概率截到 [1e−15,1−1e−15]）；同一赛事的题整体 bootstrap，2,000 次、seed=11。优势为市场损失减模型损失。移除价格的配对提升为 **含价格 Brier−blind Brier**，正值表示 blind 更好；22 个赛事聚类只提供探索性区间，不能覆盖采样偏差或事后知识风险。

纸面仓位使用 ARB-11 的 fully collateralized proper betting 和 1/4 Kelly（上限 10%）。每题独立预算 100 pUSD≈USD，共 5,000，不复利；负向仓位按买 NO 计算，持有至结算。市场基准 p=q，按相同策略不建仓，所以 PnL=0。

**主结果 `paper_protocol`** 复用 ARB-17 调用的 `negrisk.taker_fee()`：`fee=shares×rate×price×(1−price)`，五位小数舍入。费用纳入 100 pUSD 预算，并留一个费用 tick 的舍入余量。35 题 rate=0.05，9 题 rate=0.03，6 题明确无费用；全部来自 Gamma `feeSchedule` 或 `feesEnabled=false`，不猜费率。配置同样是在结算后读取，历史实际费率未独立核验；这是费用模型估计。

为与 ARB-11 原数字直接对照，另报 `paper_legacy_arb11_20bps`（入场名义金额固定 20bps）。两种费用口径分别保存，不能混称。ARB-11 p=q，在两种费用口径下都没有仓位、费用或盈亏。q 是历史成交观测，没有历史 bid/ask、深度、点差、滑点或模型延迟成交；所有纸面收益均不代表可执行回报。

## 离线复现与实网入口

从仓库根目录运行，score **不访问网络、不调用模型、无需凭据**：

```bash
research/.venv/bin/python -m arb_research.forecast.ablation score \
  --input docs/fixtures/llm-ablation-cohort-v1.json \
  --predictions docs/fixtures/llm-ablation-predictions-v1.json \
  --baseline docs/fixtures/llm-forecast-real-v0.json \
  --output .state/llm-ablation-replayed-v1.json
cmp .state/llm-ablation-replayed-v1.json docs/fixtures/llm-ablation-results-v1.json
cd research
uv run --locked pytest
```

三个实录文件分别保存公开市场/报价/结算/费用回执、逐次 prompt 与模型输出、配对结果与 ARB-11 独立比较。离线评分重新检查市场/预测绑定、prompt hash、输出、标签映射、历史报价时间、模型 effort 与无工具审计；单测 mock Codex 的进程边界，覆盖价格/答案字段扰动不改变 blind prompt、拒绝未来报价和损坏记录、计分及 YES/NO 费用，并完整重放真实 fixture。

以下命令会真实访问公开端点或调用本地 Codex，只用于明确启动新实验，不是离线重放：

```bash
research/.venv/bin/python -m arb_research.forecast.ablation collect \
  --count 50 --max-pages 30 --output .state/new-ablation-cohort.json
research/.venv/bin/python -m arb_research.forecast.ablation predict \
  --input .state/new-ablation-cohort.json --workers 4 \
  --output .state/new-ablation-predictions.json
```

所有 Polymarket 请求复用 `forecast.public.PublicClient` / ARB-7 `PublicDataClient`：固定公共 GET allowlist、HTTPX `trust_env=False`、仅客户端显式传入 `ARB_POLYMARKET_PROXY`、共享 SQLite 令牌桶及通用限流、退避。没有全局代理设置，代理值不输出或保存。复用 ARB-6 compliance guard，本次实际 `blocked=true`、`trading_allowed=false`；全流程没有订单、签名或钱包入口。

## 交付验证

2026-10-30 首轮在 `research/` 执行 `uv run --locked pytest`：305 passed，15 条依赖弃用警告，耗时 138.08 秒。开 PR 前远端 main 历史重写；已确认旧 tip `5599e58` 与新 tip `377726e` 的文件树完全一致，并 rebase 到新的 `origin/main` 后再次运行全量测试：**305 passed，15 条依赖弃用警告，耗时 126.12 秒**。真实 fixture 离线 score 后 `cmp` 逐字节一致；新增 17 个消融测试全部通过，CI 不发外网请求。

独立只读审查已复核提示白名单、label/token/payout 映射、费用核算、按赛事配对 bootstrap、真实 fixture 重放及文档数值，无阻塞 finding。最终变更检查未发现 secret 值或无关文件；保留原 ARB-11 实录，未修改其基线或混入新队列。
