"""Server-side macro/calendar/news connectors.

The connectors never substitute sample market data for missing providers. Economic calendar
release timestamps returned by Trading Economics are UTC; the UI gets both UTC and SAST values.
"""
from __future__ import annotations
import os, re, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

TIMEOUT = float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "8"))
SAST = ZoneInfo("Africa/Johannesburg")


def safe_error(exc: Exception) -> str:
    """Return a short provider error with credentials and URL query values removed."""
    message = str(exc)
    message = re.sub(r'(?i)([?&](?:apikey|api_key|token|key|c)=)[^&\s]+', r'\1[REDACTED]', message)
    message = re.sub(r'(?i)(\b(?:apikey|api_key|token|key|c)=)[^&\s]+', r'\1[REDACTED]', message)
    message = re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*', r'\1[REDACTED]', message)
    return message[:300]


def _get(url, params=None, headers=None):
    response = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and (data.get("status") == "error" or data.get("Error Message")):
        raise RuntimeError(str(data.get("message") or data.get("Error Message") or "Provider returned an error"))
    return response


def _utc_iso(raw) -> str | None:
    if raw in (None, "", "null", "None"):
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Trading Economics documents the economic calendar's Date value in UTC.
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _sast_display(raw) -> str | None:
    stamp = _utc_iso(raw)
    if not stamp:
        return None
    return datetime.fromisoformat(stamp).astimezone(SAST).strftime("%Y-%m-%d %H:%M SAST")


def get_fred_macro():
    key = os.getenv("FRED_API_KEY", "").strip()
    if not key:
        return {"connected": False, "provider": "FRED", "reason": "FRED_API_KEY not configured", "series": []}
    # Daily series. DTWEXBGS is a broad trade-weighted USD proxy, not the ICE DXY index.
    series = [("DGS10", "US 10Y Treasury yield"), ("DTWEXBGS", "Broad trade-weighted USD (not DXY)"),
              ("DFII10", "US 10Y TIPS real yield")]
    results = []
    for sid, label in series:
        try:
            data = _get("https://api.stlouisfed.org/fred/series/observations",
                        {"series_id": sid, "api_key": key, "file_type": "json", "sort_order": "desc", "limit": 5}).json()
            obs = [x for x in data.get("observations", []) if x.get("value") not in (None, ".")]
            if obs:
                current = float(obs[0]["value"])
                previous = float(obs[1]["value"]) if len(obs) > 1 else None
                results.append({"id": sid, "label": label, "date": obs[0]["date"], "value": current,
                                "previous_date": obs[1]["date"] if len(obs) > 1 else None,
                                "change": round(current - previous, 5) if previous is not None else None,
                                "unit": "percent" if sid in ("DGS10", "DFII10") else "index",
                                "source": "FRED", "url": f"https://fred.stlouisfed.org/series/{sid}",
                                "frequency": "daily/lagged"})
            else:
                results.append({"id": sid, "label": label, "error": "No valid observations returned"})
        except Exception as exc:
            results.append({"id": sid, "label": label, "error": safe_error(exc)})
    score = 0
    drivers = []
    for item in results:
        delta = item.get("change")
        if delta is None:
            continue
        # Heuristic context only: rising USD or yields are often a headwind for gold, not a deterministic signal.
        direction = -1 if delta > 0 else 1 if delta < 0 else 0
        score += direction
        drivers.append({"series": item["id"], "change": delta,
                        "gold_context": "supportive" if direction > 0 else "headwind" if direction < 0 else "neutral"})
    bias = "supportive" if score >= 2 else "headwind" if score <= -2 else "mixed/unclear"
    return {"connected": any("value" in item for item in results), "provider": "FRED API", "series": results,
            "gold_context_score": score, "gold_context_bias": bias, "drivers": drivers,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "frequency_note": "Daily/lagged macro context, not tick-level and not a standalone trade trigger."}


def get_calendar():
    key = os.getenv("TRADING_ECONOMICS_API_KEY", "").strip()
    if not key:
        return {"connected": False, "provider": "Trading Economics", "reason": "TRADING_ECONOMICS_API_KEY not configured; calendar requires an authenticated plan", "events": []}
    now = datetime.now(timezone.utc)
    start = now.strftime("%Y-%m-%d")
    end = (now + timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        # Trading Economics' documented calendar API authenticates using the `c` query parameter.
        raw_data = _get(f"https://api.tradingeconomics.com/calendar/country/united%20states/{start}/{end}",
                        {"c": key, "f": "json"}).json()
        if not isinstance(raw_data, list):
            raise ValueError("Calendar response was not a JSON event list")
        events = []
        for item in raw_data:
            raw_time = item.get("Date") or item.get("date") or item.get("time")
            utc_time = _utc_iso(raw_time)
            importance = item.get("Importance", item.get("importance"))
            try:
                importance_num = int(importance)
            except (TypeError, ValueError):
                importance_num = None
            impact = ({1: "Low", 2: "Medium", 3: "High"}.get(importance_num)
                      or str(importance or "Unknown"))
            event_name = item.get("Event") or item.get("event") or item.get("Category") or item.get("category")
            events.append({
                "time": utc_time, "time_utc": utc_time, "time_sast": _sast_display(raw_time),
                "event": event_name, "country": item.get("Country") or item.get("country"),
                "impact": impact, "importance": importance_num,
                "actual": item.get("Actual", item.get("actual")),
                "forecast": item.get("Forecast", item.get("forecast")),
                "previous": item.get("Previous", item.get("previous")),
                "revised": item.get("Revised", item.get("revised")),
                "unit": item.get("Unit") or item.get("unit"), "source": "Trading Economics",
                "source_name": item.get("Source") or item.get("source"),
                "url": item.get("URL") or "https://tradingeconomics.com/calendar",
            })
        return {"connected": True, "provider": "Trading Economics API", "events": events,
                "updated_at": datetime.now(timezone.utc).isoformat(), "timezone": "UTC source timestamps; SAST display supplied"}
    except Exception as exc:
        return {"connected": False, "provider": "Trading Economics API", "reason": safe_error(exc), "events": []}


def get_news():
    # GDELT is open-access discovery. It is not a licensed terminal feed; headlines need verification.
    query = '(gold OR XAUUSD OR "US dollar" OR Treasury OR Federal Reserve OR tariff OR war OR sanctions)'
    try:
        data = _get("https://api.gdeltproject.org/api/v2/doc/doc",
                    {"query": query, "mode": "ArtList", "format": "json", "sort": "DateDesc", "maxrecords": 20, "timespan": "24h"}).json()
        articles = []
        for item in data.get("articles", []):
            raw_published = item.get("seendate")
            published_utc = _utc_iso(raw_published)
            if not published_utc and isinstance(raw_published, str) and re.fullmatch(r"\d{8}T\d{6}Z", raw_published):
                try:
                    published_utc = datetime.strptime(raw_published, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
                except ValueError:
                    pass
            articles.append({"title": item.get("title"), "url": item.get("url"), "source": item.get("domain"),
                             "published": published_utc or raw_published, "language": item.get("language"), "provider": "GDELT"})
        return {"connected": True, "provider": "GDELT DOC API", "articles": articles,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "caveat": "Headline discovery only. Verify each source; headlines do not prove causal impact or price direction."}
    except Exception as exc:
        return {"connected": False, "provider": "GDELT DOC API", "reason": safe_error(exc), "articles": []}


def get_orderflow_status():
    if os.getenv("DATABENTO_API_KEY", "").strip():
        try:
            from databento_flow import active_flow_provider
            if active_flow_provider is not None:
                return active_flow_provider.snapshot(os.getenv("SYMBOL", "XAUUSD"))
            return {"connected": False, "provider": "Databento", "reason": "Databento key is configured but its live stream is not running."}
        except Exception as exc:
            return {"connected": False, "provider": "Databento", "reason": safe_error(exc)}
    provider = os.getenv("ORDER_FLOW_PROVIDER", "none").strip().lower()
    endpoint = os.getenv("ORDER_FLOW_API_URL", "").strip()
    token = os.getenv("ORDER_FLOW_API_TOKEN", "").strip()
    if provider == "rest" and endpoint and token:
        try:
            response = _get(endpoint, {"symbol": os.getenv("ORDER_FLOW_SYMBOL", "GC")}, {"Authorization": f"Bearer {token}"})
            data = response.json()
            if not all(k in data for k in ("timestamp", "symbol")):
                raise ValueError("Order-flow response missing timestamp or symbol")
            if data.get("delta_percent") is None and not (data.get("buy_volume") is not None and data.get("sell_volume") is not None):
                raise ValueError("Order-flow response must include delta_percent or classified buy_volume and sell_volume; CVD alone is not valid delta")
            return {"connected": True, "provider": "Configured REST order-flow feed", **data,
                    "caveat": "Confirm entitlement, exchange, contract mapping and data timestamp before use."}
        except Exception as exc:
            return {"connected": False, "provider": "Configured REST order-flow feed", "reason": safe_error(exc)}
    return {"connected": False, "provider": "none",
            "reason": "No licensed trade-level/depth provider configured. CVD, footprint and DOM remain unavailable until a real feed is supplied."}


def summarize_macro(macro, calendar, news, flow):
    bits = []
    if macro.get("connected"):
        bits.append(f"FRED macro context: {macro.get('gold_context_bias', 'mixed/unclear')} (heuristic score {macro.get('gold_context_score', 0)}; daily/lagged, not a trade trigger).")
        for item in macro.get("series", []):
            if "value" in item:
                bits.append(f"{item['label']}: {item['value']} ({item['date']}, change {item.get('change')}; {item['source']}; daily/lagged context).")
    else:
        bits.append("Macro series unavailable: configure FRED_API_KEY.")
    if calendar.get("connected"):
        bits.append(f"Economic calendar connected: {len(calendar.get('events', []))} US events returned; event times are displayed in SAST and retained in UTC for risk gates.")
    else:
        bits.append("Economic calendar unavailable: configure Trading Economics API credentials.")
    if news.get("connected"):
        bits.append(f"GDELT headline discovery: {len(news.get('articles', []))} articles retrieved; each needs source verification.")
    else:
        bits.append("News connector unavailable.")
    if not flow.get("connected"):
        bits.append("Order flow unavailable; no order-flow confirmation is permitted.")
    elif flow.get("depth_connected") is not True:
        bits.append("Trade-level flow is available but L2 depth is not confirmed; book-dependent trade gate remains closed.")
    return " ".join(bits)
