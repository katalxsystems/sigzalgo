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
POST http://127.0.0.1:5000/api/v1/pricebreach/<call_id>/deactivate
POST http://127.0.0.1:5000/api/v1/pricebreach/workflow/<workflow_id>/deactivate
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

Each workflow notifies `webhook_url` when it fires, then deactivates —
automatically, via a Flow `httpRequest` node calling back into this same
instance — but **at a different scope depending on which one fired**, since
they mean different things for the call:

- **`sl_target` firing is a real stop-loss/target breach — the call is
  over.** Its cleanup node calls `/api/v1/pricebreach/<call_id>/deactivate`,
  which tears down **both** sibling workflows together.
- **`entry_recross` firing just means price came back to entry — the call is
  not over** (price can still go on to hit `stop_loss` or `target1`
  afterwards). Its cleanup node calls
  `/api/v1/pricebreach/workflow/<workflow_id>/deactivate` with its own
  `workflow_id`, deactivating **only itself**; `sl_target` stays live.

Your webhook receiver does not need to call deactivate itself either way.

## Create

### Sample Request

```json
{
  "apikey": "<your_app_apikey>",
  "call_id": "CALL-001",
  "mentor_id": "MENTOR-007",
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
  "mentor_id": "MENTOR-007",
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
  "mentor_id": "MENTOR-007",
  "workflows": {
    "sl_target": { "workflow_id": 1, "watching": "price outside [1160.0, 1220.0]" },
    "entry_recross": { "workflow_id": 2, "watching": "price crosses_below 1180.0" }
  },
  "message": "Each watch fires once and notifies your webhook. sl_target firing ends the call and deactivates both watches; entry_recross firing deactivates only itself, since price can still go on to hit stop_loss or target1 afterwards -- no separate deactivate call needed either way."
}
```

Both workflows created above are named from `call_id`, `mentor_id`, and
`symbol` (the "script"), e.g. `CALL-001_MENTOR-007_RELIANCE (SL/Target)` —
unless you pass an explicit `name` to override the base.

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
  "mentor_id": "MENTOR-007",
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
  "mentor_id": "MENTOR-007",
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
  "mentor_id": "MENTOR-007",
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
active; once it has fired, only itself is deactivated — `sl_target` stays
live so a real stop-loss/target breach afterwards still fires (see
"What gets created" above).

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
  "mentor_id": "MENTOR-007",
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
manual/operator use, e.g. canceling a call before it fires. Two scopes,
matching the two the auto-cleanup nodes use:

### By call_id (both workflows)

Pass the `call_id` from `create` in the path — not either `workflow_id` from
its response; deactivating a call_id tears down both its `sl_target` and
`entry_recross` workflows together:

```bash
curl -X POST http://127.0.0.1:5000/api/v1/pricebreach/CALL-001/deactivate \
  -H 'Content-Type: application/json' \
  -d '{"apikey": "<your_app_apikey>"}'
```

```json
{ "status": "success", "message": "Call CALL-001 deactivated", "workflow_ids": [1, 2] }
```

Requires the same `apikey` the call was created with — a different
account's key gets `403`. An unknown `call_id` gets `404`.

### By workflow_id (one workflow only)

Pass a single `workflow_id` from `create`'s response — deactivates that
workflow alone, leaving any sibling workflow for the same `call_id`
untouched:

```bash
curl -X POST http://127.0.0.1:5000/api/v1/pricebreach/workflow/2/deactivate \
  -H 'Content-Type: application/json' \
  -d '{"apikey": "<your_app_apikey>"}'
```

```json
{ "status": "success", "message": "Workflow 2 deactivated" }
```

Requires the `apikey` that workflow was created with — a different account's
key gets `403`. An unknown `workflow_id` gets `404`.

## Request Fields

| Field | Type | Endpoint | Mandatory | Description |
|-------|------|----------|-----------|-------------|
| apikey | string | all | Mandatory | Your OpenAlgo API key |
| call_id | string | create | Mandatory | Your own identifier for this call/trade — echoed back in every webhook payload and used in the default workflow name |
| mentor_id | string | create | Mandatory | Identifier for the mentor/analyst behind this call — echoed back in every webhook payload and used in the default workflow name |
| symbol | string | create | Mandatory | Trading symbol ("script") |
| exchange | string | create | Mandatory | Exchange code |
| active_price | number | create | Mandatory | The live price at the moment of this call — used only to pick the entry re-cross direction, not stored as a threshold itself |
| entry_price | number | create | Mandatory | The level `entry_recross` watches for a re-cross through |
| stop_loss | number | create | Mandatory | Lower `sl_target` bound; must be less than `target1` |
| target1 | number | create | Mandatory | Upper `sl_target` bound |
| webhook_url | string | create | Mandatory | Where breach notifications are POSTed |
| name | string | create | Optional | Base workflow name (default: `"<call_id>_<mentor_id>_<symbol>"`); each of the two workflows suffixes `(SL/Target)` / `(Entry re-cross)` |

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
  instance can list/inspect any workflow via those routes. Both deactivate
  endpoints are where this surface adds a check: the caller's `apikey` must
  match the one the workflow(s) were created with — for the `call_id` route,
  every workflow found for that `call_id`; if any of them doesn't match,
  nothing is deactivated. The `workflow_id` route checks only that one
  workflow.
- Each workflow's auto-cleanup `httpRequest` node calls back into this same
  instance (`MCP_LOOPBACK_URL` > `HOST_SERVER` > `http://127.0.0.1:<FLASK_PORT>`,
  same resolution order `blueprints/mcp_http.py` uses) — `sl_target` via the
  `call_id` deactivate route (tears down both workflows), `entry_recross` via
  its own `workflow_id` deactivate route (tears down only itself). If a
  self-call fails (network hiccup, instance restarting mid-run), call
  deactivate manually — by `call_id` or by the specific `workflow_id` — if you
  notice a workflow still active after it should have fired.
- Rate-limited via `WEBSOCKET_CONTROL_LIMIT` (default `10 per minute`),
  shared with the `/api/v1/ws/*` control endpoints.

## Related Endpoints

- [Quote (WebSocket)](../websocket-streaming/quote.md)
- [WebSocket subscribe/unsubscribe](./websocket-control.md)

---

**Back to**: [API Documentation](../README.md)
