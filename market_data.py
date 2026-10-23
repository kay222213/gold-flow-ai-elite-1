"""Market data interfaces. Never manufacture or silently reuse stale quotes."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    timestamp: datetime
    source: str
    tick_size: Optional[float] = None
    tick_value: Optional[float] = None
    contract_size: Optional[float] = None
    volume_min: Optional[float] = None
    volume_max: Optional[float] = None
    volume_step: Optional[float] = None
    account_currency: Optional[str] = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.timestamp.astimezone(timezone.utc)).total_seconds()

class MarketDataProvider(Protocol):
    def get_quote(self, symbol: str) -> Quote: ...
    def get_bars(self, symbol: str, timeframe: str, count: int = 300) -> list[dict]: ...

class UnconfiguredProvider:
    def get_quote(self, symbol: str) -> Quote:
        raise RuntimeError("No live market-data provider configured. No simulated prices are permitted.")
    def get_bars(self, symbol: str, timeframe: str, count: int = 300) -> list[dict]:
        raise RuntimeError("Historical candles unavailable: configure a real data provider.")
