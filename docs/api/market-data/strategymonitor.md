# Strategy Monitor (Flow)

Watch a multi-leg strategy against levels an external platform owns, and get a
webhook when one is breached - without building a Flow workflow yourself or
holding a browser session.

The caller owns the strategy on its own server and pushes everything the
monitor needs - the legs and the levels. This instance keeps that copy in a
Flow workflow and checks it on a schedule:

- **combined P&L of the supplied legs**, each open leg marked to a live quote,
  against `target` and `stoploss`
- the **underlying's spot price** against `upper_spot_stoploss` and
  `lower_spot_stoploss`

On a breach it POSTs one notification to `webhook_url`. **It places no
orders** - exiting the legs is the receiver's job.

Like [Price Breach Monitor](./pricebreach.md) and
[Price Alert Monitor](./pricealert.md), this uses `apikey`-in-body auth instead
of the session-cookie `/flow/api/workflows/*` routes.

## Endpoint URLs

```http
POST http://127.0.0.1:5000/api/v1/strategymonitor/upsert
POST http://127.0.0.1:5000/api/v1/strategymonitor/deactivate
```

## Lifecycle

- A monitor is identified by **the account behind `apikey` + `strategy`**. The
  same tag on another account is a separate monitor.
- `upsert` creates the monitor and arms it. Calling it again for the same
  strategy replaces the stored legs and levels in place (same `workflow_id`)
  and re-arms it - including after it has fired.
- **Re-upsert after every change to the legs** (an adjustment, a partial exit,
  a roll). The monitor values exactly the legs it was last sent; it does not
  see orders or fills on its own.
- After a breach notification is **delivered (HTTP 2xx)** the monitor
  deactivates itself, so each breach is reported once. A failed delivery
  (non-2xx, timeout) leaves it armed and the next run sends it again; use
  `idempotency_key` to ignore duplicates.
- The monitor only checks while at least one supplied leg has a non-zero
  quantity. Closing the strategy without a breach does **not** deactivate it -
  call `deactivate`.
- Checks run only during market hours of the underlying's exchange (NSE when no
  underlying is given).

## Upsert

### Sample Request

```json
{
  "apikey": "<your_app_apikey>",
  "strategy": "NIFTY_STRADDLE_27OCT",
  "target": 3000,
  "stoploss": 2000,
  "underlying": "NIFTY",
  "underlying_exchange": "NSE_INDEX",
  "upper_spot_stoploss": 23000,
  "lower_spot_stoploss": 22400,
  "webhook_url": "https://signalz.example/hooks/strategy-breach",
  "webhook_secret": "shared-secret",
  "interval_seconds": 60,
  "legs": [
    { "symbol": "NIFTY27OCT2622700CE", "exchange": "NFO", "product": "NRML",
      "quantity": 65, "entry_price": 50.0 },
    { "symbol": "NIFTY27OCT2622700PE", "exchange": "NFO", "product": "NRML",
      "quantity": 65, "entry_price": 40.0 }
  ]
}
```

### Sample Response (201 created, 200 updated)

```json
{
  "status": "success",
  "workflow_id": 42,
  "strategy": "NIFTY_STRADDLE_27OCT",
  "created": true,
  "active": true,
  "message": "Monitor created and armed"
}
```

### Parameters

| Parameter | Required | Description |
|---|---|---|
| `apikey` | Yes | OpenAlgo API key of the account the strategy trades on |
| `strategy` | Yes | Strategy tag the legs' orders carry. Letters, digits, `_ . : -`, max 100 |
| `target` | No* | Exit when combined P&L (rupees) reaches this, e.g. `3000` |
| `stoploss` | No* | Loss amount as a positive number: `2000` fires at a P&L of -2000 |
| `underlying` | With a spot level | Spot symbol, e.g. `NIFTY`, `BANKNIFTY` |
| `underlying_exchange` | No | Default `NSE_INDEX` |
| `upper_spot_stoploss` | No* | Fire when spot rises to this level |
| `lower_spot_stoploss` | No* | Fire when spot falls to this level. Must be below the upper level |
| `webhook_url` | Yes | `http` or `https` URL that receives the notification |
| `webhook_secret` | No | Sent as the `X-Webhook-Secret` header |
| `interval_seconds` | No | Check interval, 10 to 3600. Default `60` |
| `legs` | Yes | 1 to 50 legs, see below |

\* At least one of `target`, `stoploss`, `upper_spot_stoploss`,
`lower_spot_stoploss`. Leave a level out to skip that check.

### Leg fields

| Field | Required | Description |
|---|---|---|
| `symbol` | Yes | OpenAlgo symbol, e.g. `NIFTY27OCT2622700CE` |
| `exchange` | Yes | e.g. `NFO`, `BFO`, `MCX` |
| `product` | No | `NRML` (default), `MIS` or `CNC` |
| `quantity` | Yes | Signed units: positive long, negative short. `0` for a leg already closed |
| `entry_price` | Yes | Average entry price of the open quantity |
| `realized_pnl` | No | Rupees already booked on this leg (partial exits, closed legs). Default `0` |

The monitor's P&L is, per leg, `quantity x (live price - entry_price) +
realized_pnl`, summed. Keep a closed leg in the list with `quantity: 0` and its
`realized_pnl` so that profit or loss still counts toward the target and
stoploss.

## Deactivate

```json
{ "apikey": "<your_app_apikey>", "strategy": "NIFTY_STRADDLE_27OCT" }
```

Idempotent: an already inactive monitor returns `200` with
`"message": "Monitor already inactive"`. `404` if the account has no monitor
for that strategy.

## Webhook payload

```json
{
  "event": "strategy_breach",
  "workflow_id": 42,
  "strategy": "NIFTY_STRADDLE_27OCT",
  "reason": "LOWER_SPOT_SL",
  "idempotency_key": "NIFTY_STRADDLE_27OCT-2026-10-27-LOWER_SPOT_SL",
  "levels": {
    "target": 3000.0, "stoploss": 2000.0,
    "underlying": "NIFTY", "underlying_exchange": "NSE_INDEX",
    "upper_spot_stoploss": 23000.0, "lower_spot_stoploss": 22400.0
  },
  "pnl": { "total": 325.0, "realized": 0.0, "unrealized": 325.0, "today_total": 325.0, "unpriced_legs": 0 },
  "spot": { "symbol": "NIFTY", "exchange": "NSE_INDEX", "ltp": 22390.0 },
  "open_legs": 2,
  "legs": [
    { "symbol": "NIFTY27OCT2622700CE", "exchange": "NFO", "product": "NRML", "quantity": 65.0,
      "average_price": 50.0, "ltp": 55.0, "realized": 0.0, "today_realized": 0.0, "unrealized": 325.0 }
  ],
  "triggered_at": "2026-10-27T11:42:02.118203",
  "session_date": "2026-10-27"
}
```

- `reason`: `TARGET`, `STOPLOSS`, `UPPER_SPOT_SL` or `LOWER_SPOT_SL`. Checked in
  that order each run, and the first hit is the one sent.
- `legs`: the legs you sent, each with its live `ltp` and `unrealized`, open
  legs first. Closed legs (`quantity` 0) are included - skip them. A positive
  `quantity` is long (exit with SELL), a negative one short (exit with BUY).
- `spot.ltp` is `null` on a P&L breach, since the spot is only fetched when the
  P&L levels are not hit.
- `pnl.unpriced_legs` > 0 means a leg had no live price and `pnl.total` leaves
  it out. P&L levels are skipped in that run; spot levels still apply.

## Notes

- **Leg prices** come from a live multi-quote through this account's broker
  session, so the account must be connected. Where the orders were placed does
  not matter. `strategy` is only the monitor's identifier and the tag echoed in
  the webhook.
- **Missing data never fires:** a failed leg quote or spot quote skips the run
  rather than reporting a breach.
- The monitor is an ordinary Flow workflow named
  `Strategy Monitor - <strategy>`. It shows in `/flow` with its run log, but
  edit it through `upsert`, not the editor, or the next upsert overwrites the
  edit.

## Rate Limiting

`WEBSOCKET_CONTROL_LIMIT` (default `10 per minute`), as for the other Flow
monitor endpoints.

## Related

- [Price Breach Monitor](./pricebreach.md)
- [Price Alert Monitor](./pricealert.md)
