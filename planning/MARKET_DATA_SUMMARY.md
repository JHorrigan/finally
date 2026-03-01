# Market Data Backend — Summary

The market data backend is complete. All code lives in `backend/app/market/`.

## What Was Built

| Module | Purpose |
|--------|---------|
| `models.py` | `PriceUpdate` — immutable frozen dataclass with `ticker`, `price`, `previous_price`, `timestamp`, and computed properties `change`, `change_percent`, `direction` |
| `cache.py` | `PriceCache` — thread-safe in-memory store; single source of truth for all downstream consumers |
| `interface.py` | `MarketDataSource` — abstract base class defining the lifecycle API (`start`, `stop`, `add_ticker`, `remove_ticker`, `get_tickers`) |
| `seed_prices.py` | Realistic seed prices and per-ticker GBM parameters for all 10 default tickers; correlation group definitions |
| `simulator.py` | `GBMSimulator` (pure math) + `SimulatorDataSource` (async background task); correlated price paths via Cholesky decomposition |
| `massive_client.py` | `MassiveDataSource` — polls Polygon.io REST API every 15s; runs synchronous SDK calls in `asyncio.to_thread` |
| `factory.py` | `create_market_data_source(cache)` — selects `MassiveDataSource` if `MASSIVE_API_KEY` is set, otherwise `SimulatorDataSource` |
| `stream.py` | `create_stream_router(cache)` — FastAPI router factory; SSE endpoint at `GET /api/stream/prices` |
| `__init__.py` | Public API exports |

## Public API

```python
from app.market import (
    PriceUpdate,               # Data model
    PriceCache,                # In-memory price store
    MarketDataSource,          # Abstract interface
    create_market_data_source, # Factory (env-driven)
    create_stream_router,      # FastAPI SSE router factory
)
```

## FastAPI Lifecycle Integration

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
    await source.start(DEFAULT_TICKERS)
    app.state.price_cache = cache
    app.state.market_source = source
    yield
    await source.stop()

app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(cache))
```

## Environment Variables

| Variable | Effect |
|----------|--------|
| `MASSIVE_API_KEY` | If set and non-empty → `MassiveDataSource` (real data); otherwise `SimulatorDataSource` |

## Tests

Six test modules in `backend/tests/market/` — 80+ tests covering:
- `PriceUpdate` model properties and serialization
- `PriceCache` CRUD, version tracking, thread safety
- `GBMSimulator` math, correlation structure, 10-ticker Cholesky
- `SimulatorDataSource` async lifecycle
- `MassiveDataSource` polling, error handling, timestamp conversion
- SSE `_generate_events` generator behaviour

Run with:
```bash
cd backend
uv run --extra dev pytest -v
uv run --extra dev pytest --cov=app
```

## Design Details

See `planning/archive/` for full design documents:
- `MARKET_DATA_DESIGN.md` — full architecture and implementation reference
- `MARKET_INTERFACE.md` — interface and integration patterns
- `MARKET_SIMULATOR.md` — GBM math and simulator details
- `MASSIVE_API.md` — Massive/Polygon.io client details
- `MARKET_DATA_REVIEW.md` — code review findings and resolutions
