# CLOB 多日录制运维

首段实证见 [JSON 报告与文件校验清单](evidence/arb23-clob-observation.json)。
UTC 2026-10-30 02:11:05 至 2026-10-31 01:39:59，单段 **23h28m54s**，
未达 ≥24h 验收。212,596 条事件日志的本地 SHA-256 全部通过，未见本地序号缺口；
884 次重连、1,737 次 hash 内容冲突均保留，其中 hash 冲突均经完整 book 恢复，
最长恢复等待 34.63 秒。PING/PONG 为 7,026/6,996，原始 JSONL 共 214,502,280 字节。
停止记录正常，但旧格式未持久化停止原因和单调时钟耗时，且当前用户无权读取
system journal，不能断言提前结束原因。此段仅作为部分实证，不与后续短段拼接验收。

后续健康行增加 `elapsed_seconds`（单调时钟秒数），停止行增加 `stop_reason`
（`duration_elapsed` / `cancelled` / `error`），用于区分墙钟变化和退出路径；
报告兼容旧录制，缺失信息保持未知。重新观察使用独立根目录与 26 小时上限，PR 保持 draft。

`arb_research.orderbook.ops` 在 ARB-8 的只读 recorder 上增加长运行监管、分段轮转、
磁盘预算和离线按日统计。原始事件格式、接收时间语义与重建规则见
[录制器说明](orderbook-recorder.md)。只访问公开 Gamma、geoblock、market WS，
复用 `arb_research.compliance`，不包含交易、签名或私有频道。价格按 pUSD≈USD。

## 启动与停止

在 NAS 的独立研究 checkout 中，使用 Python 3.12：

```bash
cd research
uv sync --locked
# ARB_POLYMARKET_PROXY 由 NAS secret 注入；不要在命令行或日志里输出它。
uv run --locked python -m arb_research.orderbook.ops record \
  --market CONDITION_ID --output .state/clob \
  --duration 604800 --segment-mib 64 --quota-gib 20 --reserve-gib 5
```

先用 `--duration 60` 检查完整 book、PING/PONG、数据速率与磁盘估算，再启动多日服务。
`--market` / `--token` 可重复，整个市场集合共用一个进程与既有令牌桶及退避。
HTTP 客户端显式 `proxy=` 且 `trust_env=False`；WS 显式 `proxy=`。
不要设置全局 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`。变量缺失时直连；连接失败会在
健康文件显示重连和不可用盘口。底层网络异常只打印类型，避免泄露路由。
geoblock 被封或不可用仍仅允许公共数据，原 guard 的交易拒绝语义不变。

SIGINT / SIGTERM 会取消连接、写 `stopped`、最终健康记录与快照、fsync 并退出 0。
每次启动生成新 session；同一根目录的 flock 在启动网络发现前取得。
只能有一个 writer，停止旧实例后再启动。恢复依靠新连接完整 book，不能用旧本地
快照掩盖停机时段。系统休眠、重启、网络中断均须保留在报告中。

## 独立研究服务

以下为专用 **user systemd** 单元模板，不修改生产服务；替换路径和公开 condition ID。
`/srv/arb-secrets/clob-public.env` 必须为用户私有文件（0600），仅包含
`ARB_POLYMARKET_PROXY`，不能提交。直接运行时该文件可以为空以使用直连。

```ini
[Unit]
Description=ARB public CLOB research recorder
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
WorkingDirectory=/srv/arb/research
EnvironmentFile=/srv/arb-secrets/clob-public.env
ExecStart=/srv/arb/research/.venv/bin/python -m arb_research.orderbook.ops record --market CONDITION_ID --output /srv/arb-data/clob --duration 604800 --quota-gib 20 --reserve-gib 5 --segment-mib 64
Restart=on-failure
RestartPreventExitStatus=73
RestartSec=30
TimeoutStopSec=30
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=true
LimitNOFILE=16384

[Install]
WantedBy=default.target
```

保存为 `~/.config/systemd/user/arb-clob-research.service` 后：

```bash
systemctl --user daemon-reload
systemctl --user start arb-clob-research
systemctl --user status arb-clob-research
journalctl --user -u arb-clob-research --since today
systemctl --user stop arb-clob-research
```

启用开机启动或 user lingering 属于部署者选择；本票只启动独立 NAS 研究服务。
也可用 `systemd-run --user --unit=arb-clob-research` 加相同 properties 启动 transient 服务，
其生命周期独立于 agent run，但不承诺跨 NAS 重启恢复。
用 unit 名与 `MainPID` 记录清理句柄，不按进程名批量终止。
不要用每分钟 cron 重启录制器；cron 适合已关闭数据的日报生成。
`--duration` 是每次进程启动的单调时钟秒数，重启会重新计时。
长期启用时由部署者设总运行期限，或手动停止；单段 ≥24h 不能由多个短 session 相加代替。

## 轮转、配额与存储

```text
ROOT/
  date=YYYY-MM-DD/market=CONDITION_ID/events.SESSION.000000.jsonl
  date=YYYY-MM-DD/market=CONDITION_ID/snapshots.SESSION.000000.jsonl
  date=YYYY-MM-DD/market=_control/events.SESSION.000000.jsonl
  date=YYYY-MM-DD/market=_control/gaps.SESSION.000000.jsonl
  metrics.json
  .record.lock
```

接收 UTC 日期分区自动跨日；每个 stream 文件到 `--segment-mib` 上限前关闭并 fsync，
新文件递增编号，WS 保持连接。同一 session 的 seq 跨文件、日期、市场连续。
旧 ARB-8 未分段文件仍可读取。单行大于分段限额会失败，不能分割或截断事件。
最多打开 64 个输出文件；读报告时每个 events 文件占一个句柄，按数据规模设置
`LimitNOFILE` / `ulimit -n`，不建议极小分段录多日。

`--quota-gib` 统计根目录内 date/market 下全部 JSONL（events/snapshots/gaps），
包含此前 session 的数据；每次写前检查，超限停止，退出码 73。
`--reserve-gib` 每个健康周期（默认 30 秒）检查文件系统剩余空间；不足也退出 73。
不自动删除、覆盖或压缩证据。瞬时大流量或其他进程写盘仍可能先触发 OSError；
supervisor 对普通错误退避重启并限制次数，对 73 不自动重启。
配额不包含小型 metrics、锁文件和 journald，实际应给文件系统更多余量。
archive 后停止服务，归档完整、已关闭 session 的所有市场与 control 分区，校验并保存 manifest，
再由部署者决定保留策略。不单独删一个市场，否则 replay 的全局 seq 会出现缺口。
NAS secret、原始大数据、环境和运行日志保留在 `.state/` 或专用数据盘，不进 Git。

先用短录制估算 **全部 JSONL 字节 / 秒 × 604800**，加至少一倍余量。
20 GiB 是默认停止预算，不保证足够任何市场集合的七天。若有配额报警，归档或调整已批准
研究盘预算后再启动；不要通过删除异常日志使报告变成零缺口。

## 健康与告警

监管器在连接正常、等待消息、WS 重连退避时都会定期 checkpoint。
`metrics.json` 原子更新，累计计数仅属于当前 session；健康控制行同时存入校验日志，
因此跨重启的历史心跳/重连计数可离线审计。行情安静时也有健康记录，不能伪造市场事件。

建议监控：

- unit failed 或退出 73：立即告警；磁盘剩余接近 reserve、journal 接近 quota 时提前告警。
- metrics 超过 90 秒未更新：进程或 I/O 可能停滞。
- `available_books` 小于订阅 token 数且持续 >60 秒，或 PONG 长期不增长：连接恢复失败。
- 五分钟内重连 >5 次、`server_hash_conflict` / `journal_sequence` 增长：检查并保留证据。
- 每日 `unobserved_seconds`、`unavailable_asset_seconds` 与未恢复 hash 资产。

单个无成交市场不能用成交数量判断断线。`blocked=true` 不是公共录制失败，也不是交易许可。
日志可能包含大量 gap，使用 journald 的磁盘限额；报告从 events 重算，不依赖保留全部 journal 日志。

## 七天 gap report 模板与脚本

在停止服务后的数据上运行，避免扫描仍在写入的尾行；在线健康看 metrics。
日报也可针对已关闭完整 session 的一致性副本运行，不能遗漏仍在原目录的 session 分区。

```bash
cd research
uv run --locked python -m arb_research.orderbook.ops report .state/clob \
  --start 2026-10-30 --days 7 --output .state/clob-seven-days.json
```

输出 JSON v1 本身就是七天模板：固定七个 UTC 日期，没有观察的日期为 `status: missing`，
`unobserved_seconds: 86400`。`--days` 可设 1..366。每个日期包含：

| 字段 | 单位 / 含义 |
| --- | --- |
| `observed_seconds` | session 起止窗口在当天的并集；包含断线恢复等待 |
| `unobserved_seconds` | 86400 减上项，含首尾未覆盖与停机；不能当成上游丢包时长 |
| `unavailable_asset_seconds` | 已选资产不可重建的时间之和，跨日拆分；含初次播种、停机等待 |
| `events`, `sessions` | 本地日志行数、session ID 列表 |
| `gaps` | Rebuilder 重算的各类异常数；单次故障可产生多类异常 |
| `connections`, `reconnects` | connected / disconnected 控制事件数，首次连接不算重连 |
| `recoveries` | 完整 book 使不可用资产恢复的次数，含初次播种 |
| `hash_conflict_recoveries` | 发生 hash 内容冲突后，由完整 book 恢复的资产次数 |
| `max_recovery_seconds` | 在当天恢复的最长等待；等待起点可位于前一日 |

`summary` 扫描给定根目录的**全部**日志；daily 行仅覆盖指定日期范围。summary 含每个 session
的起止/是否正常停止/市场资产集合、最长单段秒数、未恢复 hash 资产、最近健康行的 PING/PONG 等计数。
突然崩溃的尾段只计到最后一条持久事件，不外推到生成报告时刻。恢复耗时也不外推尾部未知时间。
选择集合应在一段观察内固定；换市场请用新根目录，以免旧集合不可用时间与新集合混合。

脚本逐条校验本地事件 SHA-256、版本、排序，并重算本地序号缺口、盘口一致性和恢复。
SHA 损坏/截断让脚本非零失败；`journal_sequence` 以 gap 明示，仍保留其他统计。
gap 明细逐条处理后释放，报告内存不随异常数增长。
扫描需全部分区，耗时与历史规模相关；不要在 CI 中调用实时公开端点或执行多日录制。

验收应同时提供：固定市场集合、UTC 时间、单 session ≥86400 秒、book/PONG 证据、
gap/重连/恢复指标、本地校验结果，以及异常限制。连续录制不等于行情无缺口。
协议不承诺完整上游 seq，server hash 仅做同 hash 不同内容的一致性检测，
本地 SHA-256 不能证明服务器未丢包。整文件删除和没有后继 seq 的尾记录丢失也不保证可检出。

协议依据：[Polymarket Real-Time Data](https://docs.polymarket.com/market-data/realtime-data)，
2026-10-30 核对。沿用 ARB-8 raw market 通道，保留 V2 后事件原文与本地接收顺序，
统一 SDK `polymarket-client` 用于仓库常规 API；这里无需引入其他 SDK。

## 与 ARB-7 Parquet 对齐

JSONL 是原始事实来源，不将盘口事件当成 ARB-7 价格历史行直接追加。
离线转换应映射 `market`→condition ID、`asset_id`→outcome token ID，保留
`received_ns`（观察可用时间）、上游 timestamp、session/seq、事件类型及原 SHA-256。
按 UTC/date 和市场分区，归档 manifest 包含文件校验和、schema version 和采集边界。
增量 L2、完整 book、gap/可用性应使用独立的版本化 Parquet schema；缺口不 forward-fill，
只在新完整 book 后恢复可用。对齐现有 ARB-7 元数据/价格表时使用 condition/outcome 标识，
不使用问题标题作 join key。这里交付转换契约，不声称已实现盘口 Parquet ETL。
