"""Twelve Data spot-gold OHLC/price adapter. API key must remain server-side.
Price-only feeds are indicative: they do not supply executable bid/ask or broker contract specs.
"""
from __future__ import annotations
import os, requests, time, threading, json
from datetime import datetime, timezone
from market_data import Quote

BASE = "https://api.twelvedata.com"

class TwelveDataProvider:
    bars_include_forming_candle = True
    def __init__(self):
        self.key = os.getenv("TWELVEDATA_API_KEY", "").strip()
        self.symbol = os.getenv("TWELVEDATA_SYMBOL", "XAU/USD").strip()
        if not self.key:
            raise RuntimeError("TWELVEDATA_API_KEY is missing. Configure it server-side; never put it in frontend code.")
        self.timeout = float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "8"))
        self.session = requests.Session(); self.lock=threading.RLock(); self.cache={}
        self.quote_ttl=float(os.getenv("TWELVEDATA_QUOTE_CACHE_SECONDS","10"))
        self.bar_ttls={"M5":float(os.getenv("TWELVEDATA_M5_CACHE_SECONDS","60")),"M15":float(os.getenv("TWELVEDATA_M15_CACHE_SECONDS","120")),"H1":float(os.getenv("TWELVEDATA_H1_CACHE_SECONDS","300")),"H4":300.0,"D1":1800.0}
        self.ws_enabled=os.getenv("TWELVEDATA_WS_ENABLED","true").strip().lower() in ("1","true","yes","on")
        self.ws_stale_after=max(3.0,float(os.getenv("TWELVEDATA_WS_STALE_SECONDS","15")))
        self._ws_stop=threading.Event(); self._ws_app=None; self._ws_thread=None; self._heartbeat_thread=None
        self._ws_connected=False; self._ws_last_message_monotonic=0.0; self._stream_quote=None; self._ws_error=None

    def _get(self, endpoint, params):
        p = {**params, "apikey": self.key}
        r = self.session.get(f"{BASE}/{endpoint}", params=p, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("status") == "error":
            raise RuntimeError("Twelve Data API error: " + str(data.get("message", "unknown error")))
        return data

    @staticmethod
    def parse_stream_message(raw: str, expected_symbol: str):
        """Parse a single Twelve Data price event; source timestamps are mandatory."""
        try:
            payload=json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("event") != "price" and payload.get("type") != "price":
            return None
        symbol=str(payload.get("symbol") or "").strip().upper()
        expected=str(expected_symbol or "").strip().upper()
        if not symbol or symbol.replace(" ","") != expected.replace(" ",""):
            return None
        try:
            price=float(payload.get("price"))
            raw_ts=payload.get("timestamp")
            if raw_ts is None: return None
            if isinstance(raw_ts, (int,float)) or str(raw_ts).strip().replace(".","",1).isdigit():
                stamp=float(raw_ts)
                if stamp>1e12: stamp/=1000
                ts=datetime.fromtimestamp(stamp,timezone.utc)
            else:
                ts=datetime.fromisoformat(str(raw_ts).replace("Z","+00:00"))
                if ts.tzinfo is None: ts=ts.replace(tzinfo=timezone.utc)
                ts=ts.astimezone(timezone.utc)
            if not (price>0) or ts.timestamp()<=0: return None
        except (TypeError,ValueError,OverflowError,OSError):
            return None
        return price,ts

    def start_stream(self):
        """Start a server-side WebSocket quote stream when the provider plan permits it.

        REST quote polling remains as a bounded fallback. Twelve Data WebSocket prices are
        indicative only; this stream does not provide broker bid/ask or order-book depth.
        """
        if not self.ws_enabled or self._ws_thread is not None:
            return
        try:
            import websocket  # supplied by websocket-client; optional stream, REST fallback remains
        except ImportError:
            self._ws_error="websocket-client is not installed; using bounded REST polling"
            return
        self._ws_stop.clear()
        self._ws_thread=threading.Thread(target=self._stream_loop,daemon=True,name="twelvedata-price-stream")
        self._ws_thread.start()
        self._heartbeat_thread=threading.Thread(target=self._heartbeat_loop,daemon=True,name="twelvedata-heartbeat")
        self._heartbeat_thread.start()

    def _stream_loop(self):
        try:
            import websocket
        except ImportError:
            return
        from urllib.parse import quote
        # The key is only in this backend-side socket URL. Never log this URL or expose it to the UI.
        url="wss://ws.twelvedata.com/v1/quotes/price?apikey="+quote(self.key,safe="")
        while not self._ws_stop.is_set():
            try:
                wsapp=websocket.WebSocketApp(url,on_open=self._ws_on_open,on_message=self._ws_on_message,
                                             on_error=self._ws_on_error,on_close=self._ws_on_close)
                with self.lock: self._ws_app=wsapp
                wsapp.run_forever(ping_interval=20,ping_timeout=10)
            except Exception as exc:
                with self.lock:
                    self._ws_connected=False
                    self._ws_error=f"WebSocket reconnect pending ({type(exc).__name__})"
            if not self._ws_stop.is_set(): self._ws_stop.wait(3.0)

    def _ws_on_open(self, wsapp):
        with self.lock:
            self._ws_app=wsapp; self._ws_connected=True; self._ws_error=None
        try:
            wsapp.send(json.dumps({"action":"subscribe","params":{"symbols":self.symbol}}))
        except Exception as exc:
            with self.lock:
                self._ws_connected=False; self._ws_error=f"Subscribe failed ({type(exc).__name__})"
            try: wsapp.close()
            except Exception: pass

    def _ws_on_message(self, wsapp, raw):
        try:
            parsed=self.parse_stream_message(raw,self.symbol)
            with self.lock:
                self._ws_last_message_monotonic=time.monotonic()
                if parsed:
                    price,stamp=parsed
                    quote=Quote(self.symbol,price,price,stamp,"Twelve Data WebSocket · indicative XAU/USD")
                    self._stream_quote=quote
                    self._ws_connected=True; self._ws_error=None
                else:
                    # An explicit upstream error/status remains visible in health without leaking payloads.
                    data=json.loads(raw) if isinstance(raw,str) else {}
                    if isinstance(data,dict) and (data.get("status")=="error" or data.get("event")=="error"):
                        self._ws_error="Provider rejected the WebSocket subscription; check symbol, plan and entitlements"
                        self._ws_connected=False
        except Exception as exc:
            with self.lock: self._ws_error=f"WebSocket message rejected ({type(exc).__name__})"

    def _ws_on_error(self, wsapp, error):
        with self.lock:
            self._ws_connected=False
            self._ws_error=f"WebSocket disconnected ({type(error).__name__})"

    def _ws_on_close(self, wsapp, status_code, message):
        with self.lock:
            self._ws_connected=False

    def _heartbeat_loop(self):
        while not self._ws_stop.wait(10.0):
            with self.lock: wsapp=self._ws_app; active=self._ws_connected
            if wsapp and active:
                try: wsapp.send(json.dumps({"action":"heartbeat"}))
                except Exception:
                    with self.lock: self._ws_connected=False

    def stream_status(self):
        with self.lock:
            age=(time.monotonic()-self._ws_last_message_monotonic) if self._ws_last_message_monotonic else None
            quote_age=self._stream_quote.age_seconds() if self._stream_quote else None
            connected=bool(self._ws_connected and self._stream_quote and age is not None and age<=self.ws_stale_after
                           and quote_age is not None and 0<=quote_age<=self.ws_stale_after)
            return {"enabled":self.ws_enabled,"connected":connected,
                    "last_message_age_seconds":round(age,2) if age is not None else None,
                    "price_age_seconds":round(quote_age,2) if quote_age is not None else None,
                    "reason":self._ws_error or ("Waiting for a current WebSocket price" if not connected else None)}

    def stop_stream(self):
        self._ws_stop.set()
        with self.lock: wsapp=self._ws_app
        if wsapp:
            try: wsapp.close()
            except Exception: pass

    def get_quote(self, symbol: str) -> Quote:
        now=time.monotonic()
        with self.lock:
            streamed=self._stream_quote
            stream_age=(now-self._ws_last_message_monotonic) if self._ws_last_message_monotonic else float("inf")
            if streamed and stream_age<=self.ws_stale_after and 0<=streamed.age_seconds()<=self.ws_stale_after:
                return streamed
            cached=self.cache.get("quote")
            if cached and now-cached[0] < self.quote_ttl: return cached[1]
        data = self._get("quote", {"symbol": self.symbol, "timezone": "UTC", "dp": 5})
        price = data.get("close") or data.get("price")
        if price is None:
            raise RuntimeError("Twelve Data quote did not return a usable price.")
        stamp = data.get("datetime") or data.get("timestamp")
        if isinstance(stamp, (int, float)):
            ts = datetime.fromtimestamp(float(stamp), timezone.utc)
        elif stamp:
            try: ts = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            except ValueError as exc: raise RuntimeError("Quote timestamp could not be parsed; refusing to substitute request time.") from exc
            if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
        else:
            # Quote endpoint provides no event timestamp; do not claim a verified live timestamp.
            raise RuntimeError("Quote has no source timestamp; refusing to treat request time as market time.")
        px = float(price)
        if px <= 0: raise RuntimeError("Provider returned a non-positive price.")
        quote=Quote(symbol, px, px, ts.astimezone(timezone.utc), "Twelve Data · indicative XAU/USD")
        with self.lock: self.cache["quote"]=(time.monotonic(),quote)
        return quote

    def get_bars(self, symbol: str, timeframe: str, count: int = 300) -> list[dict]:
        intervals = {"M5":"5min", "M15":"15min", "H1":"1h", "H4":"4h", "D1":"1day"}
        if timeframe not in intervals: raise ValueError(f"Unsupported timeframe {timeframe}")
        now=time.monotonic()
        with self.lock:
            cached=self.cache.get("bars:"+timeframe)
            if cached and now-cached[0] < self.bar_ttls.get(timeframe,300): return cached[1][-count:]
        data = self._get("time_series", {"symbol": self.symbol, "interval": intervals[timeframe], "outputsize": min(count, 5000), "timezone":"UTC", "dp":5})
        values = data.get("values")
        if not isinstance(values, list) or len(values) < 65:
            raise RuntimeError(f"Insufficient {timeframe} candles from Twelve Data.")
        out=[]
        for x in reversed(values):
            stamp = datetime.fromisoformat(str(x["datetime"]).replace("Z", "+00:00"))
            if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
            out.append({"time":int(stamp.timestamp()), "open":float(x["open"]), "high":float(x["high"]), "low":float(x["low"]), "close":float(x["close"]), "tick_volume":int(float(x.get("volume") or 0))})
        with self.lock: self.cache["bars:"+timeframe]=(time.monotonic(),out)
        return out
