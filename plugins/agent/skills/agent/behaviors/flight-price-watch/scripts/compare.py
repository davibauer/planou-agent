#!/usr/bin/env python3
"""Past trips as the reference, with no I/O: what was paid last time in each category (flight, hotel, parks, restaurants...)
against what the new trip costs now.

Each item is compared in its own currency (a park ticket in USD against a park ticket in USD), so the exchange rate does not
hide a real price change, and by the unit that makes the comparison fair: per night (hotel), per day (parks, car, stroller),
per person (flights, tickets) or the whole amount. A trip of 12 nights is not "more expensive" than one of 6 because the hotel
total doubled; the nightly rate is what says it.
"""


def unit_value(item, people=None):
    """(value, unit label) of an item: per night, per day or per person when the item says so, otherwise the whole amount."""
    a = item.get('amount')
    if a is None: return None, ''
    for key, label in (('nights', 'noite'), ('days', 'dia'), ('people', 'pessoa')):
        n = item.get(key) or (people if key == 'people' and item.get('per_person') else None)
        if n: return a / n, label
    return a, 'total'


def match(past_items, category):
    return [i for i in past_items if i.get('category') == category]


def rows(past, quotes):
    """One row per category present in either trip: {category, before, now, unit, currency, delta_pct, note}.

    past: the past trip ({items: [...]}); quotes: {category: item} of the new trip. Categories that only exist in the past
    trip come with now=None (still to quote); items without an amount show as unknown."""
    out, cats = [], []
    for i in past.get('items') or []:
        if i['category'] not in cats: cats.append(i['category'])
    for c in quotes or {}:
        if c not in cats: cats.append(c)
    for c in cats:
        before = match(past.get('items') or [], c)
        b = before[0] if before else None
        q = (quotes or {}).get(c)
        bv, bu = unit_value(b) if b else (None, '')
        qv, qu = unit_value(q) if q else (None, '')
        cur = (b or q or {}).get('currency', 'BRL')
        same = b and q and bu == qu and b.get('currency', 'BRL') == q.get('currency', 'BRL') and bv is not None and qv is not None
        out.append({'category': c, 'before': bv, 'now': qv if (same or not b or bv is None) else None, 'unit': bu or qu,
                    'currency': cur, 'delta_pct': (100 * (qv / bv - 1)) if same and bv else None,
                    'label_before': (b or {}).get('label', ''), 'label_now': (q or {}).get('label', ''),
                    'mismatch': bool(b and q and not same and bv is not None and qv is not None)})
    return out
