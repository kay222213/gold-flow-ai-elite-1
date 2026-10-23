import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'validation'))
from backtest import run

def test_backtest_returns_metrics_without_lookahead_claims():
    rows=[]
    for i in range(250):
        c=2000+(i%20)*.1
        rows.append({'time':str(i),'open':c-.02,'high':c+.1,'low':c-.1,'close':c})
    r=run(rows)
    assert 'net_R' in r and 'trade_log' in r
    assert r['assumptions']['entry']=='next bar open'
