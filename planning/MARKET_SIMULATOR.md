# Market Simulator

This document describes the GBM-based stock price simulator used as the default market data source when no `MASSIVE_API_KEY` is set. The simulator produces visually realistic price movements with no external dependencies.

---

## Overview

The simulator models stock prices using **Geometric Brownian Motion (GBM)** — the standard continuous-time stochastic process used in the Black-Scholes options pricing model. GBM produces prices that:

- Are always positive (no negative prices)
- Move continuously and randomly
- Have returns that are normally distributed (log-normal prices)
- Include configurable drift (long-run trend) and volatility

The simulator adds two realism enhancements on top of raw GBM:
1. **Correlated moves** across tickers via Cholesky decomposition
2. **Random shock events** for dramatic 2-5% jumps on individual tickers

---

## GBM Mathematics

The core formula for each time step:

```
S(t + dt) = S(t) * exp((mu - sigma²/2) * dt + sigma * sqrt(dt) * Z)
```

Where:

| Symbol | Meaning |
|--------|---------|
| `S(t)` | Current price |
| `mu` | Annualized drift (expected return, e.g. 0.05 = 5%/year) |
| `sigma` | Annualized volatility (e.g. 0.25 = 25%/year) |
| `dt` | Time step as a fraction of one trading year |
| `Z` | Standard normal random variable (or correlated draw) |

The `(mu - sigma²/2)` term is the Ito correction — without it, the expected price would drift upward faster than `mu` due to the convexity of the exponential.

### Time Step Calculation

The simulator ticks every 500ms. One trading year = 252 days × 6.5 hours × 3600 seconds = 5,896,800 seconds.

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
dt = 0.5 / TRADING_SECONDS_PER_YEAR           # ~8.48e-8
```

With `sigma = 0.25` and this `dt`, each tick moves the price by approximately:

```
sigma * sqrt(dt) ≈ 0.25 * sqrt(8.48e-8) ≈ 0.000073 = 0.0073% per tick
```

On a $190 stock, that is ≈$0.014 per tick — sub-cent moves that accumulate naturally over time. This matches the feel of real streaming market data.

---

## Correlated Moves

Real stocks do not move independently. Tech stocks tend to rise and fall together; financial stocks have their own correlation. The simulator models this with a sector-based correlation matrix and Cholesky decomposition.

### Correlation Groups

```python
CORRELATION_GROUPS = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6   # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR   = 0.3   # Between sectors / unknown tickers
TSLA_CORR          = 0.3   # TSLA does its own thing (overrides tech group)
```

### Pairwise Correlation Logic

```python
def _pairwise_correlation(t1: str, t2: str) -> float:
    if t1 == "TSLA" or t2 == "TSLA":
        return TSLA_CORR        # 0.3
    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR  # 0.6
    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR  # 0.5
    return CROSS_GROUP_CORR    # 0.3
```

### Cholesky Decomposition

Given `n` tickers, build the `n×n` symmetric positive-definite correlation matrix `C` (1s on diagonal, pairwise correlations off-diagonal). Then compute `L = cholesky(C)`.

To generate correlated random draws from `n` independent standard normals `Z_ind`:

```python
Z_corr = L @ Z_ind
```

Each element of `Z_corr` is a correlated standard normal. Feed each `Z_corr[i]` into the GBM formula for ticker `i`.

The Cholesky matrix is rebuilt whenever tickers are added or removed. With at most ~50 tickers, this is O(n²) and takes microseconds.

---

## Random Shock Events

Every tick, each ticker has a small probability of a large sudden move (a "shock"):

```python
event_probability = 0.001   # 0.1% per tick per ticker

if random.random() < event_probability:
    shock_magnitude = random.uniform(0.02, 0.05)  # 2-5%
    shock_sign = random.choice([-1, 1])
    price *= (1 + shock_magnitude * shock_sign)
```

With 10 tickers at 2 ticks/second, the expected rate is:
```
10 tickers × 2 ticks/s × 0.001 = 0.02 events/second → ~1 event every 50 seconds
```

This creates occasional dramatic moves that make the UI feel alive and provide visual drama for the trading workstation aesthetic.

---

## Seed Prices and Per-Ticker Parameters

Each ticker starts from a realistic seed price and has tuned GBM parameters:

```python
SEED_PRICES = {
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

TICKER_PARAMS = {
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
DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}
```

Dynamically added tickers (not in the seed list) start at a random price between $50 and $300 with the default parameters.

---

## Code Structure

### `GBMSimulator`

A pure Python class with no asyncio dependency. Manages the mathematical state: prices, parameters, and the Cholesky matrix.

```python
class GBMSimulator:
    DEFAULT_DT = 0.5 / (252 * 6.5 * 3600)  # 500ms in trading-year units

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None: ...

    def step(self) -> dict[str, float]:
        """Advance all tickers by one dt. Returns {ticker: new_price}.
        Hot path — called every 500ms.
        """

    def add_ticker(self, ticker: str) -> None:
        """Add ticker, set seed price and params, rebuild Cholesky."""

    def remove_ticker(self, ticker: str) -> None:
        """Remove ticker state and rebuild Cholesky."""

    def get_price(self, ticker: str) -> float | None:
        """Current price, or None if not tracked."""

    def get_tickers(self) -> list[str]:
        """All currently tracked tickers."""
```

**Internal state**:

```python
self._tickers: list[str]             # ordered list (index matches Cholesky columns)
self._prices: dict[str, float]       # current price per ticker
self._params: dict[str, dict]        # {"mu": ..., "sigma": ...} per ticker
self._cholesky: np.ndarray | None    # L matrix, None if n <= 1
```

### `SimulatorDataSource`

Wraps `GBMSimulator` in the `MarketDataSource` interface. Runs the step loop as an asyncio background task.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,       # seconds between ticks
        event_probability: float = 0.001,   # shock probability per tick
    ) -> None: ...

    async def start(self, tickers: list[str]) -> None:
        """Create GBMSimulator, seed cache with initial prices, start loop task."""

    async def stop(self) -> None:
        """Cancel the background task."""

    async def add_ticker(self, ticker: str) -> None:
        """Add to simulator, seed cache immediately."""

    async def remove_ticker(self, ticker: str) -> None:
        """Remove from simulator and cache."""

    def get_tickers(self) -> list[str]:
        """Delegate to simulator."""

    async def _run_loop(self) -> None:
        """Core loop: step → write cache → sleep."""
```

**Loop pseudocode**:

```python
async def _run_loop(self):
    while True:
        try:
            prices = self._sim.step()        # dict[str, float]
            for ticker, price in prices.items():
                self._cache.update(ticker, price)
        except Exception:
            logger.exception("Simulator step failed")
        await asyncio.sleep(self._interval)  # 500ms
```

Exceptions in `step()` are caught and logged so the loop continues. In practice, `step()` should never raise since it only does numpy and pure Python math.

---

## Behaviour Details

### Cache seeding on start

When `start(tickers)` is called, the simulator initialises all prices from `SEED_PRICES` and immediately writes them all to the `PriceCache`. This ensures the SSE stream has data available before the first tick fires.

```python
async def start(self, tickers):
    self._sim = GBMSimulator(tickers=tickers, ...)
    for ticker in tickers:
        price = self._sim.get_price(ticker)
        if price is not None:
            self._cache.update(ticker=ticker, price=price)
    self._task = asyncio.create_task(self._run_loop())
```

### Adding a ticker mid-session

```python
async def add_ticker(self, ticker: str):
    if self._sim:
        self._sim.add_ticker(ticker)         # adds to GBM state, rebuilds Cholesky
        price = self._sim.get_price(ticker)  # get initial price immediately
        if price is not None:
            self._cache.update(ticker=ticker, price=price)  # seed cache now
```

The ticker appears in the SSE stream on the very next event (within 500ms), not on the next tick after that.

### Removing a ticker mid-session

```python
async def remove_ticker(self, ticker: str):
    if self._sim:
        self._sim.remove_ticker(ticker)   # removes from GBM state, rebuilds Cholesky
    self._cache.remove(ticker)            # purges from SSE stream immediately
```

---

## Extending the Simulator

### Adding a new default ticker

Add entries to `SEED_PRICES` and `TICKER_PARAMS` in `seed_prices.py`. Assign it to a `CORRELATION_GROUPS` set if it belongs to an existing sector.

### Adjusting volatility

Increase `sigma` for more dramatic price swings, decrease for calmer behaviour. Typical real-world annualised volatilities:
- Low volatility (utilities, banks): 0.15–0.20
- Medium (large-cap tech): 0.20–0.30
- High (growth, biotech): 0.35–0.60
- Crypto-like: 0.80–1.50

### Adjusting shock frequency

Decrease `event_probability` for fewer events, increase for more. At `0.001` with 10 tickers at 2 ticks/second, expect ~1 shock every 50 seconds.

### Adjusting tick rate

Pass `update_interval` to `SimulatorDataSource`. Faster ticks = smoother sparklines but more CPU. The SSE stream also checks every 500ms, so ticking faster than 500ms provides no benefit to the frontend.

---

## Dependencies

- `numpy` — for `np.random.standard_normal`, `np.eye`, `np.linalg.cholesky`, and matrix multiplication
- `math` — for `math.exp`, `math.sqrt`
- `random` — for shock events (Python stdlib, no numpy needed for scalar decisions)

The simulator has no I/O, no network calls, and no database access. It is entirely in-process.
