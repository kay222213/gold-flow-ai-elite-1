"""Fail-closed XAUUSD decision-support engine.

This engine emits a CANDIDATE only when every coded price-action gate passes.
It never places orders and never claims order-flow/fundamental confirmation without provider data.
MT5's final bar is normally still forming; this module explicitly excludes it.
"""
from __future__ import annotations
from math import floor, isfinite
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_UP
import time
from typing import Any
from market_context import intraday_session_levels, structure_state, fair_value_gaps, previous_day_levels


def _valid_bars(bars: list[dict], minimum: int = 60) -> bool:
    if not isinstance(bars, list) or len(bars) < minimum:
        return False
    previous_time = None
    for b in bars:
        try:
            vals = [float(b[k]) for k in ("open", "high", "low", "close")]
            if not all(isfinite(v) for v in vals): return False
            o,h,l,c = vals
            if l <= 0 or h < max(o,c) or l > min(o,c): return False
            # Timestamp is mandatory: without it we cannot prove candle ordering/freshness.
            if "time" not in b: return False
            t = int(b["time"])
            if t <= 0: return False
            if previous_time is not None and t <= previous_time: return False
            previous_time = t
        except (KeyError, TypeError, ValueError):
            return False
    return True


def atr(bars: list[dict], period: int = 14, include_forming: bool = True) -> float:
    if period < 1 or len(bars) < period + (2 if include_forming else 1):
        raise ValueError("Insufficient bars for ATR")
    # Live REST/MT5 feeds commonly include a forming bar; close-only webhook feeds do not.
    closed = bars[:-1] if include_forming else bars
    trs = []
    for i in range(1, len(closed)):
        b, prev = closed[i], closed[i-1]
        trs.append(max(float(b["high"])-float(b["low"]),
                       abs(float(b["high"])-float(prev["close"])),
                       abs(float(b["low"])-float(prev["close"]))))
    if len(trs) < period: raise ValueError("Insufficient closed bars for ATR")
    return sum(trs[-period:]) / period


def fib_golden_zone(bars: list[dict], lookback: int = 50, include_forming: bool = True) -> dict:
    closed_source = bars[:-1] if include_forming else bars
    if len(closed_source) < lookback: raise ValueError("Insufficient bars for Fibonacci swing")
    closed = closed_source[-lookback:]
    hi = max(float(b["high"]) for b in closed)
    lo = min(float(b["low"]) for b in closed)
    span = hi-lo
    if span <= 0: raise ValueError("Invalid swing range")
    return {
        "swing_high": hi, "swing_low": lo,
        "bullish_zone": [hi-span*0.618, hi-span*0.65],
        "bearish_zone": [lo+span*0.618, lo+span*0.65],
    }


def size_position(risk_money: float, entry: float, stop: float, tick_size: float,
                  tick_value_per_lot: float, volume_min: float, volume_max: float,
                  volume_step: float) -> dict:
    """tick_value_per_lot means account-currency loss per tick for one lot."""
    vals=(risk_money,entry,stop,tick_size,tick_value_per_lot,volume_min,volume_max,volume_step)
    if not all(isfinite(float(v)) for v in vals): raise ValueError("Non-finite sizing input")
    if risk_money <= 0 or entry <= 0 or stop <= 0 or entry == stop or min(tick_size,tick_value_per_lot,volume_min,volume_step) <= 0 or volume_max < volume_min:
        raise ValueError("Invalid risk or broker contract specification")
    distance=abs(entry-stop)
    loss_per_lot=(distance/tick_size)*tick_value_per_lot
    if loss_per_lot <= 0 or not isfinite(loss_per_lot): raise ValueError("Invalid loss per lot")
    raw=min(risk_money/loss_per_lot, volume_max)
    # Broker volume steps are usually incremented from the minimum volume, not necessarily
    # from zero. Round down from volume_min so odd step sizes cannot under-size incorrectly.
    if raw + 1e-12 < volume_min:
        volume = 0.0
    else:
        volume = volume_min + floor((raw - volume_min + 1e-12) / volume_step) * volume_step
    if volume < volume_min:
        return {"volume":0.0,"risk_money":0.0,"min_volume_risk":round(loss_per_lot*volume_min,2),
                "reason":"Minimum broker volume exceeds permitted risk."}
    volume=round(volume,8)
    return {"volume":volume,"risk_money":round(loss_per_lot*volume,2),"reason":None}



def _round_to_tick(price: float, tick_size: float | None, mode: str = "nearest") -> float:
    """Decimal-safe tick alignment; direction modes never pull risk prices inward."""
    px = Decimal(str(price))
    if tick_size is None or not isfinite(float(tick_size)) or float(tick_size) <= 0:
        return float(px.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    tick = Decimal(str(tick_size))
    units = px / tick
    if mode == "down": units = units.to_integral_value(rounding=ROUND_FLOOR)
    elif mode == "up": units = units.to_integral_value(rounding=ROUND_CEILING)
    else: units = units.to_integral_value(rounding=ROUND_HALF_UP)
    aligned = units * tick
    # Quantize to the actual Decimal scale (handles 0.001 and 1E-05 safely).
    scale = max(0, -tick.normalize().as_tuple().exponent)
    quantum = Decimal(1).scaleb(-min(10, scale))
    return float(aligned.quantize(quantum, rounding=ROUND_HALF_UP))


def build_trade_levels(side: str, entry: float, structural_stop: float, atr_value: float,
                       spread: float, tick_size: float | None, config: dict,
                       target_level: float | None = None) -> dict:
    """Build volatility-aware levels; risk percentage must never set price distances."""
    min_stop_price=float(config.get("min_stop_distance_price", 0.50))
    min_stop_atr=float(config.get("min_stop_atr_multiple", 1.0))
    stop_buffer_atr=float(config.get("stop_buffer_atr_multiple", 0.20))
    target_atr=float(config.get("target_atr_multiple", 1.50))
    min_rr=float(config.get("min_reward_risk", 2.0))
    if atr_value <= 0 or entry <= 0 or structural_stop <= 0:
        raise ValueError("Invalid entry, structural stop or ATR.")
    if side not in ("BUY", "SELL"):
        raise ValueError("Side must be BUY or SELL.")
    # Normalize a market-entry price outward to the executable side of the tick grid:
    # a BUY cannot assume a fill below its quoted ask; a SELL cannot assume a fill above bid.
    entry = _round_to_tick(entry, tick_size, "up" if side == "BUY" else "down")
    if side == "BUY":
        if structural_stop >= entry: raise ValueError("BUY structural stop must be below entry.")
        buffered_stop=structural_stop-max(atr_value*stop_buffer_atr, spread*1.5, (tick_size or 0)*2)
        min_distance=max(min_stop_price, atr_value*min_stop_atr, spread*2, (tick_size or 0)*5)
        stop=min(buffered_stop, entry-min_distance)
        risk_distance=entry-stop
        target_distance=max(min_rr*risk_distance, target_atr*atr_value)
        if target_level is not None:
            if target_level <= entry: raise ValueError("Nearest opposing liquidity objective is not above the BUY entry.")
            if target_level-entry+1e-9 < target_distance:
                raise ValueError("Nearest opposing liquidity objective cannot support the minimum reward:risk and ATR target; no trade.")
            target=target_level
        else:
            target=entry+target_distance
        stop=_round_to_tick(stop,tick_size,"down")
        target=_round_to_tick(target,tick_size,"down" if target_level is not None else "up")
    elif side == "SELL":
        if structural_stop <= entry: raise ValueError("SELL structural stop must be above entry.")
        buffered_stop=structural_stop+max(atr_value*stop_buffer_atr, spread*1.5, (tick_size or 0)*2)
        min_distance=max(min_stop_price, atr_value*min_stop_atr, spread*2, (tick_size or 0)*5)
        stop=max(buffered_stop, entry+min_distance)
        risk_distance=stop-entry
        target_distance=max(min_rr*risk_distance, target_atr*atr_value)
        if target_level is not None:
            if target_level >= entry: raise ValueError("Nearest opposing liquidity objective is not below the SELL entry.")
            if entry-target_level+1e-9 < target_distance:
                raise ValueError("Nearest opposing liquidity objective cannot support the minimum reward:risk and ATR target; no trade.")
            target=target_level
        else:
            target=entry-target_distance
        stop=_round_to_tick(stop,tick_size,"up")
        target=_round_to_tick(target,tick_size,"up" if target_level is not None else "down")
    risk_distance=abs(entry-stop)
    reward_distance=abs(target-entry)
    if risk_distance <= 0 or reward_distance <= 0:
        raise ValueError("Rounded trade levels are invalid.")
    return {"entry":entry,"stop_loss":stop,"take_profit":target,
            "risk_distance":round(risk_distance,8),"reward_distance":round(reward_distance,8),
            "reward_risk":round(reward_distance/risk_distance,2),
            "minimum_stop_distance":round(max(min_stop_price,atr_value*min_stop_atr,spread*2,(tick_size or 0)*5),8)}

def _bias(bars: list[dict], lookback: int, include_forming: bool = True) -> str:
    # Compare only closed candles, respecting source semantics.
    closed=bars[:-1] if include_forming else bars
    if len(closed) < lookback+1: return "unknown"
    a=float(closed[-1]["close"]); b=float(closed[-1-lookback]["close"])
    return "bullish" if a>b else "bearish" if a<b else "neutral"


def no_trade(reason: str, context: dict | None = None) -> dict:
    return {"status":"NO TRADE","side":None,"reason":reason,"reasons":[reason],"context":context or {}}


def _order_block_proxy(bars: list[dict], side: str, lookback: int=12, include_forming: bool = True):
    """Last opposite-colour candle before a directional impulse; heuristic zone, not verified institutional orders."""
    closed=bars[:-1] if include_forming else bars
    for b in reversed(closed[max(0,len(closed)-lookback-1):-1]):
        o,c=float(b["open"]),float(b["close"])
        if side=="bullish" and c<o: return {"low":float(b["low"]),"high":float(b["high"]),"time":b.get("time"),"type":"bullish_order_block_proxy"}
        if side=="bearish" and c>o: return {"low":float(b["low"]),"high":float(b["high"]),"time":b.get("time"),"type":"bearish_order_block_proxy"}
    return None

def _equal_liquidity_pools(bars: list[dict], tolerance: float, include_forming: bool = True):
    closed=bars[:-1] if include_forming else bars
    if len(closed)<10: return {"equal_high":None,"equal_low":None}
    sample=closed[-80:]; highs=[]; lows=[]
    for i in range(2,len(sample)-2):
        if float(sample[i]["high"])>=max(float(sample[j]["high"]) for j in (i-2,i-1,i+1,i+2)): highs.append(float(sample[i]["high"]))
        if float(sample[i]["low"])<=min(float(sample[j]["low"]) for j in (i-2,i-1,i+1,i+2)): lows.append(float(sample[i]["low"]))
    def pool(vals):
        for i in range(len(vals)-1,-1,-1):
            matches=[v for v in vals[:i] if abs(v-vals[i])<=tolerance]
            if matches: return round((sum(matches)+vals[i])/(len(matches)+1),5)
        return None
    return {"equal_high":pool(highs),"equal_low":pool(lows)}

def select_liquidity_target(side: str, entry: float, structural_stop: float, atr_value: float,
                            spread: float, tick_size: float | None, config: dict,
                            target_candidates: list[float]) -> dict:
    """Choose the nearest opposing liquidity level that can actually satisfy stop/ATR/R:R rules.

    A nearby pool that is too close must not cause the engine to abandon a valid farther pool,
    but targets are still actual structure levels, never an arbitrary invented distance.
    """
    if side == "BUY":
        candidates = sorted({float(x) for x in target_candidates if isfinite(float(x)) and float(x) > entry})
    elif side == "SELL":
        candidates = sorted({float(x) for x in target_candidates if isfinite(float(x)) and float(x) < entry}, reverse=True)
    else:
        raise ValueError("Side must be BUY or SELL.")
    last_reason = None
    for target in candidates:
        try:
            result = build_trade_levels(side, entry, structural_stop, atr_value, spread,
                                        tick_size, config, target)
        except ValueError as exc:
            last_reason = str(exc)
            # Only skip an opposing pool when it is too near; other errors require no trade.
            if "cannot support the minimum reward:risk and ATR target" in last_reason:
                continue
            raise
        if result["reward_risk"] + 1e-9 >= float(config.get("min_reward_risk", 2.0)):
            result["selected_liquidity_target"] = target
            result["selected_liquidity_target_distance"] = round(abs(target - entry), 8)
            return result
    if not candidates:
        raise ValueError("No opposing liquidity level exists beyond the entry price.")
    raise ValueError("No opposing liquidity level can support the configured stop distance, ATR objective and minimum reward:risk." +
                     (f" Last rejected pool: {last_reason}" if last_reason else ""))



def requires_price_plan(sizing_available: bool, quote: dict) -> bool:
    """True unless sizing inputs are complete, the quote has executable bid/ask,
    and contract specs have a verified source.
    """
    return (not sizing_available) or quote.get("quote_type") == "indicative_price_only" or quote.get("contract_specs_verified") is not True

def generate_signal(bars: dict[str,list[dict]], quote: dict, config: dict) -> dict[str,Any]:
    required=("M5","M15","H1","H4")
    if any(tf not in bars or not _valid_bars(bars[tf], 65) for tf in required):
        return no_trade("Insufficient or invalid timestamped M5/M15/H1/H4 candles.")
    # Live OHLC APIs normally include a still-forming final candle; confirmed-bar feeds do not.
    include_forming = quote.get("bars_include_forming_candle", True) is not False
    # Fresh quotes do not make stale candles current. Check the latest completed bar per timeframe.
    now_epoch=time.time()
    max_candle_ages={"M5":float(config.get("max_m5_candle_age_seconds",600)),
                      "M15":float(config.get("max_m15_candle_age_seconds",1800)),
                      "H1":float(config.get("max_h1_candle_age_seconds",7200)),
                      "H4":float(config.get("max_h4_candle_age_seconds",28800))}
    for tf in required:
        completed_index = -2 if include_forming else -1
        if len(bars[tf]) < abs(completed_index): return no_trade(f"Insufficient closed {tf} candles.")
        completed_time=int(bars[tf][completed_index]["time"])
        candle_age=now_epoch-completed_time
        if candle_age < 0 or candle_age > max_candle_ages[tf]:
            return no_trade(f"Latest completed {tf} candle is stale ({candle_age:.0f}s old); waiting for fresh source candles.",
                             {"timeframe":tf,"candle_age_seconds":round(candle_age,1),"maximum_age_seconds":max_candle_ages[tf]})
    try:
        age=float(quote.get("age_seconds",999))
        bid=float(quote.get("bid",0)); ask=float(quote.get("ask",0))
    except (TypeError,ValueError):
        return no_trade("Quote fields are invalid.")
    max_age=float(config.get("max_quote_age_seconds",90))
    if not isfinite(age) or age < 0 or age > max_age: return no_trade("Market quote is stale.")
    if not (isfinite(bid) and isfinite(ask) and bid>0 and ask>=bid): return no_trade("Invalid bid/ask quote.")
    spread=ask-bid
    max_spread=float(config.get("max_spread_price", config.get("max_spread", 0.50)))
    if spread > max_spread: return no_trade("Spread exceeds configured maximum.")
    specs=("tick_size","tick_value","volume_min","volume_max","volume_step")
    try: spec_values=[float(quote.get(k,0) or 0) for k in specs]
    except (TypeError,ValueError): return no_trade("Instrument contract specifications are invalid.")
    sizing_available=all(isfinite(v) and v>0 for v in spec_values)
    # Price-only feeds can support a technical price plan, but not reliable monetary sizing.
    # Never invent tick value, volume step, account currency conversion, or executable spread.
    m5,m15,h1,h4=(bars[k] for k in required)
    try:
        a=atr(m5,14,include_forming)
        fib=fib_golden_zone(m5,50,include_forming)
    except ValueError as e: return no_trade(str(e))
    min_atr=float(config.get("min_atr_price",0.25))
    if a < min_atr:
        return no_trade(f"ATR(14) is abnormally small for XAUUSD ({a:.5f}); feed/scale validation failed. Minimum accepted ATR is {min_atr:.2f} price units.",
                         {"atr14":round(a,5),"minimum_atr_price":min_atr})
    # Ignore current forming candle; use latest completed M5 candle for trigger.
    closed=m5[:-1] if include_forming else m5
    if len(closed) < 22: return no_trade("Insufficient completed M5 candles for a liquidity sweep.")
    trigger=closed[-1]; prior=closed[-2]
    swing=closed[-21:-1]
    swing_low=min(float(b["low"]) for b in swing)
    swing_high=max(float(b["high"]) for b in swing)
    h4_bias=_bias(h4,10,include_forming); h1_bias=_bias(h1,20,include_forming); m15_bias=_bias(m15,10,include_forming)
    mid=(bid+ask)/2
    body=abs(float(trigger["close"])-float(trigger["open"]))
    displaced=body >= 0.8*a
    bullish= float(trigger["close"])>float(trigger["open"])
    bearish= float(trigger["close"])<float(trigger["open"])
    swept_low=float(trigger["low"])<swing_low and float(trigger["close"])>swing_low
    swept_high=float(trigger["high"])>swing_high and float(trigger["close"])<swing_high
    fib_bull=fib["bullish_zone"][1] <= mid <= fib["bullish_zone"][0]
    fib_bear=fib["bearish_zone"][0] <= mid <= fib["bearish_zone"][1]
    structure=structure_state(m5,include_forming=include_forming); fvg=fair_value_gaps(m5,include_forming=include_forming)
    levels=intraday_session_levels(m5,include_forming=include_forming)
    equal_pools=_equal_liquidity_pools(m5,max(0.05*a,0.01),include_forming)
    bullish_ob=_order_block_proxy(m5,"bullish",include_forming=include_forming); bearish_ob=_order_block_proxy(m5,"bearish",include_forming=include_forming)
    pd_levels=previous_day_levels(bars.get("D1",[]),include_forming=include_forming)
    buy_target_candidates=[x for x in [swing_high,equal_pools.get("equal_high"),levels.get("asia_high"),levels.get("london_high"),levels.get("new_york_high"),pd_levels.get("high")] if x is not None and float(x)>ask]
    sell_target_candidates=[x for x in [swing_low,equal_pools.get("equal_low"),levels.get("asia_low"),levels.get("london_low"),levels.get("new_york_low"),pd_levels.get("low")] if x is not None and float(x)<bid]
    buy_target=min((float(x) for x in buy_target_candidates),default=None)
    sell_target=max((float(x) for x in sell_target_candidates),default=None)
    context={"h4_bias":h4_bias,"h1_bias":h1_bias,"m15_bias":m15_bias,"atr14":round(a,5),"spread":None if quote.get("quote_type")=="indicative_price_only" else round(spread,5),
             "fib":fib,"swept_low":swept_low,"swept_high":swept_high,"displacement":displaced,
             "in_golden_zone":fib_bull or fib_bear,"order_flow_confirmed":False,
             "trigger_candle_time":trigger.get("time"),"closed_candle_only":True,"bars_include_forming_candle":include_forming,"structure":structure,"fvg":fvg,
             "bullish_order_block_proxy":bullish_ob,"bearish_order_block_proxy":bearish_ob,"equal_liquidity":equal_pools,
             "previous_day_high":pd_levels.get("high"),"previous_day_low":pd_levels.get("low"),
             "nearest_buy_target":buy_target,"nearest_sell_target":sell_target,**levels}
    # Do not pretend an engulfing/sweep alone establishes inducement or a full reclaim-failure model.
    side=None; reasons=[]
    if h4_bias=="bullish" and h1_bias=="bullish" and m15_bias=="bullish" and swept_low and displaced and bullish and fib_bull:
        side="BUY"
        reasons=["H4, H1 and M15 closed-candle directional bias align bullish",
                 "Completed M5 candle swept a recent swing low and closed back above it",
                 "Completed M5 candle body exceeds 0.8 × ATR(14)",
                 "Mid-price is inside calculated 61.8–65% retracement zone"]
        entry=ask
        structural_stop=min(swing_low,float(trigger["low"]))
        try: levels=select_liquidity_target("BUY",entry,structural_stop,a,spread,quote.get("tick_size"),config,[float(x) for x in buy_target_candidates])
        except ValueError as e: return no_trade(f"Trade-level validation failed: {e}",context)
        context["selected_target_level"]=levels.get("selected_liquidity_target")
        context["selected_target_distance"]=levels.get("selected_liquidity_target_distance")
        reasons.append(f"Target selected from opposing liquidity at {levels.get('selected_liquidity_target')}")
        entry,stop,target=levels["entry"],levels["stop_loss"],levels["take_profit"]
    elif h4_bias=="bearish" and h1_bias=="bearish" and m15_bias=="bearish" and swept_high and displaced and bearish and fib_bear:
        side="SELL"
        reasons=["H4, H1 and M15 closed-candle directional bias align bearish",
                 "Completed M5 candle swept a recent swing high and closed back below it",
                 "Completed M5 candle body exceeds 0.8 × ATR(14)",
                 "Mid-price is inside calculated 61.8–65% retracement zone"]
        entry=bid
        structural_stop=max(swing_high,float(trigger["high"]))
        try: levels=select_liquidity_target("SELL",entry,structural_stop,a,spread,quote.get("tick_size"),config,[float(x) for x in sell_target_candidates])
        except ValueError as e: return no_trade(f"Trade-level validation failed: {e}",context)
        context["selected_target_level"]=levels.get("selected_liquidity_target")
        context["selected_target_distance"]=levels.get("selected_liquidity_target_distance")
        reasons.append(f"Target selected from opposing liquidity at {levels.get('selected_liquidity_target')}")
        entry,stop,target=levels["entry"],levels["stop_loss"],levels["take_profit"]
    else:
        missing=[]
        if len({h4_bias,h1_bias,m15_bias})!=1 or h4_bias not in ("bullish","bearish"): missing.append("H4/H1/M15 higher-timeframe bias not aligned")
        if not (swept_low or swept_high): missing.append("no completed-candle liquidity sweep/reclaim")
        if not displaced: missing.append("M5 displacement threshold not met")
        if not (fib_bull or fib_bear): missing.append("price outside Fibonacci golden zone")
        return no_trade("No complete setup: "+", ".join(missing)+". Order flow and inducement are not independently confirmed.",context)
    risk_pct=float(config.get("risk_percent",1)); equity=float(config.get("equity",0))
    daily_loss=float(config.get("max_daily_loss",0)); daily_target=float(config.get("daily_target",0)); daily_pnl=float(config.get("realized_pnl_today",0))
    if equity<=0 or risk_pct<=0 or risk_pct>5: return no_trade("Account equity or risk percentage is invalid.",context)
    if daily_loss>0 and daily_pnl <= -daily_loss: return no_trade("Manually reported daily loss limit reached; no new candidate.",context)
    if daily_target>0 and daily_pnl >= daily_target: return no_trade("Manually reported daily target reached; stop for the day.",context)
    risk_cash=equity*risk_pct/100
    if daily_loss>0: risk_cash=min(risk_cash,max(0,daily_loss+daily_pnl))
    if risk_cash<=0: return no_trade("No remaining daily risk budget.",context)
    rr=abs(target-entry)/abs(entry-stop)
    min_rr=float(config.get("min_reward_risk",2))
    if rr+1e-9<min_rr: return no_trade("Setup fails minimum reward-to-risk after price-increment rounding.",context)
    # Never emit a tiny stop/target merely because a feed reports a near-zero ATR.
    min_stop=float(config.get("min_stop_distance_price",0.50))
    if abs(entry-stop)+1e-9 < min_stop:
        return no_trade(f"Stop distance {abs(entry-stop):.5f} is below the minimum accepted {min_stop:.2f} XAUUSD price units.",context)

    quote_is_indicative = quote.get("quote_type") == "indicative_price_only"
    size_result = None
    if sizing_available:
        try:
            size_result=size_position(risk_cash,entry,stop,*spec_values)
        except ValueError as e:
            return no_trade(str(e),context)
        if size_result["volume"]<=0:
            return no_trade(size_result.get("reason") or "Position size invalid.",context)

    # Distinguish a technically valid PRICE PLAN from an execution-ready CANDIDATE.
    # An indicative quote cannot validate spread/entry and must never be presented as trade-ready.
    # Missing broker contract metadata must not erase already-computed structural levels; instead
    # show the plan and keep the execution gate locked until volume/risk can be verified.
    plan_only = requires_price_plan(sizing_available, quote)
    if plan_only:
        if not sizing_available:
            plan_reason = "Price plan calculated, but trade sizing is blocked: verified broker tick value, tick size, volume min/max/step are required."
        elif quote_is_indicative:
            plan_reason = "Price plan calculated from an indicative price. Executable bid/ask and current spread are not verified, so this is not a trade-ready signal."
        else:
            plan_reason = "Price plan calculated, but the contract specifications have not been verified against the execution broker. Sizing is an estimate; execution remains locked."
        return {"status":"PRICE PLAN","side":side,"symbol":quote.get("symbol","XAUUSD"),
                "entry":round(entry,5),"stop_loss":round(stop,5),"take_profit":round(target,5),
                "reward_risk":round(rr,2),"atr14":round(a,5),"spread":None if quote_is_indicative else round(spread,5),
                "risk_distance":round(abs(entry-stop),5),"reward_distance":round(abs(target-entry),5),
                "price_levels_are_indicative":quote_is_indicative,
                "position_volume":size_result["volume"] if size_result else None,
                "estimated_risk_account_currency":size_result["risk_money"] if size_result else None,
                "risk_currency":quote.get("account_currency") or config.get("account_currency") or None,
                "sizing_calculated":bool(size_result),"sizing_verified":bool(quote.get("contract_specs_verified") is True),
                "sizing_source":quote.get("specification_source") or ("provider contract metadata" if quote.get("contract_specs_verified") is True else "unverified configured/user-supplied contract specifications"),
                "execution_ready":False,"reason":plan_reason,
                "stop_rationale":"Beyond the swept swing extreme plus ATR/spread buffer",
                "target_rationale":"Nearest validated opposing liquidity level, accepted only if it supports the minimum reward:risk and ATR target",
                "reasons":reasons,"caveats":["PRICE PLAN ONLY — execution gate remains locked; no order is placed.",
                "Contract specifications and account-currency tick value must be checked against the actual execution broker before sizing can be treated as verified.",
                "Indicative prices do not include verified venue bid/ask or current spread; any volume/risk number shown is an estimate and excludes commissions, slippage, margin effects and unverified spread.",
                "Verify current execution-venue spread and stop/freeze levels before any manual trade.",
                "Price levels use a volatility-aware structural stop; this does not guarantee the stop survives market noise or slippage.",
                "CME futures trade delta is a proxy for OTC spot gold. Full book depth/footprint is only available if the connected data entitlement actually supplies it."],
                "context":context}

    # Execution-readiness requires executable bid/ask AND verified instrument specs.
    return {"status":"CANDIDATE","side":side,"symbol":quote.get("symbol","XAUUSD"),
            "entry":round(entry,5),"stop_loss":round(stop,5),"take_profit":round(target,5),
            "reward_risk":round(rr,2),"atr14":round(a,5),"spread":round(spread,5),
            "risk_distance":round(abs(entry-stop),5),"reward_distance":round(abs(target-entry),5),
            "price_levels_are_indicative":False,"sizing_calculated":True,
            "sizing_verified":bool(quote.get("contract_specs_verified") is True),
            "sizing_source":quote.get("specification_source") or ("provider contract metadata" if quote.get("contract_specs_verified") is True else "unverified configured/user-supplied contract specifications"),
            "execution_ready":False,"position_volume":size_result["volume"],
            "estimated_risk_account_currency":size_result["risk_money"],
            "risk_currency":quote.get("account_currency") or config.get("account_currency") or None,
            "stop_rationale":"Beyond the swept swing extreme plus ATR/spread buffer",
            "target_rationale":"Nearest validated opposing liquidity level, accepted only if it supports the configured minimum reward:risk and ATR distance",
            "reasons":reasons,"caveats":["Candidate only; no order is placed.",
            "Position size and account-currency risk are calculated only from verified instrument tick/volume specifications in the account currency.",
            "Validate commissions, slippage, margin and broker stop rules before execution.",
            "Price levels use a minimum volatility-aware stop distance; this does not guarantee the stop survives market noise or slippage.",
            "Order book depth/footprint, news, inducement and reclaim failure must be verified independently; they are not inferred from OHLC candles."],
            "context":context}
