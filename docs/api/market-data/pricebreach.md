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

One call creates **three** Flow workflows:

1. **`sl_target`** — always created. Fires once price leaves the channel
   between `stop_loss` and `target1`. Works for buy and sell calls, told
   apart by the levels: `stop_loss` below `target1` is a **buy** (stop-loss
   hit when price falls below it), above `target1` a **sell** (stop-loss hit
   when price rises above it). `breach_type` names the level crossed either
   way.
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

3. **`candle_1m`** — always created. A backup check for the same
   stop-loss/target channel as `sl_target`, using the last **closed**
   1-minute candle instead of the LTP. It runs every minute during the
   exchange's market hours and tests the candle's **high and low**:
   - buy call: `low < stop_loss` is a stop-loss breach, `high > target1` a
     target breach;
   - sell call: `high > stop_loss` is a stop-loss breach, `low < target1` a
     target breach.

   This catches a level touched between two LTP samples, which `sl_target`
   can miss when the WebSocket feed is down and it is running on its REST
   poll (one LTP sample every `FLOW_PRICE_ALERT_POLL_INTERVAL` seconds,
   default 30). Details:
   - Only candles that start **after** the call was created are checked
     (`checks_candles_from` in the response). The candle the call was made
     in also contains prices from before the call, so that minute is covered
     by `sl_target` alone.
   - When one candle spans both levels, it is reported as `stop_loss`: a
     candle cannot say which level was touched first.
   - A failed or empty candle fetch never reports a breach.
   - A breach ends the call exactly like an `sl_target` breach: it notifies,
     then deactivates every workflow for the `call_id`.

Each workflow notifies `webhook_url` when it fires, then deactivates —
automatically, via a Flow `httpRequest` node calling back into this same
instance — but **at a different scope depending on which one fired**, since
they mean different things for the call:

- **`sl_target` or `candle_1m` firing is a real stop-loss/target breach —
  the call is over.** Its cleanup node calls
  `/api/v1/pricebreach/<call_id>/deactivate`, which tears down **all**
  workflows for the call together. Whichever of the two sees the breach
  first wins; the other is stopped by that teardown.
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
    "entry_recross": { "workflow_id": 2, "watching": "price crosses_below 1180.0" },
    "candle_1m": {
      "workflow_id": 3,
      "watching": "1-minute candle high/low outside [1160.0, 1220.0]",
      "checks_candles_from": "2026-09-12T09:13:00",
      "active": true
    }
  },
  "message": "Each watch fires once and notifies your webhook. sl_target firing ends the call and deactivates both watches; entry_recross firing deactivates only itself, since price can still go on to hit stop_loss or target1 afterwards -- no separate deactivate call needed either way. A 1-minute candle check also runs as a backup for stop_loss/target1; whichever check sees the breach first notifies and deactivates every watch for the call."
}
```

If the candle check cannot be scheduled, `create` still succeeds (the LTP
watches are live) and `candle_1m` comes back with `"active": false` and an
`error` message.

The workflows created above are named from `call_id`, `mentor_id`, and
`symbol` (the "script"), e.g. `CALL-001_MENTOR-007_RELIANCE (SL/Target)` and `... (1m Candle)` —
unless you pass an explicit `name` to override the base.

If `active_price` had equaled `entry_price`, `entry_recross` would be `null`
and only `sl_target` would exist (and an immediate notification would already
be on its way — see below).

### Webhook Payload (sent to `webhook_url` on breach)

Every payload carries `breach_type` — `"stop_loss"`, `"target1"`, or
`"entry_price"` — so you never need to compare prices yourself to know what
was breached. `alert_type` tells you which *workflow* fired
(`sl_target_breach` or `entry_recross`); `breach_type` tells you which
*bound*. `detected_by` tells you which check saw it: `"ltp"` (the
`sl_target` / `entry_recross` price watches) or `"candle_1m"` (the candle
backup).

A breach can occasionally be reported by both checks, when both see it
within the same moment before the teardown lands. De-duplicate on `call_id`
+ `breach_type`.

```json
{
  "call_id": "CALL-001",
  "mentor_id": "MENTOR-007",
  "workflow_id": 1,
  "alert_type": "sl_target_breach",
  "breach_type": "stop_loss",
  "detected_by": "ltp",
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

From the 1-minute candle check (`breach_price` is the candle extreme that
crossed the level; all candle values are strings):

```json
{
  "call_id": "CALL-001",
  "mentor_id": "MENTOR-007",
  "workflow_id": 3,
  "alert_type": "sl_target_breach",
  "breach_type": "stop_loss",
  "detected_by": "candle_1m",
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "entry_price": 1180,
  "stop_loss": 1160,
  "target1": 1220,
  "breach_price": "1158.9",
  "candle": {
    "interval": "1m",
    "timestamp": "1789188240",
    "open": "1163.2",
    "high": "1164.0",
    "low": "1158.9",
    "close": "1161.5"
  },
  "triggered_at": "2026-09-12T09:25:03.412118"
}
```

`candle.timestamp` is passed through as the broker returns it: epoch
seconds for most brokers, an ISO string for some.

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

### By call_id (all workflows)

Pass the `call_id` from `create` in the path — not a `workflow_id` from its
response; deactivating a call_id tears down its `sl_target`,
`entry_recross` and `candle_1m` workflows together:

```bash
curl -X POST http://127.0.0.1:5000/api/v1/pricebreach/CALL-001/deactivate \
  -H 'Content-Type: application/json' \
  -d '{"apikey": "<your_app_apikey>"}'
```

```json
{ "status": "success", "message": "Call CALL-001 deactivated", "workflow_ids": [1, 2, 3] }
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
| stop_loss | number | create | Mandatory | Stop-loss level. Below `target1` for a buy call, above it for a sell call; must not equal `target1` |
| target1 | number | create | Mandatory | Target level |
| webhook_url | string | create | Mandatory | Where breach notifications are POSTed |
| name | string | create | Optional | Base workflow name (default: `"<call_id>_<mentor_id>_<symbol>"`); the workflows suffix `(SL/Target)` / `(Entry re-cross)` / `(1m Candle)` |

## Notes

- `sl_target`'s condition shape is fixed: outside the channel between
  `stop_loss` and `target1`. For anything more elaborate (multiple targets, trailing stops,
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
- The candle check fetches 1-minute history through this account's broker
  session once a minute per call (requests for the same symbol share a short
  cache), so the broker must support the `1m` interval. Calls created before
  the candle check existed have none; re-create them to get one.
- Rate-limited via `WEBSOCKET_CONTROL_LIMIT` (default `10 per minute`),
  shared with the `/api/v1/ws/*` control endpoints.

## Related Endpoints

- [Quote (WebSocket)](../websocket-streaming/quote.md)
- [WebSocket subscribe/unsubscribe](./websocket-control.md)

---

**Back to**: [API Documentation](../README.md)
