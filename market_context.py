"""Pure market-context helpers with explicit, daylight-saving-aware session boundaries."""
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

SAST = ZoneInfo("Africa/Johannesburg")
UTC = ZoneInfo("UTC")
SESSION_ZONES = {
    "asia": (ZoneInfo("Asia/Tokyo"), time(9, 0), time(18, 0)),
    "london": (ZoneInfo("Europe/London"), time(8, 0), time(17, 0)),
    "new_york": (ZoneInfo("America/New_York"), time(8, 0), time(17, 0)),
}


def session_name(now: datetime | None = None) -> str:
    now = (now or datetime.now(SAST)).astimezone(timezone.utc)
    is_open = {}
    for name, (zone, start, end) in SESSION_ZONES.items():
        local = now.astimezone(zone)
        clock = local.time().replace(tzinfo=None)
        is_open[name] = start <= clock < end
    if is_open["london"] and is_open["new_york"]:
        return "London / New York overlap"
    if is_open["london"]:
        return "London session"
    if is_open["new_york"]:
        return "New York session"
    if is_open["asia"]:
        return "Asia session"
    return "Off session / rollover"


def high_low(bars: list[dict]) -> dict:
    if not bars:
        return {"high": None, "low": None}
    return {"high": max(float(x["high"]) for x in bars), "low": min(float(x["low"]) for x in bars)}


def previous_day_levels(d1_bars: list[dict], include_forming: bool = True) -> dict:
    """Return the last completed daily candle for sources that include a live/forming last bar.

    For confirmed-only sources (e.g. TradingView close alerts), set include_forming=False.
    Daily boundaries follow the input feed's timezone/session, not automatically the broker's day.
    """
    completed = d1_bars[:-1] if include_forming else d1_bars
    if len(completed) < 1:
        return {"high": None, "low": None}
    bar = completed[-1]
    return {"high": float(bar["high"]), "low": float(bar["low"])}


def intraday_session_levels(bars: list[dict], now: datetime | None = None, include_forming: bool = True) -> dict:
    """Calculate today's high/low in Tokyo, London and New York local session windows.

    Bars must carry UTC Unix timestamps. The session date/time is computed in the actual market
    timezone, so daylight-saving transitions are respected. It is not a broker-day volume profile.
    """
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    source_bars = bars[:-1] if include_forming and len(bars) > 1 else bars
    out = {}
    for name, (zone, start, end) in SESSION_ZONES.items():
        local_now = now_utc.astimezone(zone)
        session_date = local_now.date()
        selected = []
        for bar in source_bars:
            try:
                dt = datetime.fromtimestamp(int(bar["time"]), tz=timezone.utc).astimezone(zone)
                local_time = dt.time().replace(tzinfo=None)
                if dt.date() == session_date and start <= local_time < end:
                    selected.append(bar)
            except (KeyError, TypeError, ValueError, OSError):
                continue
        levels = high_low(selected)
        out[f"{name}_high"] = levels["high"]
        out[f"{name}_low"] = levels["low"]
    return out


def structure_state(bars: list[dict], lookback: int = 30, include_forming: bool = True) -> dict:
    closed = bars[:-1] if include_forming and len(bars) > 1 else bars
    if len(closed) < lookback:
        return {"state": "unknown", "swing_high": None, "swing_low": None, "bos_up": False, "bos_down": False}
    sample = closed[-lookback:]
    prev = sample[:-1]
    last = sample[-1]
    hi = max(float(x["high"]) for x in prev)
    lo = min(float(x["low"]) for x in prev)
    return {"state": "bullish_break" if float(last["close"]) > hi else "bearish_break" if float(last["close"]) < lo else "range/inside",
            "swing_high": hi, "swing_low": lo, "bos_up": float(last["close"]) > hi, "bos_down": float(last["close"]) < lo}


def fair_value_gaps(bars: list[dict], include_forming: bool = True) -> dict:
    closed = bars[:-1] if include_forming and len(bars) > 1 else bars
    if len(closed) < 3:
        return {"bullish": None, "bearish": None}
    # Three-candle displacement-gap proxy; the presence of an FVG alone is not a trade signal.
    for i in range(len(closed) - 1, 1, -1):
        a, b, c = closed[i - 2], closed[i - 1], closed[i]
        if float(c["low"]) > float(a["high"]):
            return {"bullish": [float(a["high"]), float(c["low"])], "bearish": None}
        if float(c["high"]) < float(a["low"]):
            return {"bullish": None, "bearish": [float(c["high"]), float(a["low"])]}
    return {"bullish": None, "bearish": None}
