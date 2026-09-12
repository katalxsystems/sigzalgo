# WebSocket Subscribe / Unsubscribe (REST)

Register or remove a real-time market data subscription (LTP, Quote, or Depth)
for the calling account without opening a raw WebSocket connection yourself.
This is a control-plane action, not a data feed: the actual ticks are still
delivered over the account's server-side broker feed, the same one the raw
`ws://<host>:8765` protocol and the `/websocket/test` page use. Any other
connection under this account (including the built-in test page) immediately
sees the effect of calling these endpoints, and a subscription made here
stays live until explicitly unsubscribed or the account disconnects.

Use this when an external application wants to warm up or tear down a
subscription via a simple HTTP call — e.g. before opening its own WebSocket
connection to receive the actual ticks, or to make sure a symbol is being
kept fresh in the server-side quote/LTP cache that other REST endpoints read
from.

## Endpoint URLs

```http
POST http://127.0.0.1:5000/api/v1/ws/subscribe
POST http://127.0.0.1:5000/api/v1/ws/unsubscribe
POST http://127.0.0.1:5000/api/v1/ws/unsubscribe-all
```

## Subscribe

### Sample Request

```json
{
  "apikey": "<your_app_apikey>",
  "mode": "Quote",
  "symbols": [
    {"exchange": "NSE", "symbol": "RELIANCE"},
    {"exchange": "NSE", "symbol": "INFY"}
  ]
}
```

### Sample cURL

```bash
curl -X POST http://127.0.0.1:5000/api/v1/ws/subscribe \
  -H 'Content-Type: application/json' \
  -d '{
  "apikey": "<your_app_apikey>",
  "mode": "Quote",
  "symbols": [
    {"exchange": "NSE", "symbol": "RELIANCE"}
  ]
}'
```

### Sample Response

```json
{
  "status": "success",
  "subscriptions": {
    "symbols": [{"exchange": "NSE", "symbol": "RELIANCE"}],
    "mode": "Quote",
    "count": 1
  },
  "broker": "angel"
}
```

## Unsubscribe

Same shape as subscribe, at `/api/v1/ws/unsubscribe`:

```json
{
  "apikey": "<your_app_apikey>",
  "mode": "Quote",
  "symbols": [
    {"exchange": "NSE", "symbol": "RELIANCE"}
  ]
}
```

## Unsubscribe All

Drops every symbol currently subscribed for this account, across all modes:

```json
{
  "apikey": "<your_app_apikey>"
}
```

## Request Fields

| Field | Type | Endpoint(s) | Mandatory | Description |
|-------|------|-------------|-----------|--------------|
| apikey | string | all | Mandatory | Your OpenAlgo API key |
| mode | string | subscribe, unsubscribe | Optional (default `Quote`) | One of `LTP`, `Quote`, `Depth` |
| symbols | array | subscribe, unsubscribe | Mandatory | 1–50 `{exchange, symbol}` objects |

## Notes

- This is control-only: it does not return tick data. To actually receive
  updates, connect to the raw WebSocket (see [Quote](../websocket-streaming/quote.md),
  [LTP](../websocket-streaming/ltp.md), [Depth](../websocket-streaming/depth.md)) —
  the raw protocol re-authenticates with the same `apikey`, so it reflects
  subscriptions made here — or read the cached value back via [Quote](./quotes.md) / a
  polling loop.
- Rate-limited via `WEBSOCKET_CONTROL_LIMIT` (default `10 per minute`) — each
  call fans out into a real broker-side subscribe/unsubscribe per symbol, so
  it is priced closer to an order-control action than a plain data read.
- Returns `403` for an invalid `apikey`, `404` if the account has no broker
  configured, `503` if the account's broker session isn't connected.

## Related Endpoints

- [Quote (WebSocket)](../websocket-streaming/quote.md)
- [LTP (WebSocket)](../websocket-streaming/ltp.md)
- [Depth (WebSocket)](../websocket-streaming/depth.md)
- [Quote (REST)](./quotes.md)

---

**Back to**: [API Documentation](../README.md)
