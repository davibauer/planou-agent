#!/usr/bin/env python3
"""Google Flights source.

The page is fetched with fast-flights (it builds the `tfs` protobuf of the URL and fetches with a browser-like client), which is
installed in ~/.config/travel-agent/cache/venv by setup.py and imported only inside `search()`, so the parser and the tests run
on the standard library alone.

The parsing is ours, not fast-flights': the results come in the `ds:1` script of the page as a JSON array, where index 2 holds
the flights Google ranks as "best" and index 3 the "other flights". fast-flights 3.1 reads only index 3 and crashes when it is
empty, so it misses the best fares (26/09/2026: a 1-stop GOL fare was only in index 2). For a round trip, each entry is an
outbound option and its price is the round-trip price for all passengers.

Known limits (26/09/2026): a search with a child or an infant comes back without results in the page (the prices load later
by script), so the agent searches 1 adult and multiplies; language 'pt-BR' also comes back empty, so language stays ''.
"""
import json, re

SCRIPT_RE = re.compile(r'<script[^>]*class="ds:1"[^>]*>(.*?)</script>', re.S)


class SourceError(Exception):
    """The page did not have what the parser expects: blocked, or Google changed the markup."""


def _hhmm(t):
    t = [*(t or []), None, None]            # Google drops zero components: [8] = 08:00, [None, 31] = 00:31
    return f'{t[0] or 0:02d}:{t[1] or 0:02d}'


def _date(d):
    return f'{d[0]:04d}-{d[1]:02d}-{d[2]:02d}' if d and len(d) >= 3 else ''


def parse_payload(p):
    """ds:1 payload -> offers [{price, airlines, stops, legs: [{from, to, dep, arr, minutes}], best}], cheapest first."""
    offers = []
    for idx in (2, 3):
        block = p[idx] if isinstance(p, list) and len(p) > idx else None
        if not block or not block[0]: continue
        for k in block[0]:
            try: f, price = k[0], k[1][0][1]
            except (IndexError, TypeError): continue
            if not price: continue
            legs = [{'from': s[3], 'to': s[6], 'dep': f'{_date(s[20])} {_hhmm(s[8])}', 'arr': f'{_date(s[21])} {_hhmm(s[10])}',
                     'minutes': s[11]} for s in (f[2] or [])]
            if not legs: continue
            offers.append({'price': price, 'airlines': list(f[1] or []), 'stops': len(legs) - 1, 'legs': legs, 'best': idx == 2})
    return sorted(offers, key=lambda o: o['price'])


def parse_html(html):
    m = SCRIPT_RE.search(html)
    if not m: raise SourceError('pagina sem o script ds:1 (bloqueio ou o Google mudou a pagina)')
    js = m.group(1)
    if 'data:' not in js: raise SourceError('script ds:1 sem data:')
    data = js.split('data:', 1)[1].rsplit(',', 1)[0]
    if data.endswith('errorHasStatus: true'): return []
    try: return parse_payload(json.loads(data))
    except json.JSONDecodeError as e: raise SourceError(f'ds:1 nao e JSON: {e}')


def search(origin, dest, out, ret=None, adults=1, currency='BRL'):
    """Returns (offers, url). One-way when ret is None."""
    from fast_flights import create_query, FlightQuery, Passengers, fetch_flights_html   # lazy: only the venv has it
    legs = [FlightQuery(date=out, from_airport=origin, to_airport=dest)]
    if ret: legs.append(FlightQuery(date=ret, from_airport=dest, to_airport=origin))
    q = create_query(flights=legs, trip='round-trip' if ret else 'one-way', passengers=Passengers(adults=adults),
                     currency=currency, language='')
    return parse_html(fetch_flights_html(q)), q.url()
