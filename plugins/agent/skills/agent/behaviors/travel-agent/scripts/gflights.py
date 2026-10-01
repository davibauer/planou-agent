#!/usr/bin/env python3
"""Google Flights price-tracking alert e-mails (noreply-travel@google.com), with no I/O.

The user can track routes in Google Flights and get an e-mail when the price changes. These e-mails carry what the agent's own
search cannot: the real price for the whole party (children included) and every change Google saw, including promotional
fares that open and close within a day. Each e-mail lists one or more routes:

    Cidade A to Cidade B
    Sun, Dec 13 – Tue, Dec 29
    Round trip · 2 adults 1 child
    R$17,419 (dropped from R$20,463)
    https://www.google.com/travel/flights?tfs=...

and a route whose specific flight is tracked has its price under "Your tracked flight", after the times and the airline. The
exact dates and airports come from the `tfs` parameter of the link (base64 protobuf), which has the year that the text omits.
"""
import base64, re, datetime as dt

MONTHS = {m: i for i, m in enumerate(['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
                                      'October', 'November', 'December'], 1)}
PRICE = re.compile(r'([^\d\s(]{1,4})\s?([\d.,]+)\s*\((increased|dropped) from [^\d\s]{1,4}\s?([\d.,]+)\)')
HEADER = re.compile(r'^(.+?) to (.+?)\n[A-Z][a-z]{2}, [A-Z][a-z]{2} \d{1,2}(?: – [A-Z][a-z]{2}, [A-Z][a-z]{2} \d{1,2})?\n'
                    r'(Round trip|One way) · ([^\n]+)\n', re.M)
TFS = re.compile(r'tfs=([A-Za-z0-9_\-]+)')
UPDATED = re.compile(r'Prices updated (\w+) (\d{1,2}), (\d{4}) at (\d{1,2}):(\d{2}) GMT')
FLIGHT = re.compile(r'Your tracked flight\n[–-]+\n([^\n]+)\n([^\n]+)\n')
SYMBOLS = {'R$': 'BRL', 'US$': 'USD', '$': 'USD', '€': 'EUR'}


def _num(s):
    return float(re.sub(r'[.,](?=\d{3}(\D|$))', '', s).replace(',', '.'))


def _tfs(url_param):
    raw = base64.urlsafe_b64decode(url_param + '=' * (-len(url_param) % 4))
    dates = [d.decode() for d in re.findall(rb'\d{4}-\d\d-\d\d', raw)]
    codes = [c.decode() for c in re.findall(rb'\x12\x03([A-Z]{3})', raw)]
    return dates, codes


def party(text):
    """'2 adults 1 child' -> (2, 1); infants count as children (they take a seat or a lap fare)."""
    n = lambda word: sum(int(x) for x in re.findall(rf'(\d+) {word}', text))
    return n('adult'), n('child') + n('infant')


def parse(body):
    """E-mail text -> [{ts, o, d, out, ret, adults, children, price, was, currency, trend, flight}]."""
    m = UPDATED.search(body)
    ts = (dt.datetime(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)), int(m.group(4)), int(m.group(5))).isoformat(timespec='minutes')
          if m and m.group(1) in MONTHS else '')
    heads = list(HEADER.finditer(body))
    out = []
    for i, h in enumerate(heads):
        block = body[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(body)]
        p, t = PRICE.search(block), TFS.search(block)
        if not p or not t: continue
        dates, codes = _tfs(t.group(1))
        if not dates or len(codes) < 2: continue
        adults, children = party(h.group(4))
        f = FLIGHT.search(block)
        out.append({'ts': ts, 'o': codes[0], 'd': codes[1], 'out': dates[0],
                    'ret': dates[1] if h.group(3) == 'Round trip' and len(dates) > 1 else None,
                    'adults': adults, 'children': children, 'price': _num(p.group(2)), 'was': _num(p.group(4)),
                    'currency': SYMBOLS.get(p.group(1).strip(), p.group(1).strip()),
                    'trend': 'down' if p.group(3) == 'dropped' else 'up',
                    'flight': f'{f.group(1)} {f.group(2)}' if f else ''})
    return out


def matches(row, trip, expand_dates):
    """Same route, dates inside the trip's window and the same party (the price is then the real total for the trip)."""
    if row['o'] not in (trip.get('origins') or []) or row['d'] not in (trip.get('destinations') or []): return False
    if row['out'] not in expand_dates(trip.get('depart')): return False
    rets = expand_dates(trip.get('return'))
    if rets and (row['ret'] not in rets or (trip.get('home_by') and row['ret'] > trip['home_by'])): return False
    return (row['adults'], row['children']) == (trip.get('adults') or 1, trip.get('children') or 0)
