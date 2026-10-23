from __future__ import annotations
import asyncio, os, logging, secrets, hmac, re, copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from market_data import UnconfiguredProvider
from engine import generate_signal
from intelligence import get_fred_macro, get_calendar, get_news, get_orderflow_status, summarize_macro
from ai_macro import get_ai_macro_analysis

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO")); log=logging.getLogger("goldflow")
SYMBOL=os.getenv("SYMBOL","XAUUSD")
POLL_SECONDS=max(1,float(os.getenv("POLL_SECONDS","3")))
MAX_AGE=max(5,float(os.getenv("MAX_QUOTE_AGE_SECONDS","90")))
CONFIG={"equity":float(os.getenv("ACCOUNT_EQUITY","1000")),"risk_percent":float(os.getenv("RISK_PERCENT","1")),
        "max_daily_loss":float(os.getenv("MAX_DAILY_LOSS","200")),"daily_target":float(os.getenv("DAILY_TARGET","2000")),
        "realized_pnl_today":float(os.getenv("REALIZED_PNL_TODAY","0")),
        "min_reward_risk":float(os.getenv("MIN_REWARD_RISK","2")),"max_spread_price":float(os.getenv("MAX_SPREAD_PRICE","0.50")),
        "min_atr_price":float(os.getenv("MIN_ATR_PRICE","0.25")),
        "min_stop_distance_price":float(os.getenv("MIN_STOP_DISTANCE_PRICE","0.50")),
        "min_stop_atr_multiple":float(os.getenv("MIN_STOP_ATR_MULTIPLE","1.0")),
        "stop_buffer_atr_multiple":float(os.getenv("STOP_BUFFER_ATR_MULTIPLE","0.20")),
        "target_atr_multiple":float(os.getenv("TARGET_ATR_MULTIPLE","1.50")),
        "max_m5_candle_age_seconds":float(os.getenv("MAX_M5_CANDLE_AGE_SECONDS","600")),
        "max_m15_candle_age_seconds":float(os.getenv("MAX_M15_CANDLE_AGE_SECONDS","1800")),
        "max_h1_candle_age_seconds":float(os.getenv("MAX_H1_CANDLE_AGE_SECONDS","7200")),
        "max_h4_candle_age_seconds":float(os.getenv("MAX_H4_CANDLE_AGE_SECONDS","28800")),
        "max_quote_age_seconds":MAX_AGE}
provider=UnconfiguredProvider(); tv_bridge=None; poll_task=None; intel_task=None; flow_service=None
analysis_snapshot={"bars":None,"quote":None,"flow_snapshot":None,"updated_at":None}

def safe_error(exc: Exception) -> str:
    """Avoid returning provider URLs/API query strings that may contain credentials."""
    message=str(exc)
    message=re.sub(r'(?i)(apikey|api_key|token|key)=([^&\s]+)', r'\1=[REDACTED]', message)
    message=re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*', r'\1[REDACTED]', message)
    return message[:500]

latest={"status":"unavailable","quote":None,"signal":{"status":"NO TRADE","side":None,"reason":"No live market-data provider configured."},
        "updated_at":None,"source":None,"error":None,"calendar_status":"PROVIDER NOT CONNECTED",
        "fundamental_summary":"No source-backed macro provider configured.","intelligence":{"macro":{},"calendar":{"events":[]},"news":{"articles":[]},"order_flow":{"connected":False}},
        "providers":{"market":{"connected":False,"provider":"none"},"calendar":{"connected":False,"provider":"none"},
                     "news":{"connected":False,"provider":"none"},"macro":{"connected":False,"provider":"none"},
                     "order_flow":{"connected":False,"provider":"none"},"ai_macro":{"connected":False,"provider":"OpenAI Responses API"}}}

async def intelligence_loop():
    while True:
        try:
            macro,cal,news,flow=await asyncio.gather(asyncio.to_thread(get_fred_macro),asyncio.to_thread(get_calendar),asyncio.to_thread(get_news),asyncio.to_thread(get_orderflow_status))
            ai=await asyncio.to_thread(get_ai_macro_analysis,macro,cal,news,flow,latest.get("quote"),latest.get("signal"))
            latest["intelligence"]={"macro":macro,"calendar":cal,"news":news,"order_flow":flow,"ai_analysis":ai,"updated_at":datetime.now(timezone.utc).isoformat()}
            latest["calendar_status"]="CONNECTED" if cal.get("connected") else "PROVIDER NOT CONNECTED"
            latest["fundamental_summary"]=summarize_macro(macro,cal,news,flow)
            latest["providers"]={**latest["providers"],"macro":{"connected":macro.get("connected",False),"provider":macro.get("provider","FRED")},
                "ai_macro":{"connected":ai.get("connected",False),"provider":ai.get("provider","OpenAI Responses API"),"reason":ai.get("reason")},
                "calendar":{"connected":cal.get("connected",False),"provider":cal.get("provider","none")},
                "news":{"connected":news.get("connected",False),"provider":news.get("provider","none")},
                "order_flow":{"connected":flow.get("connected",False),"provider":flow.get("provider","none"),"reason":flow.get("reason")}}
        except Exception as e: log.warning("Intelligence refresh failed: %s",e)
        await asyncio.sleep(max(60,float(os.getenv("INTELLIGENCE_REFRESH_SECONDS","300"))))

@asynccontextmanager
async def lifespan(app: FastAPI):
    global provider,poll_task,intel_task,tv_bridge,flow_service
    source=os.getenv("MARKET_DATA_SOURCE","twelvedata" if os.getenv("TWELVEDATA_API_KEY") else "none").lower()
    try:
        if source=="twelvedata":
            from twelvedata_adapter import TwelveDataProvider
            provider=TwelveDataProvider()
            # Use push pricing when the selected Twelve Data plan permits it; the adapter
            # automatically falls back to rate-limited REST pricing when the stream is unavailable.
            if hasattr(provider,"start_stream"):
                provider.start_stream()
        elif source=="tradingview_webhook":
            from tradingview_webhook import TradingViewWebhookProvider
            tv_bridge=TradingViewWebhookProvider(); provider=tv_bridge
        elif source=="mt5":
            from mt5_adapter import MT5Provider
            provider=MT5Provider()
        else: provider=UnconfiguredProvider()
    except Exception as exc:
        provider=UnconfiguredProvider(); latest["error"]=safe_error(exc)
        log.warning("Market provider not initialized; app remains available in NO TRADE mode: %s",exc)
    latest["providers"]["market"]={"connected":not isinstance(provider,UnconfiguredProvider),"provider":source,
        "reason":latest.get("error") if isinstance(provider,UnconfiguredProvider) else None}
    if os.getenv("DATABENTO_API_KEY","").strip():
        try:
            from databento_flow import DatabentoGoldFlow
            flow_service=DatabentoGoldFlow(); flow_service.start()
        except Exception as exc: log.warning("Order-flow provider could not start: %s",exc)
    poll_task=asyncio.create_task(poll_loop()); intel_task=asyncio.create_task(intelligence_loop())
    yield
    if flow_service:
        try: flow_service.stop()
        except Exception: pass
    if hasattr(provider,"stop_stream"):
        try: provider.stop_stream()
        except Exception: pass
    for task in (poll_task,intel_task):
        if task:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass

app=FastAPI(title="Gold Flow AI API",version="4.0.0",lifespan=lifespan)
origins=[x.strip() for x in os.getenv("CORS_ORIGINS","https://kay222213.github.io,http://localhost:8000,http://127.0.0.1:8000,http://localhost:5500,http://127.0.0.1:5500").split(",") if x.strip()]
app.add_middleware(CORSMiddleware,allow_origins=origins,allow_methods=["GET","POST"],allow_headers=["*"])


def apply_confirmation_gates(signal: dict, flow_snapshot: dict | None, events: list[dict], calendar: dict | None = None) -> dict:
    """Apply mandatory flow, calendar and event gates consistently across analysis paths."""
    flow_required=os.getenv("ORDER_FLOW_REQUIRED","true").lower() in ("1","true","yes")
    book_required=os.getenv("ORDER_BOOK_REQUIRED","true").lower() in ("1","true","yes")
    calendar_required=os.getenv("ECONOMIC_CALENDAR_REQUIRED","true").lower() in ("1","true","yes")
    if flow_snapshot and flow_snapshot.get("connected") and signal.get("side"):
        flow_values=flow_snapshot.get("snapshot",flow_snapshot)
        try:
            raw_flow_time=flow_values.get("timestamp")
            if not raw_flow_time: raise ValueError("missing timestamp")
            flow_time=datetime.fromisoformat(str(raw_flow_time).replace("Z","+00:00"))
            if flow_time.tzinfo is None: flow_time=flow_time.replace(tzinfo=timezone.utc)
            flow_age=(datetime.now(timezone.utc)-flow_time.astimezone(timezone.utc)).total_seconds()
            if flow_age < 0 or flow_age > max(30,2*int(os.getenv("ORDER_FLOW_WINDOW_SECONDS","300"))):
                raise ValueError(f"stale order-flow timestamp ({flow_age:.1f}s old)")
        except Exception as exc:
            return {"status":"NO TRADE","side":None,"reason":f"Order-flow confirmation failed: {exc}.",
                    "reasons":["Order flow must be timestamped and fresh."],"context":signal.get("context",{})}
        if flow_values.get("delta_percent") is not None:
            try: dp=float(flow_values.get("delta_percent"))
            except (TypeError,ValueError):
                return {"status":"NO TRADE","side":None,"reason":"Order-flow delta is not numeric.","reasons":["Invalid order-flow schema."],"context":signal.get("context",{})}
        elif flow_values.get("buy_volume") is not None and flow_values.get("sell_volume") is not None:
            try:
                buy_vol=float(flow_values.get("buy_volume") or 0); sell_vol=float(flow_values.get("sell_volume") or 0)
                total=buy_vol+sell_vol
                if total<=0: raise ValueError("Order-flow window has no classified buy/sell volume.")
                dp=(buy_vol-sell_vol)/total*100
            except (TypeError,ValueError) as exc:
                return {"status":"NO TRADE","side":None,"reason":str(exc),"reasons":["Invalid order-flow schema."],"context":signal.get("context",{})}
        else:
            return {"status":"NO TRADE","side":None,"reason":"Order-flow provider must supply timestamped delta_percent or classified buy_volume and sell_volume; cumulative CVD alone is insufficient.",
                    "reasons":["Ambiguous flow schema cannot validate a trade."],"context":signal.get("context",{})}
        if not (-100.0 <= dp <= 100.0):
            return {"status":"NO TRADE","side":None,"reason":"Order-flow delta percentage is outside the valid -100..100 range.","reasons":["Invalid order-flow delta."],"context":signal.get("context",{})}
        threshold=float(os.getenv("ORDER_FLOW_MIN_DELTA_PERCENT","5"))
        flow_ok=(dp>=threshold if signal["side"]=="BUY" else dp<=-threshold)
        signal.setdefault("context",{})["order_flow_confirmed"]=flow_ok
        signal["context"]["order_flow_delta_percent"]=dp
        signal["context"]["order_flow_provider"]=flow_snapshot.get("provider")
        if not flow_ok:
            return {"status":"NO TRADE","side":None,"reason":f"Order-flow confirmation failed: CME GC futures aggressor delta does not confirm the price-action side (delta {dp:.1f}%).",
                    "reasons":["Order flow is a required gate; futures flow is only a proxy for OTC spot gold."],"context":signal.get("context",{})}
    elif signal.get("status")=="CANDIDATE" and flow_required:
        return {"status":"NO TRADE","side":None,"reason":"Order-flow feed is required but unavailable. No trade candidate until real trade-level flow is connected.",
                "reasons":["CVD/footprint/depth must not be inferred from ordinary OHLC candles."],"context":signal.get("context",{})}

    if signal.get("status")=="CANDIDATE":
        now=datetime.now(timezone.utc)
        # A missing calendar must not be interpreted as an empty event schedule. Require a
        # timestamped, recently refreshed calendar before clearing the high-impact-event gate.
        if calendar_required:
            calendar_state = calendar if isinstance(calendar, dict) else {}
            if calendar_state.get("connected") is not True:
                return {"status":"NO TRADE","side":None,
                        "reason":"Economic calendar is required but unavailable; event risk has not been verified.",
                        "reasons":["No candidate is allowed when the configured calendar provider is disconnected."],
                        "context":signal.get("context",{})}
            try:
                raw_calendar_update = calendar_state.get("updated_at")
                if not raw_calendar_update: raise ValueError("missing update timestamp")
                calendar_update = datetime.fromisoformat(str(raw_calendar_update).replace("Z","+00:00"))
                if calendar_update.tzinfo is None: calendar_update = calendar_update.replace(tzinfo=timezone.utc)
                calendar_age = (now-calendar_update.astimezone(timezone.utc)).total_seconds()
                max_calendar_age = max(900.0, 3.0*float(os.getenv("INTELLIGENCE_REFRESH_SECONDS","300")))
                if calendar_age < -60 or calendar_age > max_calendar_age:
                    raise ValueError(f"calendar data is stale ({calendar_age:.0f}s old)")
            except (TypeError, ValueError) as exc:
                return {"status":"NO TRADE","side":None,
                        "reason":f"Economic calendar freshness check failed: {exc}.",
                        "reasons":["A timestamped, recent calendar response is required before a candidate can pass."],
                        "context":signal.get("context",{})}
        for ev in events or []:
            impact=str(ev.get("impact","")).lower(); raw=ev.get("time")
            if not any(tag in impact for tag in ("high","3","major")): continue
            if not raw:
                return {"status":"NO TRADE","side":None,
                        "reason":f"High-impact event has no usable timestamp: {ev.get('event','scheduled US release')}; blackout cannot be checked.",
                        "reasons":["Unknown event time fails the event-risk gate."],"context":signal.get("context",{})}
            try:
                event_time=datetime.fromisoformat(str(raw).replace("Z","+00:00"))
                if event_time.tzinfo is None: event_time=event_time.replace(tzinfo=timezone.utc)
                if abs((event_time-now).total_seconds())<=int(os.getenv("NEWS_BLACKOUT_MINUTES","15"))*60:
                    return {"status":"NO TRADE","side":None,"reason":f"High-impact event blackout: {ev.get('event','scheduled US release')} is within the configured blackout window.",
                            "reasons":["Avoid new candidates around high-impact releases; spreads and slippage can expand."],"context":signal.get("context",{})}
            except (TypeError,ValueError):
                return {"status":"NO TRADE","side":None,
                        "reason":f"High-impact event timestamp is invalid: {ev.get('event','scheduled US release')}; blackout cannot be checked.",
                        "reasons":["Invalid event time fails the event-risk gate."],"context":signal.get("context",{})}
    # L2 depth is a separate gate from aggressor delta. MBP-10 is CME GC futures depth,
    # not OTC spot-XAUUSD depth and not proof of footprint/absorption.
    if signal.get("status") == "CANDIDATE" and book_required:
        values = (flow_snapshot or {}).get("snapshot", flow_snapshot or {})
        if not isinstance(values, dict) or values.get("depth_connected") is not True:
            return {"status": "NO TRADE", "side": None,
                    "reason": "Fresh L2 order-book data is required but unavailable. Configure a licensed MBP-10/depth feed; no book data is inferred from candles.",
                    "reasons": ["Trade delta alone is not sufficient while the configured L2 depth gate is enabled."],
                    "context": signal.get("context", {})}
        try:
            raw_book_time = values.get("depth_timestamp")
            if not raw_book_time: raise ValueError("missing L2 timestamp")
            book_time = datetime.fromisoformat(str(raw_book_time).replace("Z", "+00:00"))
            if book_time.tzinfo is None: book_time = book_time.replace(tzinfo=timezone.utc)
            book_age = (datetime.now(timezone.utc) - book_time.astimezone(timezone.utc)).total_seconds()
            max_book_age = max(1.0, float(os.getenv("ORDER_BOOK_MAX_AGE_SECONDS", "30")))
            if book_age < 0 or book_age > max_book_age: raise ValueError(f"L2 snapshot is stale ({book_age:.1f}s old)")
            imbalance = float(values.get("depth_imbalance_percent"))
            if not (-100.0 <= imbalance <= 100.0): raise ValueError("invalid L2 imbalance range")
        except (TypeError, ValueError) as exc:
            return {"status": "NO TRADE", "side": None, "reason": f"Order-book confirmation failed: {exc}.",
                    "reasons": ["L2 book must be timestamped, fresh and valid."], "context": signal.get("context", {})}
        threshold = float(os.getenv("ORDER_BOOK_MIN_IMBALANCE_PERCENT", "5"))
        book_ok = imbalance >= threshold if signal.get("side") == "BUY" else imbalance <= -threshold
        signal.setdefault("context", {})["order_book_imbalance_percent"] = imbalance
        signal["context"]["order_book_confirmed"] = book_ok
        signal["context"]["order_book_timestamp"] = values.get("depth_timestamp")
        signal["context"]["order_book_provider"] = values.get("depth_provider") or values.get("provider")
        if not book_ok:
            return {"status": "NO TRADE", "side": None,
                    "reason": f"Fresh CME GC L2 depth does not confirm the price-action side (depth imbalance {imbalance:.1f}%).",
                    "reasons": ["L2 depth is a confluence gate, not a standalone predictor; futures/spot mismatch remains."],
                    "context": signal.get("context", {})}
    return signal


async def poll_loop():
    while True:
        stamp=datetime.now(timezone.utc).isoformat()
        try:
            frames=("M5","M15","H1","H4","D1")
            fetched=await asyncio.gather(asyncio.to_thread(provider.get_quote,SYMBOL),
                *(asyncio.to_thread(provider.get_bars,SYMBOL,tf,350) for tf in frames))
            q=fetched[0]; bars={tf:result for tf,result in zip(frames,fetched[1:])}
            # Price-only feeds do not know the user's broker contract specs. Optional values can be
            # configured server-side from the broker's instrument specification without MT5 access.
            tick_size=q.tick_size if q.tick_size is not None else (float(os.getenv("BROKER_TICK_SIZE")) if os.getenv("BROKER_TICK_SIZE","").strip() else None)
            tick_value=q.tick_value if q.tick_value is not None else (float(os.getenv("BROKER_TICK_VALUE_ACCOUNT_CCY")) if os.getenv("BROKER_TICK_VALUE_ACCOUNT_CCY","").strip() else None)
            volume_min=q.volume_min if q.volume_min is not None else (float(os.getenv("BROKER_VOLUME_MIN")) if os.getenv("BROKER_VOLUME_MIN","").strip() else None)
            volume_max=q.volume_max if q.volume_max is not None else (float(os.getenv("BROKER_VOLUME_MAX")) if os.getenv("BROKER_VOLUME_MAX","").strip() else None)
            volume_step=q.volume_step if q.volume_step is not None else (float(os.getenv("BROKER_VOLUME_STEP")) if os.getenv("BROKER_VOLUME_STEP","").strip() else None)
            account_currency=q.account_currency or os.getenv("BROKER_ACCOUNT_CURRENCY","").strip() or None
            server_specs_complete=all(os.getenv(k,"").strip() for k in ("BROKER_TICK_SIZE","BROKER_TICK_VALUE_ACCOUNT_CCY","BROKER_VOLUME_MIN","BROKER_VOLUME_MAX","BROKER_VOLUME_STEP"))
            server_specs_attested=os.getenv("BROKER_SPECS_VERIFIED","false").strip().lower() in ("1","true","yes","on")
            provider_specs=bool(getattr(provider,"provides_verified_contract_specs",False))
            specs_complete=all(v is not None for v in (tick_size,tick_value,volume_min,volume_max,volume_step))
            specs_verified=bool(specs_complete and (provider_specs or (server_specs_complete and server_specs_attested)))
            specs_source=("provider contract metadata" if provider_specs and specs_complete else
                          "operator-attested backend configuration" if server_specs_complete and server_specs_attested else
                          "backend environment configuration; not independently verified" if server_specs_complete else
                          "not configured")
            quote={"symbol":q.symbol,"bid":q.bid,"ask":q.ask,"mid":q.mid,"spread":q.spread,"timestamp":q.timestamp.isoformat(),
                   "age_seconds":q.age_seconds(),"source":q.source,"tick_size":tick_size,"tick_value":tick_value,
                   "contract_size":q.contract_size,"volume_min":volume_min,"volume_max":volume_max,"volume_step":volume_step,
                   "account_currency":account_currency,"contract_specs_verified":specs_verified,
                   "specification_source":specs_source,
                   "quote_type":"executable" if q.ask>q.bid and q.bid>0 else "indicative_price_only",
                   "bars_include_forming_candle":getattr(provider,"bars_include_forming_candle",True)}
            cfg={**CONFIG,"max_quote_age_seconds":MAX_AGE}
            signal=generate_signal(bars,quote,cfg)
            # Cache a copy of raw inputs so the API can apply each phone/browser's own risk settings
            # without mutating another user's settings or bypassing the same flow/event confirmation gates.
            analysis_snapshot.update({"bars":copy.deepcopy(bars),"quote":copy.deepcopy(quote),"flow_snapshot":None,"updated_at":stamp})
            flow_snapshot=None
            try:
                from databento_flow import active_flow_provider
                if active_flow_provider is not None:
                    flow_snapshot=active_flow_provider.snapshot(SYMBOL)
                elif os.getenv("ORDER_FLOW_PROVIDER", "none").lower()=="rest":
                    flow_snapshot=await asyncio.to_thread(get_orderflow_status)
            except Exception as flow_exc:
                log.warning("Order-flow snapshot unavailable: %s",flow_exc)
            analysis_snapshot["flow_snapshot"]=copy.deepcopy(flow_snapshot)
            # Keep the visible connection panel synchronized with the same live snapshot used by
            # the signal gate. Otherwise Databento can be feeding the engine while the UI says
            # "not connected" until the slower intelligence refresh runs.
            if flow_snapshot is not None:
                intelligence_state = copy.deepcopy(latest.get("intelligence") or {})
                intelligence_state["order_flow"] = copy.deepcopy(flow_snapshot)
                latest["intelligence"] = intelligence_state
                latest["providers"] = {**latest.get("providers", {}), "order_flow": {
                    "connected": bool(flow_snapshot.get("connected")),
                    "provider": flow_snapshot.get("provider", "unknown"),
                    "reason": flow_snapshot.get("reason"),
                    "depth_connected": bool(flow_snapshot.get("depth_connected")),
                    "depth_reason": flow_snapshot.get("depth_reason")}}
            calendar_state=latest.get("intelligence",{}).get("calendar",{}) or {}
            events=(calendar_state.get("events",[]) or [])
            signal=apply_confirmation_gates(signal,flow_snapshot,events,calendar_state)
            status="connected" if 0 <= q.age_seconds() <= MAX_AGE else "stale"
            latest.update({"status":status,"quote":quote,"signal":signal,"updated_at":stamp,"source":q.source,"error":None,
                "providers":{**latest["providers"],"market":{"connected":status=="connected","provider":q.source,"quote_age_seconds":round(q.age_seconds(),2),
                "stream":provider.stream_status() if hasattr(provider,"stream_status") else None}}})
        except Exception as exc:
            analysis_snapshot.update({"bars":None,"quote":None,"flow_snapshot":None,"updated_at":stamp})
            latest.update({"status":"unavailable","quote":None,"signal":{"status":"NO TRADE","side":None,
                "reason":"Live data or calculations unavailable; no trade candidate is permitted.","reasons":[safe_error(exc)]},
                "error":safe_error(exc),"updated_at":stamp,"providers":{**latest["providers"],"market":{"connected":False,"provider":os.getenv("MARKET_DATA_SOURCE","none"),"reason":safe_error(exc)}}})
            log.warning("Market polling failed: %s",exc)
        await asyncio.sleep(POLL_SECONDS)

@app.get("/api/health")
def health():
    return {"app":"Gold Flow AI","version":"4.0.0","service_status":"online","status":"ok","market_status":latest["status"],"source":latest["source"],"error":latest["error"],
            "poll_seconds":POLL_SECONDS,"max_quote_age_seconds":MAX_AGE,"signal_status":latest["signal"]["status"],
            "order_flow_required":os.getenv("ORDER_FLOW_REQUIRED","true").lower() in ("1","true","yes"),
            "order_book_required":os.getenv("ORDER_BOOK_REQUIRED","true").lower() in ("1","true","yes"),"providers":latest["providers"]}

@app.get("/api/state")
def state():
    """Return the latest shared feed/status snapshot. Use POST /api/analyze for per-device settings."""
    return latest


@app.post("/api/analyze")
def analyze_for_device(payload: dict):
    """Compute this device's decision with its private-local risk values, without persisting them server-side.

    Request bodies rather than query strings are used so account equity and daily P/L do not appear in URLs.
    This endpoint is analysis-only: it never places an order and cannot alter another user's settings.
    """
    out=copy.deepcopy(latest)
    if not analysis_snapshot.get("bars") or not analysis_snapshot.get("quote"):
        return out
    risk=payload.get("risk_settings") or {}
    cfg=dict(CONFIG)
    limits={"equity":(0.01,1e12),"risk_percent":(0.01,5),"max_daily_loss":(0,1e12),
            "daily_target":(0,1e12),"realized_pnl_today":(-1e12,1e12),"min_reward_risk":(1,20),"max_spread_price":(0,100)}
    for key,(lo,hi) in limits.items():
        if key not in risk or risk[key] is None:
            continue
        try: value=float(risk[key])
        except (TypeError,ValueError): raise HTTPException(status_code=400,detail=f"Invalid risk setting: {key}")
        if not (lo<=value<=hi): raise HTTPException(status_code=400,detail=f"{key} must be between {lo} and {hi}.")
        cfg[key]=value
    currency=str(risk.get("account_currency",os.getenv("BROKER_ACCOUNT_CURRENCY","ZAR"))).upper().strip()
    if currency not in {"ZAR","USD","EUR","GBP","AUD","CAD","CHF","NZD","JPY","SGD"}:
        raise HTTPException(status_code=400,detail="Unsupported account currency code.")
    cfg["account_currency"]=currency

    quote=copy.deepcopy(analysis_snapshot["quote"])
    provider_currency=str(quote.get("account_currency") or "").upper().strip()
    spec_payload=payload.get("instrument_specs") or {}
    spec_map={"tick_size":"tick_size","tick_value":"tick_value","volume_min":"volume_min",
              "volume_max":"volume_max","volume_step":"volume_step"}
    provided={k:spec_payload.get(k) for k in spec_map}
    supplied=[k for k,v in provided.items() if v not in (None,"")]
    if supplied:
        if len(supplied)!=len(spec_map):
            for key in spec_map.values(): quote[key]=None
            out["signal"]={"status":"NO TRADE","side":None,
                "reason":"Broker contract specifications are incomplete. Enter tick size, tick value per tick per lot in account currency, minimum/maximum volume and lot step together.",
                "reasons":["No mixed or guessed instrument metadata is permitted."],"context":{}}
            return out
        try:
            parsed={target:float(provided[source]) for source,target in spec_map.items()}
            if any(v<=0 for v in parsed.values()) or parsed["volume_max"]<parsed["volume_min"]:
                raise ValueError("All instrument values must be positive and maximum volume must be at least minimum volume.")
            quote.update(parsed)
            quote["contract_specs_verified"] = False
            quote["specification_source"] = "user-entered settings; not independently verified"
        except (TypeError,ValueError) as exc:
            raise HTTPException(status_code=400,detail=f"Invalid broker specification: {exc}")
    else:
        # Never relabel a provider's native tick value as another currency. A provider/broker tick
        # value is only comparable with risk amounts denominated in the same account currency.
        quote_currency=provider_currency or str(os.getenv("BROKER_ACCOUNT_CURRENCY","ZAR")).upper().strip()
        if quote_currency and quote_currency != currency and all(quote.get(k) is not None for k in spec_map.values()):
            for key in spec_map.values(): quote[key]=None
            quote["contract_specs_verified"]=False
            quote["specification_source"]=f"unusable: native tick value is {quote_currency}, selected risk currency is {currency}"
    # With no user override, use server contract metadata only if its currency matches the
    # selected risk currency; server values must not be silently converted or relabelled.
    quote["account_currency"]=currency
    signal=generate_signal(analysis_snapshot["bars"],quote,cfg)
    calendar_state=out.get("intelligence",{}).get("calendar",{}) or {}
    events=(calendar_state.get("events",[]) or [])
    signal=apply_confirmation_gates(signal,analysis_snapshot.get("flow_snapshot"),events,calendar_state)
    out["signal"]=signal
    out["analysis_settings"]={"scope":"per-request","risk_percent":cfg["risk_percent"],"equity":cfg["equity"],
        "account_currency":currency,"minimum_reward_risk":cfg["min_reward_risk"],"broker_specs_supplied_by_client":bool(supplied)}
    return out

@app.get("/api/intelligence")
def intelligence(): return latest["intelligence"]

@app.get("/api/config")
def config():
    return {"symbol":SYMBOL,"poll_seconds":POLL_SECONDS,"max_quote_age_seconds":MAX_AGE,"risk_percent":CONFIG["risk_percent"],
            "min_reward_risk":CONFIG["min_reward_risk"],"max_spread_price":CONFIG["max_spread_price"],
            "min_atr_price":CONFIG["min_atr_price"],"min_stop_distance_price":CONFIG["min_stop_distance_price"],
            "min_stop_atr_multiple":CONFIG["min_stop_atr_multiple"],"stop_buffer_atr_multiple":CONFIG["stop_buffer_atr_multiple"],
            "target_atr_multiple":CONFIG["target_atr_multiple"]}

@app.post("/api/settings")
def update_settings(payload: dict, request: Request):
    expected=os.getenv("ADMIN_API_TOKEN", "").strip()
    supplied=request.headers.get("x-admin-token", "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401,detail="Settings endpoint requires a valid X-Admin-Token configured in the backend environment.")
    allowed={"equity","risk_percent","max_daily_loss","daily_target","realized_pnl_today","min_reward_risk","max_spread_price"}; updates={}
    for key in allowed:
        if key in payload:
            try: value=float(payload[key])
            except (TypeError,ValueError): raise HTTPException(status_code=400,detail=f"Invalid numeric setting: {key}")
            if value<0 and key!="realized_pnl_today": raise HTTPException(status_code=400,detail=f"Setting cannot be negative: {key}")
            updates[key]=value
    if "equity" in updates and updates["equity"]<=0: raise HTTPException(status_code=400,detail="Equity must be positive")
    if "risk_percent" in updates and not 0<updates["risk_percent"]<=5: raise HTTPException(status_code=400,detail="Risk percentage must be above 0 and at most 5")
    if "min_reward_risk" in updates and updates["min_reward_risk"]<1: raise HTTPException(status_code=400,detail="Minimum reward:risk must be at least 1")
    if "max_spread_price" in updates and updates["max_spread_price"]<0: raise HTTPException(status_code=400,detail="Maximum spread cannot be negative")
    CONFIG.update(updates); return {"saved":True,"settings":{k:CONFIG[k] for k in allowed}}

@app.post("/api/webhook/tradingview")
async def tradingview_webhook(request: Request):
    global tv_bridge
    secret=os.getenv("TRADINGVIEW_WEBHOOK_TOKEN", "").strip()
    if len(secret)<24: raise HTTPException(status_code=503,detail="TradingView webhook is disabled until a server-side shared secret of 24+ characters is configured.")
    try: payload=await request.json()
    except Exception: raise HTTPException(status_code=400,detail="Expected JSON alert payload.")
    # Create a bridge for ingestion even if Twelve Data is the selected live provider.
    if tv_bridge is None:
        from tradingview_webhook import TradingViewWebhookProvider
        try: tv_bridge=TradingViewWebhookProvider()
        except Exception as e: raise HTTPException(status_code=503,detail=str(e))
    try: tv_bridge.accept(payload)
    except PermissionError: raise HTTPException(status_code=401,detail="Invalid webhook token.")
    except Exception as e: raise HTTPException(status_code=400,detail=str(e))
    return {"accepted":True,"timeframe":payload.get("timeframe"),"note":"Confirmed bar received; this is not an order execution or full order-flow feed."}

@app.get("/api/tradingview/bridge-status")
def tradingview_bridge_status():
    if tv_bridge is None: return {"connected":False,"reason":"No TradingView alerts received / bridge not initialized."}
    return {"connected":bool(tv_bridge.latest),"timeframes":{tf:len(bars) for tf,bars in tv_bridge.bars.items()},"source":"TradingView Pine webhook · confirmed OHLC only"}

@app.websocket("/api/stream")
async def stream(ws:WebSocket):
    await ws.accept()
    try:
        last_stamp=None
        while True:
            stamp=latest.get("updated_at")
            if stamp!=last_stamp: await ws.send_json(latest); last_stamp=stamp
            await asyncio.sleep(min(POLL_SECONDS,1))
    except WebSocketDisconnect: return


# Serve the phone-first frontend from this API in the one-URL production deployment.
# GitHub Pages can still host frontend/ separately; those clients set this API URL in Data & Connections.
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True, check_dir=True), name="frontend")
