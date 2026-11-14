import hashlib
import json
import logging
import os
import time
import urllib.parse
from datetime import datetime, timedelta

import httpx
import pandas as pd
from cachetools import TTLCache

from broker.pocketful.api.pocketfulwebsocket import (
    PocketfulSocket,
    get_snapquotedata,
    get_ws_connection_status,
)
from broker.pocketful.database.master_contract_db import SymToken, db_session
from database.token_db import get_br_symbol, get_oa_symbol
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

#: Client id per login token (hashed). Bounded: one entry per live login.
_CLIENT_ID_CACHE = TTLCache(maxsize=64, ttl=12 * 3600)

# --- chart history (GET algo.pocketful.in/api/v1/charts) --------------------
#
# Verified against the live API: candletype "1" returns 1-minute candles,
# "3" returns one candle per session (daily); "D"/"1D"/"day" are rejected as
# an invalid candle type. Prices are in rupees (unlike the WebSocket feed's
# paise), and each candle is [ISO timestamp with +0530, open, high, low,
# close, volume]. trade.pocketful.in rejects this endpoint; algo serves it.
# One 1-minute request returned six weeks (10,945 candles) without error.
CHART_URL = "https://algo.pocketful.in/api/v1/charts"
CANDLE_ONE_MINUTE = "1"
CANDLE_DAILY = "3"
CHART_CHUNK_DAYS = {CANDLE_ONE_MINUTE: 30, CANDLE_DAILY: 365}
CHART_EXCHANGE = {"NSE_INDEX": "NSE", "BSE_INDEX": "BSE"}
#: Session open (IST) intraday bars are aligned to, so a 15m bar is 09:15-09:30.
SESSION_OPEN = {"MCX": (9, 0), "CDS": (9, 0), "BCD": (9, 0)}
IST = "Asia/Kolkata"


def _empty_history() -> pd.DataFrame:
    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "oi"])


def _candles_to_frame(rows: list) -> pd.DataFrame:
    """Raw chart candles to an OHLCV frame indexed by tz-aware IST time."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(
        [r[:6] for r in rows if isinstance(r, (list, tuple)) and len(r) >= 6],
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(IST)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").set_index("timestamp")
    df.index.name = None
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["volume"] = df["volume"].fillna(0).astype("int64")
    return df


_OHLCV = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def _resample_intraday(df: pd.DataFrame, minutes: int, session_open: tuple[int, int]) -> pd.DataFrame:
    """1-minute candles to N-minute bars aligned to the session open."""
    hour, minute = session_open
    out = df.resample(
        f"{minutes}min", origin="start_day", offset=f"{hour}h{minute}min", label="left", closed="left"
    ).agg(_OHLCV)
    return out.dropna(subset=["open"])


def _resample_daily(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Daily candles to weekly (Monday) or monthly (1st) bars."""
    out = df.resample(rule, label="left", closed="left").agg(_OHLCV)
    return out.dropna(subset=["open"])


#: How long a quote waits for its symbol's first tick after subscribing.
QUOTE_WAIT_SEC = 10.0
#: How often it looks. Ticks are stored per symbol as they arrive, so a short
#: poll costs nothing and returns as soon as the tick is there.
QUOTE_POLL_SEC = 0.1


class PocketfulPermissionError(Exception):
    """Custom exception for Pocketful API permission errors"""

    pass


class PocketfulAPIError(Exception):
    """Custom exception for other Pocketful API errors"""

    pass


def get_api_response(endpoint, auth, method="GET", payload=""):
    AUTH_TOKEN = auth
    base_url = "https://api.pocketful.in"
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}", "Content-Type": "application/json"}

    try:
        # Log the complete request details for debugging
        logger.info("=== API Request Details ===")
        logger.info(f"URL: {base_url}{endpoint}")
        logger.info(f"Method: {method}")
        logger.info(f"Headers: {json.dumps(headers, indent=2)}")
        if payload:
            logger.info(f"Payload: {payload}")

        # Get the shared httpx client
        client = get_httpx_client()
        url = f"{base_url}{endpoint}"

        # Make request based on method
        if method == "GET":
            res = client.get(url, headers=headers)
        elif method == "POST":
            res = client.post(url, headers=headers, content=payload)
        elif method == "PUT":
            res = client.put(url, headers=headers, content=payload)
        elif method == "DELETE":
            res = client.delete(url, headers=headers)
        else:
            res = client.request(method, url, headers=headers, content=payload)

        response = res.json()

        # Log the complete response
        logger.info("=== API Response Details ===")
        logger.info(f"Status Code: {res.status_code}")
        logger.info(f"Response Headers: {dict(res.headers)}")
        logger.info(f"Response Body: {json.dumps(response, indent=2)}")

        # Check for permission errors
        if response.get("status") == "error":
            error_type = response.get("error_type")
            error_message = response.get("message", "Unknown error")

            if error_type == "PermissionException" or "permission" in error_message.lower():
                raise PocketfulPermissionError(f"API Permission denied: {error_message}.")
            else:
                raise PocketfulAPIError(f"API Error: {error_message}")

        return response
    except PocketfulPermissionError:
        raise
    except PocketfulAPIError:
        raise
    except Exception as e:
        logger.error(f"API request failed: {str(e)}")
        raise PocketfulAPIError(f"API request failed: {str(e)}")


class BrokerData:
    def __init__(self, auth_token):
        """Initialize Pocketful data handler with authentication token"""
        self.auth_token = auth_token
        self.client_id = None  # Will be fetched when needed
        self.ws_connection = None
        self.ws_connected = False
        self.last_depth = {}

        # Exchange code mapping for Pocketful WebSocket
        self.exchange_map = {"NSE": 1, "NFO": 2, "CDS": 3, "MCX": 4, "BSE": 6, "BFO": 7}

        # Candles come from GET algo.pocketful.in/api/v1/charts, which serves
        # two resolutions: candletype 1 (1 minute) and 3 (daily). Every other
        # timeframe is built from one of them: intraday from 1-minute candles,
        # weekly and monthly from daily ones. Value: (source, minutes or rule).
        self.timeframe_map = {
            "1m": ("intraday", 1),
            "3m": ("intraday", 3),
            "5m": ("intraday", 5),
            "10m": ("intraday", 10),
            "15m": ("intraday", 15),
            "30m": ("intraday", 30),
            "1h": ("intraday", 60),
            "D": ("daily", None),
            "W": ("daily", "W"),
            "M": ("daily", "M"),
        }

        # Market timing configuration for different exchanges
        self.market_timings = {
            "NSE": {"start": "09:15:00", "end": "15:30:00"},
            "BSE": {"start": "09:15:00", "end": "15:30:00"},
            "NFO": {"start": "09:15:00", "end": "15:30:00"},
            "CDS": {"start": "09:00:00", "end": "17:00:00"},
            "BCD": {"start": "09:00:00", "end": "17:00:00"},
            "MCX": {"start": "09:00:00", "end": "23:30:00"},
        }

        # Default market timings if exchange not found
        self.default_market_timings = {"start": "00:00:00", "end": "23:59:59"}

    def get_market_timings(self, exchange: str) -> dict:
        """Get market start and end times for given exchange"""
        return self.market_timings.get(exchange, self.default_market_timings)

    def get_quotes(self, symbol: str, exchange: str) -> dict:
        """
        Get real-time quotes for given symbol using Compact Market Data WebSocket
        Args:
            symbol: Trading symbol
            exchange: Exchange (e.g., NSE, BSE)
        Returns:
            dict: Quote data with required fields
        """
        try:
            # Get quotes using WebSocket compact market data - no fallbacks
            return self._get_quotes_compact(symbol, exchange)
        except PocketfulPermissionError as e:
            logger.error(f"Permission error fetching quotes: {str(e)}")
            raise
        except (PocketfulAPIError, Exception) as e:
            logger.error(f"Error fetching quotes: {str(e)}")
            raise PocketfulAPIError(f"Error fetching quotes: {str(e)}")

    def _get_quotes_from_depth(self, symbol: str, exchange: str) -> dict:
        """
        Get quotes from market depth data (fallback method)
        """
        # Use the market depth method which already has its own fallbacks
        depth = self.get_market_depth(symbol, exchange)

        # Extract basic quote information from the depth data
        return {
            "ask": depth["asks"][0]["price"] if depth["asks"] else 0,
            "bid": depth["bids"][0]["price"] if depth["bids"] else 0,
            "high": depth.get("high", 0),
            "low": depth.get("low", 0),
            "ltp": depth.get("ltp", 0),
            "open": depth.get("open", 0),
            "prev_close": depth.get("prev_close", 0),
            "volume": depth.get("volume", 0),
        }

    def _get_quotes_compact(self, symbol: str, exchange: str) -> dict:
        """
        Get quotes using detailed market data WebSocket (provides open/close/volume)
        """
        # Ensure WebSocket connection is established
        if not self._ensure_websocket_connection():
            raise PocketfulAPIError("WebSocket connection not established")

        # Convert symbol to broker format and get instrument token
        br_symbol = get_br_symbol(symbol, exchange)
        logger.debug(f"Fetching quotes using detailed market data for {exchange}:{br_symbol}")

        # Get token from database
        with db_session() as session:
            symbol_info = (
                session.query(SymToken)
                .filter(SymToken.exchange == exchange, SymToken.brsymbol == br_symbol)
                .first()
            )

            if not symbol_info:
                raise PocketfulAPIError(f"Could not find token for {exchange}:{br_symbol}")

            # Get the instrument token from the database
            instrument_token = int(symbol_info.token)

        # Map exchange to Pocketful exchange code
        if exchange == "NSE_INDEX":
            exchange_code = self.exchange_map.get("NSE", 1)
        elif exchange == "BSE_INDEX":
            exchange_code = self.exchange_map.get("BSE", 6)
        else:
            exchange_code = self.exchange_map.get(exchange, 1)

        # Log the instrument details
        logger.debug(f"Using exchange_code={exchange_code}, instrument_token={instrument_token}")

        # Hold a shared subscription (includes open/close/volume). A symbol
        # another request already holds, or one still lingering from a recent
        # request, answers at once from its stored tick.
        detailed_payload = {"exchangeCode": exchange_code, "instrumentToken": instrument_token}
        self.ws_connection.acquire_detailed(detailed_payload)

        detailed_data = None
        try:
            deadline = time.time() + QUOTE_WAIT_SEC
            while True:
                # This symbol's own tick, never whichever symbol ticked last.
                detailed_data = self.ws_connection.read_detailed_for(detailed_payload)
                if detailed_data:
                    break
                if time.time() >= deadline:
                    break
                time.sleep(QUOTE_POLL_SEC)
        finally:
            self.ws_connection.release_detailed(detailed_payload)

        # If no valid data received, raise exception
        if not detailed_data or not isinstance(detailed_data, dict):
            raise PocketfulAPIError(f"No detailed market data received for {exchange}:{br_symbol}")

        # Extract and format quote data from detailed market data
        # Note: Price values are multiplied by 100
        last_traded_price = (
            detailed_data.get("last_traded_price", 0) / 100
            if detailed_data.get("last_traded_price")
            else 0
        )
        bid_price = (
            detailed_data.get("best_bid_price", 0) / 100
            if detailed_data.get("best_bid_price")
            else 0
        )
        ask_price = (
            detailed_data.get("best_ask_price", 0) / 100
            if detailed_data.get("best_ask_price")
            else 0
        )
        high_price = (
            detailed_data.get("high_price", 0) / 100 if detailed_data.get("high_price") else 0
        )
        low_price = detailed_data.get("low_price", 0) / 100 if detailed_data.get("low_price") else 0
        open_price = (
            detailed_data.get("open_price", 0) / 100 if detailed_data.get("open_price") else 0
        )
        close_price = (
            detailed_data.get("close_price", 0) / 100 if detailed_data.get("close_price") else 0
        )
        volume = detailed_data.get("trade_volume", 0)

        # Calculate change from LTP and previous close
        change = last_traded_price - close_price if close_price else 0

        # Return formatted quote data
        return {
            "ask": ask_price,
            "bid": bid_price,
            "high": high_price,
            "low": low_price,
            "ltp": last_traded_price,
            "open": open_price,
            "prev_close": close_price,
            "volume": volume,
            "oi": detailed_data.get("currentOpenInterest", 0),
            "change": change,
        }

    def get_history(
        self, symbol: str, exchange: str, timeframe: str, from_date: str, to_date: str
    ) -> pd.DataFrame:
        """
        Get historical data for given symbol and timeframe
        Args:
            symbol: Trading symbol
            exchange: Exchange (e.g., NSE, BSE)
            timeframe: Timeframe (e.g., 1m, 5m, 15m, 60m, D)
            from_date: Start date in format YYYY-MM-DD
            to_date: End date in format YYYY-MM-DD
        Returns:
            pd.DataFrame: Historical data with OHLCV
        """
        spec = self.timeframe_map.get(timeframe)
        if not spec:
            raise PocketfulAPIError(
                f"Pocketful charts do not offer the {timeframe} timeframe. "
                f"Choose one of: {', '.join(self.timeframe_map)}."
            )
        source, step = spec

        token = self._chart_token(symbol, exchange)
        chart_exchange = CHART_EXCHANGE.get(exchange, exchange)
        start = datetime.strptime(from_date, "%Y-%m-%d")
        end = datetime.strptime(to_date, "%Y-%m-%d")
        if end < start:
            return _empty_history()

        candletype = CANDLE_ONE_MINUTE if source == "intraday" else CANDLE_DAILY
        chunk_days = CHART_CHUNK_DAYS[candletype]

        rows: list = []
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=chunk_days - 1), end)
            rows.extend(
                self._fetch_chart_candles(chart_exchange, token, candletype, cursor, chunk_end)
            )
            cursor = chunk_end + timedelta(days=1)

        df = _candles_to_frame(rows)
        if df.empty:
            return _empty_history()

        if source == "intraday":
            if step != 1:
                df = _resample_intraday(df, step, SESSION_OPEN.get(exchange, (9, 15)))
        else:
            df.index = df.index.normalize()  # one candle per date, at midnight IST
            if step == "W":
                df = _resample_daily(df, "W-MON")
            elif step == "M":
                df = _resample_daily(df, "MS")

        out = df.reset_index().rename(columns={"index": "timestamp"})
        out["timestamp"] = out["timestamp"].map(lambda ts: int(ts.timestamp()))
        out["oi"] = 0
        return out[["timestamp", "open", "high", "low", "close", "volume", "oi"]]

    def _chart_token(self, symbol: str, exchange: str) -> int:
        br_symbol = get_br_symbol(symbol, exchange)
        with db_session() as session:
            row = (
                session.query(SymToken)
                .filter(SymToken.exchange == exchange, SymToken.brsymbol == br_symbol)
                .first()
            )
            if not row:
                raise PocketfulAPIError(
                    f"{symbol} on {exchange} is not in the Pocketful master contract. "
                    "Download the master contract again, then retry."
                )
            return int(row.token)

    def _fetch_chart_candles(self, exchange, token, candletype, start, end) -> list:
        """One /api/v1/charts request; returns its raw candle rows."""
        headers = {
            "Authorization": f"Bearer {self.auth_token}",
            "clientid": self._get_client_id() or "",
            "accept": "application/json",
        }
        params = {
            "exchange": exchange,
            "token": str(token),
            "candletype": candletype,
            "from_date": start.strftime("%Y-%m-%dT00:00:00"),
            "to_date": end.strftime("%Y-%m-%dT23:59:59"),
        }
        try:
            response = get_httpx_client().get(
                CHART_URL, params=params, headers=headers, timeout=30
            )
            body = response.json()
        except Exception as e:
            logger.exception("Pocketful chart request failed")
            raise PocketfulAPIError(
                "Could not reach Pocketful for chart data. Try again in a moment."
            ) from e
        if response.status_code != 200 or body.get("status") != "success":
            logger.error(
                f"Pocketful chart data refused: HTTP {response.status_code}, "
                f"message={body.get('message')}"
            )
            if response.status_code == 401:
                raise PocketfulAPIError(
                    "Pocketful did not accept the login for chart data. Log in to Pocketful again."
                )
            raise PocketfulAPIError(
                f"Pocketful did not return chart data: {body.get('message') or 'no reason given'}."
            )
        return (body.get("data") or {}).get("candles") or []

    def get_intervals(self) -> list:
        """The chart timeframes Pocketful history serves."""
        return list(self.timeframe_map)

    def _get_client_id(self):
        """
        Get client_id from Pocketful API
        Returns:
            str: Client ID for the authenticated user
        """
        if not self.client_id:
            # A new BrokerData is built for every request, so without this
            # every quote paid a trading_info round trip first. Keyed by a
            # hash of the token: a new login (new token) looks it up again.
            cache_key = hashlib.sha256((self.auth_token or "").encode()).hexdigest()
            cached = _CLIENT_ID_CACHE.get(cache_key)
            if cached:
                self.client_id = cached
                return self.client_id
            try:
                # Fetch client_id from trading_info endpoint
                logger.debug("Fetching client_id from trading_info endpoint")

                # Get the shared httpx client
                client = get_httpx_client()
                headers = {
                    "Authorization": f"Bearer {self.auth_token}",
                    "Content-Type": "application/json",
                }

                response = client.get(
                    "https://trade.pocketful.in/api/v1/user/trading_info", headers=headers
                )
                info_response = response.json()

                if info_response.get("status") == "success":
                    self.client_id = info_response.get("data", {}).get("client_id")
                    if self.client_id:
                        _CLIENT_ID_CACHE[cache_key] = self.client_id
                    logger.debug(f"Got client_id from API: {self.client_id}")
                else:
                    raise PocketfulAPIError(
                        f"Failed to fetch client_id: {info_response.get('message', 'Unknown error')}"
                    )
            except httpx.HTTPError as e:
                logger.error(f"Error fetching client_id: {str(e)}")
                raise PocketfulAPIError(f"Error fetching client_id: {str(e)}")
            except Exception as e:
                logger.error(f"Error fetching client_id: {str(e)}")
                raise PocketfulAPIError(f"Error fetching client_id: {str(e)}")

        return self.client_id

    def _ensure_websocket_connection(self):
        """
        Ensure WebSocket connection is established
        Returns:
            bool: True if connection is successful, False otherwise
        """
        if self.ws_connection is None:
            logger.debug("Initializing WebSocket connection")
            # Get client_id first
            client_id = self._get_client_id()
            if not client_id:
                logger.error("Failed to get client_id for WebSocket connection")
                raise PocketfulAPIError("Failed to get client_id for WebSocket connection")

            try:
                self.ws_connection = PocketfulSocket(self.client_id, self.auth_token)
                self.ws_connected = self.ws_connection.run_socket()

                if not self.ws_connected:
                    logger.error("Failed to establish WebSocket connection")
                    raise PocketfulAPIError("Failed to establish WebSocket connection")

                logger.debug("WebSocket connection established successfully")
                return True
            except Exception as e:
                logger.error(f"Error establishing WebSocket connection: {str(e)}")
                raise PocketfulAPIError(f"Error establishing WebSocket connection: {str(e)}")
        return self.ws_connected

    def get_market_depth(self, symbol: str, exchange: str) -> dict:
        """
        Get market depth for given symbol using WebSocket
        Args:
            symbol: Trading symbol
            exchange: Exchange (e.g., NSE, BSE)
        Returns:
            dict: Market depth data
        """
        try:
            # Get market depth using WebSocket - no fallback to mock data
            return self._get_market_depth_websocket(symbol, exchange)
        except PocketfulPermissionError as e:
            logger.error(f"Permission error fetching market depth: {str(e)}")
            raise
        except (PocketfulAPIError, Exception) as e:
            logger.error(f"Error fetching market depth: {str(e)}")
            raise PocketfulAPIError(f"Error fetching market depth: {str(e)}")

    def _get_mock_market_depth(self, symbol: str, exchange: str) -> dict:
        """
        Generate mock market depth data with proper structure
        This is a fallback when WebSocket fails
        """
        logger.warning(f"Generating mock market depth data for {exchange}:{symbol}")

        # Try to get approximate price data from compact market data
        approx_price = 100.0  # Default starting price
        try:
            # Try to get a more realistic price from compact market data
            compact_data = self._get_quotes_compact_noexcept(symbol, exchange)
            if compact_data and "ltp" in compact_data and compact_data["ltp"] > 0:
                approx_price = compact_data["ltp"]
                logger.info(
                    f"Using approximate price of {approx_price} from compact data for mock depth"
                )
        except Exception:
            pass  # Ignore errors, just use default

        # Create structured mock data matching Pocketful format with realistic prices
        mock_data = {
            "asks": [
                {"price": approx_price, "quantity": 100, "orders": 1},
                {"price": approx_price + (approx_price * 0.005), "quantity": 200, "orders": 2},
                {"price": approx_price + (approx_price * 0.010), "quantity": 300, "orders": 3},
                {"price": approx_price + (approx_price * 0.015), "quantity": 400, "orders": 4},
                {"price": approx_price + (approx_price * 0.020), "quantity": 500, "orders": 5},
            ],
            "bids": [
                {"price": approx_price - (approx_price * 0.005), "quantity": 100, "orders": 1},
                {"price": approx_price - (approx_price * 0.010), "quantity": 200, "orders": 2},
                {"price": approx_price - (approx_price * 0.015), "quantity": 300, "orders": 3},
                {"price": approx_price - (approx_price * 0.020), "quantity": 400, "orders": 4},
                {"price": approx_price - (approx_price * 0.025), "quantity": 500, "orders": 5},
            ],
            "high": approx_price + (approx_price * 0.025),
            "low": approx_price - (approx_price * 0.03),
            "ltp": approx_price,
            "ltq": 10,
            "oi": 0,
            "open": approx_price - (approx_price * 0.01),
            "prev_close": approx_price - (approx_price * 0.015),
            "totalbuyqty": 1500,
            "totalsellqty": 1500,
            "volume": 5000,
            "instrument_token": 0,  # Placeholder
        }

        return mock_data

    def _get_market_depth_websocket(self, symbol: str, exchange: str) -> dict:
        """
        Get market depth using WebSocket implementation
        Internal method called by get_market_depth
        """
        try:
            # Ensure WebSocket connection is established
            if not self._ensure_websocket_connection():
                raise PocketfulAPIError("WebSocket connection not established")

            # Convert symbol to broker format and get instrument token
            br_symbol = get_br_symbol(symbol, exchange)
            logger.info(f"Fetching market depth for {exchange}:{br_symbol}")

            # Get token from database
            with db_session() as session:
                symbol_info = (
                    session.query(SymToken)
                    .filter(SymToken.exchange == exchange, SymToken.brsymbol == br_symbol)
                    .first()
                )

                if not symbol_info:
                    raise Exception(f"Could not find token for {exchange}:{br_symbol}")

                # Get the instrument token from the database
                instrument_token = int(symbol_info.token)

            # Map exchange to Pocketful exchange code
            if exchange == "NSE_INDEX":
                exchange_code = self.exchange_map.get("NSE", 1)
            elif exchange == "BSE_INDEX":
                exchange_code = self.exchange_map.get("BSE", 6)
            else:
                exchange_code = self.exchange_map.get(exchange, 1)

            # Log the instrument details
            logger.info(f"Using exchange_code={exchange_code}, instrument_token={instrument_token}")

            # Subscribe to snapquote data
            snapquote_payload = {"exchangeCode": exchange_code, "instrumentToken": instrument_token}
            subscription_result = self.ws_connection.subscribe_snapquote_data(snapquote_payload)
            logger.info(f"Subscription result: {subscription_result}")

            # Wait for data to be received with increased timeout
            attempts = 0
            max_attempts = 15  # Increased attempts further
            snapquote_data = None

            # Set debug logging to see all messages
            logging.getLogger("broker.pocketful.api.packet_decoder").setLevel(logging.DEBUG)
            logging.getLogger("broker.pocketful.api.pocketfulwebsocket").setLevel(logging.DEBUG)

            # Send a dummy heartbeat to ensure connection is active
            if hasattr(self.ws_connection, "_send_heartbeat"):
                self.ws_connection._send_heartbeat()

            logger.info(f"Waiting for snapquote data for instrument {instrument_token}")

            # Try a different approach - multiple shorter waits instead of longer ones
            while attempts < max_attempts:
                time.sleep(1.0)  # Standard wait time
                snapquote_data = self.ws_connection.read_snapquote_data()
                logger.info(f"Attempt {attempts + 1}: Received data: {snapquote_data}")

                # If we get any data at all, dump the raw data to help with debugging
                if isinstance(snapquote_data, dict) and snapquote_data:
                    logger.info(f"Received some data on attempt {attempts + 1}: {snapquote_data}")

                # More flexible check for valid data
                if snapquote_data and isinstance(snapquote_data, dict):
                    # Try different keys that might be present
                    token_in_data = snapquote_data.get("instrument_token") or snapquote_data.get(
                        "instrumentToken"
                    )
                    if token_in_data:
                        logger.info(
                            f"Received data with token {token_in_data} (looking for {instrument_token})"
                        )

                        # More flexible token matching
                        if str(token_in_data) == str(instrument_token):
                            logger.info(
                                f"Received valid market depth data for {exchange}:{br_symbol}"
                            )
                            break
                        else:
                            logger.debug(f"Received data for different instrument: {token_in_data}")
                    else:
                        # If no token is found, log the full response
                        logger.info(f"Received response without token field: {snapquote_data}")

                attempts += 1

            # Unsubscribe after receiving data
            self.ws_connection.unsubscribe_snapquote_data(snapquote_payload)

            # If no valid data received, try to use cached data or raise error
            if (
                not snapquote_data
                or not isinstance(snapquote_data, dict)
                or "instrument_token" not in snapquote_data
            ):
                logger.warning(f"No market depth data received for {exchange}:{br_symbol}")
                # Return last known depth if available
                if self.last_depth.get(f"{exchange}:{br_symbol}"):
                    logger.info(f"Using cached market depth data for {exchange}:{br_symbol}")
                    return self.last_depth.get(f"{exchange}:{br_symbol}")
                raise Exception(f"No market depth data received for {exchange}:{br_symbol}")

            # Store the data for reference (in case subsequent calls fail)
            self.last_depth[f"{exchange}:{br_symbol}"] = snapquote_data

            # Process snapquote data
            # Note: Pocketful price values are multiplied by 100, need to convert back

            # Format asks and bids
            asks = []
            bids = []

            # Process ask prices and quantities
            ask_prices = snapquote_data.get("askPrices", [])
            ask_qtys = snapquote_data.get("askQtys", [])
            sellers = snapquote_data.get("sellers", [])

            for i in range(min(5, len(ask_prices))):
                asks.append(
                    {
                        "price": ask_prices[i] / 100
                        if ask_prices[i]
                        else 0,  # Convert price back to standard format
                        "quantity": ask_qtys[i] if i < len(ask_qtys) else 0,
                        "orders": sellers[i] if i < len(sellers) else 0,
                    }
                )

            # Add empty entries if fewer than 5 provided
            while len(asks) < 5:
                asks.append({"price": 0, "quantity": 0, "orders": 0})

            # Process bid prices and quantities
            bid_prices = snapquote_data.get("bidPrices", [])
            bid_qtys = snapquote_data.get("bidQtys", [])
            buyers = snapquote_data.get("buyers", [])

            for i in range(min(5, len(bid_prices))):
                bids.append(
                    {
                        "price": bid_prices[i] / 100
                        if bid_prices[i]
                        else 0,  # Convert price back to standard format
                        "quantity": bid_qtys[i] if i < len(bid_qtys) else 0,
                        "orders": buyers[i] if i < len(buyers) else 0,
                    }
                )

            # Add empty entries if fewer than 5 provided
            while len(bids) < 5:
                bids.append({"price": 0, "quantity": 0, "orders": 0})

            # Return formatted market depth data
            return {
                "asks": asks,
                "bids": bids,
                "high": snapquote_data.get("high", 0) / 100 if snapquote_data.get("high") else 0,
                "low": snapquote_data.get("low", 0) / 100 if snapquote_data.get("low") else 0,
                "ltp": snapquote_data.get("averageTradePrice", 0) / 100
                if snapquote_data.get("averageTradePrice")
                else 0,
                "ltq": 0,  # Pocketful doesn't provide last traded quantity in snapquote
                "oi": 0,  # Pocketful doesn't provide open interest in snapquote
                "open": snapquote_data.get("open", 0) / 100 if snapquote_data.get("open") else 0,
                "prev_close": snapquote_data.get("close", 0) / 100
                if snapquote_data.get("close")
                else 0,
                "totalbuyqty": snapquote_data.get("totalBuyQty", 0),
                "totalsellqty": snapquote_data.get("totalSellQty", 0),
                "volume": snapquote_data.get("volume", 0),
            }

        except PocketfulPermissionError as e:
            logger.error(f"Permission error fetching market depth: {str(e)}")
            raise
        except (PocketfulAPIError, Exception) as e:
            logger.error(f"Error fetching market depth: {str(e)}")
            raise PocketfulAPIError(f"Error fetching market depth: {str(e)}")

    def _get_quotes_compact_noexcept(self, symbol: str, exchange: str) -> dict:
        """
        Get quotes using compact market data, but don't raise exceptions
        This is a helper method to safely get quote data for other functions
        """
        try:
            return self._get_quotes_compact(symbol, exchange)
        except Exception as e:
            logger.debug(f"Non-critical error getting compact data: {str(e)}")
            return {}

    def get_depth(self, symbol: str, exchange: str) -> dict:
        """Alias for get_market_depth to maintain compatibility with common API"""
        return self.get_market_depth(symbol, exchange)

    def get_multiquotes(self, symbols: list) -> list:
        """
        Get real-time quotes for multiple symbols using WebSocket
        Pocketful WebSocket supports subscribing to multiple instruments

        Args:
            symbols: List of dicts with 'symbol' and 'exchange' keys
                     Example: [{'symbol': 'SBIN', 'exchange': 'NSE'}, ...]
        Returns:
            list: List of quote data for each symbol with format:
                  [{'symbol': 'SBIN', 'exchange': 'NSE', 'data': {...}}, ...]
        """
        try:
            # Pocketful WebSocket can handle multiple instruments
            # Using batch size of 50 for practical response times
            BATCH_SIZE = 50

            if len(symbols) > BATCH_SIZE:
                logger.debug(f"Processing {len(symbols)} symbols in batches of {BATCH_SIZE}")
                all_results = []

                for i in range(0, len(symbols), BATCH_SIZE):
                    batch = symbols[i : i + BATCH_SIZE]
                    logger.info(
                        f"Processing batch {i // BATCH_SIZE + 1}: symbols {i + 1} to {min(i + BATCH_SIZE, len(symbols))}"
                    )

                    batch_results = self._process_multiquotes_batch(batch)
                    all_results.extend(batch_results)

                logger.debug(f"Successfully processed {len(all_results)} quotes")
                return all_results
            else:
                return self._process_multiquotes_batch(symbols)

        except Exception as e:
            logger.exception("Error fetching multiquotes")
            raise PocketfulAPIError(f"Error fetching multiquotes: {e}") from e

    def _process_multiquotes_batch(self, symbols: list) -> list:
        """
        Process a batch of symbols using WebSocket subscription
        Args:
            symbols: List of dicts with 'symbol' and 'exchange' keys
        Returns:
            list: List of quote data for the batch
        """
        results = []
        skipped_symbols = []
        instruments_to_subscribe = []
        symbol_map = {}  # Map instrument_token to original symbol/exchange

        # Ensure WebSocket connection is established
        try:
            if not self._ensure_websocket_connection():
                raise PocketfulAPIError("WebSocket connection not established")
        except Exception as e:
            logger.error(f"Failed to establish WebSocket connection: {str(e)}")
            # Return all symbols as errors
            for item in symbols:
                results.append(
                    {
                        "symbol": item["symbol"],
                        "exchange": item["exchange"],
                        "error": f"WebSocket connection failed: {str(e)}",
                    }
                )
            return results

        # Step 1: Prepare all instruments
        for item in symbols:
            symbol = item["symbol"]
            exchange = item["exchange"]

            try:
                br_symbol = get_br_symbol(symbol, exchange)

                # Get token from database
                with db_session() as session:
                    symbol_info = (
                        session.query(SymToken)
                        .filter(SymToken.exchange == exchange, SymToken.brsymbol == br_symbol)
                        .first()
                    )

                    if not symbol_info:
                        logger.warning(
                            f"Skipping symbol {symbol} on {exchange}: could not find token"
                        )
                        skipped_symbols.append(
                            {
                                "symbol": symbol,
                                "exchange": exchange,
                                "error": "Could not resolve token",
                            }
                        )
                        continue

                    instrument_token = int(symbol_info.token)

                # Map exchange to Pocketful exchange code
                if exchange == "NSE_INDEX":
                    exchange_code = self.exchange_map.get("NSE", 1)
                elif exchange == "BSE_INDEX":
                    exchange_code = self.exchange_map.get("BSE", 6)
                else:
                    exchange_code = self.exchange_map.get(exchange, 1)

                # Store instrument details for subscription
                instruments_to_subscribe.append(
                    {"exchangeCode": exchange_code, "instrumentToken": instrument_token}
                )

                # Store mapping for response processing
                symbol_map[str(instrument_token)] = {
                    "symbol": symbol,
                    "exchange": exchange,
                    "br_symbol": br_symbol,
                    "token": instrument_token,
                    "exchange_code": exchange_code,
                }

            except Exception as e:
                logger.warning(f"Skipping symbol {symbol} on {exchange}: {str(e)}")
                skipped_symbols.append({"symbol": symbol, "exchange": exchange, "error": str(e)})
                continue

        if not instruments_to_subscribe:
            logger.warning("No valid symbols to fetch quotes for")
            return skipped_symbols

        # Step 2: Hold a shared subscription for each instrument. Symbols held
        # by another request, or lingering from the last refresh, are already
        # streaming and answer at once.
        logger.debug(f"Holding {len(instruments_to_subscribe)} symbols via WebSocket")
        for instrument in instruments_to_subscribe:
            try:
                self.ws_connection.acquire_detailed(instrument)
            except Exception as e:
                logger.warning(f"Failed to subscribe to instrument {instrument}: {str(e)}")

        # Step 3: Collect each instrument's own tick. Ticks are stored per
        # symbol as they arrive, so none is lost to another symbol ticking.
        received_data = {}
        num_instruments = len(instruments_to_subscribe)
        max_wait_time = min(max(num_instruments * 0.5, 3), 15)
        start_time = time.time()
        try:
            while True:
                for instrument in instruments_to_subscribe:
                    token_str = str(instrument["instrumentToken"])
                    if token_str in received_data:
                        continue
                    detailed_data = self.ws_connection.read_detailed_for(instrument)
                    if detailed_data:
                        received_data[token_str] = detailed_data
                if len(received_data) >= num_instruments:
                    break
                if time.time() - start_time >= max_wait_time:
                    break
                time.sleep(QUOTE_POLL_SEC)
        finally:
            # Step 4: give the subscriptions back; they linger briefly so the
            # next refresh of the same symbols is immediate.
            for instrument in instruments_to_subscribe:
                try:
                    self.ws_connection.release_detailed(instrument)
                except Exception as e:
                    logger.warning(f"Failed to release instrument {instrument}: {str(e)}")

        logger.debug(
            f"Data collection completed: {len(received_data)}/{num_instruments} instruments received"
        )

        # Step 5: Build results from received data
        for token_str, info in symbol_map.items():
            detailed_data = received_data.get(token_str)

            if detailed_data:
                # Extract and format quote data from detailed market data
                # Note: Price values are multiplied by 100
                last_traded_price = (
                    detailed_data.get("last_traded_price", 0) / 100
                    if detailed_data.get("last_traded_price")
                    else 0
                )
                bid_price = (
                    detailed_data.get("best_bid_price", 0) / 100
                    if detailed_data.get("best_bid_price")
                    else 0
                )
                ask_price = (
                    detailed_data.get("best_ask_price", 0) / 100
                    if detailed_data.get("best_ask_price")
                    else 0
                )
                high_price = (
                    detailed_data.get("high_price", 0) / 100
                    if detailed_data.get("high_price")
                    else 0
                )
                low_price = (
                    detailed_data.get("low_price", 0) / 100 if detailed_data.get("low_price") else 0
                )
                open_price = (
                    detailed_data.get("open_price", 0) / 100
                    if detailed_data.get("open_price")
                    else 0
                )
                close_price = (
                    detailed_data.get("close_price", 0) / 100
                    if detailed_data.get("close_price")
                    else 0
                )
                volume = detailed_data.get("trade_volume", 0)

                results.append(
                    {
                        "symbol": info["symbol"],
                        "exchange": info["exchange"],
                        "data": {
                            "bid": bid_price,
                            "ask": ask_price,
                            "open": open_price,
                            "high": high_price,
                            "low": low_price,
                            "ltp": last_traded_price,
                            "prev_close": close_price,
                            "volume": volume,
                            "oi": detailed_data.get("currentOpenInterest", 0),
                        },
                    }
                )
            else:
                results.append(
                    {
                        "symbol": info["symbol"],
                        "exchange": info["exchange"],
                        "error": "No data received",
                    }
                )

        logger.debug(
            f"Retrieved quotes for {len([r for r in results if 'data' in r])}/{len(symbol_map)} symbols"
        )
        return skipped_symbols + results
