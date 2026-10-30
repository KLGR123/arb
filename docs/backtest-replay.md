## 回放与标注（ARB-5）

打开 `/backtest`，从 API 目录选择 frozen Dataset 并创建真实 run。结果就绪后，Vela 使用该 run 的 `GET /backtests/:id/bars` 与阶段结果，停在第 32 根已收盘 bar（不足 32 根时显示全部）。可切换已经完成的 fast / draft、realistic / final 阶段，每次切换重置游标。两个阶段各自保留成交与净值，不将成交相加。主图显示当前 run id、Dataset id、引擎包版本和 `as_of`。没有运行、请求失败或结果为空时显示明确空态，不自动加载录制文件。连接方式见 [回测 UI](backtest-ui.md)。

使用 Vela 0.9.1 的原生 replay 与 native indicator 接口。价格、成交量、Equity 分 pane，共享同一 UTC 时间轴，平移、缩放及光标天然联动。下方读数在悬停时显示同一根 bar 的 OHLCV 与收盘净值，离开图表恢复回放游标。桌面和触屏均可通过“回放进度”定位。

开始、定位和逐根前进时，视窗跟随游标，最多显示最近 96 根；初始视窗覆盖至多 32 根数据。暂停后可以自由平移缩放；显示完整 run 时展开当前 run 的全部 bars。视窗的空白时间范围不会揭示隐藏数据。

- “下一根”揭示一根完整 K 线；“播放回放”支持 1×、4×、16×（每秒 1、4、16 根），可暂停、倒回或显示完整 run。
- 成交箭头取当前阶段 `fills`，仅显示已揭示 bar 的成交。蓝色均价线与淡蓝背景由成交重建 long-only 持仓；平仓后均价线中断。
- 红色止损 −2%、黄色止盈 +4% 以持仓均价为基准，是可开关的分析标注。ARB-4 的 SMA 策略没有保护订单，这些线未参与撮合，不表示触发、成交或引擎风险控制；开关不会改变收益。
- 净值按 `close_timestamp` 对齐 bar，而不是错误地将收盘值关联到下一根开盘。未揭示的净值、成交、持仓与参考线不会绘出。回放单位是完整已收盘 bar，不模拟 bar 内的逐笔路径。
- 仅注入固定离线 feed，`live: false`；无 provider、订阅或下单。离开路由销毁 Vela、回放计时器、指标注册及回调；再次进入恢复初始状态。

## 录制数据与复录

`apps/web/src/features/backtest/fixtures/recorded-run.json` 包含 `{version: 1, run, bars}`；`run` 和 `bars` 分别是 ARB-4 Store 的完整 `GET /backtests/:id`、`GET /backtests/:id/bars` 等价响应，保留原始请求、Dataset 摘要、来源、`as_of`、两阶段结果与计算时间。不是手编造的成交或收益。

此文件由已发布冻结的 `demo-synthetic-1h-v2` 经真实 VectorBT 0.28.5 和 NautilusTrader 1.231.0 执行后生成：336 根，两个阶段各 10 笔成交，初始资金 10000 USD。数据为合成快照，不代表市场表现。复录从仓库根目录执行：

```bash
cd research
uv sync --locked
uv run --locked python scripts/record_replay.py
```

脚本创建临时 SQLite 队列，顺序完成两个真实引擎阶段并检查成功后才写入前端 fixture。重新录制会改变 run id 和计算时间。无需 API 常驻、不读密钥、不联网加载行情。该录制只用于浏览器回归测试，不进入运行页面。页面轮询完整结构化结果，因此不需要处理事件分页、游标和去重。

## 验证

`npm run build`、`npm run typecheck`、`npm run smoke:web`。回放浏览器测试覆盖桌面与手机：默认阶段、起点、逐根、16×、暂停、入场/平仓边界、倒回不泄漏未来成交、全部净值与 fixture 对齐、完整 run、切换阶段不累计成交、参考线开关、终点停止、路由销毁重建与外部请求为零。另保留 ARB-3 配置与 preset 回归测试。
