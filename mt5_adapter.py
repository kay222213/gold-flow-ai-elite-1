"""Optional MetaTrader 5 adapter. Run on the same host as the MT5 terminal."""
from datetime import datetime, timezone
from market_data import Quote

class MT5Provider:
    bars_include_forming_candle = True
    # Tick and volume metadata comes directly from the selected terminal/account symbol info.
    provides_verified_contract_specs = True
    def __init__(self):
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise RuntimeError("MetaTrader5 Python package is not installed on this host.") from exc
        self.mt5 = mt5
        if not mt5.initialize():
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

    def get_quote(self, symbol: str) -> Quote:
        mt5 = self.mt5
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"MT5 symbol unavailable: {symbol}; {mt5.last_error()}")
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None or tick.bid <= 0 or tick.ask <= 0:
            raise RuntimeError(f"No valid MT5 quote for {symbol}.")
        stamp = datetime.fromtimestamp(tick.time, tz=timezone.utc)
        account = mt5.account_info()
        return Quote(symbol, float(tick.bid), float(tick.ask), stamp, "MT5 terminal",
                     float(info.trade_tick_size or info.point), float(info.trade_tick_value or 0),
                     float(info.trade_contract_size or 0), float(info.volume_min or 0),
                     float(info.volume_max or 0), float(info.volume_step or 0),
                     getattr(account, "currency", None))

    def get_bars(self, symbol: str, timeframe: str, count: int = 300) -> list[dict]:
        mt5 = self.mt5
        tfs = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
               "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}
        if timeframe not in tfs:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"MT5 symbol unavailable: {symbol}")
        rows = mt5.copy_rates_from_pos(symbol, tfs[timeframe], 0, count)
        if rows is None or len(rows) < 30:
            raise RuntimeError(f"Insufficient {timeframe} candles from MT5.")
        return [{"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"]), "tick_volume": int(r["tick_volume"])} for r in rows]
