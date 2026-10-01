#!/usr/bin/env python3
"""The trip's dates that cost money when missed: free-cancellation deadlines, when bookings open, online check-in, documents.

Items come from three places, all dated:
- `deadlines` in the trip config (the user's own: "restaurant bookings open", "passport of the child", "ticket prices out");
- the bookings registered with `--booked` (`cancel_by`), and the hotels being watched that were booked;
- the booked flight: online check-in the day before departure and the day before the return.

Each item reminds at `remind_days` before its date (default 14, 3, 1 and on the day), once per threshold, and can be synced
to the user's calendar (the model creates the event and records its id with `--timeline synced ID EVENT_ID`).
"""
import re, unicodedata, datetime as dt


def _slug(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')[:40]


def items(trip, tstate):
    out = []
    for d in trip.get('deadlines') or []:
        out.append({'id': d.get('id') or f"{d['date']}-{_slug(d['title'])}", 'date': d['date'], 'title': d['title'], 'notes': d.get('notes', '')})
    for b in tstate.get('bookings') or []:
        if b.get('cancel_by'):
            out.append({'id': f"cancel-{_slug(b['label'] or b['category'])}", 'date': b['cancel_by'],
                        'title': f"Último dia para cancelar grátis: {b['label'] or b['category']}", 'notes': b.get('ref', '')})
        if b['category'] == 'voo':
            for key, what in (('out', 'ida'), ('ret', 'volta')):
                if b.get(key):
                    day = (dt.date.fromisoformat(b[key]) - dt.timedelta(days=1)).isoformat()
                    out.append({'id': f'checkin-{what}', 'date': day, 'title': f'Check-in online do voo de {what} ({b[key]})',
                                'notes': b.get('ref', '')})
    return sorted(out, key=lambda i: i['date'])


def due(all_items, tstate, today=None, remind_days=(14, 3, 1, 0)):
    """Reminder lines for the items inside a threshold, once per (item, threshold); marks them in tstate['reminded']."""
    today = dt.date.fromisoformat(today) if today else dt.date.today()
    sent, lines = tstate.setdefault('reminded', {}), []
    for it in all_items:
        left = (dt.date.fromisoformat(it['date']) - today).days
        if left < 0: continue
        hit = [d for d in sorted(remind_days) if left <= d]
        if not hit: continue
        th = hit[0]
        if sent.get(it['id']) is not None and sent[it['id']] <= th: continue
        sent[it['id']] = th
        when = 'hoje' if left == 0 else 'amanhã' if left == 1 else f'em {left} dias'
        lines.append(f"PRAZO {when} ({it['date'][8:10]}/{it['date'][5:7]}): {it['title']}" + (f" | {it['notes']}" if it['notes'] else ''))
    return lines


def unsynced(all_items, tstate):
    synced = tstate.get('calendar') or {}
    return [it for it in all_items if it['id'] not in synced]
