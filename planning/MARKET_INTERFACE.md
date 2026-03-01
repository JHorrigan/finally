# Market Data Interface

This document describes the unified market data system used in FinAlly. The system provides live stock prices to the SSE stream, portfolio valuation, and trade execution — regardless of whether prices come from the Massive API or the built-in simulator.

---

## Architecture Overview

```
Environment variable MASSIVE_API_KEY set?
    YES → MassiveDataSource   (polls Massive REST API every 15s)
    NO  → SimulatorDataSource (GBM simulation, ticks every 500ms)
              |
              ▼
         PriceCache  (thread-safe, in-memory, single source of truth)
              |
    ┌─────────┼──────────┐
    ▼         ▼          ▼
SSE stream  Portfolio  Trade
endpoint    valuation  execution
```

Both data sources implement the same `MarketDataSource` abstract base class. All downstream code reads from `PriceCache` — it never calls the data source directly.

---

## Module Structure

Located in `backend/app/market/`:

| Module | Contents |
|--------|----------|
| `models.py` | `PriceUpdate` dataclass |
| `cache.py` | `PriceCache` |
| `interface.py` | `MarketDataSource` ABC |
| `simulator.py` | `GBMSimulator`, `SimulatorDataSource` |
| `massive_client.py` | `MassiveDataSource` |
| `factory.py` | `create_market_data_source()` |
| `stream.py` | FastAPI SSE router factory |
| `seed_prices.py` | Seed prices and GBM parameters |

Public exports via `app/market/__init__.py`:

```python
from app.market import (
    PriceUpdate,
    PriceCache,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)
```

---

## Core Types

### `PriceUpdate`

Immutable frozen dataclass representing a single price tick.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float          # Unix seconds

    # Computed properties (not stored):
    @property
    def change(self) -> float: ...           # price - previous_price
    @property
    def change_percent(self) -> float: ...   # % change
    @property
    def direction(self) -> str: ...          # "up" | "down" | "flat"

    def to_dict(self) -> dict: ...           # JSON-serializable dict
```

`to_dict()` output (used by SSE stream and API responses):

```json
{
  "ticker": "AAPL",
  "price": 190.25,
  "previous_price": 190.10,
  "timestamp": 1705678234.567,
  "change": 0.15,
  "change_percent": 0.0789,
  "direction": "up"
}
```

---

### `PriceCache`

Thread-safe in-memory store. One writer (the active data source), many readers.

```python
class PriceCache:
    # Write
    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. Auto-computes direction from previous price."""

    # Read
    def get(self, ticker: str) -> PriceUpdate | None
    def get_price(self, ticker: str) -> float | None
    def get_all(self) -> dict[str, PriceUpdate]   # shallow copy

    # Remove
    def remove(self, ticker: str) -> None

    # Change detection (for SSE)
    @property
    def version(self) -> int   # increments on every update
```

**Thread safety**: Uses `threading.Lock`. Safe for reads from asyncio tasks and writes from background threads (used by `MassiveDataSource` via `asyncio.to_thread`).

**First update**: If a ticker has never been seen before, `previous_price == price` and `direction == "flat"`.

---

### `MarketDataSource` (Abstract Interface)

```python
from abc import ABC, abstractmethod

class MarketDataSource(ABC):

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Call exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker and purge it from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return currently tracked tickers."""
```

All methods except `get_tickers` are async. Implementations schedule their own background tasks internally.

---

## Factory

```python
# backend/app/market/factory.py

def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select data source based on MASSIVE_API_KEY environment variable.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource
    - Otherwise → SimulatorDataSource
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        return SimulatorDataSource(price_cache=price_cache)
```

The factory creates an **unstarted** source. The caller must call `await source.start(tickers)`.

---

## Implementations

### `SimulatorDataSource`

Default when `MASSIVE_API_KEY` is not set.

- Uses `GBMSimulator` to generate realistic correlated price movements
- Ticks every 500ms (configurable via `update_interval`)
- Runs as a pure asyncio background task — no threads, no external calls
- Seeds the cache immediately on `start()` so the SSE stream has data from the first request
- Adding a ticker seeds the cache immediately with an initial price

See `MARKET_SIMULATOR.md` for the full GBM math and implementation details.

### `MassiveDataSource`

Active when `MASSIVE_API_KEY` is set.

- Calls `GET /v2/snapshot/locale/us/markets/stocks/tickers` with all watched tickers in one request
- Extracts `snap.last_trade.price` and `snap.last_trade.sip_timestamp` from each snapshot
- Writes to `PriceCache` via `cache.update(ticker, price, timestamp)`
- Runs the synchronous `RESTClient` call in `asyncio.to_thread` to avoid blocking the event loop
- Polls every 15 seconds by default (free-tier safe: 5 calls/min = 1 call per 12s minimum)
- Does an immediate poll on `start()` so the cache is populated before the SSE stream opens
- On poll failure (network error, 401, 429), logs the error and retries on the next interval

```python
class MassiveDataSource(MarketDataSource):
    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,   # seconds between polls
    ) -> None: ...
```

---

## SSE Stream

```python
from app.market import create_stream_router, PriceCache

router = create_stream_router(price_cache)
# Mounts at: GET /api/stream/prices
# Media type: text/event-stream
```

Event format (sent every 500ms when the cache version has changed):

```
retry: 1000

data: {"AAPL": {...PriceUpdate.to_dict()...}, "MSFT": {...}, ...}

data: {"AAPL": {...}, "MSFT": {...}, ...}
```

The SSE generator uses version-based change detection: it only emits an event when `cache.version` changes since the last emission. This means:
- Simulator: emits every 500ms (version increments every tick)
- Massive: emits once per poll cycle (every 15s), then holds until next poll

Clients use the native `EventSource` API and rely on the `retry: 1000` directive for automatic reconnection.

---

## Application Lifecycle

In FastAPI, the data source is started on application startup and stopped on shutdown:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, create_market_data_source, create_stream_router

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]

@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    source = create_market_data_source(cache)
    await source.start(DEFAULT_TICKERS)     # begins background task
    app.state.price_cache = cache
    app.state.market_source = source
    yield
    await source.stop()                     # clean shutdown

app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(cache))
```

---

## Dynamic Watchlist Integration

When the user adds or removes a ticker via the watchlist API, the route handler calls the data source:

```python
# Add ticker
await request.app.state.market_source.add_ticker("PYPL")

# Remove ticker
await request.app.state.market_source.remove_ticker("NFLX")
```

Both sources handle this correctly:
- `SimulatorDataSource.add_ticker`: adds to GBM simulator and seeds cache immediately
- `MassiveDataSource.add_ticker`: adds to ticker list; new ticker appears on next poll
- Both `remove_ticker` implementations call `cache.remove(ticker)` to purge from SSE

---

## Reading Prices (Downstream Code)

```python
# In trade execution or portfolio valuation:
cache: PriceCache = request.app.state.price_cache

price = cache.get_price("AAPL")           # float | None
update = cache.get("AAPL")               # PriceUpdate | None
all_prices = cache.get_all()             # dict[str, PriceUpdate]

if update:
    print(f"{update.ticker}: ${update.price} ({update.direction})")
```

**Important**: Always get prices from the cache at execution time, not from a snapshot taken earlier in the request. This ensures trade prices match the most current data.

---

## Environment Variables

| Variable | Effect |
|----------|--------|
| `MASSIVE_API_KEY` | If set and non-empty, uses `MassiveDataSource` with this key |
| `LLM_MOCK` | Unrelated to market data; used by the LLM layer |

No other configuration is required. The factory selects the source automatically.
