# Price Breach Monitor (Flow)

Create and activate a live watch for a symbol's price breaching a stop-loss
or target level, with a webhook notification on breach — without building a
Flow workflow JSON or a raw WebSocket client yourself. This wraps Flow's
`priceAlert` trigger (a real background service polling the symbol's LTP
every second — not a one-shot check) chained to an `httpRequest` action node
that POSTs to your webhook the moment price goes outside
`[stop_loss, target1]`.

Unlike the Flow editor's own `/flow/api/workflows/*` routes (session-cookie
authenticated), this surface uses the ordinary `apikey`-in-body auth every
other `/api/v1/` endpoint uses.

## Endpoint URLs

```http
POST http://127.0.0.1:5000/api/v1/pricebreach/create
POST http://127.0.0.1:5000/api/v1/pricebreach/<workflow_id>/deactivate
```

## Create

### Sample Request

```json
{
  "apikey": "<your_app_apikey>",
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "entry_price": 1180,
  "stop_loss": 1160,
  "target1": 1220,
  "webhook_url": "https://myapp.example.com/breach-webhook"
}
```

### Sample cURL

```bash
curl -X POST http://127.0.0.1:5000/api/v1/pricebreach/create \
  -H 'Content-Type: application/json' \
  -d '{
  "apikey": "<your_app_apikey>",
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "entry_price": 1180,
  "stop_loss": 1160,
  "target1": 1220,
  "webhook_url": "https://myapp.example.com/breach-webhook"
}'
```

### Sample Response

```json
{
  "status": "success",
  "workflow_id": 42,
  "message": "Watching RELIANCE@NSE for price outside [1160.0, 1220.0]"
}
```

### Webhook Payload (sent to `webhook_url` on breach)

```json
{
  "workflow_id": 42,
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "entry_price": 1180,
  "stop_loss": 1160,
  "target1": 1220,
  "breach_price": 1159.4,
  "triggered_at": "2026-09-12T09:15:42.123456"
}
```

## Deactivate

The watch is `trigger: "once"` — it stops polling the instant it fires, but
the workflow still shows **active** until you deactivate it. Call this from
your webhook receiver right after handling the notification above:

```bash
curl -X POST http://127.0.0.1:5000/api/v1/pricebreach/42/deactivate \
  -H 'Content-Type: application/json' \
  -d '{"apikey": "<your_app_apikey>"}'
```

```json
{ "status": "success", "message": "Workflow 42 deactivated" }
```

Deactivating requires the same `apikey` the watch was created with — a
different account's key gets `403`.

## Request Fields

| Field | Type | Endpoint | Mandatory | Description |
|-------|------|----------|-----------|-------------|
| apikey | string | both | Mandatory | Your OpenAlgo API key |
| symbol | string | create | Mandatory | Trading symbol |
| exchange | string | create | Mandatory | Exchange code |
| entry_price | number | create | Optional | Informational only — not itself a trigger condition |
| stop_loss | number | create | Mandatory | Lower breach bound; must be less than `target1` |
| target1 | number | create | Mandatory | Upper breach bound |
| webhook_url | string | create | Mandatory | Where the breach notification is POSTed |
| name | string | create | Optional | Workflow name (default: `"<symbol> breach watch"`) |

## Notes

- Only one condition shape is exposed here: outside a `[stop_loss, target1]`
  channel. For anything more elaborate (multiple targets, trailing stops,
  time windows), build the workflow directly — see
  [scripts/price_breach_monitor.py](../../../scripts/price_breach_monitor.py)
  for the equivalent session-based flow using the Flow editor's own routes,
  and `docs/prompt/flow-import-format.md` for the full node reference.
- Flow workflows have no per-account ownership (same as the underlying
  `/flow/api/workflows/*` routes) — anyone with a valid `apikey` on this
  instance can list/inspect any workflow via those routes. Deactivate here is
  the one place this surface adds a check: the caller's `apikey` must match
  the one the workflow was created with.
- Rate-limited via `WEBSOCKET_CONTROL_LIMIT` (default `10 per minute`),
  shared with the `/api/v1/ws/*` control endpoints.

## Related Endpoints

- [Quote (WebSocket)](../websocket-streaming/quote.md)
- [WebSocket subscribe/unsubscribe](./websocket-control.md)

---

**Back to**: [API Documentation](../README.md)
