"""Read-only, reproducible local funding audit; no exchange requests or writes."""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from StateStore import LendingStateStore

DAY = 86400000
c = sqlite3.connect((ROOT / '.state/lendingbot-v3.sqlite3').as_uri() + '?mode=ro', uri=True)
c.row_factory = sqlite3.Row
c.execute('PRAGMA query_only=ON')
c.execute('BEGIN')
def rows(sql, args=()):
    return [dict(r) for r in c.execute(sql, args)]
def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone(timedelta(hours=8))).isoformat()
def weight(rs, key, amount='amount'):
    den = sum(abs(float(r[amount])) for r in rs)
    return sum(abs(float(r[amount])) * float(r[key]) for r in rs) / den if den else None
def quantile(values, q):
    values = sorted(values)
    return values[min(len(values)-1, int((len(values)-1)*q))] if values else None

samples = rows('SELECT * FROM account_samples ORDER BY mts')
end = samples[-1]['mts']
policy = json.loads(rows("SELECT policy_json FROM strategy_versions WHERE status='ACTIVE'")[0]['policy_json'])
fee = float(policy['normal_fee_rate'])
intents = rows('SELECT * FROM order_intents ORDER BY created_at_ms')
offers = rows('SELECT * FROM offers')
credits = rows('SELECT * FROM credits')
trades = rows('SELECT * FROM funding_trades ORDER BY mts')
closures = rows('SELECT * FROM credit_closures ORDER BY closed_at_ms')
ledgers = rows("SELECT * FROM ledger_entries WHERE currency='USD' AND wallet='funding' AND category=28 ORDER BY mts")
chains = rows('SELECT * FROM reprice_chains')
intent_by_offer = defaultdict(list)
for r in intents:
    intent_by_offer[r['exchange_offer_id']].append(r)
waits = {r['trade_id']: r for r in LendingStateStore._funding_wait_records(c, 'USD', 0, end + 1)}
for r in trades:
    r['amount'] = abs(float(r['amount']))
    r['net_apr_percent'] = float(r['rate']) * 365 * (1-fee) * 100
    linked = intent_by_offer[r['offer_id']]
    intent = linked[0] if len(linked) == 1 else {}
    for key in ['pool','layer','pricing_curve_version','strategy_version']:
        r[key] = intent.get(key)
    w = waits.get(r['trade_id'], {}).get('wait_seconds')
    r['wait_minutes'] = None if w is None else float(w) / 60
    r['latest_offer_wait_minutes'] = (r['mts'] - intent['created_at_ms'])/60000 if intent else None

out = {'snapshot_bjt': stamp(end), 'policy': policy,
       'counts': {k:len(v) for k,v in [('intents',intents),('offers',offers),('credits',credits),('trades',trades),('closures',closures),('interest_ledgers',ledgers),('account_samples',samples)]},
       'time_ranges': {'intents': [stamp(intents[0]['created_at_ms']),stamp(intents[-1]['created_at_ms'])],
                       'trades': [stamp(trades[0]['mts']), stamp(trades[-1]['mts'])]},
       'account': samples[-1], 'windows': {}, 'cohorts': {}}
for days in (7,14,30):
    start = end-days*DAY
    principal_ms = credit_ms = idle_ms = covered_ms = 0
    gaps = []
    for a,b in zip(samples,samples[1:]):
        lo,hi = max(start,a['mts']),min(end,b['mts'])
        if hi<=lo: continue
        dt=hi-lo
        principal_ms+=float(a['total_principal'])*dt
        credit_ms+=float(a['active_credits'])*dt
        covered_ms+=dt
        if b['mts']-a['mts']>3600000: gaps.append({'from':stamp(a['mts']),'to':stamp(b['mts']),'hours':(b['mts']-a['mts'])/3600000})
    income=sum(float(r['amount']) for r in ledgers if start<=r['mts']<end)
    out['windows'][days]={'net_income_usd':income,'mean_principal':principal_ms/covered_ms,
        'utilization_percent':credit_ms/principal_ms*100,'net_simple_apr_percent':income/(principal_ms/DAY)*36500,
        'covered_days':covered_ms/DAY,'sample_gaps':gaps}

def summarize(rs):
    valid=[r for r in rs if r['wait_minutes'] is not None]
    amount=sum(r['amount'] for r in rs)
    return {'fills':len(rs),'distinct_offers':len({r['offer_id'] for r in rs}),'amount':amount,
        'net_apr_percent':weight(rs,'net_apr_percent'),'chain_wait_minutes':weight(valid,'wait_minutes'),
        'wait_valid':len(valid),'wait_coverage_percent':sum(r['amount'] for r in valid)/amount*100 if amount else None,
        'wait_p50_minutes':quantile([r['wait_minutes'] for r in valid],.5),'wait_p90_minutes':quantile([r['wait_minutes'] for r in valid],.9),
        'latest_offer_wait_minutes':weight([r for r in rs if r['latest_offer_wait_minutes'] is not None],'latest_offer_wait_minutes')}
for label,predicate in [('all',lambda r: True),('last14days',lambda r:r['mts']>=end-14*DAY),('curveV4',lambda r:r['pricing_curve_version']=='EXACT_TERM_EXPLORATION_V4')]:
    groups=defaultdict(list)
    selected=[r for r in trades if predicate(r) and r['managed']]
    for r in selected: groups[f"{r['period']}d/{r['layer'] or 'unknown'}"].append(r)
    out['cohorts'][label]={'total':summarize(selected),'groups':{k:summarize(v) for k,v in sorted(groups.items())}}

active=[r for r in credits if r['status']=='ACTIVE']
out['active_credits']=active
out['active_by_period']={}
for p in sorted({r['period'] for r in active}):
    rs=[r for r in active if r['period']==p]
    for r in rs: r['effective_rate']=float(r['rate_real'] if r['rate_real'] is not None else r['rate'])
    out['active_by_period'][p]={'count':len(rs),'amount':sum(abs(float(r['amount'])) for r in rs),'net_apr_percent':weight(rs,'effective_rate')*365*(1-fee)*100,'external_amount':sum(abs(float(r['amount'])) for r in rs if not r['managed'])}
out['active_offers']=[]
for r in offers:
    if r['status']!='ACTIVE':continue
    starts={ch['started_at_ms'] for ch in chains if ch['current_offer_id']==r['offer_id']}
    out['active_offers'].append({**r,'chain_wait_minutes':(end-next(iter(starts)))/60000 if len(starts)==1 else None,'snapshot_bjt':stamp(r['last_seen_ms'])})
out['closed_holding']={}
for p in sorted({r['period'] for r in closures}):
    rs=[{**r,'hours':(r['closed_at_ms']-r['opened_at_ms'])/3600000} for r in closures if r['period']==p and r['opened_at_ms'] and r['closed_at_ms']>=r['opened_at_ms']]
    out['closed_holding'][p]={'count':len(rs),'median_hours':median(r['hours'] for r in rs),'weighted_days':weight(rs,'hours')/24,'under_one_day':sum(r['hours']<24 for r in rs)}

out['market_last7days']=[]
for p in (2,7,14,30,120):
    floor=float(policy['short_floor_apr' if p<=7 else 'medium_floor_apr' if p<=30 else 'long_floor_apr'])/(365*(1-fee))
    out['market_last7days'].append(rows('''SELECT period,count(*) AS trades,sum(abs(cast(amount AS REAL))) AS volume,
        sum(abs(cast(amount AS REAL))*cast(rate AS REAL))/sum(abs(cast(amount AS REAL))) AS weighted_daily_rate,
        sum(CASE WHEN cast(rate AS REAL)>=? THEN abs(cast(amount AS REAL)) ELSE 0 END)/sum(abs(cast(amount AS REAL)))*100 AS volume_above_floor_percent
        FROM market_trades WHERE mts>=? AND mts<=? AND period=?''',(floor,end-7*DAY,end,p))[0])
out['order_states']=rows('SELECT state,count(*) AS n FROM order_intents GROUP BY state')
out['curve_counts']=rows('SELECT pricing_curve_version,count(*) AS n,min(created_at_ms) AS first_ms,max(created_at_ms) AS last_ms FROM order_intents GROUP BY pricing_curve_version')
c.rollback();c.close()
(ROOT/'docs/yield-review-2026-09-06-data.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
(ROOT/'docs/yield-review-2026-09-06-fills.json').write_text(json.dumps(trades,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in out.items() if k not in ('policy','active_credits')},ensure_ascii=False,indent=2))
