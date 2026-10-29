# 合规开关与地区检测

当前产品只支持只读研究与纸面交易。`arb_research.compliance` 是所有未来外部交易入口共用的拒绝层；本期没有任何放行路径。

## 红线与开关

- 禁止实现或调用真实下单、订单签名、私钥、钱包充值、提现或撤单路径；只读与离线回测不受此 guard 限制。
- 禁止通过 VPN、代理或其他方式绕过地区限制。检测使用服务器实际出口，不代表访问者所在地，也不构成交易许可或法律意见。
- 密钥、token、交易配置只放 NAS secret，不进 git、前端或日志。审计的 `caller`、`action` 必须是静态代码标签，禁止传入订单、请求体或用户输入。
- `TRADING_ENABLED` 默认 `false`。只有大小写不敏感的 `true` 表示启用意图，其他值均视为关闭。即使设成 `true` 且地区未受阻，仍以 `allowlist_not_implemented` 拒绝。本期没有白名单配置、管理员旁路或 allow 参数；未来放行必须同时经过明确白名单及独立审查，不能只修改开关。
- 配置在 guard 初始化时读取，更新后重启进程。模块不会自行加载 `.env`，部署环境负责注入配置。

## 地区检测

依据 [Polymarket 官方 geoblock 文档](https://docs.polymarket.com/api-reference/geoblock)，仅调用 `GET https://polymarket.com/api/geoblock`，解析严格布尔型 `blocked`、双字母 `country` 和字符串 `region`。不保存或返回上游 IP，不维护易过时的国家白名单。

默认连接/读取 socket 超时 2 秒，响应最多 16 KiB，不跟随重定向，忽略环境代理。429、5xx 和传输失败最多尝试 3 次，间隔 0.25 / 0.5 秒；其他 HTTP 错误或结构异常直接降级为 `blocked=true`。错误只返回固定代码，不暴露传输异常原文。socket 超时不等于整个 DNS/响应过程的硬性总时限。

成功与失败均缓存 60 秒；过期后重新检测，刷新失败不能继续使用旧的未受阻结果。同一 client 串行刷新，避免并发重复请求。缓存是进程内的，不持久化。`checked_at` 为上游检测时间，`cached` 表示本次是否命中缓存。

所有检测尝试共用 `PUBLIC_API_BUDGET` 令牌桶：容量 900，补充速率 810 次/秒，保守满足通用 9,000 req/10s 预算（瞬时突发 900 + 10 秒补充 8,100）。未来公开 API 客户端应在每次外部请求前调用 `PUBLIC_API_BUDGET.acquire()`；多进程/多实例各有预算，部署者必须限制实例总量或引入跨实例限流，不能把它误认为分布式额度保证。本模块不引入任何交易 SDK。

## 一行接入

策略入口支持同步或异步函数，装饰器在调用函数体前抛出 `ComplianceDenied`：

```python
from arb_research.compliance import guarded

@guarded("order", caller="strategy.example")
def unavailable_order_entry():
    raise NotImplementedError("本期无真实交易实现")
```

也可在已有策略中直接使用 `get_guard().require("sign", caller="strategy.example")`。未知动作同样拒绝，不存在通过动作名绕过的行为。

FastAPI 用同步依赖（网络与审计运行在线程池内）：

```python
from fastapi import Depends
from arb_research.compliance import compliance_dependency

@app.post("/example-unavailable", dependencies=[Depends(
    compliance_dependency("cancel", caller="api.example")
)])
def unavailable_cancel_entry():
    raise NotImplementedError("本期无真实撤单实现")
```

依赖返回 HTTP 403，`detail.code=compliance_denied`。动作建议使用 `order`、`sign`、`deposit`、`cancel`、`withdraw`。装饰器和依赖是接入边界，不是 Python 沙箱；未来新增入口必须逐一接入，不可直接调用外部写接口。当前仓库没有这些真实入口，上述示例也不提供它们。

`GET /compliance/status` 返回 `mode=read_only`、开关、固定为 false 的 `trading_allowed` / `allowlist_configured`，以及地区检测结果。不触发交易、不更改配置。首次读取或缓存过期会执行只读检测；仅调用状态接口不会创建审计记录。应用可用 `create_app(compliance=guard)` 注入 guard；独立策略默认使用进程单例 `get_guard()`。

ARB-10 的 [negRisk 只读扫描器](negrisk-scanner.md) 复用上述 guard，但仅在自己的实例上注入公共读客户端作为 geoblock fetch。按用户明确授权，该客户端显式读取 `ARB_POLYMARKET_PROXY`；不设全局代理、不记录配置值，不修改这里的默认直连实现或任何拒绝规则。代理仅用于已限定的公共读接口，受阻结果照常记录，`trading_allowed` 仍为 false。

## 审计与运维

每次 `require`（包括缓存命中）向 SQLite `compliance_audit` 表提交一条记录：UTC 时间、调用方、动作、拒绝原因、开关、地区结果与缓存状态。默认文件 `.state/compliance.sqlite3`，可通过 NAS 环境变量 `ARB_COMPLIANCE_AUDIT_DB` 指定；该目录已被 git 忽略。SQLite 提交后再抛拒绝异常，支持并发调用与进程重启后的追加。

拒绝原因包括 `trading_disabled`、`geoblocked`、`geoblock_unavailable`、`allowlist_not_implemented`。审计无法落盘时仍以 `audit_unavailable` 拒绝，异常的 `decision` 保留原判定详情；此时无法保证本地审计持久化，运维应修复磁盘/权限后再使用。日志包含地区与调用方信息，应限制文件访问并按部署要求备份和保留；本期没有日志轮转或审计查询接口。

## 验证

```bash
cd research
uv sync --frozen
uv run pytest tests/test_compliance.py -q
uv run pytest -q
```

测试使用 `tests/fixtures/geoblock.json` 和注入的传输函数，覆盖默认拒绝、开关不能单独放行、US/VA 受阻、超时/HTTP/结构错误、缓存过期、退避限流、并发审计、同步/异步策略与 FastAPI 403。所有这些测试不访问外网；fixture 中的地区是测试数据，不表示当前部署的实测出口。
