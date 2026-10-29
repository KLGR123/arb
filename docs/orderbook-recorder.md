# CLOB market 盘口录制与重放

入口在 `research/src/arb_research/orderbook/`，独立于回测 worker。只连接公开的
`wss://ws-subscriptions-clob.polymarket.com/ws/market`，记录 `book`、
`price_change`、`last_trade_price`、`tick_size_change`。没有 user 频道、交易、
签名、钱包或凭据配置；数据价格按 pUSD≈USD 理解。

## 安装与录制

需要 Linux、Python 3.12 和 uv。从仓库根目录：

```bash
cd research
uv sync --locked
uv run --locked python -m arb_research.orderbook record \
  --token TOKEN_ID --token SECOND_TOKEN_ID \
  --output .state/orderbooks --duration 60

# 或指定 condition ID，经公开 Gamma API 解析该 market 的 outcome token IDs
uv run --locked python -m arb_research.orderbook record \
  --market CONDITION_ID --output .state/orderbooks
```

`--token` / `--market` 均可重复，合并去重后最多 200 个 token。token ID 是公开行情标识，
不是认证 token。market 必须是 condition ID，不能填 event slug。不指定 duration 持续运行；
SIGINT / SIGTERM 会落盘、写停止事件并关闭连接。程序不会自动安装系统服务。
有限时长内任一选定 token 未收到有效完整 book，CLI 会以非零状态退出，避免空录制被误认为成功。

每次录制通过 main 的 `arb_research.compliance.GeoblockClient` 检查公共数据访问出口
（进程内缓存 60 秒）；仅注入公共 HTTP fetch，复用严格解析、退避和共享预算。
返回不完整或检查失败时告警并继续公共录制，CLI 输出保留 `error`，指标
`geoblock_check_failed=1`；检查不是公共录制的硬门槛。`blocked=true` / US/VA 是交易限制信息，按用户授权可继续
访问公开只读行情；该结果保留在 CLI 输出与指标中，不能解释为交易许可。
main 的交易 guard 和默认拒绝路径保持原样，读行情不会改变交易开关。

用户授权的公共数据路由由 NAS secret / runtime custom_env 的 `ARB_POLYMARKET_PROXY` 注入，
只支持 socks5h。HTTP 显式传 `proxy` 并设置 `trust_env=False`；WS 显式传相同的 `proxy`。
未配置时直连，不读取全局 HTTP(S)_PROXY / ALL_PROXY，不写全局代理环境变量。
该配置仅进入固定公共 geoblock / Gamma / market WS 客户端，不进入交易 guard 或其他研究服务。
不要打印、提交或将配置值放入命令行参数；录制器不持久化该值，CLI 也不回显可能含它的底层异常。

使用官方当前文档列出的原始 market WebSocket 协议，适配 V2 后的盘口格式。
统一 `polymarket-client` SDK 优先用于常规 API；本模块选用直接 WebSocket，是为了保留原始
事件字段、接收顺序和生命周期，而不经过 SDK 的事件转换及自动重连。没有引入已归档的
`py-clob-client`，也没有引入任何交易 SDK。HTTP 仅用于 geoblock 和 Gamma token 解析。

所有外部请求共用合规模块的 `PUBLIC_API_BUDGET`（900 容量、810/s 补充，低于
9,000 req/10s）；Gamma HTTP、WS 连接和发送帧另加保守令牌桶（5/s，突发 5）。
geoblock 使用共享 client 的 3 次尝试及退避，见 [合规说明](compliance.md)。Gamma HTTP
暂时错误/429 最多尝试 4 次，WS 断线指数退避 1–30 秒加抖动；稳定连接
超过一分钟后重置退避。限制是进程级，**同一出口请共用一个 recorder 进程**，与其他研究
服务共享出口时应由上层统一分配预算。这里只读检查地区，不调用始终拒绝交易的
`ComplianceGuard.require`，也不改变交易开关。一个输出目录用 Linux flock 禁止并发 writer。

## 存储合约 v1

采用 JSONL（本票在 JSONL / Parquet 中选择 JSONL），每行 UTF-8 JSON：

```text
OUTPUT/
  date=2026-10-30/market=CONDITION_ID/events.SESSION.jsonl
  date=2026-10-30/market=CONDITION_ID/snapshots.SESSION.jsonl
  date=2026-10-30/market=_control/events.SESSION.jsonl
  date=2026-10-30/market=_control/gaps.SESSION.jsonl
  metrics.json
  .record.lock
```

日期是接收时间的 UTC 日期。市场数据按上游 `market` 分区；连接和停止等控制事件进入
`_control`。每次运行使用新 session，重启不会覆盖历史文件。snapshot 每 60 秒生成，
`--snapshot-seconds` 可调整；即使只有心跳也会生成，按 market 分区保存各 token 的状态。
完整 book 和原始增量均进入 events；price_change 中的 size 是该价位的新总量，0 删除。
交易价格和 tick size 不直接改变深度。

events 行包含：

| 字段 | 含义 |
| --- | --- |
| `version` | 当前为 1，后续格式迁移必须版本化 |
| `session`, `seq` | 本进程日志序号，从 1 开始，全 market 共用；不是服务端序列号 |
| `received_ns` | 本地接收顺序时间，Unix 纳秒；同一 session 内对时钟回拨做单调修正 |
| `kind` | `event` 或 `control` |
| `payload` | 上游事件原始 JSON 对象，保留服务端 timestamp、hash 及未知字段 |
| `sha256` | 除本字段外整行 canonical JSON 的 SHA-256，用于本地完整性检查 |

WS 数组帧按数组顺序拆成事件，不保存原始空白或字节编码。snapshots 包含 `through_seq`，
可定位对应的原始日志；当前 replay 直接扫描 events，**不信任 checkpoint 作为数据来源**。
因此重建是流式扫描，耗时随历史增长，状态内存与订阅盘口大小相关。
返回的缺口明细另外占用与缺口数量相关的内存。
读取各分区通过有序归并，每个事件文件占一个读句柄；长时间录制请按服务的
`LimitNOFILE` 控制一次扫描的日期范围（可将完整日期目录复制到单独的重放根目录）。
完整性序号检查需要同一 session 的全部 market/control 分区，不能只复制一个 market。

写入每条消息后 flush，每次 snapshot 和正常退出时 fsync。突然断电可能丢失最近一个
snapshot 周期内的缓冲数据；不是零数据损失保证。磁盘满/不可写会让进程失败退出，交给
supervisor 报警。截断尾行或 checksum 错误会让 replay/gaps 非零退出，不能静默跳过。
保留损坏文件供排查；修复必须对副本操作，不能把修复日志宣称为连续原始行情。

## 重放与缺口

```bash
uv run --locked python -m arb_research.orderbook replay .state/orderbooks \
  --at 2026-10-30T12:34:56.123456+00:00 --token TOKEN_ID
uv run --locked python -m arb_research.orderbook replay .state/orderbooks \
  --at-ns 1793363696123456789
uv run --locked python -m arb_research.orderbook gaps .state/orderbooks
uv run --locked pytest -q tests/test_orderbook.py
```

重放按 **接收时间**（received_ns、session、seq）排序，`--at` 是含端点的“当时已观察到什么”，
不是服务端生成时刻；`--at-ns` 可精确指定日志中的时间。exchange timestamp 留在 payload，
不用于重新排列因果顺序。`--token` 只过滤输出，仍扫描全日志以检测本地序号缺失。
无 at 时返回录制结束的状态；正常 stopped 后 `available=false, reason=recording_stopped`。
要查看录制内的最后有效深度，指定最后一条市场事件的 received_ns。
时间点早于首条记录时结果为空；晚于最后已知事件时，输出同时给出 last_record_received_ns，
调用方必须检查数据新鲜度，不能把历史数据当作该时点的真实盘口。

首次收到完整 `book` 才可重建。断线、缺失初始 book、PONG 超时、无基线 delta、非法数据、
best bid/ask 不一致、同一 server hash 对应不同深度、或本地序号缺失都会留下证据。
已知缺口使盘口 `available=false` 并清空导出的深度，增量不能修复它；新的完整 book 才能恢复。
连续完整 book 与本地重建深度不一致记录 `snapshot_mismatch`，新快照作为权威基线恢复。
这表示可能丢失或不同步，不单凭该现象断言是服务器故障。

market 协议当前没有承诺连续的 server sequence，也没有可对每个 delta 独立重算的完整 hash
算法/元数据。模块保存上游 hash，检测相同 hash 的内容冲突；**不把本地 SHA-256 当作服务端
hash 校验，不声称能发现所有上游丢包**。整段历史文件被删除、尾部完整记录丢失而没有后继
序号、或静默的非最优价位丢包，都可能无法检测。`gaps=0` 只表示未观察到这些异常。

`metrics.json` 原子更新（默认每 60 秒及退出），包括事件/心跳/重连/各类缺口计数、有效盘口数、
最后接收时间和检查能力。计数是当前 session 的累计值，重启清零；历史 gaps JSONL 保留。
`gaps` CLI 从原始日志重新计算，适合离线审计；损坏日志以非零退出报告错误。

## NAS 常驻部署

由用户选择长期部署目录和数据盘，先短录制确认有 book/PONG，再启用服务。示例 systemd 单元
（将绝对路径和 token 替换为自己的值；示例不包含凭据）：

```ini
[Unit]
Description=Public CLOB market recorder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=taropie
WorkingDirectory=/srv/arb/research
ExecStart=/srv/arb/research/.venv/bin/python -m arb_research.orderbook record --token TOKEN_ID --output /srv/arb-data/orderbooks
Restart=on-failure
RestartSec=30
TimeoutStopSec=30
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=true
LimitNOFILE=16384

[Install]
WantedBy=multi-user.target
```

本票不自动写入 `/etc` 或启动常驻服务。部署者可通过 systemctl start/stop 控制，通过
journalctl 查看 JSON 缺口日志。监控 supervisor 状态、metrics 文件更新时间、重连和缺口计数、
available_books，以及磁盘空闲容量。不要仅用市场无成交判断断线，心跳用于连接活性检测。
单一进程断线后重新订阅全部 token 并等待每个 token 的完整 book。

磁盘估算用短样本实测：若整个订阅平均每秒 r 条事件、每条日志 b 字节，原始日量约为
`r × b × 86,400` 字节。例：10 条/s、1 KiB/条约 0.82 GiB/天，7 天约 5.8 GiB；
每分钟 100 个 token、每个完整 snapshot 20 KiB，再增加约 2.75 GiB/天。
给事件、checkpoint、gap 和文件系统至少预留 2 倍实测量；这些是估算，不是当前实录速率。
按 UTC 日期归档已关闭分区，勿移动仍在写入的文件；当前不自动删数据或压缩。

连续 7 天无已观察缺口是后续观察项，不阻塞 PR；也不等于能证明上游绝对无丢包。

## 协议依据与验收边界

协议于 2026-10-30 核对官方 [Real-Time Data](https://docs.polymarket.com/market-data/realtime-data)
与 [API](https://docs.polymarket.com/getting-started/api)：原始 public market 订阅、
10 秒 PING/PONG、四种事件字段；统一 SDK 见
[Python SDK](https://docs.polymarket.com/getting-started/python)。

离线测试通过生产 writer 录制受控协议帧，再验证历史 L2、重连、缺口和文件完整性；
另有真实公开 market 录制 fixture，路径及复现结果见下节。两者均可离线运行，测试不访问外网。
真实录制只证明该样本覆盖的时段，不代替后续 7 天观察。

## 真实公开行情样本

`research/tests/fixtures/orderbook-live/` 保存未经改写的真实 events、周期 snapshots、
最终 metrics，以及 `capture.json` 中的时间范围、文件 SHA-256 和运行摘要。
样本来自活跃的 “Will Benjamin Netanyahu be the next Prime Minister of Israel?” 市场，
订阅两个 outcome token；UTC 2026-10-29 16:55:22–16:58:24，设定录制 180 秒（另含连接关闭时间）。

- 3,456 条日志：22 条 book、3,420 条 price_change、9 条 last_trade_price、5 条生命周期事件。
- 34 条周期/退出快照；PING/PONG 各 16 次；最后市场事件时两个盘口均可重建。
- `gaps` 报告 2 条 `server_hash_conflict` 和 1 条 `disconnected`。同一服务端 hash 对应的
  价位数量从 197597.89 变为 197587.89，触发两个 outcome 的保守失效与一次重连；重新收到
  两个完整 book 后恢复。这是观察到的一致性异常，不能仅据此断言上游丢包。
- 样本期间没有 tick_size_change；该类型由受控离线 fixture 验证。
- geoblock 返回 `blocked=true`；独立调用未修改的共享交易 guard，仍以 `geoblocked` 拒绝。
- Gamma markets 和 CLOB `/time` 经显式公共代理均返回 200。
- 包含元数据约 2.85 MiB。样本不含代理配置或认证信息；不是连续 7 天运行证明。

从 `research/` 离线复现：

```bash
uv run --locked python -m arb_research.orderbook replay tests/fixtures/orderbook-live \
  --at-ns 1793293102367213618
uv run --locked python -m arb_research.orderbook gaps tests/fixtures/orderbook-live
uv run --locked python -m pytest -q tests/test_orderbook.py
```

真实 fixture 测试将完整 book **到达之前**的增量重建结果，与下一条服务端权威 book 的所有
价位和数量比较，避免仅验证“加载快照后等于自身”。另外验证原始文件校验和、两个盘口、
心跳、快照数量与真实异常后的失效/恢复。重连后的首次完整 book 用于重新建立基准，
不将缺口期间不可用的深度与其比较；缺口报告保留全部观察异常。
