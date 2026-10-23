"""Small, conservative OHLC event backtest for research only; no look-ahead fills.
Input CSV columns: time,open,high,low,close. Time must be ascending and bars must be same timeframe.
Entry at next bar open; if stop and target are both touched in one bar, stop is assumed first.
"""
import argparse, csv, json
from pathlib import Path
from math import isfinite

def load_csv(path):
    rows=[]
    with open(path,newline='',encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            b={k:float(r[k]) for k in ('open','high','low','close')}; b['time']=r['time']
            if not all(isfinite(b[k]) for k in ('open','high','low','close')) or b['low']<=0 or b['high']<max(b['open'],b['close']) or b['low']>min(b['open'],b['close']): raise ValueError(f"Invalid OHLC row {r}")
            rows.append(b)
    if len(rows)<200: raise ValueError('Need at least 200 bars for a minimally useful sample')
    return rows

def run(rows, spread=0.0, slippage=0.0, risk_r=1.0, rr=2.0):
    # Demonstration sweep-reclaim + displacement baseline. No external HTF or order-flow confluence is implied.
    trades=[]; equity=0.0; peak=0.0; max_dd=0.0; i=30
    while i < len(rows)-2:
        hist=rows[:i+1]; bar=rows[i]; prior=hist[-21:-1]
        lo=min(x['low'] for x in prior); hi=max(x['high'] for x in prior)
        ranges=[hist[j]['high']-hist[j]['low'] for j in range(max(1,i-14),i+1)]
        atr=sum(ranges)/len(ranges) if ranges else 0
        body=abs(bar['close']-bar['open'])
        side=None
        if atr>0 and body>=0.8*atr and bar['low']<lo and bar['close']>lo and bar['close']>bar['open']: side='BUY'
        elif atr>0 and body>=0.8*atr and bar['high']>hi and bar['close']<hi and bar['close']<bar['open']: side='SELL'
        if not side: i+=1; continue
        nxt=rows[i+1]; entry=nxt['open']+(spread/2+slippage if side=='BUY' else -(spread/2+slippage))
        stop=(min(lo,bar['low'])-max(.2*atr,spread) if side=='BUY' else max(hi,bar['high'])+max(.2*atr,spread))
        dist=abs(entry-stop)
        if dist<=0: i+=1; continue
        target=entry+rr*dist if side=='BUY' else entry-rr*dist
        outcome=None; exit_px=None; exit_i=None
        for j in range(i+1,len(rows)):
            x=rows[j]
            hit_stop=(x['low']<=stop if side=='BUY' else x['high']>=stop)
            hit_target=(x['high']>=target if side=='BUY' else x['low']<=target)
            if hit_stop: outcome=-1.0; exit_px=stop; exit_i=j; break
            if hit_target: outcome=rr; exit_px=target; exit_i=j; break
        if outcome is None: break
        trades.append({'side':side,'entry_time':nxt['time'],'exit_time':rows[exit_i]['time'],'entry':round(entry,5),'stop':round(stop,5),'target':round(target,5),'R':outcome})
        equity+=outcome; peak=max(peak,equity); max_dd=max(max_dd,peak-equity); i=exit_i+1
    wins=sum(t['R']>0 for t in trades); losses=sum(t['R']<0 for t in trades)
    gross_win=sum(t['R'] for t in trades if t['R']>0); gross_loss=-sum(t['R'] for t in trades if t['R']<0)
    return {'trades':len(trades),'wins':wins,'losses':losses,'win_rate_pct':round(100*wins/len(trades),2) if trades else 0,
            'net_R':round(sum(t['R'] for t in trades),2),'expectancy_R':round(sum(t['R'] for t in trades)/len(trades),3) if trades else 0,
            'profit_factor':round(gross_win/gross_loss,3) if gross_loss else None,'max_drawdown_R':round(max_dd,2),
            'assumptions':{'entry':'next bar open','same_bar_stop_and_target':'stop first','spread_price_units':spread,'slippage_price_units':slippage,'target_R':rr,
                           'strategy':'M5-like sweep/reclaim + displacement proxy only; excludes HTF, macro, order flow, fees and financing'},'trade_log':trades}

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('csv'); p.add_argument('--spread',type=float,default=0); p.add_argument('--slippage',type=float,default=0); p.add_argument('--rr',type=float,default=2); p.add_argument('--out',default='backtest-report.json'); a=p.parse_args()
    report=run(load_csv(a.csv),a.spread,a.slippage,rr=a.rr); Path(a.out).write_text(json.dumps(report,indent=2),encoding='utf-8'); print(json.dumps({k:v for k,v in report.items() if k!='trade_log'},indent=2))
