import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from engine import atr, fib_golden_zone, size_position, generate_signal
from market_context import session_name, high_low

def bars(n=110, base=2000.0, step=0.2, interval=300):
    out=[]
    start=int(time.time())-n*interval
    for i in range(n):
        c=base+i*step
        out.append({"time":start+i*interval,"open":c-step/2,"high":c+0.4,"low":c-0.4,"close":c,"tick_volume":100})
    return out

def test_atr_positive():
    assert atr(bars(),14)>0

def test_fib_zone_is_ordered():
    z=fib_golden_zone(bars())
    assert z["swing_high"]>z["swing_low"]
    assert z["bullish_zone"][0]>z["bullish_zone"][1]
    assert z["bearish_zone"][0]<z["bearish_zone"][1]

def test_size_position_never_rounds_up_risk():
    s=size_position(10,2000,1990,0.01,1,0.01,100,0.01)
    assert s["volume"]>0
    assert s["risk_money"]<=10

def test_missing_data_is_no_trade():
    s=generate_signal({}, {}, {"equity":1000})
    assert s["status"]=="NO TRADE"

def test_missing_contract_specs_never_invents_position_size():
    q={"bid":2000,"ask":2000.2,"age_seconds":0,"symbol":"XAUUSD"}
    s=generate_signal({"M5":bars(),"M15":bars(),"H1":bars()},q,{"equity":1000})
    # The fixture has no valid sweep setup, so it must remain NO TRADE; no fake volume/risk is invented.
    assert s["status"]=="NO TRADE"
    assert s.get("position_volume") is None

def test_stale_quote_is_no_trade():
    q={"bid":2000,"ask":2000.2,"age_seconds":20,"symbol":"XAUUSD",
       "tick_size":.01,"tick_value":1,"volume_min":.01,"volume_max":100,"volume_step":.01}
    b=bars()
    assert generate_signal({"M5":b,"M15":b,"H1":b},q,{"equity":1000,"max_quote_age_seconds":5})["status"]=="NO TRADE"

def test_excess_spread_is_no_trade():
    q={"bid":2000,"ask":2002,"age_seconds":0,"symbol":"XAUUSD",
       "tick_size":.01,"tick_value":1,"volume_min":.01,"volume_max":100,"volume_step":.01}
    b=bars()
    assert generate_signal({"M5":b,"M15":b,"H1":b},q,{"equity":1000,"max_spread_price":.5})["status"]=="NO TRADE"

def test_bad_ohlc_is_no_trade():
    b=bars(); b[-2]["low"]=3000
    q={"bid":2000,"ask":2000.1,"age_seconds":0,"symbol":"XAUUSD",
       "tick_size":.01,"tick_value":1,"volume_min":.01,"volume_max":100,"volume_step":.01}
    assert generate_signal({"M5":b,"M15":bars(),"H1":bars()},q,{"equity":1000})["status"]=="NO TRADE"

def test_session_name_is_string():
    assert isinstance(session_name(),str)

def test_high_low():
    h=high_low([{"high":5,"low":2},{"high":7,"low":3}])
    assert h=={"high":7.0,"low":2.0}


def test_timestamp_is_required_for_candles():
    b=bars()
    for candle in b:
        candle.pop("time", None)
    q={"bid":2000,"ask":2000.1,"age_seconds":0,"symbol":"XAUUSD"}
    s=generate_signal({"M5":b,"M15":bars(),"H1":bars(),"H4":bars()},q,{"equity":1000})
    assert s["status"] == "NO TRADE"


def test_future_quote_is_rejected():
    q={"bid":2000,"ask":2000.1,"age_seconds":-2,"symbol":"XAUUSD"}
    b=bars()
    s=generate_signal({"M5":b,"M15":bars(),"H1":bars(),"H4":bars()},q,{"equity":1000})
    assert s["status"] == "NO TRADE"


def test_trade_levels_reject_cent_stop_by_enforcing_atr_and_minimum_distance():
    from engine import build_trade_levels
    levels = build_trade_levels("BUY", 4015.70, 4015.69, 1.20, 0.0, 0.01,
                                {"min_stop_distance_price": 0.50,
                                 "min_stop_atr_multiple": 1.0,
                                 "stop_buffer_atr_multiple": 0.20,
                                 "target_atr_multiple": 1.50}, target_level=4019.00)
    assert levels["entry"] - levels["stop_loss"] >= 1.19
    assert levels["take_profit"] - levels["entry"] >= 2.0 * (levels["entry"] - levels["stop_loss"])
    assert levels["reward_risk"] >= 2.0


def test_sell_trade_levels_are_rounded_outward_and_keep_minimum_rr():
    from engine import build_trade_levels
    levels = build_trade_levels("SELL", 4015.70, 4015.71, 0.80, 0.0, 0.01,
                                {"min_stop_distance_price": 0.50,
                                 "min_stop_atr_multiple": 1.0,
                                 "stop_buffer_atr_multiple": 0.20,
                                 "target_atr_multiple": 1.50}, target_level=4012.00)
    assert levels["stop_loss"] - levels["entry"] >= 0.79
    assert levels["entry"] - levels["take_profit"] >= 2.0 * (levels["stop_loss"] - levels["entry"])
    assert levels["reward_risk"] >= 2.0


def test_risk_percent_does_not_change_price_levels():
    from engine import build_trade_levels
    a = build_trade_levels("BUY", 4015.70, 4014.90, 1.00, 0.10, 0.01,
                           {"risk_percent": 1, "min_stop_distance_price": 0.50}, target_level=4018.00)
    b = build_trade_levels("BUY", 4015.70, 4014.90, 1.00, 0.10, 0.01,
                           {"risk_percent": 3, "min_stop_distance_price": 0.50}, target_level=4018.00)
    assert (a["entry"], a["stop_loss"], a["take_profit"]) == (b["entry"], b["stop_loss"], b["take_profit"])


def test_stale_completed_candles_are_no_trade_even_with_fresh_quote():
    old = bars()
    for candle in old: candle["time"] -= 3600
    quote={"bid":2020,"ask":2020.1,"age_seconds":0,"symbol":"XAUUSD",
           "tick_size":0.01,"tick_value":1,"volume_min":0.01,"volume_max":100,"volume_step":0.01}
    result=generate_signal({"M5":old,"M15":bars(interval=900),"H1":bars(interval=3600),"H4":bars(interval=14400)},quote,
                           {"equity":1000,"max_quote_age_seconds":90})
    assert result["status"] == "NO TRADE"
    assert "stale" in result["reason"].lower()


def test_fresh_multitimeframe_data_builds_context_without_exception():
    m5=bars(base=2000,step=0.2,interval=300)
    m15=bars(base=2000,step=0.2,interval=900)
    h1=bars(base=2000,step=0.2,interval=3600)
    h4=bars(base=2000,step=0.2,interval=14400)
    q={"bid":2021.7,"ask":2021.8,"age_seconds":1,"symbol":"XAUUSD",
       "tick_size":0.01,"tick_value":1,"volume_min":0.01,"volume_max":100,"volume_step":0.01}
    result=generate_signal({"M5":m5,"M15":m15,"H1":h1,"H4":h4,"D1":bars(interval=86400)},q,
                           {"equity":1000,"risk_percent":1,"max_quote_age_seconds":90})
    assert result["status"] == "NO TRADE"
    assert "context" in result


def test_size_position_respects_volume_step_from_broker_minimum():
    result = size_position(1.55, 100.0, 99.0, 1.0, 10.0, 0.10, 1.0, 0.03)
    assert result["volume"] == 0.13
    assert result["risk_money"] <= 1.55


def test_nearest_liquidity_pool_too_close_does_not_force_invalid_target():
    from engine import select_liquidity_target
    result = select_liquidity_target(
        "BUY", 100.0, 99.5, 0.5, 0.0, 0.01,
        {"min_stop_distance_price": 0.5, "min_stop_atr_multiple": 1.0,
         "stop_buffer_atr_multiple": 0.2, "target_atr_multiple": 1.5,
         "min_reward_risk": 2.0}, [101.0, 101.5]
    )
    assert result["selected_liquidity_target"] == 101.5
    assert result["reward_risk"] >= 2.0


def test_entry_rounds_outward_to_executable_side_of_tick_grid():
    from engine import build_trade_levels
    cfg = {"min_stop_distance_price": 0.5, "min_stop_atr_multiple": 1.0,
           "stop_buffer_atr_multiple": 0.2, "target_atr_multiple": 1.5,
           "min_reward_risk": 2.0}
    buy = build_trade_levels("BUY", 4015.701, 4014.0, 1.0, 0.0, 0.05, cfg, target_level=4020.0)
    sell = build_trade_levels("SELL", 4015.701, 4017.0, 1.0, 0.0, 0.05, cfg, target_level=4010.0)
    assert buy["entry"] == 4015.75
    assert sell["entry"] == 4015.70
    assert buy["reward_risk"] >= 2.0 and sell["reward_risk"] >= 2.0


def _candidate_fixture(monkeypatch, verified_specs):
    """Build a deterministic, synthetic setup for status-gate contract tests only."""
    import engine
    now = int(time.time())

    def up_bars(interval, n=110):
        arr=[]
        start=now-(n-1)*interval
        for i in range(n):
            close=1980.0+i*0.2
            arr.append({"time":start+i*interval,"open":close-0.1,"high":close+0.3,"low":close-0.3,"close":close,"tick_volume":100})
        return arr

    m5=up_bars(300)
    # Latest confirmed M5 bar: a low sweep followed by a strong bullish close.
    m5[-2]={**m5[-2],"open":1998.0,"high":2002.0,"low":1990.0,"close":2001.0}
    bars_by_tf={"M5":m5,"M15":up_bars(900),"H1":up_bars(3600),"H4":up_bars(14400),"D1":up_bars(86400)}

    monkeypatch.setattr(engine, "_valid_bars", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(engine, "atr", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(engine, "fib_golden_zone", lambda *_args, **_kwargs: {
        "swing_high":2025.0,"swing_low":1975.0,"bullish_zone":[2001.0,1999.0],"bearish_zone":[1980.0,1982.0]})
    monkeypatch.setattr(engine, "structure_state", lambda *_args, **_kwargs: {"bos_up":True,"bos_down":False})
    monkeypatch.setattr(engine, "fair_value_gaps", lambda *_args, **_kwargs: {"bullish":True,"bearish":False})
    monkeypatch.setattr(engine, "intraday_session_levels", lambda *_args, **_kwargs: {
        "asia_high":2020.0,"asia_low":1980.0,"london_high":2022.0,"london_low":1978.0,
        "new_york_high":None,"new_york_low":None})
    monkeypatch.setattr(engine, "previous_day_levels", lambda *_args, **_kwargs: {"high":2025.0,"low":1975.0})
    monkeypatch.setattr(engine, "_equal_liquidity_pools", lambda *_args, **_kwargs: {"equal_high":2020.0,"equal_low":1980.0})
    monkeypatch.setattr(engine, "_order_block_proxy", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(engine, "select_liquidity_target", lambda *_args, **_kwargs: {
        "entry":2000.10,"stop_loss":1989.80,"take_profit":2025.0,"reward_risk":2.42,
        "risk_distance":10.30,"reward_distance":24.90,"selected_liquidity_target":2025.0,
        "selected_liquidity_target_distance":24.90})

    quote={"symbol":"XAUUSD","bid":2000.0,"ask":2000.1,"mid":2000.05,"spread":0.1,"age_seconds":0,
           "quote_type":"executable","tick_size":0.01,"tick_value":0.01,"volume_min":0.01,"volume_max":100,
           "volume_step":0.01,"account_currency":"ZAR","contract_specs_verified":verified_specs,
           "specification_source":"test fixture"}
    config={"equity":100000,"risk_percent":5,"max_daily_loss":0,"daily_target":0,"realized_pnl_today":0,
            "min_reward_risk":2,"max_spread_price":0.5,"min_atr_price":0.25,"min_stop_distance_price":0.5,
            "min_stop_atr_multiple":1.0,"stop_buffer_atr_multiple":0.2,"target_atr_multiple":1.5,
            "max_quote_age_seconds":90,"max_m5_candle_age_seconds":600,"max_m15_candle_age_seconds":1800,
            "max_h1_candle_age_seconds":7200,"max_h4_candle_age_seconds":28800}
    return bars_by_tf, quote, config


def test_unverified_broker_specs_can_only_produce_price_plan(monkeypatch):
    b, q, cfg = _candidate_fixture(monkeypatch, verified_specs=False)
    result = generate_signal(b, q, cfg)
    assert result["status"] == "PRICE PLAN"
    assert result["side"] == "BUY"
    assert result["sizing_calculated"] is True
    assert result["sizing_verified"] is False
    assert result["execution_ready"] is False


def test_source_checked_broker_specs_allow_engine_candidate(monkeypatch):
    b, q, cfg = _candidate_fixture(monkeypatch, verified_specs=True)
    result = generate_signal(b, q, cfg)
    assert result["status"] == "CANDIDATE"
    assert result["side"] == "BUY"
    assert result["sizing_verified"] is True


def test_complete_but_unverified_contract_specs_never_become_trade_ready():
    """A math result is not provenance verification; unverified specs remain price-plan-only."""
    from engine import requires_price_plan
    base={"quote_type":"executable","contract_specs_verified":False}
    assert requires_price_plan(True,base) is True
    assert requires_price_plan(False,{**base,"contract_specs_verified":True}) is True
    assert requires_price_plan(True,{**base,"contract_specs_verified":True,"quote_type":"indicative_price_only"}) is True
    assert requires_price_plan(True,{**base,"contract_specs_verified":True}) is False

