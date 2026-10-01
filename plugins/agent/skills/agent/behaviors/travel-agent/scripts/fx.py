#!/usr/bin/env python3
"""Exchange rate watch: the trip's foreign-currency spending (tickets, food, shopping) is bought over time, so a good day to
buy matters. The rate comes from api.frankfurter.dev (European Central Bank reference rates, free, no key), once per day.

An alert fires when the rate is the lowest of the last `window_days` (default 30) or reaches `target`, and only when it is
at least `min_step_pct` below the last alerted rate, so a flat week stays silent.
"""
import json, urllib.request, datetime as dt

URL = 'https://api.frankfurter.dev/v1/latest?from={cur}&to={home}'


def fetch(cur='USD', home='BRL', timeout=20):
    req = urllib.request.Request(URL.format(cur=cur, home=home), headers={'User-Agent': 'travel-agent/1.0 (+claude-plugins)'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return d['date'], float(d['rates'][home])


def evaluate(history, cfg, fstate, today=None):
    """history: [{date, rate}] oldest first, the last one is today's. Returns alert lines and updates fstate."""
    if not history: return []
    today = today or dt.date.today().isoformat()
    window = cfg.get('window_days', 30)
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=window)).isoformat()
    recent = [h['rate'] for h in history if h['date'] >= start]
    rate, cur, home = history[-1]['rate'], cfg.get('currency', 'USD'), cfg.get('home', 'BRL')
    low, avg = min(recent), sum(recent) / len(recent)
    target, step = cfg.get('target'), cfg.get('min_step_pct', 0.5) / 100
    last = fstate.get('alerted')
    hit = (rate <= low and len(recent) >= 5) or (target and rate <= target)
    if not hit or (last and rate > last * (1 - step)) or fstate.get('alerted_on') == today: return []
    fstate.update(alerted=rate, alerted_on=today)
    budget = cfg.get('budget')
    why = f'meta {target:.2f}' if target and rate <= target else f'menor dos últimos {window} dias'
    line = (f"DÓLAR: {cur} a {home} {rate:.4f} ({why}; média {avg:.4f}, {100 * (rate / avg - 1):+.1f}%)"
            .replace('.', ','))
    if budget: line += f" | {cur} {budget:,.0f} da viagem = {home} {budget * rate:,.0f}".replace(',', '.')
    return [line]
