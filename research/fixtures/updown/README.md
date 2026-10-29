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
