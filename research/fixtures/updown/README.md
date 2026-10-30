# Recorded BTC five-minute study

`btc-5m-20261029.json.gz` is an actual public recording, not synthetic data.
It contains 60 consecutive resolved Polymarket markets starting at Unix
1793272800, their raw Gamma event metadata and Data API v2 Up price points,
and 19,000 normalized finalized Binance BTCUSDT one-second closes.

The CEX response page parameters and canonical JSON SHA256 values are retained.
Point timestamps are candle close-time + 1ms, converted to seconds; prices are
the raw decimal close strings. These are held until the next recorded close.
No account, order, signer, proxy value or authentication data is recorded.

See `docs/crypto-updown-pricing.md` for provenance, model, time conventions,
limitations and offline reproduction commands. The generated report records
the compressed input's SHA256. Do not silently replace this historical cohort.

## Recorded L2 fill-cost studies (ARB-21)

`btc-books-20261030.json.gz` contains 180 seconds of public BTC Up/Down
WebSocket L2 from 02:00:47 UTC (812 checksum-validated ARB-8 journal rows).
It preserves all observed gaps; do not treat recovery snapshots as continuous.

`btc-rest-books-20261030.json.gz` contains 90 seconds of subsequent independent
public `/book` polls from 02:05:23 UTC (258 rows, with request/receipt times).
Its explicit `rest_snapshot` source distinguishes sampled full refreshes from
WebSocket continuity. Holding a snapshot between polls is an optimistic replay
assumption, not evidence of unchanged liquidity.

Both preserve current/next BTC market rules, fees, outcome identifiers and
retrospectively fetched finalized CEX one-second closes with page digests.
Only past closes with an assumed extra second of visibility lag enter each
signal; actual CEX receipt times were not recorded. Public scoped-proxy routing
was used without storing its value. Trading remains denied by main's guard.
See `docs/crypto-updown-fillable-edge.md` for reproduction and limitations.
