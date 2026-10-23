/* Gold Flow AI enhancement layer: local evidence log, live module status, risk tracking, CSV backtest.
   Never substitutes fake quotes, candles, provider data or broker specifications. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const HISTORY_KEY = 'gfa_signal_history_v1';
  const MAX_HISTORY = 200;
  let lastFingerprint = '';
  let latestBacktestReport = null;

  function safeJson(value, fallback) {
    try { const parsed = JSON.parse(value); return parsed == null ? fallback : parsed; }
    catch { return fallback; }
  }
  function timeSast(value) {
    if (!value) return '—';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    return d.toLocaleString('en-ZA', { timeZone: 'Africa/Johannesburg', hour12: false });
  }
  function finite(value) { return Number.isFinite(Number(value)); }
  function px(value) { return value == null || !finite(value) ? '—' : fmtPrice(Number(value)); }
  function moduleText(id, value, kind = 'neutral') {
    const el = $(id); if (!el) return;
    el.textContent = value;
    el.className = kind === 'ok' ? 'green' : kind === 'bad' ? 'red' : 'amber';
  }

  function updateDailyProgress() {
    if (!['targetProgressText','targetProgressFill','targetRemainingRead','riskRemainingRead','dailyStatusRead'].every(id => $(id))) return;
    const s = getSettings();
    const pnl = Number(s.realizedPnlToday) || 0;
    const target = Math.max(0, Number(s.dailyTarget) || 0);
    const maxLoss = Math.max(0, Number(s.dailyLoss) || 0);
    const progress = target > 0 ? Math.max(0, Math.min(100, pnl / target * 100)) : 0;
    $('targetProgressText').textContent = target > 0 ? progress.toFixed(1) + '%' : 'Off';
    $('targetProgressFill').style.width = progress + '%';
    $('targetRemainingRead').textContent = target > 0 ? money(Math.max(0, target - pnl), s.accountCurrency) : 'Off';
    $('riskRemainingRead').textContent = maxLoss > 0 ? money(Math.max(0, maxLoss + pnl), s.accountCurrency) : 'Off';
    let status = 'Tracking';
    if (maxLoss > 0 && pnl <= -maxLoss) status = 'DAILY LOSS LIMIT REACHED';
    else if (target > 0 && pnl >= target) status = 'TARGET REACHED';
    else if (target > 0 && pnl < 0) status = 'Below daily P/L';
    $('dailyStatusRead').textContent = status;
    $('dailyStatusRead').className = status.includes('REACHED') ? 'red' : 'green';
  }

  function historyLoad() {
    try {
      const history = safeJson(localStorage.getItem(HISTORY_KEY), []);
      return Array.isArray(history) ? history.filter(x => x && typeof x === 'object').slice(0, MAX_HISTORY) : [];
    } catch { return []; }
  }
  function historyRender() {
    const host = $('signalHistoryRows'); if (!host) return;
    const rows = historyLoad();
    if (!rows.length) {
      host.innerHTML = '<tr><td colspan="8" class="muted">No signal evidence saved yet. New source updates will be logged here on this device.</td></tr>';
      return;
    }
    host.innerHTML = rows.map(r => `<tr><td>${esc(timeSast(r.recorded_at))}</td><td title="${esc(r.reference || '')}">${esc(r.reference || '—')}</td><td>${esc((r.status || 'NO TRADE') + (r.side ? ' / ' + r.side : ''))}</td><td>${esc(px(r.entry))}</td><td>${esc(px(r.stop))}</td><td>${esc(px(r.target))}</td><td>${r.reward_risk != null && finite(r.reward_risk) ? esc(Number(r.reward_risk).toFixed(2) + ':1') : '—'}</td><td>${esc(r.reason || '—')}</td></tr>`).join('');
  }
  function recordSignalSnapshot(s) {
    if (!s || typeof s !== 'object') return;
    const q = s.quote || {}, sig = s.signal || {};
    const sourceStamp = q.timestamp || q.time || s.updated_at || 'no-source-time';
    const reference = String(q.symbol || 'XAUUSD') + '-' + String(sourceStamp).replace(/[^0-9A-Za-z]/g, '').slice(-14);
    const identity = [reference, sig.status || 'NO TRADE', sig.side || '', sig.entry ?? '', sig.stop_loss ?? '', sig.take_profit ?? '', sig.reason || ''].join('|');
    if (identity === lastFingerprint) return;
    lastFingerprint = identity;
    // Never log a polling state as a new signal when there was no source quote timestamp.
    if (!q.timestamp && !q.time && !s.updated_at) return;
    const row = {
      recorded_at: new Date().toISOString(), reference,
      source: q.source || s.source || 'unknown', source_timestamp: sourceStamp,
      status: sig.status || 'NO TRADE', side: sig.side || null,
      entry: sig.entry ?? null, stop: sig.stop_loss ?? null, target: sig.take_profit ?? null,
      reward_risk: sig.reward_risk ?? null, volume: sig.position_volume ?? null,
      estimated_risk: sig.estimated_risk_account_currency ?? null,
      risk_currency: sig.risk_currency || s.analysis_settings?.account_currency || 'ZAR',
      reason: sig.reason || (sig.reasons || []).join('; ') || 'No complete validated setup.'
    };
    const history = historyLoad();
    history.unshift(row);
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(0, MAX_HISTORY))); } catch { /* Private browsing/storage quotas must never break live rendering. */ }
    historyRender();
  }
  function buildPlanText() {
    const s = state || {}, q = s.quote || {}, sig = s.signal || {};
    const settings = getSettings(), ctx = sig.context || {};
    const verified = sig.sizing_verified === true;
    const lines = [
      'GOLD FLOW AI — THE FUNDAMENTAL EDGE',
      'Decision support only · manual execution only · not a guarantee of outcome',
      `Reference: ${q.symbol || 'XAUUSD'}-${String(q.timestamp || s.updated_at || 'NO-TIMESTAMP').replace(/[^0-9A-Za-z]/g, '').slice(-14)}`,
      `Generated (SAST): ${timeSast(s.updated_at)}`,
      `Market data source: ${q.source || s.source || 'NOT CONNECTED'}`,
      `Quote timestamp: ${q.timestamp || 'unavailable'} · age: ${q.age_seconds == null ? 'unknown' : Number(q.age_seconds).toFixed(1) + 's'}`,
      `Market state: ${s.status || 'unavailable'} · Signal: ${sig.status || 'NO TRADE'} · Side: ${sig.side || 'NONE'}`,
      `Entry: ${px(sig.entry)} · Stop-loss: ${px(sig.stop_loss)} · Take-profit: ${px(sig.take_profit)}`,
      `Reward:risk: ${sig.reward_risk == null ? '—' : Number(sig.reward_risk).toFixed(2) + ':1'}`,
      `Position volume: ${sig.position_volume == null ? 'not calculated' : sig.position_volume}`,
      `Estimated loss at stop: ${sig.estimated_risk_account_currency == null ? 'not calculated' : money(sig.estimated_risk_account_currency, sig.risk_currency || settings.accountCurrency)} (${verified ? 'provider/broker metadata flagged verified' : 'ESTIMATE — specifications not independently verified'})`,
      `Stop rationale: ${sig.stop_rationale || 'not provided'}`,
      `Target rationale: ${sig.target_rationale || 'not provided'}`,
      `H4/H1/M15 bias: ${ctx.h4_bias || '—'} / ${ctx.h1_bias || '—'} / ${ctx.m15_bias || '—'}`,
      `Liquidity: ${ctx.swept_low ? 'low swept/reclaimed' : ctx.swept_high ? 'high swept/rejected' : 'no confirmed sweep'}`,
      `Order flow: ${ctx.order_flow_confirmed ? 'confirmed by current configured feed' : 'NOT CONFIRMED'}`,
      `Decision reason: ${sig.reason || (sig.reasons || []).join('; ') || 'No validated setup.'}`,
      `Evidence: ${(sig.reasons || []).join(' | ') || 'No additional evidence supplied.'}`,
      '',
      'EXECUTION RULE: Do not place a trade unless state is CANDIDATE and the live quote, tick grid, size, spread, stop and target have all been independently checked.',
      'A PRICE PLAN is indicative only; missing/stale data or unverified specifications mean NO TRADE.'
    ];
    return lines.join('\n');
  }
  async function copyPlan() {
    const text = buildPlanText();
    const status = $('copyStatus');
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
      else throw new Error('clipboard API unavailable');
      status.textContent = 'Plan copied to clipboard. Verify all displayed values before execution.';
    } catch {
      const area = document.createElement('textarea');
      area.value = text; area.setAttribute('readonly', '');
      area.style.position = 'fixed'; area.style.opacity = '0'; area.style.left = '-10000px';
      document.body.appendChild(area); area.select();
      let ok = false; try { ok = document.execCommand('copy'); } catch {}
      area.remove();
      status.textContent = ok ? 'Plan copied to clipboard.' : 'Clipboard blocked by this browser. Select and copy the plan from the text area below.';
      if (!ok) {
        let pre = $('copyFallback');
        if (!pre) { pre = document.createElement('textarea'); pre.id = 'copyFallback'; pre.rows = 10; pre.style.cssText = 'width:100%;margin-top:8px;background:#0d0f13;color:#f1f1ed;border:1px solid #292d35;border-radius:8px;padding:8px;'; status.after(pre); }
        pre.value = text; pre.focus(); pre.select();
      }
    }
  }

  function parseCsv(text) {
    const rows = []; let row = [], field = '', quoted = false;
    for (let i=0; i<text.length; i++) {
      const ch = text[i];
      if (quoted) {
        if (ch === '"' && text[i+1] === '"') { field += '"'; i++; }
        else if (ch === '"') quoted = false;
        else field += ch;
      } else if (ch === '"') quoted = true;
      else if (ch === ',') { row.push(field); field = ''; }
      else if (ch === '\n' || ch === '\r') {
        if (ch === '\r' && text[i+1] === '\n') i++;
        row.push(field); field = '';
        if (row.some(v => String(v).trim() !== '')) rows.push(row);
        row = [];
      } else field += ch;
    }
    if (quoted) throw new Error('CSV has an unclosed quoted field.');
    if (field.length || row.length) { row.push(field); if (row.some(v => String(v).trim() !== '')) rows.push(row); }
    if (rows.length < 2) throw new Error('CSV contains no data rows.');
    const headers = rows.shift().map(h => String(h).trim().replace(/^\uFEFF/, '').toLowerCase());
    const idx = Object.fromEntries(['time','open','high','low','close'].map(k=>[k,headers.indexOf(k)]));
    if (Object.values(idx).some(i=>i<0)) throw new Error('Required CSV headers: time,open,high,low,close. Extra columns are permitted.');
    if (rows.length < 200) throw new Error(`Only ${rows.length} data bars found. At least 200 ordered bars are required.`);
    if (rows.length > 100000) throw new Error('CSV exceeds the 100,000-bar safety limit. Split the history into smaller runs.');
    const bars = []; let priorKey = null, timeMode = null;
    for (let i=0; i<rows.length; i++) {
      const r = rows[i];
      const stamp = String(r[idx.time] ?? '').trim();
      let key, mode;
      if (/^-?\d+(?:\.\d+)?$/.test(stamp)) { key = Number(stamp); mode='numeric'; }
      else { key = Date.parse(stamp); mode='date'; }
      if (!stamp || !Number.isFinite(key)) throw new Error(`Row ${i+2}: invalid time value.`);
      if (timeMode && timeMode !== mode) throw new Error('Time column mixes numeric timestamps and date strings. Use one consistent format.');
      timeMode = mode;
      if (priorKey !== null && key <= priorKey) throw new Error(`Row ${i+2}: timestamps must be strictly ascending with no duplicates.`);
      priorKey = key;
      const b = {time:stamp, key};
      for (const k of ['open','high','low','close']) b[k] = Number(String(r[idx[k]] ?? '').trim());
      if (!['open','high','low','close'].every(k=>Number.isFinite(b[k]) && b[k] > 0)) throw new Error(`Row ${i+2}: OHLC values must be finite and positive.`);
      if (b.high < Math.max(b.open,b.close) || b.low > Math.min(b.open,b.close) || b.low > b.high) throw new Error(`Row ${i+2}: invalid OHLC range.`);
      bars.push(b);
    }
    return bars;
  }
  function runBaselineBacktest(rows, spread, slippage, rr) {
    const trades=[]; let equity=0, peak=0, maxDD=0, i=30;
    while (i < rows.length-2) {
      const bar=rows[i];
      let lo=Infinity, hi=-Infinity;
      for (let k=i-20;k<i;k++) { lo=Math.min(lo,rows[k].low); hi=Math.max(hi,rows[k].high); }
      let rangeSum=0, rangeCount=0;
      for (let k=Math.max(1,i-14);k<=i;k++) { rangeSum+=rows[k].high-rows[k].low; rangeCount++; }
      const atr=rangeCount?rangeSum/rangeCount:0, body=Math.abs(bar.close-bar.open);
      let side=null;
      if (atr>0 && body>=0.8*atr && bar.low<lo && bar.close>lo && bar.close>bar.open) side='BUY';
      else if (atr>0 && body>=0.8*atr && bar.high>hi && bar.close<hi && bar.close<bar.open) side='SELL';
      if (!side) { i++; continue; }
      const nxt=rows[i+1], executionCost=spread/2+slippage;
      const entry=nxt.open+(side==='BUY'?executionCost:-executionCost);
      const stop=side==='BUY' ? Math.min(lo,bar.low)-Math.max(0.2*atr,spread) : Math.max(hi,bar.high)+Math.max(0.2*atr,spread);
      const dist=Math.abs(entry-stop);
      if (!(dist>0)) {i++;continue;}
      const target=entry+(side==='BUY'?1:-1)*rr*dist;
      let outcome=null,exitPx=null,exitIndex=null;
      for (let j=i+1;j<rows.length;j++) {
        const x=rows[j], hitStop=side==='BUY'?x.low<=stop:x.high>=stop, hitTarget=side==='BUY'?x.high>=target:x.low<=target;
        if (hitStop) {outcome=-1;exitPx=stop;exitIndex=j;break;}
        if (hitTarget) {outcome=rr;exitPx=target;exitIndex=j;break;}
      }
      if (outcome===null) break;
      trades.push({side,entry_time:nxt.time,exit_time:rows[exitIndex].time,entry:Number(entry.toFixed(5)),stop:Number(stop.toFixed(5)),target:Number(target.toFixed(5)),exit:Number(exitPx.toFixed(5)),R:outcome});
      equity+=outcome; peak=Math.max(peak,equity); maxDD=Math.max(maxDD,peak-equity); i=exitIndex+1;
    }
    const wins=trades.filter(t=>t.R>0), losses=trades.filter(t=>t.R<0), grossWin=wins.reduce((a,t)=>a+t.R,0), grossLoss=-losses.reduce((a,t)=>a+t.R,0), sum=trades.reduce((a,t)=>a+t.R,0);
    return {bars_tested:rows.length,trades:trades.length,wins:wins.length,losses:losses.length,win_rate_pct:trades.length?Number((wins.length/trades.length*100).toFixed(2)):0,
      net_R:Number(sum.toFixed(3)),expectancy_R:trades.length?Number((sum/trades.length).toFixed(3)):0,profit_factor:grossLoss?Number((grossWin/grossLoss).toFixed(3)):null,
      max_drawdown_R:Number(maxDD.toFixed(3)),assumptions:{entry:'next bar open',same_bar_stop_and_target:'stop first',spread_price_units:spread,slippage_price_units:slippage,target_R:rr,
      strategy:'Sweep/reclaim + displacement baseline only; it is NOT the live multi-timeframe/flow-gated strategy',excludes:['higher-timeframe confirmation','macro/calendar','order flow/depth/footprint','commission','swap/financing','margin and broker execution differences'],
      timing:'No future candles are used to form the signal; each trade enters on the next bar. Results are research estimates, not a profit guarantee.'},trade_log:trades};
  }
  function renderBacktest(report) {
    const metrics=[['Bars',report.bars_tested],['Trades',report.trades],['Win rate',report.win_rate_pct+'%'],['Net R',report.net_R],['Expectancy',report.expectancy_R+' R'],['Profit factor',report.profit_factor==null?'N/A':report.profit_factor],['Max drawdown',report.max_drawdown_R+' R']];
    $('backtestMetrics').innerHTML=metrics.map(([a,b])=>`<div class="card span-3"><div class="muted tiny">${esc(a)}</div><div class="big">${esc(b)}</div></div>`).join('');
    $('backtestTrades').innerHTML=report.trade_log.length?report.trade_log.slice(-250).reverse().map(t=>`<tr><td>${esc(t.side)}</td><td>${esc(t.entry_time)}</td><td>${esc(t.exit_time)}</td><td>${esc(fmt(t.entry,5))}</td><td>${esc(fmt(t.stop,5))}</td><td>${esc(fmt(t.target,5))}</td><td class="${t.R>0?'green':'red'}">${esc(t.R>0?'+':'')+esc(String(t.R))}R</td></tr>`).join(''):'<tr><td colspan="7" class="muted">No completed trades matched the baseline rules. The report records zero trades rather than inventing data.</td></tr>';
    $('backtestStatus').textContent=`Completed on ${report.bars_tested} supplied OHLC bars. ${report.trades} closed trades. Stop-first handling applies when both stop and target occur in the same candle. Research baseline only—not the live engine.`;
    $('downloadBacktestBtn').disabled=false;
  }
  async function runBacktest() {
    $('backtestStatus').textContent='Reading and validating the uploaded real OHLC file…';
    $('backtestMetrics').innerHTML='';
    try {
      const file=$('backtestCsv').files?.[0];
      if(!file) throw new Error('Choose a historical OHLC CSV file first. No sample data is generated.');
      if(file.size>20*1024*1024) throw new Error('CSV is larger than the 20 MB safety limit. Split it into smaller files.');
      const spread=Number($('backtestSpread').value),slippage=Number($('backtestSlippage').value),rr=Number($('backtestRR').value);
      if(![spread,slippage,rr].every(Number.isFinite)||spread<0||slippage<0||rr<1||rr>10) throw new Error('Spread/slippage must be non-negative; target R must be from 1 to 10.');
      const rows=parseCsv(await file.text());
      const report=runBaselineBacktest(rows,spread,slippage,rr);
      report.created_at=new Date().toISOString(); report.file_name=file.name; latestBacktestReport=report;
      renderBacktest(report);
    } catch(e) {
      latestBacktestReport=null; $('downloadBacktestBtn').disabled=true;
      $('backtestStatus').textContent='Backtest stopped: '+(e?.message||'unknown validation error');
      $('backtestTrades').innerHTML='<tr><td colspan="7" class="muted">No report. Fix the reported input issue and run again.</td></tr>';
    }
  }

  const originalPaint = paint;
  paint = function(s) {
    originalPaint(s);
    updateDailyProgress();
    updateStrategyModules(s);
    recordSignalSnapshot(s);
  };
  function updateStrategyModules(s) {
    const sig=s?.signal||{}, c=sig.context||{}, intel=s?.intelligence||{}, flow=intel.order_flow||{}, macro=intel.macro||{}, cal=intel.calendar||{}, news=intel.news||{};
    const biases=[c.h4_bias,c.h1_bias,c.m15_bias];
    const aligned=biases.every(Boolean)&&biases.every(x=>x===biases[0]);
    moduleText('moduleHtf',aligned?'Aligned: '+String(biases[0]).toUpperCase():biases.some(Boolean)?'Mixed / not aligned':'Waiting for validated candles',aligned?'ok':'neutral');
    moduleText('moduleSweep',c.swept_low?'Low swept and reclaimed':c.swept_high?'High swept and rejected':'No confirmed completed-candle sweep',c.swept_low||c.swept_high?'ok':'neutral');
    moduleText('moduleM5',c.displacement?'Displacement threshold met; reclaim-failure must still be confirmed by the rule set':'No valid closed-candle displacement',c.displacement?'ok':'neutral');
    const fib=c.fib||{};
    moduleText('moduleFib',c.in_golden_zone?'Inside calculated Fibonacci golden zone':(fib.bullish_zone||fib.bearish_zone)?'Outside calculated golden zone':'Waiting for valid swing anchors',c.in_golden_zone?'ok':'neutral');
    moduleText('moduleOB',c.bullish_order_block_proxy?'Bullish OHLC proxy (heuristic)':c.bearish_order_block_proxy?'Bearish OHLC proxy (heuristic)':'No current OHLC proxy zone',c.bullish_order_block_proxy||c.bearish_order_block_proxy?'ok':'neutral');
    moduleText('moduleFlow',c.order_flow_confirmed?'Directional aggressor delta confirmed':flow.connected?'Feed connected; directional gate not confirmed':'Provider not connected',c.order_flow_confirmed?'ok':'neutral');
    moduleText('moduleDepth',flow.depth_connected?(c.order_book_confirmed?'Depth imbalance confirmed':'Depth feed connected; gate not confirmed'):(flow.depth_reason||'L2 provider not connected'),flow.depth_connected&&c.order_book_confirmed?'ok':'neutral');
    moduleText('moduleFootprint','Not available from the current trade + MBP-10 adapter; not inferred','neutral');
    const providers=[macro.connected,cal.connected,news.connected].filter(Boolean).length;
    moduleText('moduleMacro',providers===3?'Macro, calendar and news connected':providers?'Partial sources connected ('+providers+'/3)':'Macro/calendar/news providers not connected',providers===3?'ok':'neutral');
    const hasLevels=[c.previous_day_high,c.previous_day_low,c.asia_high,c.asia_low,c.london_high,c.london_low,c.new_york_high,c.new_york_low].some(v=>v!=null&&finite(v));
    const lev=$('sessionLevelsStatus'); if(lev){lev.textContent=hasLevels?'Levels derived from source candles':'WAITING FOR CANDLES';lev.className='status '+(hasLevels?'ok':'warn');}
  }
  const originalUnavailable = showUnavailable;
  showUnavailable = function(reason, backendOnline=false) {
    originalUnavailable(reason,backendOnline);
    updateDailyProgress(); historyRender();
    for (const [id,msg] of Object.entries({moduleHtf:'Waiting for live candles',moduleSweep:'Unverified · no live candles',moduleM5:'Unverified · no live candles',moduleFib:'Unavailable · no live candles',moduleOB:'Unavailable · no live candles',moduleFlow:'Unavailable · no live flow feed',moduleDepth:'Unavailable · no live depth feed',moduleFootprint:'Not provided by current adapter',moduleMacro:'Unavailable · providers not connected'})) moduleText(id,msg,'neutral');
    const lev=$('sessionLevelsStatus');if(lev){lev.textContent='WAITING FOR CANDLES';lev.className='status warn';}
  };

  // Save settings and update the progress display whenever editable values change.
  const originalUpdateRiskReadouts = updateRiskReadouts;
  updateRiskReadouts = function() { originalUpdateRiskReadouts(); updateDailyProgress(); };
  ['equity','riskPercent','dailyLoss','dailyTarget','realizedPnlToday','accountCurrency'].forEach(id=>$(id)?.addEventListener('input',updateDailyProgress));
  $('copyPlanBtn')?.addEventListener('click',copyPlan);
  $('clearSignalHistory')?.addEventListener('click',()=>{try{localStorage.removeItem(HISTORY_KEY);}catch{}lastFingerprint='';historyRender();$('copyStatus').textContent='Local signal evidence log cleared from this device.';});
  $('runBacktestBtn')?.addEventListener('click',runBacktest);
  $('downloadBacktestBtn')?.addEventListener('click',()=>{
    if(!latestBacktestReport){$('backtestStatus').textContent='No completed report available to download.';return;}
    const blob=new Blob([JSON.stringify(latestBacktestReport,null,2)],{type:'application/json'});
    const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='gold-flow-ai-backtest-report.json';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  historyRender(); updateDailyProgress();
})();
