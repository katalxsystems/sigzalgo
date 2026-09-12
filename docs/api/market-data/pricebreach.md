# Price Breach Monitor (Flow)

Create and activate a live watch for a symbol's price breaching a stop-loss,
target, or the entry price itself — with a webhook notification on breach —
without building a Flow workflow JSON or a raw WebSocket client yourself.
This wraps Flow's `priceAlert` trigger (a real background service polling the
symbol's LTP every second — not a one-shot check) chained to `httpRequest`
action nodes that POST to your webhook and clean up after themselves the
moment either condition fires.

Unlike the Flow editor's own `/flow/api/workflows/*` routes (session-cookie
authenticated), this surface uses the ordinary `apikey`-in-body auth every
other `/api/v1/` endpoint uses.

## Endpoint URLs

```http
POST http://127.0.0.1:5000/api/v1/pricebreach/create
POST http://127.0.0.1:5000/api/v1/pricebreach/<workflow_id>/deactivate
```

## What gets created

One call creates up to **two** independent Flow workflows:

1. **`sl_target`** — always created. Fires once price goes outside
   `[stop_loss, target1]`.
2. **`entry_recross`** — created unless `active_price` equals `entry_price`.
   Fires once price crosses back through `entry_price`, in whichever
   direction is a genuine "re-cross" given where price was when you made the
   call:
   - `active_price > entry_price` → watches for price **crossing below**
     `entry_price` (it ran up from/past entry and came back down through it).
   - `active_price < entry_price` → watches for price **crossing above**
     `entry_price` (it was below entry and rose back through it).
   - `active_price == entry_price` → no direction to watch; only `sl_target`
     is created (`entry_recross` is `null` in the response), and an
     `entry_price` breach notification is sent to `webhook_url`
     **immediately**, in the background — see "Immediate notification"
     below.

Whichever workflow fires first: it notifies `webhook_url`, then
**deactivates both itself and the other workflow automatically** — a Flow
`httpRequest` node calls this same instance's own
`/api/v1/pricebreach/<id>/deactivate` for each. Your webhook receiver does
not need to call deactivate itself.

## Create

### Sample Request

```json
{
  "apikey": "<your_app_apikey>",
  "call_id": "CALL-001",
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "active_price": 1190,
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
  "call_id": "CALL-001",
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "active_price": 1190,
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
  "call_id": "CALL-001",
  "workflows": {
    "sl_target": { "workflow_id": 1, "watching": "price outside [1160.0, 1220.0]" },
    "entry_recross": { "workflow_id": 2, "watching": "price crosses_below 1180.0" }
  },
  "message": "Each watch fires once, notifies your webhook, then deactivates both itself and its sibling watch automatically -- no separate deactivate call needed."
}
```

If `active_price` had equaled `entry_price`, `entry_recross` would be `null`
and only `sl_target` would exist (and an immediate notification would already
be on its way — see below).

### Webhook Payload (sent to `webhook_url` on breach)

Every payload carries `breach_type` — `"stop_loss"`, `"target1"`, or
`"entry_price"` — so you never need to compare prices yourself to know what
was breached. `alert_type` tells you which *workflow* fired
(`sl_target_breach` or `entry_recross`); `breach_type` tells you which
*bound*.

```json
{
  "call_id": "CALL-001",
  "workflow_id": 1,
  "alert_type": "sl_target_breach",
  "breach_type": "stop_loss",
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "entry_price": 1180,
  "stop_loss": 1160,
  "target1": 1220,
  "breach_price": 1159.4,
  "triggered_at": "2026-09-12T09:15:42.123456"
}
```

```json
{
  "call_id": "CALL-001",
  "workflow_id": 1,
  "alert_type": "sl_target_breach",
  "breach_type": "target1",
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "entry_price": 1180,
  "stop_loss": 1160,
  "target1": 1220,
  "breach_price": 1220.6,
  "triggered_at": "2026-09-12T09:18:02.221100"
}
```

```json
{
  "call_id": "CALL-001",
  "workflow_id": 2,
  "alert_type": "entry_recross",
  "breach_type": "entry_price",
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "entry_price": 1180,
  "stop_loss": 1160,
  "target1": 1220,
  "breach_price": 1179.8,
  "triggered_at": "2026-09-12T09:20:11.654321"
}
```

`entry_recross` fires **at most once** — the underlying `priceAlert` trigger
is registered as `"once"` and the live watch is removed the instant it
fires, same as `sl_target`. It cannot re-notify while the workflow stays
active; once it has fired, both it and its sibling are deactivated.

#### Immediate notification (`active_price == entry_price`)

When the create call's `active_price` already equals `entry_price`, there is
no cross direction to wait for — the condition is already true. Rather than
create a workflow that would never fire, `create` sends one `entry_recross` /
`breach_type: "entry_price"` notification to `webhook_url` right away, in the
background (it does not delay the HTTP response to `create`). Its
`workflow_id` is the `sl_target` workflow's id — the one real, live workflow
this call created — not `null`, so you can still correlate or manually
deactivate it if needed:

```json
{
  "call_id": "CALL-001",
  "workflow_id": 1,
  "alert_type": "entry_recross",
  "breach_type": "entry_price",
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "entry_price": 1180,
  "stop_loss": 1160,
  "target1": 1220,
  "breach_price": 1180,
  "triggered_at": "2026-09-12T09:12:00.000000"
}
```

## Deactivate

Not usually needed — the auto-cleanup above handles it. Exposed for
manual/operator use, e.g. canceling a watch before it fires:

```bash
curl -X POST http://127.0.0.1:5000/api/v1/pricebreach/1/deactivate \
  -H 'Content-Type: application/json' \
  -d '{"apikey": "<your_app_apikey>"}'
```

```json
{ "status": "success", "message": "Workflow 1 deactivated" }
```

Requires the same `apikey` the watch was created with — a different
account's key gets `403`.

## Request Fields

| Field | Type | Endpoint | Mandatory | Description |
|-------|------|----------|-----------|-------------|
| apikey | string | both | Mandatory | Your OpenAlgo API key |
| call_id | string | create | Mandatory | Your own identifier for this call/trade — echoed back in every webhook payload |
| symbol | string | create | Mandatory | Trading symbol |
| exchange | string | create | Mandatory | Exchange code |
| active_price | number | create | Mandatory | The live price at the moment of this call — used only to pick the entry re-cross direction, not stored as a threshold itself |
| entry_price | number | create | Mandatory | The level `entry_recross` watches for a re-cross through |
| stop_loss | number | create | Mandatory | Lower `sl_target` bound; must be less than `target1` |
| target1 | number | create | Mandatory | Upper `sl_target` bound |
| webhook_url | string | create | Mandatory | Where breach notifications are POSTed |
| name | string | create | Optional | Base workflow name (default: `"<symbol> breach watch"`); each of the two workflows suffixes `(SL/Target)` / `(Entry re-cross)` |

## Notes

- `sl_target`'s condition shape is fixed: outside a `[stop_loss, target1]`
  channel. For anything more elaborate (multiple targets, trailing stops,
  time windows), build the workflow directly — see
  [scripts/price_breach_monitor.py](../../../scripts/price_breach_monitor.py)
  for the equivalent session-based flow using the Flow editor's own routes
  (that script predates `entry_recross`/`call_id`/auto-cleanup and only
  builds the `sl_target` watch — its receiver example still calls deactivate
  manually), and `docs/prompt/flow-import-format.md` for the full node
  reference.
- Flow workflows have no per-account ownership (same as the underlying
  `/flow/api/workflows/*` routes) — anyone with a valid `apikey` on this
  instance can list/inspect any workflow via those routes. Deactivate here is
  the one place this surface adds a check: the caller's `apikey` must match
  the one the workflow was created with.
- The auto-cleanup `httpRequest` nodes call back into this same instance
  (`MCP_LOOPBACK_URL` > `HOST_SERVER` > `http://127.0.0.1:<FLASK_PORT>`, same
  resolution order `blueprints/mcp_http.py` uses). If that self-call fails
  (network hiccup, instance restarting mid-run), the workflow that fired is
  still deactivated via its own `deactivate_self` step in most failure modes,
  but the sibling can be left stranded — call deactivate on it manually if
  you notice a workflow still active with no counterpart.
- Rate-limited via `WEBSOCKET_CONTROL_LIMIT` (default `10 per minute`),
  shared with the `/api/v1/ws/*` control endpoints.

## Related Endpoints

- [Quote (WebSocket)](../websocket-streaming/quote.md)
- [WebSocket subscribe/unsubscribe](./websocket-control.md)

---

**Back to**: [API Documentation](../README.md)
