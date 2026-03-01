# Massive API Reference

Massive (formerly Polygon.io, rebranded October 2025) is a financial market data platform providing REST and WebSocket APIs for stocks, options, forex, indices, and crypto.

**Official docs**: https://massive.com/docs
**Python package**: `massive` (PyPI), formerly `polygon-api-client`
**GitHub**: https://github.com/massive-com/client-python

---

## Authentication

All requests require an API key passed as a Bearer token:

```
Authorization: Bearer <MASSIVE_API_KEY>
```

The Python client reads the key from the `MASSIVE_API_KEY` environment variable automatically, or it can be passed explicitly.

---

## Base URL

```
https://api.massive.com
```

The legacy `https://api.polygon.io` base URL remains functional for backwards compatibility.

---

## Python Client

### Installation

```bash
uv add massive
# or: pip install massive
```

### Initialisation

```python
from massive import RESTClient

# Reads MASSIVE_API_KEY from environment automatically
client = RESTClient()

# Or pass explicitly
client = RESTClient(api_key="your-key-here")
```

### Constructor parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `api_key` | env `MASSIVE_API_KEY` | API key |
| `connect_timeout` | `10.0` | TCP connect timeout (seconds) |
| `read_timeout` | `10.0` | HTTP read timeout (seconds) |
| `retries` | `3` | Retry count on transient failures |
| `base` | `https://api.massive.com` | Override base URL |
| `pagination` | `True` | Auto-paginate list endpoints |
| `verbose` | `False` | Log request details |
| `trace` | `False` | Log full request/response |

---

## Rate Limits

| Tier | Calls/minute | Recommended poll interval |
|------|-------------|--------------------------|
| Free | 5 | 15 seconds (minimum safe) |
| Starter | ~unlimited for snapshot | 5 seconds |
| Developer | ~unlimited for snapshot | 2 seconds |
| Advanced | ~unlimited for snapshot | 1 second |

The free tier allows 5 API calls per minute. The multi-ticker snapshot counts as 1 call regardless of how many tickers are requested, making it very efficient.

**Note**: The Massive API is synchronous (blocking HTTP via `urllib3`). Run it in a thread (`asyncio.to_thread`) to avoid blocking an asyncio event loop.

---

## Key Endpoints

### 1. Multi-Ticker Snapshot (Primary)

The most important endpoint for this project. Returns the latest price data for multiple tickers in a single API call.

**Method**: `GET`
**Path**: `/v2/snapshot/locale/us/markets/stocks/tickers`

**Query parameters**:

| Parameter | Type | Description |
|-----------|------|-------------|
| `tickers` | comma-separated string | Tickers to fetch (e.g. `AAPL,MSFT,GOOGL`). Omit to get all tickers. |
| `include_otc` | boolean | Include OTC securities (default: `false`) |

**Python SDK**:

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="your-key")

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "MSFT", "GOOGL", "TSLA"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price:.2f}")
```

**Raw HTTP example**:

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT
Authorization: Bearer your-key
```

**Example response**:

```json
{
  "count": 2,
  "status": "OK",
  "tickers": [
    {
      "ticker": "AAPL",
      "day": {
        "o": 189.10,
        "h": 191.50,
        "l": 188.40,
        "c": 190.25,
        "v": 48234100,
        "vw": 190.01
      },
      "prevDay": {
        "o": 187.80,
        "h": 190.00,
        "l": 187.10,
        "c": 188.90,
        "v": 52100000,
        "vw": 188.70
      },
      "min": {
        "av": 48234100,
        "o": 190.10,
        "h": 190.30,
        "l": 190.00,
        "c": 190.25,
        "v": 124800,
        "vw": 190.18,
        "t": 1705678200000
      },
      "lastTrade": {
        "T": "AAPL",
        "t": 1705678234567890000,
        "p": 190.25,
        "s": 100,
        "q": 123456789,
        "c": [14, 41],
        "x": 4
      },
      "todaysChange": 1.35,
      "todaysChangePerc": 0.715,
      "updated": 1705678234567890000
    }
  ]
}
```

**Python `TickerSnapshot` object fields**:

| Attribute | Type | Description |
|-----------|------|-------------|
| `ticker` | `str` | Ticker symbol |
| `day` | `Agg` | Current day's OHLCV bar |
| `prev_day` | `Agg` | Previous day's OHLCV bar |
| `min` | `MinuteSnapshot` | Most recent 1-minute bar |
| `last_trade` | `LastTrade` | Most recent trade |
| `last_quote` | `LastQuote` | Most recent bid/ask quote |
| `todays_change` | `float` | Absolute price change from previous close |
| `todays_change_percent` | `float` | Percentage change from previous close |
| `updated` | `int` | Last update (Unix nanoseconds) |
| `fair_market_value` | `float` | Business plan only |

**`LastTrade` fields** (from snapshot's `lastTrade` JSON object):

| JSON key | Python attribute | Type | Description |
|----------|-----------------|------|-------------|
| `p` | `price` | `float` | Trade price |
| `s` | `size` | `float` | Trade size (shares) |
| `t` | `sip_timestamp` | `int` | SIP timestamp (Unix **nanoseconds**) |
| `y` | `participant_timestamp` | `int` | Exchange timestamp (nanoseconds) |
| `T` | `ticker` | `str` | Ticker symbol |
| `q` | `sequence_number` | `float` | Sequence number |
| `c` | `conditions` | `list[int]` | Trade condition codes |
| `x` | `exchange` | `int` | Exchange ID |
| `z` | `tape` | `int` | Tape (A/B/C) |

**Important**: `sip_timestamp` is Unix **nanoseconds**. Convert to seconds: `sip_timestamp / 1_000_000_000`.

**`Agg` fields** (used for `day` and `prev_day`):

| JSON key | Python attribute | Type | Description |
|----------|-----------------|------|-------------|
| `o` | `open` | `float` | Opening price |
| `h` | `high` | `float` | High price |
| `l` | `low` | `float` | Low price |
| `c` | `close` | `float` | Closing price |
| `v` | `volume` | `float` | Volume |
| `vw` | `vwap` | `float` | Volume-weighted average price |
| `t` | `timestamp` | `int` | Bar timestamp (Unix milliseconds) |
| `n` | `transactions` | `int` | Number of transactions |

---

### 2. Single Ticker Snapshot

**Method**: `GET`
**Path**: `/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}`

Returns the same `TickerSnapshot` object as above, for a single ticker.

```python
snap = client.get_snapshot_ticker(
    market_type=SnapshotMarketType.STOCKS,
    ticker="AAPL",
)
print(f"AAPL last trade: ${snap.last_trade.price:.2f}")
print(f"AAPL day change: {snap.todays_change_percent:.2f}%")
```

---

### 3. Previous Day OHLCV

**Method**: `GET`
**Path**: `/v2/aggs/ticker/{ticker}/prev`

Returns the previous trading day's open, high, low, close, and volume.

**Python SDK**:

```python
# Returns PreviousCloseAgg
prev = client.get_previous_close_agg("AAPL")
print(f"AAPL previous close: ${prev.close:.2f}")
print(f"AAPL previous volume: {prev.volume:,.0f}")
```

**`PreviousCloseAgg` fields**:

| JSON key | Python attribute | Description |
|----------|-----------------|-------------|
| `T` | `ticker` | Ticker symbol |
| `o` | `open` | Open price |
| `h` | `high` | High price |
| `l` | `low` | Low price |
| `c` | `close` | Close price |
| `v` | `volume` | Volume |
| `vw` | `vwap` | VWAP |
| `t` | `timestamp` | Bar timestamp (Unix milliseconds) |

**Example response**:

```json
{
  "status": "OK",
  "ticker": "AAPL",
  "results": [{
    "T": "AAPL",
    "o": 187.80,
    "h": 190.00,
    "l": 187.10,
    "c": 188.90,
    "v": 52100000,
    "vw": 188.70,
    "t": 1705622400000
  }]
}
```

---

### 4. Last Trade

**Method**: `GET`
**Path**: `/v1/last/stocks/{ticker}`

Returns the most recent trade for a single ticker.

```python
trade = client.get_last_trade(ticker="AAPL")
print(f"Price: ${trade.price:.2f}, Size: {trade.size}")
# trade.sip_timestamp is Unix nanoseconds
ts_seconds = trade.sip_timestamp / 1_000_000_000
```

---

### 5. Aggregate Bars (Historical)

**Method**: `GET`
**Path**: `/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`

Fetch OHLCV bars for a time range. Useful for building historical charts.

```python
from massive import RESTClient

client = RESTClient()
aggs = []
for bar in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",         # "minute", "hour", "day", "week", "month"
    from_="2024-01-01",
    to="2024-12-31",
    limit=50000,
):
    aggs.append(bar)

# Each bar: bar.open, bar.high, bar.low, bar.close, bar.volume,
#           bar.vwap, bar.timestamp (Unix ms), bar.transactions
```

---

### 6. WebSocket (Real-time Streaming)

For real-time trade and quote data without polling. Requires a paid plan.

```python
from massive import WebSocketClient
from massive.websocket.models import WebSocketMessage
from typing import List

ws = WebSocketClient(
    api_key="your-key",
    subscriptions=["T.AAPL", "T.MSFT", "T.GOOGL"],  # T.* = all trades
)

def handle_msg(msgs: List[WebSocketMessage]):
    for m in msgs:
        print(m)

ws.run(handle_msg=handle_msg)
```

**Subscription format**: `{channel}.{ticker}`
- `T.AAPL` — trades for AAPL
- `Q.AAPL` — quotes for AAPL
- `T.*` — all trades (paid tiers)

**Note**: WebSocket is not used in this project. We use REST polling via `MassiveDataSource` for simplicity and free-tier compatibility.

---

## Error Handling

The client raises exceptions for API errors:

```python
from massive.exceptions import AuthError, BadResponse

try:
    snapshots = client.get_snapshot_all(
        market_type=SnapshotMarketType.STOCKS,
        tickers=["AAPL"],
    )
except AuthError:
    # Invalid or missing API key (401)
    pass
except BadResponse as e:
    # 4xx/5xx responses; e.g. 429 rate limit exceeded
    pass
except Exception as e:
    # Network errors, timeouts, etc.
    pass
```

The `RESTClient` constructor retries transient failures up to `retries` times (default 3) with backoff.

---

## Data Availability Notes

- **Market hours**: Snapshot data is cleared at midnight EST and repopulates from ~4am EST (pre-market)
- **Free tier**: Latest trade/quote data available; minute and day bars included
- **After-hours**: `lastTrade` reflects the most recent trade including extended hours
- **Snapshot vs aggregate**: The snapshot endpoint (`/v2/snapshot/...`) returns real-time last-trade data; aggregate endpoints (`/v2/aggs/...`) return historical OHLCV bars
- **Price field to use**: Use `snap.last_trade.price` for the current traded price. `snap.day.close` is the last intraday close (may lag). `snap.prev_day.close` is the previous day's closing price.

---

## Complete Usage Example

```python
import asyncio
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]


def fetch_prices(api_key: str) -> dict[str, float]:
    """Fetch latest prices for all watchlist tickers in one API call."""
    client = RESTClient(api_key=api_key)
    snapshots = client.get_snapshot_all(
        market_type=SnapshotMarketType.STOCKS,
        tickers=TICKERS,
    )
    prices = {}
    for snap in snapshots:
        if snap.last_trade and snap.last_trade.price:
            prices[snap.ticker] = snap.last_trade.price
    return prices


async def poll_prices(api_key: str, interval: float = 15.0) -> None:
    """Poll prices every interval seconds (async-safe via thread)."""
    while True:
        prices = await asyncio.to_thread(fetch_prices, api_key)
        for ticker, price in prices.items():
            print(f"{ticker}: ${price:.2f}")
        await asyncio.sleep(interval)
```
