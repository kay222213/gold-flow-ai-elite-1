# Historical validation harness

Run `python validation/backtest.py your_xauusd_m5.csv --spread 0.30 --slippage 0.10`. Input must have `time,open,high,low,close` columns and at least 200 bars. This is a transparent baseline harness, not proof of profitability and not the full live strategy. It uses next-bar-open entry and pessimistically assumes stop first if stop and target are touched within the same bar. For defensible validation, add fees, rollover, realistic historical spread, out-of-sample splits, walk-forward selection, regime breakdowns, and test the exact live strategy version.
