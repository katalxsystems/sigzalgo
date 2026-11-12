import re

from marshmallow import Schema, ValidationError, fields, validate, validates_schema
from utils.constants import SUPPORTED_INTERVALS

from utils.constants import VALID_EXCHANGES, VALID_PRODUCT_TYPES


# Custom validator for date or timestamp string
def validate_date_or_timestamp(data: str) -> None:
    """
    Validates that the input string is either in 'YYYY-MM-DD' format or a numeric timestamp.
    """
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    timestamp_pattern = re.compile(r"^\d{10,13}$")  # Allows for seconds or milliseconds
    if not (isinstance(data, str) and (date_pattern.match(data) or timestamp_pattern.match(data))):
        raise ValidationError(
            "Field must be a string in 'YYYY-MM-DD' format or a numeric timestamp."
        )


# Custom validator for option offset
def validate_option_offset(data: str) -> bool:
    """
    Validates option offset: ATM, ITM1-ITM50, OTM1-OTM50
    """
    data_upper = data.upper()
    if data_upper == "ATM":
        return True

    # Check for ITM pattern: ITM followed by 1-50
    itm_pattern = re.compile(r"^ITM([1-9]|[1-4][0-9]|50)$")
    otm_pattern = re.compile(r"^OTM([1-9]|[1-4][0-9]|50)$")

    if not (itm_pattern.match(data_upper) or otm_pattern.match(data_upper)):
        raise ValidationError("Offset must be ATM, ITM1-ITM50, or OTM1-OTM50")

    return True


class QuotesSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    symbol = fields.Str(required=True)  # Single symbol
    exchange = fields.Str(
        required=True, validate=validate.OneOf(VALID_EXCHANGES)
    )  # Exchange (e.g., NSE, BSE)


class SymbolExchangePair(Schema):
    symbol = fields.Str(required=True)
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))


class MultiQuotesSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    symbols = fields.List(
        fields.Nested(SymbolExchangePair), required=True, validate=validate.Length(min=1)
    )


class WebSocketSubscribeSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    symbols = fields.List(
        fields.Nested(SymbolExchangePair), required=True, validate=validate.Length(min=1, max=50)
    )
    mode = fields.Str(load_default="Quote", validate=validate.OneOf(["LTP", "Quote", "Depth"]))


class WebSocketUnsubscribeAllSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))


class PriceBreachCreateSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    call_id = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    mentor_id = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    symbol = fields.Str(required=True)
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))
    # The live price at the moment of the call -- used only to pick which
    # direction counts as "back to entry" (see
    # services.price_breach_service._entry_cross_condition), not stored as a
    # trigger threshold itself.
    active_price = fields.Float(required=True)
    entry_price = fields.Float(required=True)
    stop_loss = fields.Float(required=True)
    target1 = fields.Float(required=True)
    webhook_url = fields.Url(required=True, schemes={"http", "https"})
    name = fields.Str(load_default=None, allow_none=True, validate=validate.Length(max=200))
    # The LTP watches ignore prices outside market hours and at weekends:
    # NSE/BSE/NFO/BFO from 15:45 to 09:00 IST, MCX/NCDEX from 23:45 to 08:45.
    # Other exchanges are unaffected.
    pause_outside_market_hours = fields.Bool(load_default=False)


class PriceBreachDeactivateSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    # Sent by a watch's own deactivate node after it notified a breach, so
    # the Flow list can show it as breached rather than just deactivated.
    breach = fields.Bool(load_default=False)


# A strategy tag as orders carry it. Restricted so it is safe to embed in a
# workflow name and an idempotency key without escaping.
STRATEGY_TAG = validate.Regexp(
    r"^[A-Za-z0-9_.:-]{1,100}$",
    error="strategy may contain only letters, digits and _ . : - (max 100)",
)


def ExternalIdField(**kwargs) -> fields.Integer:  # noqa: N802 - used like a field class
    """An external platform's integer record id (strategy, call, mentor).

    A JSON integer only: strict, so "42", 42.0 and booleans are refused rather
    than coerced. Positive, and within the range a JavaScript client reads
    exactly.
    """
    return fields.Integer(strict=True, validate=validate.Range(min=1, max=2**53 - 1), **kwargs)


class StrategyLegSchema(Schema):
    """One leg as the external platform records it."""

    # The caller's id for the call this leg belongs to; echoed per leg in the
    # breach webhook so it can map each leg back without matching on symbol.
    call_id = ExternalIdField(required=True)
    symbol = fields.Str(required=True, validate=validate.Length(min=1, max=100))
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))
    product = fields.Str(load_default="NRML", validate=validate.OneOf(VALID_PRODUCT_TYPES))
    # Signed: positive long, negative short, 0 for a leg already closed (kept
    # so its realized_pnl still counts toward the total).
    quantity = fields.Float(required=True)
    entry_price = fields.Float(required=True, validate=validate.Range(min=0))
    realized_pnl = fields.Float(load_default=0.0)


class StrategyMonitorUpsertSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True, validate=STRATEGY_TAG)
    # The caller's own ids, stored with the monitor and echoed in the breach
    # webhook. Not part of the monitor's identity: that stays account + strategy.
    strategy_id = ExternalIdField(required=True)
    mentor_id = ExternalIdField(required=True)
    # Combined strategy P&L in rupees. stoploss is the loss amount as a
    # positive number: 2000 exits at a P&L of -2000.
    target = fields.Float(
        load_default=None, allow_none=True, validate=validate.Range(min=0, min_inclusive=False)
    )
    stoploss = fields.Float(
        load_default=None, allow_none=True, validate=validate.Range(min=0, min_inclusive=False)
    )
    underlying = fields.Str(
        load_default=None, allow_none=True, validate=validate.Length(min=1, max=50)
    )
    underlying_exchange = fields.Str(
        load_default="NSE_INDEX", validate=validate.OneOf(VALID_EXCHANGES)
    )
    upper_spot_stoploss = fields.Float(
        load_default=None, allow_none=True, validate=validate.Range(min=0, min_inclusive=False)
    )
    lower_spot_stoploss = fields.Float(
        load_default=None, allow_none=True, validate=validate.Range(min=0, min_inclusive=False)
    )
    webhook_url = fields.Url(required=True, schemes={"http", "https"})
    webhook_secret = fields.Str(
        load_default=None, allow_none=True, validate=validate.Length(max=256)
    )
    interval_seconds = fields.Int(load_default=60, validate=validate.Range(min=10, max=3600))
    legs = fields.List(
        fields.Nested(StrategyLegSchema), required=True, validate=validate.Length(min=1, max=50)
    )

    @validates_schema
    def check_levels(self, data, **kwargs):
        levels = ("target", "stoploss", "upper_spot_stoploss", "lower_spot_stoploss")
        if all(data.get(k) is None for k in levels):
            raise ValidationError(f"Set at least one of: {', '.join(levels)}")
        spot = (
            data.get("upper_spot_stoploss") is not None
            or data.get("lower_spot_stoploss") is not None
        )
        if spot and not data.get("underlying"):
            raise ValidationError("underlying is required for a spot stoploss", "underlying")
        upper, lower = data.get("upper_spot_stoploss"), data.get("lower_spot_stoploss")
        if upper is not None and lower is not None and lower >= upper:
            raise ValidationError(
                "lower_spot_stoploss must be below upper_spot_stoploss", "lower_spot_stoploss"
            )


class StrategyMonitorDeactivateSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    strategy = fields.Str(required=True, validate=STRATEGY_TAG)
    # Sent by the monitor's own deactivate node after a delivered breach.
    breach = fields.Bool(load_default=False)


# The four conditions the chart's own "Create Alert" dialog offers -- the
# same alias vocabulary FlowPriceMonitor.normalize_condition() already
# accepts (see services/flow_price_monitor_service.py), so nothing extra is
# needed to translate between what a caller sends and what the price
# monitor understands.
PRICE_ALERT_CONDITIONS = ("above", "below", "crosses_above", "crosses_below")


class PriceAlertCreateSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    symbol = fields.Str(required=True)
    exchange = fields.Str(required=True, validate=validate.OneOf(VALID_EXCHANGES))
    condition = fields.Str(required=True, validate=validate.OneOf(PRICE_ALERT_CONDITIONS))
    price = fields.Float(required=True)
    trigger = fields.Str(load_default="once", validate=validate.OneOf(("once", "every_time")))
    expiration = fields.Str(
        load_default="none", validate=validate.OneOf(("none", "1h", "4h", "1d", "1w"))
    )
    webhook_url = fields.Url(required=True, schemes={"http", "https"})
    message = fields.Str(load_default=None, allow_none=True, validate=validate.Length(max=500))
    name = fields.Str(load_default=None, allow_none=True, validate=validate.Length(max=200))


class PriceAlertDeactivateSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))


class HistorySchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    symbol = fields.Str(required=True)
    exchange = fields.Str(
        required=True, validate=validate.OneOf(VALID_EXCHANGES)
    )  # Exchange (e.g., NSE, BSE)
    interval = fields.Str(
        required=True,
        validate=validate.OneOf(SUPPORTED_INTERVALS),
    )
    start_date = fields.Date(required=True, format="%Y-%m-%d")  # YYYY-MM-DD
    end_date = fields.Date(required=True, format="%Y-%m-%d")  # YYYY-MM-DD
    # Optional: Data source - 'api' (broker, default) or 'db' (DuckDB/Historify)
    source = fields.Str(required=False, load_default="api", validate=validate.OneOf(["api", "db"]))
    # OI is now always included by default for F&O exchanges


class DepthSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    symbol = fields.Str(required=True)
    exchange = fields.Str(
        required=True, validate=validate.OneOf(VALID_EXCHANGES)
    )  # Exchange (e.g., NSE, BSE)


class IntervalsSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))


class SymbolSchema(Schema):
    apikey = fields.Str(
        required=True, validate=validate.Length(min=1, max=256)
    )  # API Key for authentication
    symbol = fields.Str(required=True)  # Symbol code (e.g., RELIANCE)
    exchange = fields.Str(
        required=True, validate=validate.OneOf(VALID_EXCHANGES)
    )  # Exchange (e.g., NSE, BSE)


class TickerSchema(Schema):
    apikey = fields.Str(required=True, validate=validate.Length(min=1, max=256))
    symbol = fields.Str(required=True)  # Combined exchange:symbol format
    interval = fields.Str(
        required=True,
        validate=validate.OneOf(["1m", "5m", "15m", "30m", "1h", "4h", "D", "W", "M"]),
    )  # Supported intervals: 1m, 5m, 15m, 30m, 1h, 4h, D, W, M etc.
    from_ = fields.Str(
        data_key="from", required=True, validate=validate_date_or_timestamp
    )  # YYYY-MM-DD or millisecond timestamp
    to = fields.Str(
        required=True, validate=validate_date_or_timestamp
    )  # YYYY-MM-DD or millisecond timestamp
    adjusted = fields.Bool(required=False, default=True)  # Adjust for splits
    sort = fields.Str(
        required=False, default="asc", validate=validate.OneOf(["asc", "desc"])
    )  # Sort direction


class SearchSchema(Schema):
    apikey = fields.Str(
        required=True, validate=validate.Length(min=1, max=256)
    )  # API Key for authentication
    query = fields.Str(required=True)  # Search query/symbol name
    exchange = fields.Str(
        required=False, validate=validate.OneOf(VALID_EXCHANGES)
    )  # Optional exchange filter (e.g., NSE, BSE)


#: Exchanges that list derivatives. Defined once: the same list was repeated in
#: three schemas, and NCO was added to the platform without reaching any of
#: them, so every expiry and option-chain request for NSE commodities was
#: rejected at the API boundary before the service ever ran. See #1748.
F_AND_O_EXCHANGES = ["NFO", "BFO", "MCX", "CDS", "NCO", "BCD", "NCDEX", "CRYPTO"]


class ExpirySchema(Schema):
    apikey = fields.Str(
        required=True, validate=validate.Length(min=1, max=256)
    )  # API Key for authentication
    symbol = fields.Str(required=True)  # Underlying symbol (e.g., NIFTY, BANKNIFTY)
    exchange = fields.Str(
        required=True, validate=validate.OneOf(F_AND_O_EXCHANGES)
    )  # Exchange (e.g., NFO, BFO, MCX, CDS, CRYPTO)
    instrumenttype = fields.Str(
        required=True, validate=validate.OneOf(["futures", "options"])
    )  # futures or options


class OptionSymbolSchema(Schema):
    apikey = fields.Str(
        required=True, validate=validate.Length(min=1, max=256)
    )  # API Key for authentication
    strategy = fields.Str(
        required=False, allow_none=True
    )  # DEPRECATED: Strategy name (optional, will be removed in future versions)
    underlying = fields.Str(required=True)  # Underlying symbol (NIFTY, RELIANCE, NIFTY28OCT25FUT)
    exchange = fields.Str(
        required=True, validate=validate.OneOf(VALID_EXCHANGES)
    )  # Exchange (NSE_INDEX, NSE, NFO)
    expiry_date = fields.Str(
        required=False
    )  # Expiry date in DDMMMYY format (e.g., 28OCT25). Optional if underlying includes expiry
    strike_int = fields.Int(
        required=False, validate=validate.Range(min=1), allow_none=True
    )  # OPTIONAL: Strike interval. If not provided, actual strikes from database will be used (RECOMMENDED for accuracy)
    offset = fields.Str(
        required=True, validate=validate_option_offset
    )  # Strike offset from ATM (ATM, ITM1-ITM50, OTM1-OTM50)
    option_type = fields.Str(
        required=True, validate=validate.OneOf(["CE", "PE", "ce", "pe"])
    )  # Call or Put option


class OptionGreeksSchema(Schema):
    apikey = fields.Str(
        required=True, validate=validate.Length(min=1, max=256)
    )  # API Key for authentication
    symbol = fields.Str(required=True)  # Option symbol (e.g., NIFTY28NOV2424000CE)
    exchange = fields.Str(
        required=True, validate=validate.OneOf(F_AND_O_EXCHANGES)
    )  # Exchange (NFO, BFO, CDS, MCX, CRYPTO)
    interest_rate = fields.Float(
        required=False, validate=validate.Range(min=0, max=100)
    )  # Risk-free interest rate (annualized %). Optional, defaults per exchange
    forward_price = fields.Float(
        required=False, validate=validate.Range(min=0)
    )  # Optional: Custom forward/synthetic futures price. If provided, skips underlying price fetch
    underlying_symbol = fields.Str(
        required=False
    )  # Optional: Specify underlying symbol (e.g., NIFTY or NIFTY28NOV24FUT)
    underlying_exchange = fields.Str(
        required=False
    )  # Optional: Specify underlying exchange (NSE_INDEX, NFO, etc.)
    expiry_time = fields.Str(
        required=False
    )  # Optional: Custom expiry time in HH:MM format (e.g., "15:30", "19:00"). If not provided, uses exchange defaults


class InstrumentsSchema(Schema):
    apikey = fields.Str(
        required=True, validate=validate.Length(min=1, max=256)
    )  # API Key for authentication
    exchange = fields.Str(
        required=False,
        validate=validate.OneOf(VALID_EXCHANGES),
    )  # Optional exchange filter
    format = fields.Str(
        required=False, validate=validate.OneOf(["json", "csv"])
    )  # Output format (json or csv), defaults to json


class OptionChainSchema(Schema):
    apikey = fields.Str(
        required=True, validate=validate.Length(min=1, max=256)
    )  # API Key for authentication
    underlying = fields.Str(required=True)  # Underlying symbol (e.g., NIFTY, BANKNIFTY, RELIANCE)
    exchange = fields.Str(
        required=True, validate=validate.OneOf(VALID_EXCHANGES)
    )  # Exchange (NSE_INDEX, NSE, NFO, BSE_INDEX, BSE, BFO, MCX, CDS)
    expiry_date = fields.Str(
        required=True
    )  # Expiry date in DDMMMYY format (e.g., 28NOV25) - MANDATORY
    strike_count = fields.Int(
        required=False, validate=validate.Range(min=1, max=100), allow_none=True
    )  # Number of strikes above/below ATM. If not provided, returns entire chain
    with_greeks = fields.Bool(
        required=False, load_default=False
    )  # Attach IV + delta/gamma/theta/vega to every leg, from the quotes already fetched
    interest_rate = fields.Float(
        required=False, validate=validate.Range(min=0, max=100), allow_none=True
    )  # Annualized risk-free rate percentage, Greeks only. Defaults to the exchange default (0)


class MarketHolidaysSchema(Schema):
    apikey = fields.Str(
        required=True, validate=validate.Length(min=1, max=256)
    )  # API Key for authentication
    year = fields.Int(
        required=False, validate=validate.Range(min=2020, max=2050)
    )  # Year to get holidays for (defaults to current year)


class MarketTimingsSchema(Schema):
    apikey = fields.Str(
        required=True, validate=validate.Length(min=1, max=256)
    )  # API Key for authentication
    date = fields.Str(required=True)  # Date in YYYY-MM-DD format


class OptionSymbolRequest(Schema):
    """Schema for a single option symbol request in batch"""

    symbol = fields.Str(required=True)  # Option symbol (e.g., NIFTY28NOV2424000CE)
    exchange = fields.Str(required=True, validate=validate.OneOf(F_AND_O_EXCHANGES))
    underlying_symbol = fields.Str(required=False)  # Optional: Specify underlying symbol
    underlying_exchange = fields.Str(required=False)  # Optional: Specify underlying exchange


class MultiOptionGreeksSchema(Schema):
    """Schema for batch option greeks requests"""

    apikey = fields.Str(
        required=True, validate=validate.Length(min=1, max=256)
    )  # API Key for authentication
    symbols = fields.List(
        fields.Nested(OptionSymbolRequest),
        required=True,
        validate=validate.Length(min=1, max=50),  # Max 50 symbols per request
    )
    interest_rate = fields.Float(
        required=False, validate=validate.Range(min=0, max=100)
    )  # Common interest rate for all
    expiry_time = fields.Str(required=False)  # Optional: Common expiry time for all
