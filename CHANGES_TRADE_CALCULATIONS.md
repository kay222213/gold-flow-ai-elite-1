# Gold Flow AI — trade-calculation hardening

## What changed

- Stop distance is structural and volatility-aware. It is not compressed by a low risk percentage. The engine enforces a configurable minimum stop distance and minimum ATR sanity floor.
- Take-profit is selected from the nearest available opposing liquidity objective (recent swing, session level, equal-high/low pool, or previous-day level) and rejected if that objective cannot support the configured minimum reward:risk and ATR distance.
- Price levels are rounded outward to the instrument tick where a verified tick size is configured. Indicative XAUUSD without tick metadata is displayed to two decimals only.
- Monetary position sizing is calculated only from exact broker tick value, tick size, volume minimum/maximum/step and account currency. If those specifications are absent, the engine cannot produce verified monetary sizing and remains at PRICE PLAN or NO TRADE instead of claiming risk is verified. These values can be configured on the backend without connecting MT5.
- The latest completed candle in M5/M15/H1/H4 is checked for freshness independently of quote freshness.
- Cumulative CVD is no longer divided by window volume and mislabeled as delta percentage. Flow must provide an explicit delta percentage or classified buy/sell volume, and must be timestamped and recent.
- The default decision gate now rejects candidates when the required economic calendar is disconnected, stale, or unable to timestamp a high-impact event; an empty event list is no longer silently treated as verified event safety.
- Spot quote refresh cache default is 10 seconds; actual update speed remains limited by the market-data provider's feed and subscription.
- Service-worker cache version is v11 and the frontend script URL matches it, reducing stale cached GitHub Pages assets.

## Important configuration before a trade-ready candidate can appear

Set these values in the backend host's environment from the broker's actual XAUUSD contract specification:

- `BROKER_TICK_SIZE`
- `BROKER_TICK_VALUE_ACCOUNT_CCY` — P/L in the account currency for one tick at 1.00 lot
- `BROKER_VOLUME_MIN`
- `BROKER_VOLUME_MAX`
- `BROKER_VOLUME_STEP`
- `BROKER_ACCOUNT_CURRENCY`

Do not guess these values. The engine intentionally stays at NO TRADE if sizing cannot be verified.

For full live signal validation, also configure actual market data, the economic calendar, macro/news providers and a licensed order-flow feed. CME GC futures flow is only a proxy for OTC spot XAUUSD. The TradingView chart widget is visual; confirmed Pine alerts provide OHLC but not full bid/ask or footprint depth.

## Verification completed in this build

- `pytest -q`: 40 passed in the final source-tree test run; the suite includes status-gate regression coverage. Re-run CI after any subsequent change.
- Python compile check: passed
- Frontend JavaScript syntax check (`node --check`): passed

These are code-level tests. They do not establish that API credentials are active, that the hosted backend is deployed, that the public live website is updated, or that a trading strategy is profitable.
