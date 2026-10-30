import {
  latestPrice,
  marketFor,
  qualityMessages,
  selectedAsset,
  type Dashboard,
} from './contract'

export function DataStatus({ data }: { data: Dashboard }) {
  const market = marketFor(data)
  const asset = selectedAsset(market, data.outcome_index)
  const price = latestPrice(data)
  const resolution = data.resolutions.data[0]
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
        Protocol {market.protocol} · Data API v2 · CLOB V2 · pUSD ≈ USD ·
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
      <ul
        aria-label="数据质量提示"
        className="list-inside list-disc text-muted-foreground"
      >
        {qualityMessages(data).map((message) => (
          <li key={message}>{message}</li>
        ))}
      </ul>
      <p className="text-muted-foreground">
        质量按快照 as_of 判断；离线演示未执行地区检查。交易权限始终由后端合规
        guard 决定。
      </p>
    </section>
  )
}
