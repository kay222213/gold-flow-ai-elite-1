"""Source-grounded OpenAI macro analyst for Gold Flow AI.

The API key is server-side only. This module never invents missing market fields; if the key or
upstream API is unavailable, it returns an explicit disconnected state rather than a fake analysis.
"""
from __future__ import annotations
import json
import os
import threading
import time
from datetime import datetime, timezone
import requests

_lock = threading.RLock()
_cache: dict = {"created": 0.0, "result": None}

SYSTEM_PROMPT = """You are the macro and geopolitical analyst inside Gold Flow AI — The Fundamental Edge.
Analyze XAUUSD with a disciplined institutional workflow. Use only supplied source-attributed data and any results returned by the explicitly enabled web_search tool. Never invent prices, yields, DXY values, economic actuals/forecasts, order flow, timestamps, or causal claims. Clearly separate (1) verified facts with source/time, (2) market reaction if the data supports it, (3) plausible interpretation, and (4) alternative scenarios / invalidation. Treat headlines as unconfirmed until cross-checked. Explain whether USD/yields/real yields and scheduled US events are supportive, bearish, mixed or unknown for gold, including conflicts and lags. Do not infer a trade from a geopolitical headline alone. If data is missing or stale, say so. Give a concise structured output with: DATA STATUS; MACRO REGIME; GEOPOLITICAL WATCH; GOLD BIAS (bullish/bearish/mixed/unknown, never certainty); WHAT WOULD CONFIRM; WHAT WOULD INVALIDATE; EVENT RISK; SOURCE LIMITATIONS. No price target or trade signal unless the supplied market analysis engine already generated a valid candidate. No guarantees."""


def _output_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"].strip()
    chunks=[]
    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for c in item.get("content", []) or []:
            if c.get("type") in ("output_text", "text") and isinstance(c.get("text"), str):
                chunks.append(c["text"])
    return "\n".join(chunks).strip()


def _citations(payload: dict) -> list[dict]:
    found=[]
    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for c in item.get("content", []) or []:
            for a in c.get("annotations", []) or []:
                if a.get("type") in ("url_citation", "web_search_result_location"):
                    entry={"title":a.get("title") or a.get("url") or "Source", "url":a.get("url")}
                    if entry["url"] and entry not in found:
                        found.append(entry)
    return found[:15]


def get_ai_macro_analysis(macro: dict, calendar: dict, news: dict, flow: dict,
                          quote: dict | None = None, signal: dict | None = None) -> dict:
    key=os.getenv("OPENAI_API_KEY", "").strip()
    now=datetime.now(timezone.utc).isoformat()
    if not key:
        return {"connected":False,"provider":"OpenAI Responses API","reason":"OPENAI_API_KEY not configured on the backend.",
                "analysis":"AI macro analysis is not connected. Source-backed feeds are shown separately; no generated macro conclusion is being claimed.",
                "updated_at":now,"citations":[]}
    refresh_seconds=max(60,int(os.getenv("AI_MACRO_REFRESH_SECONDS","900")))
    with _lock:
        if _cache["result"] and time.monotonic()-_cache["created"] < refresh_seconds:
            return dict(_cache["result"])
    evidence={
        "analysis_time_utc":now,
        "market_quote":({k:quote.get(k) for k in ("symbol","mid","bid","ask","spread","timestamp","age_seconds","source","quote_type")} if quote else None),
        "signal_engine":({k:signal.get(k) for k in ("status","side","reason","entry","stop_loss","take_profit","reward_risk","reasons")} if signal else None),
        "macro":macro or {},
        "economic_calendar":calendar or {},
        "news_headlines":(news or {}).get("articles", [])[:20],
        "order_flow":flow or {},
    }
    model=os.getenv("OPENAI_MODEL","gpt-4.1-mini").strip() or "gpt-4.1-mini"
    use_web=os.getenv("OPENAI_WEB_SEARCH","true").strip().lower() in ("1","true","yes","on")
    body={"model":model,"instructions":SYSTEM_PROMPT,
          "input":"Analyze these current source-attributed records. Be explicit about their timestamps, frequency, uncertainty, and missing feeds. Do not create a trade recommendation if the engine status is not CANDIDATE. Evidence JSON:\n"+json.dumps(evidence,ensure_ascii=False,default=str)[:24000],
          "max_output_tokens":1100}
    if use_web:
        body["tools"]=[{"type":"web_search"}]
    try:
        r=requests.post("https://api.openai.com/v1/responses",headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},json=body,timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS","35")))
        r.raise_for_status()
        payload=r.json(); text=_output_text(payload)
        if not text:
            raise RuntimeError("OpenAI response had no text output.")
        result={"connected":True,"provider":"OpenAI Responses API","model":model,"analysis":text,
                "updated_at":now,"citations":_citations(payload),"web_search_enabled":use_web,
                "note":"Generated interpretation of supplied sources; not a verified price feed or trade guarantee."}
    except Exception as exc:
        # Do not expose request headers, the API key, or full upstream error payload.
        reason=f"AI analyst request failed ({type(exc).__name__}). Check API key, model access, billing and backend logs."
        result={"connected":False,"provider":"OpenAI Responses API","model":model,"reason":reason,
                "analysis":"AI macro analysis is temporarily unavailable. The application remains in guarded mode and will not fabricate an AI conclusion.",
                "updated_at":now,"citations":[],"web_search_enabled":use_web}
    with _lock:
        _cache["created"]=time.monotonic(); _cache["result"]=dict(result)
    return result
