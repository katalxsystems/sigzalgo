# Price Alert Monitor (Flow)

Create and activate a live, single-condition price watch for a symbol —
with a webhook notification when it fires — without building a Flow
workflow JSON yourself. This wraps Flow's `priceAlert` trigger (a real
background service polling the symbol's LTP every second — not a one-shot
check) chained to a single `httpRequest` action node that POSTs to your
webhook.

Unlike the Flow editor's own `/flow/api/workflows/*` routes (session-cookie
authenticated), this surface uses the ordinary `apikey`-in-body auth every
other `/api/v1/` endpoint uses — same relationship
[Price Breach Monitor](./pricebreach.md) has to that session-based surface.

If you need a stop-loss/target pair plus an entry re-cross watch tied to a
call/mentor identifier, see [Price Breach Monitor](./pricebreach.md)
instead — this endpoint is the plain, one-condition case.

## Endpoint URLs

```http
POST http://127.0.0.1:5000/api/v1/pricealert/create
POST http://127.0.0.1:5000/api/v1/pricealert/<int:workflow_id>/deactivate
```

## What gets created

One call creates **one** Flow workflow: a `priceAlert` trigger wired to an
`httpRequest` node that POSTs `webhook_url` when `condition` is met.

- `trigger: "once"` (the default) — the workflow deactivates itself the
  instant it fires. This is Flow's own behavior for a one-shot trigger, not
  something this endpoint has to arrange.
- `trigger: "every_time"` — the workflow keeps watching and can fire
  repeatedly until its `expiration` (or a manual deactivate call).

## Create

### Sample Request

```json
{
  "apikey": "<your_app_apikey>",
  "symbol": "IRCTC",
  "exchange": "NSE",
  "condition": "crosses_above",
  "price": 458.70,
  "trigger": "once",
  "expiration": "1d",
  "webhook_url": "https://myapp.example.com/alert-webhook",
  "message": "IRCTC crossing 458.70"
}
```

### Sample cURL

```bash
curl -X POST http://127.0.0.1:5000/api/v1/pricealert/create \
  -H 'Content-Type: application/json' \
  -d '{
  "apikey": "<your_app_apikey>",
  "symbol": "IRCTC",
  "exchange": "NSE",
  "condition": "crosses_above",
  "price": 458.70,
  "trigger": "once",
  "expiration": "1d",
  "webhook_url": "https://myapp.example.com/alert-webhook",
  "message": "IRCTC crossing 458.70"
}'
```

### Sample Response

```json
{
  "status": "success",
  "workflow_id": 3,
  "name": "IRCTC crosses_above 458.7",
  "watching": "IRCTC@NSE crosses_above 458.7",
  "message": "Alert created and armed. It will POST to your webhook_url when the condition is met."
}
```

### Webhook Payload (sent to `webhook_url` on trigger)

```json
{
  "symbol": "IRCTC",
  "exchange": "NSE",
  "condition": "crosses_above",
  "price": 458.70,
  "message": "IRCTC crossing 458.70",
  "breach_price": 459.15,
  "triggered_at": "2026-09-12T09:18:02.221100"
}
```

## Deactivate

Not usually needed for a `once` alert — it deactivates itself when it
fires. Exposed for manual/operator use (e.g. canceling an alert before it
fires, or stopping an `every_time` alert early).

```bash
curl -X POST http://127.0.0.1:5000/api/v1/pricealert/3/deactivate \
  -H 'Content-Type: application/json' \
  -d '{"apikey": "<your_app_apikey>"}'
```

```json
{ "status": "success", "message": "Workflow 3 deactivated" }
```

Requires the same `apikey` the alert was created with — a different
account's key gets `403`. An unknown `workflow_id` gets `404`.

## Request Fields

| Field | Type | Endpoint | Mandatory | Description |
|-------|------|----------|-----------|-------------|
| apikey | string | all | Mandatory | Your OpenAlgo API key |
| symbol | string | create | Mandatory | Trading symbol ("script") |
| exchange | string | create | Mandatory | Exchange code |
| condition | string | create | Mandatory | One of: `above`, `below`, `crosses_above`, `crosses_below` |
| price | number | create | Mandatory | The level `condition` is evaluated against |
| trigger | string | create | Optional | `once` (default) or `every_time` |
| expiration | string | create | Optional | `none` (default), `1h`, `4h`, `1d`, `1w` |
| webhook_url | string | create | Mandatory | Where the trigger notification is POSTed |
| message | string | create | Optional | Custom text echoed back in the webhook payload (default: auto-generated from symbol/condition/price) |
| name | string | create | Optional | Workflow name (default: `"<symbol> <condition> <price>"`) |

## Notes

- `condition` accepts the same alias vocabulary the Flow editor's own
  "Create Alert" UI does (`above`/`below`/`crosses_above`/`crosses_below`);
  see `FlowPriceMonitor.normalize_condition()` in
  `services/flow_price_monitor_service.py` for the full alias table if you
  need the canonical spelling instead (e.g. `greater_than`, `crossing_up`).
- For a channel (`priceLower`/`priceUpper`), percentage-move, or plain
  movement condition, or anything needing more than one action node, build
  the workflow directly against `/flow/api/workflows/import` (session-based)
  — see `docs/prompt/flow-import-format.md` for the full node reference.
- Flow workflows have no per-account ownership beyond what this surface
  adds: the `deactivate` endpoint checks the caller's `apikey` matches the
  one the workflow was created with (same check
  [Price Breach Monitor](./pricebreach.md) makes, reusing its
  `deactivate_one()` directly — it was already generic over any
  `priceAlert`-backed workflow, not pricebreach-specific).
- Rate-limited via `WEBSOCKET_CONTROL_LIMIT` (default `10 per minute`),
  shared with the `/api/v1/ws/*` and `/api/v1/pricebreach/*` control
  endpoints.

## Related Endpoints

- [Price Breach Monitor](./pricebreach.md)
- [Quote (WebSocket)](../websocket-streaming/quote.md)
- [WebSocket subscribe/unsubscribe](./websocket-control.md)

---

**Back to**: [API Documentation](../README.md)
