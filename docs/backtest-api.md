## 回测 API / 异步 job（ARB-4）

这是独立的本机研究服务，沿用 ARB-3 的请求字段。API 与 worker 是两个前台进程，共用同一个 SQLite 文件；创建接口只校验、冻结引用并持久化入队，不等待 Python 引擎。没有启动 worker 时任务保持 queued，重启 API 不丢任务。没有 live provider、交易所连接或密钥读取路径。

在仓库的 research 目录，两个终端分别执行：

```bash
uv sync --locked
# 终端一；OpenAPI 在 /docs，JSON 在 /openapi.json
uv run --locked uvicorn arb_research.api:create_app --factory --host 127.0.0.1 --port 8000
# 终端二，同一目录；持续消费，Ctrl-C 停止
uv run --locked python -m arb_research.worker
# 或只处理一个阶段并退出
uv run --locked python -m arb_research.worker --once
```

默认数据库是当前目录的 .state/backtests.sqlite3；API 与 worker 都可用 ARB_BACKTEST_DB 指定同一个绝对路径。worker 也支持 --db。此服务尚未做身份认证、租户隔离、托管部署或远程开放；保持 loopback。TanStack 的 /backtest 页面仍使用原 UI stub，本票不改布局或接入行为；真实请求发向研究服务的 8000 端口。

## Dataset 绑定

GET /datasets 返回唯一可用的已发布、frozen 目录：demo-synthetic-1h-v1（744 根小时线）、demo-synthetic-1h-v2（336 根）。它们是仓库内随 Python 包发布的不可变合成 fixture，不是行情，也不代表生产 Dataset 发布服务。每份文件都有 SHA-256、来源、覆盖范围和 as_of（2026-02-01T00:00:00Z）。读取时校验摘要，不能由请求提供文件路径、URL、provider 或价格数组。

创建时将选中区间的完整 bars、发布元数据、摘要及原始请求一起保存到 run，后续阶段只读取该快照，不重查可能变化的目录。摘要对应完整发布文件；bars 是此次选中的子区间。更改 fixture 内容必须发布新 id 与摘要，不覆盖旧 id。起止必须带时区，统一转 UTC，落在覆盖范围内；只选完整落入区间的小时 bar，数量必须大于 slow_window，不自动加载区间前的 warmup。

## 创建与查询

POST /backtests，Content-Type: application/json：

```json
{
  "dataset_id": "demo-synthetic-1h-v2",
  "engine": "realistic",
  "strategy": {"id": "sma_cross", "parameters": {"fast_window": 10, "slow_window": 30}},
  "execution": {"fee_bps": 10, "slippage_bps": 5, "fill_policy": "next_bar_open"},
  "start": "2026-01-01T00:00:00Z",
  "end": "2026-01-14T23:59:59Z",
  "initial_capital": 10000,
  "currency": "USD"
}
```

返回 HTTP 202、Location: /backtests/{run_id} 和完整 run envelope。请求校验失败为 422，且不入队。未知 run 为 404。字段严格拒绝额外属性；快慢窗口为整数 1–1000 且 fast < slow，资金为 0.01–1e12 USD 且必须是整分，费用为 0–10000 bps，滑点为 0–10000 bps 的左闭右开区间（100% 滑点没有有效卖价）。UI 当前允许不足一分的资金和 10000 bps 滑点，接入时需收紧这两项边界。币种仅 USD，策略仅 sma_cross。

GET /backtests/{run_id} 返回：

| 字段 | 含义 |
| --- | --- |
| run_id / created_at | 持久化 UUID / 入队 UTC 时间 |
| request | 最初创建请求，升级不会改写 |
| requested_engine | 当前最高请求阶段，fast 或 realistic |
| status | queued、running、draft、succeeded、partial、failed |
| dataset | 发布 id、frozen、来源、as_of、SHA-256、完整覆盖范围等元数据 |
| source | 结果就绪前为 arb.backtests；就绪后为 {package, version, adapter} |
| as_of | 固定 Dataset 发布时点，不是计算完成时间或行情新鲜度 |
| phase | 结果就绪前 null；VectorBT 为 draft；Nautilus 为 final |
| metrics | initial_capital、final_equity、total_return、max_drawdown、fill_count、fees_paid |
| equity | [{timestamp, value}]，每根 bar 收盘的净值，含未平仓市值 |
| fills | [{timestamp, side, quantity, price, fee, currency}]，实际成交记录 |
| stages | fast / realistic → {status, error, result}，各阶段永久保留 |

stage.result 包含 engine、phase、source、as_of、computed_at、metrics、equity、fills。computed_at 是计算完成 UTC 时间；每阶段独立记录包版本，adapter 为 arb.sma_cross.v1。收益与回撤是比例（0.1 = 10%）；max_drawdown ≤ 0，未实现收益按收盘价计入，不计算年化或风险调整指标。

engine=fast 仅排 VectorBT，完成后 status=succeeded 且 phase=draft。engine=realistic 先排 fast，再排 Nautilus，两个阶段归属同一 UUID。fast 完成而 realistic 等待时 status=draft；realistic 执行时 status=running，顶层仍提供 fast 结果。realistic 成功后顶层选择 final，同时 stages.fast.result 原样保留。若最高阶段失败但已有草稿则 partial；没有任何结果且所有阶段失败则 failed。fast 失败不会阻止 Nautilus 独立尝试。

POST /backtests/{run_id}/realistic 可将原 fast run 升级，返回 202，同一 run_id、相同数据与参数。重复调用幂等，不生成第二个 realistic job，也不重试已失败的阶段。要重跑失败阶段，创建新 run。普通 POST /backtests 每次创建新 run，不提供提交去重键。

## 执行合约 v1

与 PieQuant 对齐的是显式执行参数、数据冻结和阶段来源分离的思路；这里没有复用或声称通过 PieQuant 的生产撮合一致性验证。实现依据锁定的 VectorBT 0.28.5 与 NautilusTrader 1.231.0 源码，核验日期 2026-10-29。

两引擎独立执行同一 long-only SMA 规则：使用前一根及更早的收盘价计算快慢均线；快线高于慢线时进入 long，否则退出。没有区间前的仓位或 warmup；信号在下一根开盘才生效，不在产生信号的收盘成交。买入使用可用现金的 95%（含费用），卖出清空仓位，不能加杠杆或做空，结束不强制平仓。

- 单边费用 = quantity × fill_price × fee_bps / 10000。10 bps = 0.1%，每次买入和卖出各收一次。
- 买入价 = open × (1 + slippage_bps / 10000)，卖出价 = open × (1 − slippage_bps / 10000)。费用基于滑点后成交价，没有另加固定费。
- VectorBT 通过 from_signals 的 price=open、close=收盘估值、fees、slippage 参数执行，使用浮点可分割手数。
- Nautilus 以冻结开盘价生成双边 QuoteTick，bid/ask 各按上述滑点偏移；close tick 只用于收盘估值。market order 使用原生模拟交易场所 SIM、现金账户和 MakerTakerFeeModel，无真实连接。价格精度 8 位、手数向下取 6 位、USD 费用由原生 Money 取到分，因此不承诺与 fast 逐分一致。输入资金必须为整分，拒绝会被原生 USD Money 静默取整的金额。
- 这是固定 bps / 足量流动性的离线模型，不模拟订单簿排队、冲击、延迟、部分成交或真实市场深度。Nautilus 的 realistic 标签表示所用引擎，不表示已校准现实交易成本。

参考：[VectorBT Portfolio API](https://vectorbt.dev/api/portfolio/base/)、[Nautilus fill models](https://nautilustrader.io/docs/latest/concepts/backtesting/fill-models/)。在线 Nautilus 文档可能已指向新版；本实现以锁定版源码与真实执行测试为准。

## ARB-5 回放结构

GET /backtests/{run_id}/bars 提供保存的冻结 OHLCV bars，响应含 run_id、source、as_of、sha256、bars。每根 bar 有 timestamp（开盘）、close_timestamp、open/high/low/close/volume；所有时间均为 UTC ISO 8601 字符串。

GET /backtests/{run_id}/events?after=0&limit=200 为持久化增量事件页（JSON 轮询，不是 SSE）。limit 为 1–1000；seq 是全局单调游标，在单个 run 内可能有间隔。将 next_cursor 原样传入下一页；空页保留传入游标。多次读取相同游标会重放相同事件。生命周期与阶段结果原子写入，API 重启不会丢游标。

```json
{
  "run_id": "<uuid>",
  "source": "arb.backtests",
  "as_of": "2026-02-01T00:00:00Z",
  "next_cursor": 4,
  "events": [{
    "seq": 4,
    "run_id": "<uuid>",
    "type": "fill",
    "engine": "fast",
    "recorded_at": "<UTC persistence time>",
    "source": {"package": "vectorbt", "version": "0.28.5", "adapter": "arb.sma_cross.v1"},
    "as_of": "2026-02-01T00:00:00Z",
    "computed_at": "<UTC computation time>",
    "data": {"timestamp": "2026-01-02T06:00:00Z", "side": "buy", "quantity": 89.708743, "price": 105.7924698, "fee": 9.490509, "currency": "USD"}
  }]
}
```

事件类型为 stage_queued、stage_running、stage_failed（带 error）、fill、equity、result_available（带 phase）。fill 与 equity 含 data 及结果来源；生命周期事件的来源与数据时点由 envelope 给出。事件按持久化顺序，不按市场时间交错：一个阶段先写 fills 再写 equity，最后 result_available。回放应按 engine 分组，按 data.timestamp 排序，用 (run_id, engine, seq) 去重；不要把两阶段成交相加。结果只在整阶段结束后一次性发布，不宣称逐笔实时推送。

## 队列与验证

SQLite BEGIN IMMEDIATE 原子认领、每阶段唯一键、多 worker 不重复执行同一有效租约。引擎在独立子进程运行，120 秒硬超时，租约 180 秒；worker 崩溃后由后续 claim 将过期阶段标记 failed，不自动重试或让迟到结果覆盖。API 可以继续接受任务；再次启动 worker 会处理持久化队列。异常详情仅进入 worker 本地日志，对外只给稳定错误码。SQLite 文件含研究配置和结果，需由运行环境负责备份和访问权限。

```bash
cd research
uv run --locked pytest -q
uv run --locked python -m arb_research.smoke
```

测试通过实际 API 请求、SQLite 和两个真实引擎子进程验证 202 入队、同 run 草稿→最终、重启持久化、费用滑点、无未来数据引用、事件分页、原子认领、过期租约、失败保留草稿、升级幂等及非法请求不入队。
