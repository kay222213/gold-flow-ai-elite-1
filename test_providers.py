import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from tradingview_webhook import TradingViewWebhookProvider

def test_tradingview_webhook_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv('TRADINGVIEW_WEBHOOK_TOKEN','x'*32)
    p=TradingViewWebhookProvider()
    try: p.accept({'token':'bad','timeframe':'5','time':1760000000000,'open':1,'high':2,'low':.5,'close':1.5})
    except PermissionError: pass
    else: raise AssertionError('bad webhook token accepted')

def test_tradingview_webhook_normalizes_milliseconds(monkeypatch):
    monkeypatch.setenv('TRADINGVIEW_WEBHOOK_TOKEN','x'*32)
    p=TradingViewWebhookProvider()
    p.accept({'token':'x'*32,'ticker':'OANDA:XAUUSD','timeframe':'5','time':1760000000000,'open':2000,'high':2002,'low':1999,'close':2001,'volume':10})
    assert p.bars['M5'][0]['time']==1760000000
    assert p.latest['price']==2001


def test_settings_endpoint_requires_admin_token(monkeypatch):
    from fastapi.testclient import TestClient
    import app
    monkeypatch.setenv('MARKET_DATA_SOURCE','none')
    monkeypatch.setenv('ADMIN_API_TOKEN','z'*40)
    with TestClient(app.app) as c:
        assert c.post('/api/settings',json={'risk_percent':1}).status_code==401
        assert c.post('/api/settings',json={'risk_percent':1},headers={'X-Admin-Token':'z'*40}).status_code==200


def test_tradingview_webhook_rejects_non_gold_ticker(monkeypatch):
    monkeypatch.setenv('TRADINGVIEW_WEBHOOK_TOKEN','x'*32)
    p=TradingViewWebhookProvider()
    payload={'token':'x'*32,'ticker':'OANDA:EURUSD','timeframe':'5','time':1760000000000,
             'open':1.1,'high':1.2,'low':1.0,'close':1.15,'volume':10}
    try:
        p.accept(payload)
    except ValueError as exc:
        assert 'only configured gold tickers' in str(exc)
    else:
        raise AssertionError('non-gold ticker accepted into XAUUSD bar history')


def test_twelvedata_invalid_timestamp_never_replaced_with_request_time(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
    from twelvedata_adapter import TwelveDataProvider
    monkeypatch.setenv('TWELVEDATA_API_KEY','test-key')
    provider=TwelveDataProvider()
    provider._get=lambda *args, **kwargs: {'close':'4000.1','datetime':'not-a-real-timestamp'}
    try:
        provider.get_quote('XAUUSD')
    except RuntimeError as exc:
        assert 'refusing to substitute request time' in str(exc)
    else:
        raise AssertionError('invalid provider timestamp was replaced with now')


def test_twelvedata_websocket_price_parser_requires_source_timestamp_and_matching_symbol(monkeypatch):
    from twelvedata_adapter import TwelveDataProvider
    parser=TwelveDataProvider.parse_stream_message
    good='{"event":"price","symbol":"XAU/USD","price":"4021.5","timestamp":1760000000}'
    price,stamp=parser(good,"XAU/USD")
    assert price == 4021.5 and stamp.tzinfo is not None
    assert parser('{"event":"price","symbol":"XAU/USD","price":"4021.5"}',"XAU/USD") is None
    assert parser('{"event":"price","symbol":"EUR/USD","price":"1.08","timestamp":1760000000}',"XAU/USD") is None


def test_twelvedata_websocket_parser_accepts_millisecond_timestamps():
    from twelvedata_adapter import TwelveDataProvider
    price,stamp=TwelveDataProvider.parse_stream_message(
        '{"type":"price","symbol":"XAU/USD","price":4021.5,"timestamp":1760000000000}',"XAU/USD")
    assert price == 4021.5 and int(stamp.timestamp()) == 1760000000
