"""Optional provider contracts.

Credentials and provider terms are deployment-specific. This module deliberately does not
pretend to fetch news/calendar/order-flow when no authenticated provider is configured.
"""
from dataclasses import dataclass
from typing import Protocol, Any

class CalendarProvider(Protocol):
    def upcoming_events(self, currency: str="USD", hours: int=48) -> list[dict]: ...

class NewsProvider(Protocol):
    def headlines(self, query: str, hours: int=24) -> list[dict]: ...

class OrderFlowProvider(Protocol):
    def snapshot(self, symbol: str) -> dict[str, Any]: ...

@dataclass
class UnavailableProvider:
    name: str
    reason: str
    def status(self) -> dict:
        return {"connected":False,"provider":self.name,"reason":self.reason}
