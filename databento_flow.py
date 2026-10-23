"""Optional Databento live CME gold-futures order flow.

`trades` supplies aggressor-classified trade delta/CVD. `mbp-10` supplies ten-level L2 market-by-price
book updates when entitled. CME GC is a futures proxy for OTC spot XAUUSD, not the whole gold market.
No footprint-by-price or absorption claim is made by this adapter.
"""
from __future__ import annotations
import os, threading, time, logging
from collections import deque
from datetime import datetime, timezone

log = logging.getLogger("goldflow.orderflow")
active_flow_provider = None


class DatabentoGoldFlow:
    def __init__(self):
        global active_flow_provider
        self.key = os.getenv("DATABENTO_API_KEY", "").strip()
        if not self.key:
            raise RuntimeError("DATABENTO_API_KEY is not configured")
        self.dataset = os.getenv("DATABENTO_DATASET", "GLBX.MDP3")
        self.symbol = os.getenv("DATABENTO_GOLD_SYMBOL", "GC.FUT")
        self.depth_enabled = os.getenv("DATABENTO_DEPTH_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
        self.depth_max_age = max(1.0, float(os.getenv("ORDER_BOOK_MAX_AGE_SECONDS", "30")))
        self.lock = threading.RLock()
        self.events = deque(maxlen=200000)
        self.error = None
        self.depth_error = None
        self.last_book = None
        self.client = None
        self.thread = None
        self.depth_subscription_added = False
        active_flow_provider = self

    def start(self):
        try:
            import databento as db
        except ImportError as exc:
            raise RuntimeError("Databento SDK is not installed; install backend requirements and confirm provider entitlement") from exc
        self.client = db.Live(key=self.key, reconnect_policy="reconnect")
        # Both subscriptions share the same dataset/session; depth is attempted separately so a
        # subscription-level validation error can be reported without silently claiming a book.
        self.client.subscribe(dataset=self.dataset, schema="trades", stype_in="parent", symbols=self.symbol)
        if self.depth_enabled:
            try:
                self.client.subscribe(dataset=self.dataset, schema="mbp-10", stype_in="parent", symbols=self.symbol)
                self.depth_subscription_added = True
            except Exception as exc:
                self.depth_error = f"MBP-10 subscription failed ({type(exc).__name__}); confirm dataset and depth entitlement"
                log.warning(self.depth_error)
        self.client.add_callback(self._on_record, self._on_error)
        self.thread = threading.Thread(target=self._run, name="databento-gold-flow", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.client.start()
            self.client.block_for_close()
        except Exception as exc:
            self._on_error(exc)

    def _on_error(self, exc):
        # Never log provider exception payloads that might contain service metadata or credentials.
        self.error = f"Databento live-stream error ({type(exc).__name__}); check key, dataset, symbol and entitlements."
        log.error(self.error)

    @staticmethod
    def _side_label(raw) -> str:
        return str(raw or "").upper().split(".")[-1]

    def _on_depth(self, record):
        levels = getattr(record, "levels", None)
        ts_ns = int(getattr(record, "ts_event", 0) or 0)
        if levels is None or ts_ns <= 0:
            return
        bid_depth = 0.0
        ask_depth = 0.0
        bid_px = None
        ask_px = None
        levels_seen = 0
        for level in levels:
            try:
                bsize = float(getattr(level, "bid_sz", 0) or 0)
                asize = float(getattr(level, "ask_sz", 0) or 0)
                bp = int(getattr(level, "bid_px", 0) or 0)
                ap = int(getattr(level, "ask_px", 0) or 0)
                bid_depth += max(0.0, bsize)
                ask_depth += max(0.0, asize)
                # Databento fixed-point prices use 1e-9 units. Avoid sentinel / invalid values.
                if levels_seen == 0 and 0 < bp < 9_000_000_000_000_000_000:
                    bid_px = bp / 1e9
                if levels_seen == 0 and 0 < ap < 9_000_000_000_000_000_000:
                    ask_px = ap / 1e9
                levels_seen += 1
            except (TypeError, ValueError, OverflowError):
                continue
        total = bid_depth + ask_depth
        if total <= 0:
            return
        stamp = datetime.fromtimestamp(ts_ns / 1e9, timezone.utc).isoformat()
        snapshot = {
            "timestamp": stamp,
            "bid_depth_top10": bid_depth,
            "ask_depth_top10": ask_depth,
            "depth_imbalance_percent": round(100 * (bid_depth - ask_depth) / total, 2),
            "depth_levels_seen": levels_seen,
            "best_bid_gc": bid_px,
            "best_ask_gc": ask_px,
            "book_spread_gc": round(ask_px - bid_px, 8) if bid_px is not None and ask_px is not None and ask_px >= bid_px else None,
        }
        with self.lock:
            self.last_book = snapshot

    def _on_record(self, record):
        try:
            # MBP-10 has `levels`; do not misclassify book updates as individual executed trades.
            if getattr(record, "levels", None) is not None:
                self._on_depth(record)
                return
            side = self._side_label(getattr(record, "side", ""))
            size = float(getattr(record, "size", 0) or 0)
            ts_ns = int(getattr(record, "ts_event", 0) or 0)
            if size <= 0 or ts_ns <= 0:
                return
            # Databento Trade side: B = buyer-initiated, A = seller-initiated; unknown side excluded.
            signed = size if side in ("B", "BID") else -size if side in ("A", "ASK") else 0.0
            with self.lock:
                self.events.append((ts_ns / 1e9, size, signed, side))
        except Exception as exc:
            log.debug("Ignoring unparseable order-flow record (%s)", type(exc).__name__)

    def snapshot(self, symbol="XAUUSD"):
        now = time.time()
        window = max(30.0, float(os.getenv("ORDER_FLOW_WINDOW_SECONDS", "300")))
        cutoff = now - window
        with self.lock:
            events = [event for event in self.events if event[0] >= cutoff]
            book = dict(self.last_book) if self.last_book else None
        depth_age = (now - datetime.fromisoformat(book["timestamp"].replace("Z", "+00:00")).timestamp()) if book else None
        depth_connected = bool(book and depth_age is not None and 0 <= depth_age <= self.depth_max_age)
        depth_payload = {
            "depth_connected": depth_connected,
            "depth_provider": "Databento MBP-10 · CME GC futures L2" if depth_connected else "Unavailable",
            "depth_reason": (None if depth_connected else self.depth_error or ("No L2 book received yet" if book is None else "Last L2 book update is stale")),
            "depth_age_seconds": round(depth_age, 2) if depth_age is not None else None,
            "depth_timestamp": book.get("timestamp") if book else None,
            "depth_imbalance_percent": book.get("depth_imbalance_percent") if depth_connected else None,
            "bid_depth_top10": book.get("bid_depth_top10") if depth_connected else None,
            "ask_depth_top10": book.get("ask_depth_top10") if depth_connected else None,
            "depth_levels_seen": book.get("depth_levels_seen") if depth_connected else None,
            "best_bid_gc": book.get("best_bid_gc") if depth_connected else None,
            "best_ask_gc": book.get("best_ask_gc") if depth_connected else None,
            "book_spread_gc": book.get("book_spread_gc") if depth_connected else None,
        }
        if not events:
            return {"connected": False, "provider": "Databento · CME GC futures trades",
                    "reason": self.error or "Waiting for fresh live futures trades.",
                    "timestamp": None, "trade_count": 0, "proxy_for": "XAUUSD spot; not the same market",
                    "caveat": "No active trade-level sample. L2 depth may be available independently.", **depth_payload}
        buy = sum(event[1] for event in events if event[3] in ("B", "BID"))
        sell = sum(event[1] for event in events if event[3] in ("A", "ASK"))
        unknown = sum(event[1] for event in events if event[3] not in ("B", "BID", "A", "ASK"))
        total = buy + sell
        delta = buy - sell
        return {"connected": bool(total > 0), "provider": "Databento · CME GC futures trades",
                "timestamp": datetime.fromtimestamp(events[-1][0], timezone.utc).isoformat(),
                "symbol": self.symbol, "window_seconds": int(window), "trade_count": len(events),
                "buy_aggressor_volume": buy, "sell_aggressor_volume": sell, "cvd_delta": delta,
                "delta_percent": round(100 * delta / total, 2) if total else None,
                "unknown_side_volume": unknown, "proxy_for": "XAUUSD spot; futures basis/venue mismatch must be considered",
                "caveat": "Trade-level aggressor delta plus optional L2 depth. Not a full spot-gold book or proof of absorption.", **depth_payload}

    def stop(self):
        if self.client:
            try:
                self.client.stop()
            except Exception:
                pass
