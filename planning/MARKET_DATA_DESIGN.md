# Market Data Backend — Design Document

Implementation-ready design for the FinAlly market data subsystem. This document covers the unified interface, in-memory price cache, GBM simulator, Massive API client, SSE streaming endpoint, and FastAPI lifecycle integration. All code lives under `backend/app/market/`.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model)
4. [Price Cache — `cache.py`](#4-price-cache)
5. [Abstract Interface — `interface.py`](#5-abstract-interface)
6. [Seed Prices & Ticker Parameters — `seed_prices.py`](#6-seed-prices--ticker-parameters)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client)
9. [Factory — `factory.py`](#9-factory)
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint)
11. [Public API — `__init__.py`](#11-public-api)
12. [FastAPI Lifecycle Integration](#12-fastapi-lifecycle-integration)
13. [Watchlist Coordination](#13-watchlist-coordination)
14. [Testing Strategy](#14-testing-strategy)
15. [Error Handling & Edge Cases](#15-error-handling--edge-cases)
16. [Configuration Summary](#16-configuration-summary)

---

## 1. Architecture Overview

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

Both data sources implement the same `MarketDataSource` abstract base class. All downstream code reads from `PriceCache` — it never calls the data source directly for prices. The data source only writes; the cache owns reads.

**Key design properties:**
- **One writer, many readers**: Only one data source is active at a time; all consumers read from the shared cache
- **Strategy pattern**: Swapping simulator ↔ Massive requires only a different class at construction time; all downstream code is unchanged
- **Version-based change detection**: SSE stream only pushes events when the cache version increments, avoiding redundant payloads

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py         # Public exports
      models.py           # PriceUpdate dataclass
      cache.py            # PriceCache (thread-safe in-memory store)
      interface.py        # MarketDataSource ABC
      seed_prices.py      # SEED_PRICES, TICKER_PARAMS, CORRELATION_GROUPS
      simulator.py        # GBMSimulator + SimulatorDataSource
      massive_client.py   # MassiveDataSource
      factory.py          # create_market_data_source()
      stream.py           # FastAPI SSE router factory
```

---

## 3. Data Model

**`backend/app/market/models.py`**

The `PriceUpdate` dataclass is the single unit of data flowing through the system. It is immutable (`frozen=True`) and slot-optimized for memory efficiency since many instances are created per second.

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

**Example `to_dict()` output** (what the SSE stream and API send to the frontend):

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

**Design notes:**
- `frozen=True` prevents mutation — once created, a `PriceUpdate` is immutable. Updates are represented as new instances.
- `slots=True` reduces memory overhead by ~40% compared to a regular dataclass, which matters since one is created per ticker per tick.
- `timestamp` defaults to `time.time()` at construction, but the Massive client overrides it with the exchange timestamp from the API response.
- `price` and `previous_price` are rounded to 2 decimal places by the cache before being stored; the `change` and `change_percent` properties apply additional rounding for display.

---

## 4. Price Cache

**`backend/app/market/cache.py`**

Thread-safe in-memory store. One writer (the active data source) and many readers (SSE streaming, portfolio valuation, trade execution). Uses `threading.Lock` for correctness when the Massive API client calls from `asyncio.to_thread`.

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker."""

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        If this is the first update for the ticker, previous_price == price (direction='flat').
        """
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache (e.g., when removed from watchlist)."""
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Current version counter. Increments on every update call."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

**Usage examples:**

```python
cache = PriceCache()

# Writing (done by the data source)
update = cache.update("AAPL", 190.25)
# update.direction == "flat"  (first time — no previous price)

update = cache.update("AAPL", 190.50)
# update.direction == "up"
# update.change == 0.25
# update.change_percent == 0.1314

# Reading (done by SSE, portfolio, trade execution)
price = cache.get_price("AAPL")          # 190.50
update = cache.get("AAPL")              # PriceUpdate(...)
all_prices = cache.get_all()            # {"AAPL": PriceUpdate(...), ...}

# Version-based change detection (used by SSE stream)
v = cache.version                        # 2 (incremented twice above)

# Removal (when user removes ticker from watchlist)
cache.remove("AAPL")
```

**Thread safety:** All write operations (`update`, `remove`) and read operations (`get`, `get_all`, `__contains__`, `__len__`) acquire `self._lock`. This ensures that the Massive API client calling `cache.update()` from a worker thread (via `asyncio.to_thread`) cannot race with SSE readers running in the asyncio event loop.

The `version` property reads `self._version` without acquiring the lock. On CPython with the GIL, reading an `int` is atomic, so no data corruption is possible. The SSE stream uses `version` for "has anything changed?" polling — a momentarily stale read is acceptable (it delays an SSE push by at most one 500ms cycle).

---

## 5. Abstract Interface

**`backend/app/market/interface.py`**

The `MarketDataSource` ABC defines the contract that both the simulator and the Massive client must fulfill. All downstream code that manages ticker subscriptions (the watchlist API) depends on this interface, not on concrete implementations.

```python
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # ... app runs ...
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        # ... app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to PriceCache.
        Call exactly once. Calling start() twice is undefined behavior.
        Seeds the cache with initial prices before returning.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times. After stop(), the source will not
        write to the cache again.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.

        The next update cycle will include this ticker.
        Implementations should seed the cache immediately if possible.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.

        Also removes the ticker from the PriceCache immediately.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

**Interface contract summary:**

| Method | Async | Side effects |
|--------|-------|-------------|
| `start(tickers)` | yes | Seeds cache, starts background task |
| `stop()` | yes | Cancels background task |
| `add_ticker(ticker)` | yes | Adds to active set, seeds cache if possible |
| `remove_ticker(ticker)` | yes | Removes from active set, removes from cache |
| `get_tickers()` | no | Read-only |

---

## 6. Seed Prices & Ticker Parameters

**`backend/app/market/seed_prices.py`**

Defines realistic starting prices and per-ticker GBM parameters for the 10 default watchlist tickers. Also defines the correlation structure used by the Cholesky decomposition.

```python
# Realistic starting prices for the default watchlist
SEED_PRICES: dict[str, float] = {
    "AAPL":  190.00,
    "GOOGL": 175.00,
    "MSFT":  420.00,
    "AMZN":  185.00,
    "TSLA":  250.00,
    "NVDA":  800.00,
    "META":  500.00,
    "JPM":   195.00,
    "V":     280.00,
    "NFLX":  600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility  mu: annualized drift (expected return)
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},   # Stable large-cap
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},   # Lowest volatility tech
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # Very high volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High vol, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Lowest vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Default for tickers dynamically added to the watchlist
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for the Cholesky decomposition
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR    = 0.6   # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR   = 0.3   # Between sectors / unknown tickers
TSLA_CORR          = 0.3   # TSLA does its own thing (overrides tech group)
```

**Adding a new default ticker:**

1. Add to `SEED_PRICES` with a realistic starting price
2. Add to `TICKER_PARAMS` with appropriate `sigma` and `mu`
3. Optionally add to a `CORRELATION_GROUPS` set if it belongs to an existing sector

**Dynamically added tickers** (not in `SEED_PRICES`) start at a random price between $50 and $300 with `DEFAULT_PARAMS`.

**Volatility reference guide:**

| Annualized sigma | Typical stocks |
|-----------------|----------------|
| 0.15–0.20 | Utilities, banks, payments (V, JPM) |
| 0.20–0.30 | Large-cap tech (AAPL, MSFT, GOOGL) |
| 0.30–0.40 | Growth tech (META, AMZN, NFLX) |
| 0.40–0.60 | High-growth / spec (NVDA, TSLA) |
| 0.80–1.50 | Crypto-like |

---

## 7. GBM Simulator

**`backend/app/market/simulator.py`**

The simulator has two classes:
- `GBMSimulator` — pure math, no async, no I/O
- `SimulatorDataSource` — wraps `GBMSimulator` in the `MarketDataSource` interface

### 7.1 GBM Mathematics

The core formula for each time step:

```
S(t + dt) = S(t) * exp((mu - sigma²/2) * dt + sigma * sqrt(dt) * Z)
```

Where:

| Symbol | Meaning |
|--------|---------|
| `S(t)` | Current price |
| `mu` | Annualized drift (e.g., 0.05 = 5%/year) |
| `sigma` | Annualized volatility (e.g., 0.25 = 25%/year) |
| `dt` | Time step as a fraction of one trading year |
| `Z` | Standard normal random variable (correlated) |

The `(mu - sigma²/2)` term is the Itô correction. Without it, the expected price would drift upward faster than `mu` due to the convexity of the exponential (Jensen's inequality).

**Time step calculation:**

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800 seconds
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8
```

With `sigma = 0.25` and this `dt`, each tick moves prices by:

```
sigma * sqrt(dt) ≈ 0.25 * sqrt(8.48e-8) ≈ 0.0073% per tick
```

On a $190 stock, that's ~$0.014 per tick — sub-cent moves that accumulate naturally, matching real streaming market data feel.

### 7.2 Correlated Moves

Real stocks don't move independently. The simulator models sector-based correlation using Cholesky decomposition.

**Algorithm:**

1. Build an `n×n` symmetric correlation matrix `C` (1s on diagonal, pairwise correlations off-diagonal)
2. Compute `L = cholesky(C)` (lower triangular)
3. Per tick: generate `n` independent standard normals `Z_ind`, then compute `Z_corr = L @ Z_ind`
4. Feed `Z_corr[i]` into the GBM formula for ticker `i`

```python
def _rebuild_cholesky(self) -> None:
    n = len(self._tickers)
    if n <= 1:
        self._cholesky = None
        return

    corr = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
            corr[i, j] = rho
            corr[j, i] = rho

    self._cholesky = np.linalg.cholesky(corr)

@staticmethod
def _pairwise_correlation(t1: str, t2: str) -> float:
    tech = CORRELATION_GROUPS["tech"]
    finance = CORRELATION_GROUPS["finance"]

    if t1 == "TSLA" or t2 == "TSLA":
        return TSLA_CORR             # 0.3 — TSLA does its own thing

    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR       # 0.6

    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR    # 0.5

    return CROSS_GROUP_CORR          # 0.3
```

The Cholesky matrix is rebuilt (`O(n²)`) whenever tickers are added or removed. With at most ~50 tickers, this takes microseconds.

### 7.3 Random Shock Events

Every tick, each ticker has a small probability of a sudden large move:

```python
event_probability = 0.001   # 0.1% per tick per ticker

if random.random() < event_probability:
    shock_magnitude = random.uniform(0.02, 0.05)  # 2-5% jump
    shock_sign = random.choice([-1, 1])
    self._prices[ticker] *= (1 + shock_magnitude * shock_sign)
```

With 10 tickers at 2 ticks/second: `10 × 2 × 0.001 = 0.02 events/s` → ~1 shock every 50 seconds. This creates occasional dramatic moves that make the UI feel alive.

### 7.4 `GBMSimulator` Class

```python
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability

        # Per-ticker state
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance all tickers by one dt. Returns {ticker: new_price}. Hot path."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock event
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1 + shock)

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add ticker, seed price and params, rebuild Cholesky."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove ticker and rebuild Cholesky."""
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add without rebuilding Cholesky (for batch initialization)."""
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))
```

### 7.5 `SimulatorDataSource` Class

Wraps `GBMSimulator` in the `MarketDataSource` interface. Runs the step loop as an asyncio background task.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,       # seconds between ticks
        event_probability: float = 0.001,   # shock probability per tick
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)

        # Seed the cache so SSE has data before the first tick fires
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)

        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)  # seed immediately

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)  # purge from SSE stream immediately

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        """Core loop: step → write cache → sleep."""
        while True:
            try:
                if self._sim:
                    prices = self._sim.step()
                    for ticker, price in prices.items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

**Loop timeline:**

```
t=0.0s  start() seeds cache, creates task
t=0.5s  _run_loop: step() → update cache (version increments for each ticker)
t=1.0s  _run_loop: step() → update cache
t=1.5s  ...
```

---

## 8. Massive API Client

**`backend/app/market/massive_client.py`**

The Massive client polls the Massive (Polygon.io) REST API for all watched tickers in a single request per cycle. It uses `asyncio.to_thread` to run the synchronous HTTP client without blocking the event loop.

```python
import asyncio
import logging

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive REST API.

    Polls /v2/snapshot/locale/us/markets/stocks/tickers for all tickers in
    one API call. Free-tier safe at 15s interval (5 calls/min limit).
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,   # seconds; free-tier safe
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)

        # Immediate first poll so cache has data before SSE stream opens
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(tickers), self._interval,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            # New ticker will appear in cache on the next poll cycle (~15s)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)  # Remove from SSE stream immediately

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        """Poll every interval seconds. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Fetch one batch of snapshots and write to cache."""
        if not self._tickers or not self._client:
            return

        try:
            # RESTClient is synchronous — run in thread to avoid blocking event loop
            snapshots = await asyncio.to_thread(self._fetch_snapshots)

            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    # Massive last_trade.timestamp is Unix milliseconds → convert to seconds
                    timestamp = snap.last_trade.timestamp / 1000.0
                    self._cache.update(
                        ticker=snap.ticker,
                        price=price,
                        timestamp=timestamp,
                    )
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s",
                        getattr(snap, "ticker", "???"), e,
                    )

            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — the loop will retry on the next interval
            # Common failures: 401 (bad key), 429 (rate limit), network errors

    def _fetch_snapshots(self) -> list:
        """Synchronous HTTP call. Called via asyncio.to_thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

**API response mapping:**

| Snapshot attribute | Python attribute | Notes |
|-------------------|-----------------|-------|
| `snap.ticker` | `str` | Ticker symbol |
| `snap.last_trade.price` | `float` | Current traded price — use this |
| `snap.last_trade.timestamp` | `int` | Unix **milliseconds** → divide by 1000 |
| `snap.day.close` | `float` | Intraday close (may lag last_trade) |
| `snap.prev_day.close` | `float` | Previous day's close |
| `snap.todays_change_percent` | `float` | Daily % change from prev close |

**Rate limit behavior:**

| Tier | Calls/min | Safe poll interval |
|------|-----------|--------------------|
| Free | 5 | 15 seconds (default) |
| Starter | ~unlimited | 5 seconds |
| Developer | ~unlimited | 2 seconds |

Override with: `MassiveDataSource(api_key=key, price_cache=cache, poll_interval=5.0)`

**Error handling:** All exceptions in `_poll_once` are caught and logged. The loop continues on the next interval. Common failures:
- `401` — invalid or expired API key
- `429` — rate limit exceeded (reduce `poll_interval`)
- Network timeout — transient; will self-recover

---

## 9. Factory

**`backend/app/market/factory.py`**

Selects the appropriate data source based on the `MASSIVE_API_KEY` environment variable.

```python
import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select data source based on MASSIVE_API_KEY environment variable.

    Returns an unstarted source — caller must await source.start(tickers).

    Selection logic:
      - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real data)
      - Otherwise → SimulatorDataSource (GBM simulation, default)
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

**Usage:**

```python
cache = PriceCache()
source = create_market_data_source(cache)  # unstarted

# Must call start before the source does anything
await source.start(["AAPL", "GOOGL", "MSFT", ...])
```

---

## 10. SSE Streaming Endpoint

**`backend/app/market/stream.py`**

FastAPI router that serves the `GET /api/stream/prices` SSE endpoint. Uses a factory function to inject the `PriceCache` without relying on global state.

```python
import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE router bound to a specific PriceCache instance."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint. Client connects with native EventSource API.

        Event format:
            retry: 1000

            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, "MSFT": {...}}

            data: {"AAPL": {...}, "MSFT": {...}}
        """
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Prevent nginx buffering if proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Async generator yielding SSE-formatted price events.

    Change detection: Only emits an event when cache.version changes since
    the last emission. This means:
      - Simulator: emits every 500ms (version increments every tick)
      - Massive: emits once per poll cycle (~every 15s), then holds
    """
    yield "retry: 1000\n\n"  # Browser auto-reconnects after 1 second on disconnect

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()

                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

**SSE wire format** (what the browser EventSource receives):

```
retry: 1000\r\n
\r\n
data: {"AAPL":{"ticker":"AAPL","price":190.25,...},"MSFT":{...}}\r\n
\r\n
data: {"AAPL":{"ticker":"AAPL","price":190.38,...},"MSFT":{...}}\r\n
\r\n
```

**Frontend consumption:**

```typescript
const es = new EventSource('/api/stream/prices');

es.onmessage = (event) => {
  const prices: Record<string, PriceUpdate> = JSON.parse(event.data);
  for (const [ticker, update] of Object.entries(prices)) {
    // update sparkline buffer, flash price cell, update state
  }
};

es.onerror = () => {
  // EventSource automatically reconnects after `retry` ms (1000ms)
  // No manual reconnect logic needed
};
```

**Key properties:**
- `retry: 1000` — instructs the browser to reconnect 1 second after any disconnect
- `X-Accel-Buffering: no` — prevents nginx from buffering SSE events (common pitfall with proxied deployments)
- `Cache-Control: no-cache` — prevents any intermediate caching of the stream
- Version-based change detection prevents redundant payloads when the cache hasn't changed

---

## 11. Public API

**`backend/app/market/__init__.py`**

Everything downstream code needs is re-exported from the package root:

```python
"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate              - Immutable price snapshot dataclass
    PriceCache               - Thread-safe in-memory price store
    MarketDataSource         - Abstract interface for data providers
    create_market_data_source - Factory: selects simulator or Massive
    create_stream_router     - FastAPI router factory for SSE endpoint
"""

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
]
```

**Import pattern for the rest of the backend:**

```python
from app.market import PriceCache, create_market_data_source, create_stream_router
```

---

## 12. FastAPI Lifecycle Integration

The data source is started on application startup and stopped cleanly on shutdown using FastAPI's `lifespan` context manager.

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, create_market_data_source, create_stream_router

DEFAULT_TICKERS = [
    "AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
    "NVDA", "META", "JPM", "V", "NFLX",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    cache = PriceCache()
    source = create_market_data_source(cache)
    await source.start(DEFAULT_TICKERS)   # seeds cache, starts background task

    app.state.price_cache = cache
    app.state.market_source = source

    yield  # App is running

    # Shutdown
    await source.stop()                   # cancels background task cleanly


app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(app.state.price_cache))
```

**Note on router inclusion timing:** `create_stream_router` must be called after startup completes and `app.state.price_cache` is set. In practice, include the router inside the lifespan after the cache is created, or pass the cache explicitly:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    source = create_market_data_source(cache)
    await source.start(DEFAULT_TICKERS)

    app.state.price_cache = cache
    app.state.market_source = source

    # Include the SSE router after cache exists
    app.include_router(create_stream_router(cache))

    yield

    await source.stop()
```

**Accessing shared state in route handlers:**

```python
from fastapi import Request

@router.get("/api/watchlist")
async def get_watchlist(request: Request):
    cache: PriceCache = request.app.state.price_cache
    source = request.app.state.market_source

    tickers = source.get_tickers()
    return [
        {"ticker": t, "price": cache.get_price(t)}
        for t in tickers
    ]
```

---

## 13. Watchlist Coordination

When the user adds or removes a ticker via the watchlist API, the route handler notifies the data source and the cache is kept in sync.

```python
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class AddTickerRequest(BaseModel):
    ticker: str


@router.post("")
async def add_ticker(body: AddTickerRequest, request: Request):
    ticker = body.ticker.upper().strip()
    source = request.app.state.market_source

    await source.add_ticker(ticker)
    # SimulatorDataSource: seeds cache immediately, ticker in SSE within 500ms
    # MassiveDataSource: adds to poll list, appears in SSE on next poll (~15s)

    return {"ticker": ticker, "status": "added"}


@router.delete("/{ticker}")
async def remove_ticker(ticker: str, request: Request):
    ticker = ticker.upper().strip()
    source = request.app.state.market_source

    await source.remove_ticker(ticker)
    # Both sources: remove from cache immediately, disappears from SSE next event

    return {"ticker": ticker, "status": "removed"}
```

**Behavior summary:**

| Action | SimulatorDataSource | MassiveDataSource |
|--------|--------------------|--------------------|
| `add_ticker` | Seeds cache immediately; in SSE within 500ms | Added to poll list; appears on next poll (~15s) |
| `remove_ticker` | Removed from GBM state + cache immediately | Removed from poll list + cache immediately |

---

## 14. Testing Strategy

### Unit Tests

**`backend/tests/market/`**

#### `test_models.py` — `PriceUpdate`
```python
def test_direction_up():
    u = PriceUpdate(ticker="AAPL", price=100.0, previous_price=99.0, timestamp=0.0)
    assert u.direction == "up"
    assert u.change == 1.0
    assert u.change_percent == pytest.approx(1.0101, rel=1e-3)

def test_direction_flat_on_first_update():
    u = PriceUpdate(ticker="AAPL", price=100.0, previous_price=100.0, timestamp=0.0)
    assert u.direction == "flat"
    assert u.change == 0.0

def test_to_dict_keys():
    u = PriceUpdate(ticker="AAPL", price=100.0, previous_price=99.0, timestamp=1234.5)
    d = u.to_dict()
    assert set(d.keys()) == {"ticker", "price", "previous_price", "timestamp",
                              "change", "change_percent", "direction"}
```

#### `test_cache.py` — `PriceCache`
```python
def test_first_update_is_flat():
    cache = PriceCache()
    update = cache.update("AAPL", 190.0)
    assert update.direction == "flat"
    assert update.previous_price == 190.0

def test_second_update_tracks_previous():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    update = cache.update("AAPL", 191.0)
    assert update.previous_price == 190.0
    assert update.direction == "up"

def test_version_increments():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 100.0)
    assert cache.version == v0 + 1

def test_remove_purges_ticker():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    cache.remove("AAPL")
    assert cache.get("AAPL") is None
    assert "AAPL" not in cache

def test_get_all_returns_copy():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    snapshot = cache.get_all()
    cache.update("AAPL", 191.0)  # mutate after snapshot
    assert snapshot["AAPL"].price == 190.0  # snapshot is unaffected
```

#### `test_simulator.py` — `GBMSimulator`
```python
def test_step_returns_all_tickers():
    sim = GBMSimulator(["AAPL", "GOOGL", "MSFT"])
    result = sim.step()
    assert set(result.keys()) == {"AAPL", "GOOGL", "MSFT"}

def test_prices_always_positive():
    sim = GBMSimulator(["AAPL", "TSLA"], event_probability=0.5)  # high shock rate
    for _ in range(100):
        prices = sim.step()
        assert all(p > 0 for p in prices.values())

def test_add_ticker_appears_in_step():
    sim = GBMSimulator(["AAPL"])
    sim.add_ticker("GOOGL")
    result = sim.step()
    assert "GOOGL" in result

def test_remove_ticker_absent_from_step():
    sim = GBMSimulator(["AAPL", "GOOGL"])
    sim.remove_ticker("GOOGL")
    result = sim.step()
    assert "GOOGL" not in result

def test_seed_prices_used():
    sim = GBMSimulator(["AAPL"])
    # Price should start close to seed (190.0) before any steps
    assert 100.0 < sim.get_price("AAPL") < 300.0

def test_cholesky_succeeds_for_all_defaults():
    """Verify the correlation matrix is valid for all 10 default tickers."""
    from app.market.seed_prices import SEED_PRICES
    sim = GBMSimulator(list(SEED_PRICES.keys()))
    # If Cholesky fails (non-positive-definite), this raises LinAlgError
    assert sim._cholesky is not None
    result = sim.step()
    assert len(result) == 10
```

#### `test_simulator_source.py` — `SimulatorDataSource`
```python
@pytest.mark.asyncio
async def test_start_seeds_cache():
    cache = PriceCache()
    source = SimulatorDataSource(cache)
    await source.start(["AAPL", "GOOGL"])

    assert cache.get_price("AAPL") is not None
    assert cache.get_price("GOOGL") is not None
    await source.stop()

@pytest.mark.asyncio
async def test_stop_cancels_task():
    cache = PriceCache()
    source = SimulatorDataSource(cache)
    await source.start(["AAPL"])
    await source.stop()
    assert source._task is None

@pytest.mark.asyncio
async def test_add_ticker_seeds_cache_immediately():
    cache = PriceCache()
    source = SimulatorDataSource(cache)
    await source.start(["AAPL"])
    await source.add_ticker("GOOGL")

    assert cache.get_price("GOOGL") is not None
    await source.stop()

@pytest.mark.asyncio
async def test_remove_ticker_purges_cache():
    cache = PriceCache()
    source = SimulatorDataSource(cache)
    await source.start(["AAPL", "GOOGL"])
    await source.remove_ticker("GOOGL")

    assert cache.get("GOOGL") is None
    await source.stop()
```

#### `test_factory.py` — `create_market_data_source`
```python
def test_returns_simulator_without_api_key(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)

def test_returns_massive_with_api_key(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key-123")
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, MassiveDataSource)

def test_empty_api_key_uses_simulator(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "   ")  # whitespace only
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)
```

#### `test_massive.py` — `MassiveDataSource`
```python
@pytest.mark.asyncio
async def test_poll_updates_cache():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)

    # Mock the internal fetch method to avoid real HTTP calls
    mock_snap = MagicMock()
    mock_snap.ticker = "AAPL"
    mock_snap.last_trade.price = 190.25
    mock_snap.last_trade.timestamp = 1705678234000  # milliseconds

    source._client = MagicMock()
    source._tickers = ["AAPL"]

    with patch.object(source, "_fetch_snapshots", return_value=[mock_snap]):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.25

@pytest.mark.asyncio
async def test_malformed_snapshot_is_skipped():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache)
    source._client = MagicMock()
    source._tickers = ["AAPL"]

    bad_snap = MagicMock()
    bad_snap.ticker = "AAPL"
    bad_snap.last_trade = None  # malformed — no last_trade

    with patch.object(source, "_fetch_snapshots", return_value=[bad_snap]):
        await source._poll_once()  # Should not raise

    assert cache.get_price("AAPL") is None  # gracefully skipped

@pytest.mark.asyncio
async def test_remove_ticker_removes_from_cache():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    source = MassiveDataSource(api_key="test", price_cache=cache)
    source._tickers = ["AAPL"]

    await source.remove_ticker("AAPL")

    assert cache.get("AAPL") is None
    assert "AAPL" not in source._tickers
```

### Running Tests

```bash
cd backend
uv run pytest tests/market/ -v
uv run pytest tests/market/ --cov=app/market --cov-report=term-missing
```

### SSE Integration Test (with ASGI client)

```python
import pytest
from httpx import AsyncClient, ASGITransport

@pytest.mark.asyncio
async def test_sse_stream_sends_prices(app):
    """app fixture creates a FastAPI app with a running SimulatorDataSource."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with client.stream("GET", "/api/stream/prices") as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]

            # Read the retry directive and first data event
            lines = []
            async for line in response.aiter_lines():
                lines.append(line)
                if len(lines) >= 4:  # retry line, blank, data line, blank
                    break

            assert lines[0] == "retry: 1000"
            assert lines[2].startswith("data: ")

            import json
            payload = json.loads(lines[2][6:])  # strip "data: " prefix
            assert "AAPL" in payload
            assert "price" in payload["AAPL"]
```

---

## 15. Error Handling & Edge Cases

### Simulator

| Scenario | Behavior |
|----------|----------|
| `step()` raises exception | Caught in `_run_loop`, logged, loop continues |
| Empty ticker list | `step()` returns `{}`, no cache writes, no crash |
| Single ticker (no Cholesky) | `_cholesky = None`, uses raw normal random variable |
| Add ticker already present | No-op (guard in `add_ticker`) |
| Remove ticker not present | No-op (guard in `remove_ticker`) |
| Price approaches zero | Mathematically impossible with GBM (log-normal prices always positive) |

### Massive Client

| Scenario | Behavior |
|----------|----------|
| 401 Unauthorized | Logged as error, retry on next interval |
| 429 Rate Limit | Logged as error, retry on next interval (consider increasing `poll_interval`) |
| Network timeout | Logged as error, retry on next interval |
| `snap.last_trade` is `None` | Caught by `AttributeError` in per-snap try/except, snapshot skipped |
| Ticker not found in response | Simply not updated; previous cache value remains |
| `start()` poll fails | Logged; cache remains empty; SSE has no data until next poll |

### SSE Stream

| Scenario | Behavior |
|----------|----------|
| Client disconnects | Detected via `request.is_disconnected()`, generator exits cleanly |
| `asyncio.CancelledError` | Caught and logged (happens on app shutdown) |
| Cache is empty | No `data:` event sent; retry fires after 500ms |
| Multiple concurrent clients | Each client has its own generator; all read from the same shared cache |

### Cache

| Scenario | Behavior |
|----------|----------|
| First update for a ticker | `previous_price == price`, `direction == "flat"` |
| `get()` on unknown ticker | Returns `None` |
| `remove()` on unknown ticker | No-op (`dict.pop` with default) |
| Concurrent writes (e.g., test) | Protected by `threading.Lock`; safe on CPython and no-GIL builds |

---

## 16. Configuration Summary

| Environment Variable | Default | Effect |
|---------------------|---------|--------|
| `MASSIVE_API_KEY` | (not set) | If set and non-empty, uses Massive REST API |
| `LLM_MOCK` | `false` | Unrelated to market data; affects LLM layer only |

| Tunable parameter | Location | Default | Effect |
|-------------------|----------|---------|--------|
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` | Seconds between simulator ticks |
| `event_probability` | `SimulatorDataSource.__init__` | `0.001` | Shock event probability per tick |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` | Seconds between Massive API polls |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | Time step (fraction of trading year) |
| Per-ticker `sigma` | `seed_prices.py / TICKER_PARAMS` | See table | Annualized volatility |
| Per-ticker `mu` | `seed_prices.py / TICKER_PARAMS` | See table | Annualized drift |

**No other configuration is required.** The factory selects the data source automatically based on `MASSIVE_API_KEY`. All other parameters have sensible defaults for the trading workstation use case.
