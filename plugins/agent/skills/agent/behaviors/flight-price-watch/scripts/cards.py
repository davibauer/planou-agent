"""One Planou card per trip plan (PLN0374): what `agent.py --plan-cards` prints for the agent plugin to sync.

Pure: the config, the state and the price files already read come in, {code: item} goes out, in the item shape of
watch_core.planou.sync (titulo, planou_description, status, prio, concluida...). Each trip in the config is one plan (a
trip with several date options is several trips, as the spreadsheet's Resumo shows them side by side):

  watching, flight not bought   -> open card ("A fazer"); the chosen plan (`card.chosen`) with high priority
  flight bought (status booked or done, or a "voo" booking)   -> closed as done ("Feito")
  any other status (paused...)  -> closed without action ("Sem ação")

Per trip, optional `card`: {"code": the card's code (default "plano-<id>"; set it to adopt a card made before),
"name": the short name for the title (default the trip's name), "chosen": true for the plan the user picked}.
The text is only the user's own trip (itinerary, fares, hotels, totals), never a third party's.
"""
import datetime as dt

PREFIX = 'plano-'
OPEN, DONE, PAUSED = 'A fazer', 'Feito', 'Sem ação'
BOUGHT = ('booked', 'done')
SOURCE_NAME = {'expedia': 'Expedia', 'manual': 'informado', 'google-page': 'Google Flights'}


def code_of(trip):
    return str((trip.get('card') or {}).get('code') or f"{PREFIX}{trip['id']}")


def ddmm(iso):
    return f'{iso[8:10]}/{iso[5:7]}' if iso and len(iso) >= 10 else '?'


def short(v, cur='BRL', money=None):
    """'R$ 18,6 mil' (thousands with one decimal, ',0' dropped); below a thousand, the full amount."""
    sym = {'BRL': 'R$', 'USD': 'US$', 'EUR': '€'}.get(cur, cur)
    if abs(v) < 1000: return money(v, cur) if money else f'{sym} {v:.0f}'
    k = f'{v / 1000:.1f}'.replace('.', ',')
    return f"{sym} {k[:-2] if k.endswith(',0') else k} mil"


def seats(trip):
    return (trip.get('adults') or 1) + (trip.get('children') or 0)


def travellers(trip):
    return (trip.get('adults') or 1) + (trip.get('children') or 0) * trip.get('child_factor', 1.0)


def flight_of(trip, tstate, alerts, grid_rows, to_home):
    """(total in the home currency, head, detail) of the plan's flight: the purchase, else the latest real fare for the
    whole party on the plan's dates (Expedia, Google alert, typed in), else the latest grid fare x travellers (an
    estimate). The total is None when there is no price yet."""
    plan = trip.get('plan') or {}
    bought = next((b for b in tstate.get('bookings') or [] if b.get('category') == 'voo'), None)
    if bought:
        return to_home(bought['amount'], bought['currency']), f"Voo comprado em {ddmm(bought.get('on'))}", bought.get('label') or ''
    party = (trip.get('adults') or 1, trip.get('children') or 0)
    reals = sorted((r for r in alerts if (r.get('out'), r.get('ret')) == (plan.get('out'), plan.get('ret'))
                    and (r.get('adults'), r.get('children')) == party), key=lambda r: r['ts'])
    if reals:
        r = reals[-1]
        return r['price'], f"Voo ({SOURCE_NAME.get(r.get('source'), 'alerta do Google')}, {ddmm(r['ts'])})", r.get('flight') or ''
    grid = sorted((g for g in grid_rows if g.get('trip') == trip['id'] and g.get('ok')
                   and (g.get('out'), g.get('ret')) == (plan.get('out'), plan.get('ret'))), key=lambda g: g['ts'])
    if grid:
        g, pax = grid[-1], travellers(trip)
        off = g['ok']
        stops = off.get('stops') or 0
        stops = 'direto' if not stops else f"{stops} {'conexão' if stops == 1 else 'conexões'}"
        return (round(off['price'] * pax), f"Voo (estimativa do Google Flights, {ddmm(g['ts'])})",
                ', '.join([*(off.get('airlines') or []), stops, f'preço por adulto x {pax:g}']))
    return None, 'Voo', 'ainda sem preço para as datas do plano'


def hotels_of(trip, tstate, hotel_rows, to_home, money, cur):
    """([text per hotel], total of the priced ones, names without a price)."""
    texts, total, missing = [], 0, []
    for h in trip.get('hotels') or []:
        nights = (dt.date.fromisoformat(h['checkout']) - dt.date.fromisoformat(h['checkin'])).days
        what = f"{h['name']} {ddmm(h['checkin'])}→{ddmm(h['checkout'])} ({nights} noites)"
        b = next((x for x in tstate.get('bookings') or [] if x.get('hotel') == h['id']), None)
        rows = sorted((r for r in hotel_rows if (r.get('trip'), r.get('hotel'), r.get('checkin'), r.get('checkout'))
                       == (trip['id'], h['id'], h['checkin'], h['checkout']) and r.get('home') is not None), key=lambda r: r['ts'])
        if b:
            amt = to_home(b['amount'], b['currency'])
            texts.append(f"{what}: reservado" + (f" {money(amt, cur)}" if amt is not None else '')
                         + (f", cancela grátis até {ddmm(b['cancel_by'])}" if b.get('cancel_by') else ''))
            if amt is not None: total += amt
            else: missing.append(h['name'])
        elif rows:
            r = rows[-1]
            texts.append(f"{what}: {money(r['home'], cur)} ({r.get('source') or '?'}, {ddmm(r['ts'])})")
            total += r['home']
        else:
            texts.append(f'{what}: sem preço ainda')
            missing.append(h['name'])
    return texts, total, missing


def card(trip, tstate, cfg, alerts, hotel_rows, grid_rows, to_home, money):
    cur = cfg.get('currency', 'BRL')
    c = trip.get('card') or {}
    status = trip.get('status', 'watching')
    flight, fhead, fdetail = flight_of(trip, tstate, alerts, grid_rows, to_home)
    bought_on = next((b.get('on') for b in tstate.get('bookings') or [] if b.get('category') == 'voo'), None)
    htexts, htotal, missing = hotels_of(trip, tstate, hotel_rows, to_home, money, cur)
    n = seats(trip)
    chosen = c.get('chosen') is True
    plan = trip.get('plan') or {}
    name = str(c.get('name') or trip.get('name') or trip['id']).strip()
    title = name + (f" {plan['label']}" if plan.get('label') and plan['label'] not in name else '') + (' (escolhido)' if chosen else '')
    if flight is not None: title += f' · voo {short(flight, cur, money)} p/{n}'
    if flight is not None and htexts and not missing: title += f' · voo+hotéis ~{short(flight + htotal, cur, money)}'
    lines = []
    it = trip.get('itinerary') or []
    if it:
        lines.append('Roteiro: ' + '; '.join(f"{ddmm(x.get('date'))} {x.get('text', '')}".strip()
                                            + (f" ({x['where']})" if x.get('where') else '') for x in it))
    price = f'{money(flight, cur)} para {n}' if flight is not None else ''
    lines.append(f"{fhead}: {', '.join(x for x in (price, fdetail) if x)}.")
    if htexts: lines.append('Hotéis: ' + '; '.join(htexts) + '.')
    if flight is not None and htexts:
        lines.append(f"Voo+hotéis: ~{money(flight + htotal, cur)}" + (f" (sem preço de: {', '.join(missing)})" if missing else '') + '.')
    marks = []
    if trip.get('target_total'): marks.append(f"meta do voo {money(trip['target_total'], cur)}")
    ref = trip.get('reference') or {}
    if ref.get('total'): marks.append(f"referência {money(ref['total'], cur)}" + (f" ({ref['label']})" if ref.get('label') else ''))
    if marks:
        m = '; '.join(marks)
        lines.append(m[0].upper() + m[1:] + '.')
    item = {'titulo': title[:300], 'planou_description': '\n'.join(lines), 'uid': trip['id'], 'origem': 'travel-agent',
            'area': 'viagem', 'prio': 1 if chosen else 2}
    if bought_on or status in BOUGHT:
        item['status'] = DONE
        if bought_on: item['concluida'] = bought_on
    elif status == 'watching':
        item['status'] = OPEN
    else:
        item['status'] = PAUSED
    return item


def build(cfg, state, alerts, hotel_rows, grid_rows, to_home, money):
    """{code: item} for every trip in the config (open or closed: the agent plugin decides which closes go up)."""
    out = {}
    for trip in cfg.get('trips') or []:
        if not trip.get('id'): continue
        tstate = (state.get('trips') or {}).get(trip['id']) or {}
        out[code_of(trip)] = card(trip, tstate, cfg, alerts, hotel_rows, grid_rows, to_home, money)
    return out
