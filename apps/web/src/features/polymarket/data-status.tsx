import {
  latestPrice,
  marketFor,
  qualityMessages,
  selectedAsset,
  type Dashboard,
} from './contract'

export function DataStatus({ data, referenceTime = data.as_of }: { data: Dashboard; referenceTime?: string }) {
  const market = marketFor(data)
  const asset = selectedAsset(market, data.outcome_index)
  const price = latestPrice(data)
  const resolution = data.resolutions.data[0]
  const prices = data.prices.data.filter((p) => p.token_id === asset?.id)
    .sort((a, b) => a.timestamp - b.timestamp)
  return (
    <section
      aria-label="市场与数据质量"
      className="mt-5 grid gap-3 rounded-lg border bg-card p-4 text-xs leading-6"
    >
      <p>
        {data.event.title ?? '事件标题缺失'} ·{' '}
        {market.question ?? '市场标题缺失'}
      </p>
      <p className="break-all font-mono text-[10px] text-muted-foreground">
        event_id: {market.event_id} · market_id: {market.id} · condition_id:{' '}
        {market.condition_id}
      </p>
      <p>
        Protocol {market.protocol} · Data API v2 · pUSD ≈ USD ·
        neg_risk: {market.neg_risk === null ? '未知' : String(market.neg_risk)}
      </p>
      <p className="break-all">
        {market.protocol === 'v2' ? 'position_id' : 'token_id'}:{' '}
        {asset?.id ?? '—（资产已隔离）'} · outcome: {asset?.outcome ?? '—'} ·
        价格：{price ? price.price.toFixed(4) : '—'} pUSD / 份
      </p>
      {price && (
        <p>
          价格时间：{new Date(price.timestamp * 1000).toISOString()} ·
          resolution_seconds: {price.resolution_seconds}
        </p>
      )}
      <p>
        closed: {market.closed === null ? '未知' : String(market.closed)} ·
        结算状态：{resolution?.status ?? '未知'} · payouts（pUSD / 份）：
        {resolution?.payouts?.join(' / ') ?? '—'} · resolved_at:{' '}
        {resolution?.resolved_at ?? '—'}
      </p>
      <details>
        <summary className="cursor-pointer">价格历史 · {prices.length} 条（展示末 30 条）</summary>
        {prices.length ? (
          <div className="overflow-x-auto">
            <table className="w-full text-left font-mono text-[10px]">
              <caption className="text-left">历史价格桶 · 非可执行报价</caption>
              <thead><tr><th>UTC</th><th>pUSD / 份</th><th>精度 / 秒</th></tr></thead>
              <tbody>{prices.slice(-30).map((p) => (
                <tr key={`${p.timestamp}-${p.resolution_seconds}`}>
                  <td>{new Date(p.timestamp * 1000).toISOString()}</td>
                  <td>{p.price.toFixed(4)}</td><td>{p.resolution_seconds}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : <p>暂无价格历史。</p>}
      </details>
      <ul
        aria-label="数据质量提示"
        className="list-inside list-disc text-muted-foreground"
      >
        {qualityMessages(data, referenceTime).map((message) => (
          <li key={message}>{message}</li>
        ))}
      </ul>
      <p className="text-muted-foreground">
        as_of: {data.as_of} · 采集时间：{data.quality.collected_at ?? '未知'} ·
        新鲜度参考时间：{referenceTime}。地区状态来自包内记录，未重新检查。交易权限始终由后端合规 guard 决定。
      </p>
    </section>
  )
}
