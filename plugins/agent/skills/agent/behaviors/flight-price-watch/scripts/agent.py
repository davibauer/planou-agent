#!/usr/bin/env python3
"""travel-agent: watches the fares of the user's trips and prints only what deserves attention.

  agent.py                         tick: searches every trip in "watching", saves the observations, prints the alerts
  agent.py --trip ID               tick of one trip only
  agent.py --dry                   tick without saving anything (test)
  agent.py --report [ID]           latest date grid and history of each trip, without searching
  agent.py --gmail                 cursor and query of the promotions step (Claude reads the e-mails with the Gmail connector)
  agent.py --gmail mark EPOCH      moves the cursor after the e-mails were read
  agent.py --gflights [body|mark EPOCH|import CSV ORIGIN DEST]
                                   Google Flights price-tracking e-mails: real price for the whole party (gflights.py)
  agent.py --points MILES TAXES CASH [--bonus PCT] [--program NAME]
                                   is an award ticket worth it, compared with the cash value of the user's points?
  agent.py --bonus [PROGRAM PCT [--until DATE] [--source TEXT]]   transfer bonus in force (PCT 0 removes)
  agent.py --award TRIP PROGRAM MILES TAXES [--note TEXT]          award price per person, round trip ("clear" removes all)
  agent.py --miles [ID]            break-even per program and the best cash/miles combination, totals for all travellers
  agent.py --quote TRIP CATEGORY AMOUNT [CURRENCY] [--nights N] [--days N] [--people N] [--label TEXT]
                                   price found now for the new trip (hotel, parques, restaurantes...); AMOUNT '-' removes
  agent.py --real TRIP OUT RET PRICE CUR [--source S] [--flight T] [--bags T]   real fare for the whole party (Expedia...)
  agent.py --hotels [ID] / --hotel-price TRIP HOTEL TOTAL CUR [--source S]      hotels being watched, rebooking alerts
  agent.py --booked TRIP CATEGORY AMOUNT CUR [--label --ref --cancel-by --sender --hotel --out --ret]   something was bought
  agent.py --timeline [calendar | synced TRIP ID EVENT_ID]                      deadlines and calendar sync
  agent.py --dashboard ID          writes the trip page (cache/dashboard/ID.html) for the model to publish
  agent.py --sheet                 rewrites the Google Sheets spreadsheet now (the tick does it every sheet.every_hours)
  agent.py --done STEP             marks a model step as done (expedia, hoteis, calendario, painel, miles:<program>)
  agent.py --plan-cards             one Planou card per trip plan, as JSON (read only; the agent plugin syncs it: cards.py)
  agent.py --compare [ID]          each category of the new trip against the last trip to the same destination
                                   (config/past_trips.json), in the same currency and unit (per night, per day, total)

Prices are searched for 1 adult (Google does not return a search with children in the page) and multiplied by the number of
travellers of the trip (adults + children x child_factor). Each date cell keeps two fares: the best one within max_stops and the
best one with more stops, which only becomes an alert when it is at least extra_stops_min_saving_pct below the reference (or
below the best within max_stops, when the trip has no reference).

Exit: 0 = ok (empty output = nothing new), 2 = source broken ("AGENTE QUEBRADO (...)").
"""
import os, sys, json, tempfile, time, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import paths, miles, compare, gflights, fx, timeline, dashboard, sheets, cards

INF = float('inf')
SYMBOL = {'BRL': 'R$', 'USD': 'US$', 'EUR': '€'}
DEFAULTS = {'currency': 'BRL', 'pause_seconds': 2, 'min_drop_pct': 3, 'rise_warn_pct': 15}


# ---------- config and state ----------

def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f: return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # unique temporary file, then an atomic replace: a fixed '<path>.tmp' breaks when the runner and the CLI write at once
    # (the same approach as watch_core.fileio, which this plugin does not carry)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f: json.dump(data, f, indent=1, ensure_ascii=False)
        try: mode = os.stat(path).st_mode & 0o7777
        except OSError: um = os.umask(0); os.umask(um); mode = 0o666 & ~um
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try: os.remove(tmp)
        except OSError: pass
        raise


def config():
    return {**DEFAULTS, **load_json(paths.CONFIG, {})}


def money(v, cur='BRL'):
    return f'{SYMBOL.get(cur, cur)} {v:,.0f}'.replace(',', '.')


# ---------- date grid ----------

def expand_dates(items):
    """["2026-12-17..2026-12-19", "2026-12-21"] -> every date, sorted and without repeats."""
    out = set()
    for it in items or []:
        if '..' in it:
            a, b = (dt.date.fromisoformat(x.strip()) for x in it.split('..', 1))
            while a <= b: out.add(a.isoformat()); a += dt.timedelta(days=1)
        else: out.add(dt.date.fromisoformat(it.strip()).isoformat())
    return sorted(out)


def grid(trip):
    """(origin, destination, out, ret) of every cell; ret is None for a one-way trip."""
    outs, rets = expand_dates(trip.get('depart')), expand_dates(trip.get('return'))
    if trip.get('home_by'): rets = [r for r in rets if r <= trip['home_by']]     # must be back home by that date
    lo, hi = trip.get('min_nights') or 0, trip.get('max_nights') or 10 ** 6
    cells = []
    for o in trip.get('origins') or []:
        for d in trip.get('destinations') or []:
            for a in outs:
                if not rets: cells.append((o, d, a, None)); continue
                for b in rets:
                    n = (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days
                    if n > 0 and lo <= n <= hi: cells.append((o, d, a, b))
    return cells


def travellers(trip):
    return (trip.get('adults') or 1) + (trip.get('children') or 0) * trip.get('child_factor', 1.0)


def split(offers, max_stops):
    """Best offer within max_stops and best offer with more stops (None when there is none)."""
    ok = [x for x in offers if x['stops'] <= max_stops]
    extra = [x for x in offers if x['stops'] > max_stops]
    return (min(ok, key=lambda x: x['price']) if ok else None), (min(extra, key=lambda x: x['price']) if extra else None)


# ---------- alerts ----------

def describe(cell, trip, cur):
    """One line for a cell: family total, price per adult, dates, airlines, stops, outbound times and link."""
    off, pax = cell['offer'], travellers(trip)
    legs = off['legs']
    via = ', '.join(l['to'] for l in legs[:-1])
    stops = 'direto' if not off['stops'] else f"{off['stops']} conex{'ão' if off['stops'] == 1 else 'ões'} ({via})"
    dates = f"{cell['out'][8:10]}/{cell['out'][5:7]}" + (f"→{cell['ret'][8:10]}/{cell['ret'][5:7]}" if cell.get('ret') else '')
    pax_txt = f" x {pax:g}" if pax != 1 else ''
    return (f"{money(off['price'] * pax, cur)} ({money(off['price'], cur)}{pax_txt}) {cell['o']}→{cell['d']} {dates} "
            f"{', '.join(off['airlines'])}, {stops}, ida sai {legs[0]['dep'][11:]} chega {legs[-1]['arr'][8:10]}/{legs[-1]['arr'][5:7]} "
            f"{legs[-1]['arr'][11:]} | {cell.get('url', '')}").rstrip(' |')


def evaluate(trip, ok_cells, extra_cells, tstate, cfg):
    """Pure: the best cells of this tick + the trip's alert marks -> alert lines (and the marks are updated in tstate).

    First tick of a trip = one BASE line. After that: new low (min_drop_pct below the last alerted fare), price rising
    (rise_warn_pct above the last alerted fare, once), crossing the target and the reference, and a fare with more stops than
    max_stops that saves at least extra_stops_min_saving_pct.
    """
    cur, pax = cfg['currency'], travellers(trip)
    drop, rise = cfg['min_drop_pct'] / 100, cfg['rise_warn_pct'] / 100
    best = min(ok_cells, key=lambda c: c['offer']['price']) if ok_cells else None
    best_x = min(extra_cells, key=lambda c: c['offer']['price']) if extra_cells else None
    total = round(best['offer']['price'] * pax) if best else None
    total_x = round(best_x['offer']['price'] * pax) if best_x else None
    lines = []

    if 'alerted' not in tstate:
        tstate['alerted'] = total
        tstate['baseline'] = dt.date.today().isoformat()
        lines.append('BASE: ' + (describe(best, trip, cur) if best else f"nenhuma tarifa com até {trip.get('max_stops', 1)} conexão(ões)"))
    elif total is not None:
        last = tstate.get('alerted') or INF
        if total <= last * (1 - drop):
            before = f' [antes {money(last, cur)}]' if last < INF else ''
            lines.append('QUEDA: ' + describe(best, trip, cur) + before)
            tstate['alerted'], tstate['rise_warned'] = total, False
        elif last < INF and total >= last * (1 + rise) and not tstate.get('rise_warned'):
            lines.append(f"SUBIU: o menor agora é {money(total, cur)}, {100 * (total / last - 1):.0f}% acima do último aviso "
                         f"({money(last, cur)}): " + describe(best, trip, cur))
            tstate['rise_warned'] = True

    for key, label in (('target_total', 'META'), ('reference', 'ABAIXO DA REFERÊNCIA')):
        limit = (trip.get('reference') or {}).get('total') if key == 'reference' else trip.get(key)
        if not limit or total is None: continue
        mark = f'hit_{key}'
        if total <= limit and not tstate.get(mark):
            name = (trip.get('reference') or {}).get('label') if key == 'reference' else ''
            lines.append(f"{label} ({money(limit, cur)}{', ' + name if name else ''}): " + describe(best, trip, cur))
            tstate[mark] = True
        elif total > limit: tstate[mark] = False

    ref = (trip.get('reference') or {}).get('total') or total
    pct = trip.get('extra_stops_min_saving_pct', 25) / 100
    if total_x is not None and ref and total_x <= ref * (1 - pct) and total_x <= (tstate.get('alerted_extra') or INF) * (1 - drop):
        against = 'da referência' if (trip.get('reference') or {}).get('total') else f"do melhor com até {trip.get('max_stops', 1)} conexão(ões)"
        lines.append(f"MAIS CONEXÕES COMPENSA ({100 * (1 - total_x / ref):.0f}% abaixo {against}): " + describe(best_x, trip, cur))
        tstate['alerted_extra'] = total_x
    return lines


# ---------- tick ----------

def run_trip(trip, cfg, search, save=True):
    """Searches every cell of the trip. Returns (lines, cells_ok, cells_extra, errors)."""
    max_stops = trip.get('max_stops', 1)
    ok_cells, extra_cells, errors, stamp = [], [], [], dt.datetime.now().isoformat(timespec='seconds')
    cells = grid(trip)
    for i, (o, d, a, b) in enumerate(cells):
        if i: time.sleep(cfg['pause_seconds'])
        try: offers, url = search(o, d, a, b, 1, cfg['currency'])
        except Exception as e:                       # one bad cell does not stop the grid
            errors.append(f'{o}-{d} {a}/{b}: {type(e).__name__}: {str(e)[:120]}'); continue
        ok, extra = split(offers, max_stops)
        base = {'o': o, 'd': d, 'out': a, 'ret': b, 'url': url}
        if ok: ok_cells.append({**base, 'offer': ok})
        if extra: extra_cells.append({**base, 'offer': extra})
        if save:
            with open(paths.PRICES, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'ts': stamp, 'trip': trip['id'], **base, 'ok': ok, 'extra': extra, 'n': len(offers)},
                                   ensure_ascii=False) + '\n')
    return ok_cells, extra_cells, errors, len(cells)


def header(trip):
    pax = travellers(trip)
    who = f"{trip.get('adults') or 1} adulto(s)" + (f" + {trip['children']} criança(s)" if trip.get('children') else '')
    return f"== VOOS {trip['id']} ({trip.get('name', '')}): {who}, total = preço por adulto x {pax:g}"


def tick(only=None, save=True, search=None):
    cfg = config()
    trips = [t for t in cfg.get('trips') or [] if t.get('status', 'watching') == 'watching' and (not only or t['id'] == only)]
    if not trips:
        print('nenhuma viagem em watching no config.json' if not only else f'viagem {only} nao existe ou nao esta em watching')
        return 0
    if search is None:
        try:
            import flights; flights.search                                   # noqa: B018
            from fast_flights import create_query                            # noqa: F401  (venv check)
            search = flights.search
        except ImportError:
            print('AGENTE QUEBRADO (setup): fast-flights nao instalado; rodar setup.py e usar o python do venv'); return 2
    paths.mkdirs()
    state = load_json(paths.STATE, {})
    rc = 0
    for trip in trips:
        tstate = state.setdefault('trips', {}).setdefault(trip['id'], {})
        if any(b['category'] == 'voo' for b in tstate.get('bookings') or []): continue   # flight bought: no more fare search
        ok_cells, extra_cells, errors, n = run_trip(trip, cfg, search, save)
        if n and len(errors) == n:
            print(f"AGENTE QUEBRADO (google-flights): {trip['id']}: as {n} buscas falharam; primeira: {errors[0]}"); rc = 2; continue
        if n and not ok_cells and not extra_cells:
            print(f"AGENTE QUEBRADO (google-flights): {trip['id']}: {n} buscas sem nenhuma tarifa (parser ou bloqueio?)"); rc = 2; continue
        tstate = state.setdefault('trips', {}).setdefault(trip['id'], {})
        lines = evaluate(trip, ok_cells, extra_cells, tstate, cfg)
        tstate['last_tick'] = dt.datetime.now().isoformat(timespec='minutes')
        if lines:
            print(header(trip))
            for l in lines: print(l)
            if ok_cells:
                cash_pp = min(c['offer']['price'] for c in ok_cells)
                line = flight_vs_past(trip, cash_pp, cfg)
                if line: print(line)
                for l in miles_lines(trip, cash_pp, state, cfg): print(l)
            if errors: print(f'  ({len(errors)} de {n} buscas falharam; primeira: {errors[0]})')
    for l in extra_tick(cfg, state, save): print(l)
    if save and sheet_due(cfg, state):
        try: sheet_push(cfg, state)
        except Exception as e: print(f'(planilha não atualizada: {type(e).__name__}: {str(e)[:150]})')
    due = gmail_due(state, cfg)
    if due: print(due)
    if save: save_json(paths.STATE, state)
    return rc


def gmail_due(state, cfg, now=None):
    """The runner has no Gmail: when the Google alerts were last read more than gmail.check_hours ago (default 12), the tick
    prints one line so the session wakes up and does the Gmail step (which moves the cursor)."""
    if not (cfg.get('gmail') or {}).get('google_alerts', True) or 'gflights_cursor' not in state: return ''
    hours = (cfg.get('gmail') or {}).get('check_hours', 12)
    age = ((now or time.time()) - state['gflights_cursor']) / 3600
    return f'== GMAIL: alertas do Google e promoções sem leitura há {age:.0f} h (passo do Gmail)' if age >= hours else ''


# ---------- miles ----------

def seats(trip):
    return (trip.get('adults') or 1) + (trip.get('children') or 0)


def miles_lines(trip, cash_pp, state, cfg, full=False):
    """MILHAS block: what the points are worth, active bonuses, break-even per program and the best cash/miles combination
    with the award prices registered for the trip (--award). Totals are always for all the seats of the trip."""
    p, cur = cfg.get('points') or {}, cfg['currency']
    pp = miles.per_point(p)
    if not pp or not p.get('programs'): return []
    bon, taxes, n = state.get('bonuses') or {}, p.get('award_taxes_estimate', 700), seats(trip)
    active = [f"{k} +{miles.active_bonus(bon, k):g}%" + (f" até {bon[k]['until'][8:10]}/{bon[k]['until'][5:7]}" if bon[k].get('until') else '')
              for k in p['programs'] if miles.active_bonus(bon, k)]
    even = []
    for k in p['programs']:
        m = miles.break_even(cash_pp, taxes, pp, miles.miles_per_point(p, bon, k))
        even.append(f"{k} {m / 1000:.0f} mil")
    out = [f"MILHAS: pontos a {money(pp * 1000, cur)} por mil; bônus ativo: {', '.join(active) or 'nenhum'}; "
           f"empate por pessoa ida e volta (taxas estimadas {money(taxes, cur)}): {', '.join(even)}"]
    if full:
        scen = [f"+{b}% → {miles.break_even(cash_pp, taxes, pp, 1 + b / 100) / 1000:.0f} mil" for b in (0, 50, 80, 100)]
        bal = p.get('balance', 0)
        thousands = lambda v: f'{v:,.0f}'.replace(',', '.')
        out.append(f"  com bônus de transferência (1:1), o empate por pessoa vai de: {', '.join(scen)}; "
                   f"o saldo de {thousands(bal)} pontos vira {thousands(bal * 2)} milhas com 100%")
    awards = (state.get('awards') or {}).get(trip['id']) or []
    if awards:
        opts = miles.combos(n, cash_pp, awards, p, bon)
        cash_all, best = n * cash_pp, opts[0]
        if best['k']:
            pts = f"{best['points']:,.0f}".replace(',', '.')
            out.append(f"MELHOR COMBINAÇÃO: {best['k']} pessoa(s) com {best['program']} ({best['miles'] / 1000:.0f} mil milhas cada) + "
                       f"{n - best['k']} em dinheiro = {money(best['cost'], cur)} para {n} ({money(best['cash_out'], cur)} saem do bolso, "
                       f"{pts} pontos usados); tudo em dinheiro = {money(cash_all, cur)}")
        else:
            out.append(f"MELHOR COMBINAÇÃO: tudo em dinheiro, {money(cash_all, cur)} para {n}; nenhum resgate registrado compensa")
        if full:
            for o in opts[1:6]:
                out.append(f"  {o['k']} com {o['program'] or 'dinheiro'}: {money(o['cost'], cur)} (bolso {money(o['cash_out'], cur)})"
                           + (f" [{o['note']}]" if o['note'] else ''))
    return out


def latest_cash_pp(trip_id):
    if not os.path.exists(paths.PRICES): return None
    obs = [json.loads(l) for l in open(paths.PRICES, encoding='utf-8')]
    mine = [o for o in obs if o['trip'] == trip_id and o['ok']]
    if not mine: return None
    last = max(o['ts'] for o in mine)
    return min(o['ok']['price'] for o in mine if o['ts'] == last)


def bonus_cmd(args):
    """--bonus PROGRAM PCT [--until AAAA-MM-DD] [--source TEXT]; PCT 0 removes it."""
    def opt(name):
        if name in args: i = args.index(name); v = args[i + 1]; del args[i:i + 2]; return v
    until, source = opt('--until'), opt('--source')
    state = load_json(paths.STATE, {}); b = state.setdefault('bonuses', {})
    if not args:
        print(json.dumps(b, ensure_ascii=False, indent=1) if b else 'nenhum bônus registrado'); return
    prog, pct = args[0], float(args[1])
    if pct: b[prog] = {'pct': pct, 'until': until, 'source': source or '', 'seen': dt.date.today().isoformat()}
    else: b.pop(prog, None)
    save_json(paths.STATE, state); print(f'bônus {prog}: {pct:g}%' + (f' até {until}' if until else ''))


def award_cmd(args):
    """--award TRIP PROGRAM MILES TAXES [--note TEXT] (per person, round trip); --award TRIP clear."""
    note = ''
    if '--note' in args: i = args.index('--note'); note = args[i + 1]; del args[i:i + 2]
    state = load_json(paths.STATE, {}); aw = state.setdefault('awards', {})
    trip = args[0]
    if args[1:2] == ['clear']: aw.pop(trip, None)
    else:
        lst = [x for x in aw.setdefault(trip, []) if x['program'] != args[1] or x.get('note') != note]
        lst.append({'program': args[1], 'miles': float(args[2]), 'taxes': float(args[3]), 'note': note, 'seen': dt.date.today().isoformat()})
        aw[trip] = lst
    save_json(paths.STATE, state); print(f"resgates de {trip}: {len(aw.get(trip) or [])}")


def miles_cmd(only=None):
    cfg, state = config(), load_json(paths.STATE, {})
    for trip in cfg.get('trips') or []:
        if only and trip['id'] != only: continue
        cash = latest_cash_pp(trip['id'])
        if cash is None: print(f"{trip['id']}: sem preço ainda"); continue
        print(f"== {trip['id']}: melhor em dinheiro agora {money(cash * seats(trip), cfg['currency'])} para {seats(trip)}")
        for l in miles_lines(trip, cash, state, cfg, full=True): print(l)


# ---------- past trips ----------

def past_trip_for(trip):
    """The past trip to compare with: trip.compare_with, or the latest past trip that shares a destination."""
    past = load_json(paths.PAST_TRIPS, {}).get('trips') or []
    if trip.get('compare_with'): return next((p for p in past if p['id'] == trip['compare_with']), None)
    dests = set(trip.get('destinations') or []) | {k.lower() for k in trip.get('keywords') or []}
    hits = [p for p in past if dests & (set(p.get('destinations') or []) | {k.lower() for k in p.get('keywords') or []})]
    return max(hits, key=lambda p: p.get('start', '')) if hits else None


def flight_quote(trip, past, cash_pp):
    """Flight of the new trip in the unit of the past one: total when the party has the same size, per person otherwise."""
    n = seats(trip)
    same = (past or {}).get('seats') in (None, n)
    return {'category': 'voo', 'amount': cash_pp * n, 'currency': 'BRL', 'label': 'melhor tarifa agora',
            **({} if same else {'people': n})}


def flight_vs_past(trip, cash_pp, cfg):
    past = past_trip_for(trip)
    if not past: return ''
    r = next((r for r in compare.rows(past, {'voo': flight_quote(trip, past, cash_pp)}) if r['category'] == 'voo'), None)
    if not r or r['delta_pct'] is None: return ''
    unit = f" por {r['unit']}" if r['unit'] != 'total' else f" para {seats(trip)}"
    return (f"VS {past.get('name', past['id'])}: voo {money(r['now'], r['currency'])}{unit} agora contra "
            f"{money(r['before'], r['currency'])} ({r['delta_pct']:+.0f}%)")


def item_args(args):
    """CATEGORY AMOUNT [CURRENCY] [--nights N] [--days N] [--people N] [--label TEXT] -> item."""
    item = {}
    for flag, key, cast in (('--nights', 'nights', int), ('--days', 'days', int), ('--people', 'people', int), ('--label', 'label', str)):
        if flag in args: i = args.index(flag); item[key] = cast(args[i + 1]); del args[i:i + 2]
    item.update(category=args[0], amount=float(args[1]) if args[1] != '-' else None, currency=(args[2] if len(args) > 2 else 'BRL').upper())
    return item


def quote_cmd(args):
    """--quote TRIP CATEGORY AMOUNT [CURRENCY] [--nights N] [--days N] [--people N] [--label TEXT]; AMOUNT '-' removes."""
    state = load_json(paths.STATE, {}); q = state.setdefault('quotes', {}).setdefault(args[0], {})
    item = item_args(args[1:])
    if item['amount'] is None: q.pop(item['category'], None)
    else: q[item['category']] = {**item, 'seen': dt.date.today().isoformat()}
    save_json(paths.STATE, state); print(f"cotações de {args[0]}: {', '.join(q) or '-'}")


def compare_cmd(only=None):
    cfg, state = config(), load_json(paths.STATE, {})
    for trip in cfg.get('trips') or []:
        if only and trip['id'] != only: continue
        past = past_trip_for(trip)
        if not past: print(f"{trip['id']}: nenhuma viagem passada para o mesmo destino em past_trips.json"); continue
        quotes = dict((state.get('quotes') or {}).get(trip['id']) or {})
        cash = latest_cash_pp(trip['id'])
        if cash is not None and 'voo' not in quotes: quotes['voo'] = flight_quote(trip, past, cash)   # a quoted real fare wins
        print(f"== {trip['id']} contra {past.get('name', past['id'])} ({past.get('start', '')} a {past.get('end', '')}, "
              f"{past.get('nights', '?')} noites, {past.get('seats', '?')} pessoas)")
        for r in compare.rows(past, quotes):
            unit = '' if r['unit'] in ('total', '') else f"/{r['unit']}"
            before = (money(r['before'], r['currency']) + unit if r['before'] is not None
                      else '?' if r['label_before'] else 'não teve')
            now = money(r['now'], r['currency']) + unit if r['now'] is not None else ('unidade diferente' if r['mismatch'] else 'a cotar')
            delta = f" ({r['delta_pct']:+.0f}%)" if r['delta_pct'] is not None else ''
            print(f"  {r['category']:12s} antes {before:18s} agora {now}{delta}  [{r['label_before'] or r['label_now']}]")


# ---------- Google Flights alert e-mails ----------

def pair_key(r):
    return (r['o'], r['d'], r['out'], r['ret'], r['adults'], r['children'])


def load_alerts():
    if not os.path.exists(paths.GOOGLE_ALERTS): return []
    return [json.loads(l) for l in open(paths.GOOGLE_ALERTS, encoding='utf-8') if l.strip()]


def record_alerts(rows, cfg):
    """Saves new rows (dedupe by time, pair and price) and returns the lines to show: every drop and every row that crosses the
    trip's target or reference or beats the lowest price seen for that pair, for the trips in watching."""
    old = load_alerts()
    seen = {(r['ts'], pair_key(r), r['price']) for r in old}
    new = [r for r in rows if (r['ts'], pair_key(r), r['price']) not in seen]
    if new:
        paths.mkdirs()
        with open(paths.GOOGLE_ALERTS, 'a', encoding='utf-8') as f:
            for r in new: f.write(json.dumps(r, ensure_ascii=False) + '\n')
    lines = []
    for trip in cfg.get('trips') or []:
        if trip.get('status', 'watching') != 'watching': continue
        for r in sorted(new, key=lambda r: r['ts']):
            if not gflights.matches(r, trip, expand_dates): continue
            before = [x for x in old if pair_key(x) == pair_key(r) and x['ts'] < r['ts']]
            low = min(before, key=lambda x: x['price'], default=None)
            ref, target = (trip.get('reference') or {}).get('total'), trip.get('target_total')
            tags = [t for t, ok in (('NOVO MÍNIMO', low and r['price'] < low['price']), ('META', target and r['price'] <= target),
                                    ('ABAIXO DA REFERÊNCIA', ref and r['price'] <= ref)) if ok]
            if r['trend'] != 'down' and not tags: continue
            cur, n = r['currency'], r['adults'] + r['children']
            pct = 100 * (r['price'] / r['was'] - 1) if r['was'] else 0
            dates = f"{r['out'][8:10]}/{r['out'][5:7]}" + (f"→{r['ret'][8:10]}/{r['ret'][5:7]}" if r['ret'] else '')
            lines.append(f"{' '.join(tags) + ': ' if tags else ''}{trip['id']} {r['o']}→{r['d']} {dates}: {money(r['price'], cur)} para {n} "
                         f"(antes {money(r['was'], cur)}, {pct:+.0f}%)" + (f", {r['flight']}" if r['flight'] else '')
                         + (f" | menor já visto nesse par: {money(low['price'], cur)} em {low['ts'][:10]}" if low else '')
                         + (f" | alerta de {r['ts'].replace('T', ' ')} UTC" if not r.get('source') else f" | {r['source']} em {r['ts'].replace('T', ' ')}"))
    return new, lines


def gflights_cmd(args):
    """--gflights: cursor and query | body < file: parse one e-mail | mark EPOCH | import CSV ORIGIN DEST."""
    state, cfg = load_json(paths.STATE, {}), config()
    if args[:1] == ['mark']:
        state['gflights_cursor'] = int(args[1]); save_json(paths.STATE, state); print('cursor gravado'); return
    if args[:1] == ['body']:
        rows = gflights.parse(sys.stdin.read())
        if not rows: print('nenhuma rota reconhecida neste e-mail (formato mudou? ver gflights.py)'); return
        new, lines = record_alerts(rows, cfg)
        if lines:
            print('== ALERTAS DO GOOGLE (preço real para o grupo inteiro)')
            for l in lines: print(l)
        print(f'({len(rows)} rota(s) no e-mail, {len(new)} nova(s) no histórico)')
        return
    if args[:1] == ['import']:
        import csv
        o, d = args[2], args[3]
        rows = []
        for c in csv.DictReader(open(args[1], encoding='utf-8')):
            a, k = gflights.party(c['passengers'])
            rows.append({'ts': c['email_date'][:16], 'o': o, 'd': d, 'out': c['depart_date'], 'ret': c['return_date'] or None,
                         'adults': a, 'children': k, 'price': float(c['price_now_brl']),
                         'was': float(c['price_was_brl'] or 0), 'currency': 'BRL',
                         'trend': c.get('trend') or '', 'flight': ' '.join(x for x in (c.get('airline'), c.get('stops')) if x)})
        new, _ = record_alerts(rows, {})
        print(f'importadas {len(new)} de {len(rows)} linhas'); return
    cursor = state.get('gflights_cursor') or int(time.time()) - 7 * 86400
    print(f'cursor: {cursor} ({dt.datetime.fromtimestamp(cursor):%d/%m %H:%M})')
    print(f'query: from:noreply-travel@google.com after:{cursor}')
    print('para cada e-mail: get_message em texto puro -> arquivo -> agent.py --gflights body < arquivo')
    print(f'depois: agent.py --gflights mark {int(time.time())}')


def alerts_summary(trip, cfg):
    """Latest real price per date pair of the trip seen in the Google alerts, with the lowest ever for that pair."""
    rows = [r for r in load_alerts() if gflights.matches(r, trip, expand_dates)]
    if not rows: return []
    out = ['  alertas do Google (preço real para o grupo; último e menor de cada par):']
    for key in sorted({pair_key(r) for r in rows}, key=lambda k: min(r['price'] for r in rows if pair_key(r) == k and r['ts'] == max(x['ts'] for x in rows if pair_key(x) == k))):
        mine = [r for r in rows if pair_key(r) == key]
        last, low = max(mine, key=lambda r: r['ts']), min(mine, key=lambda r: r['price'])
        cur = last['currency']
        out.append(f"    {key[2][8:10]}/{key[2][5:7]}→{(key[3] or '')[8:10]}/{(key[3] or '')[5:7]}: {money(last['price'], cur)} em {last['ts'][:10]}"
                   f" | menor {money(low['price'], cur)} em {low['ts'][:10]} | {len(mine)} alertas")
    return out


# ---------- the rest of the tick: dollar, deadlines, miles, model steps ----------

def fx_step(cfg, state, save=True, fetch=None):
    c = cfg.get('fx')
    if not c: return []
    today = dt.date.today().isoformat()
    hist = [json.loads(l) for l in open(paths.FX, encoding='utf-8') if l.strip()] if os.path.exists(paths.FX) else []
    if not hist or hist[-1].get('fetched') != today:
        try: day, rate = (fetch or fx.fetch)(c.get('currency', 'USD'), c.get('home', 'BRL'))
        except Exception as e: return [f'(câmbio indisponível agora: {type(e).__name__})']
        row = {'date': day, 'rate': rate, 'fetched': today}
        hist.append(row)
        if save:
            paths.mkdirs()
            with open(paths.FX, 'a', encoding='utf-8') as f: f.write(json.dumps(row) + '\n')
    return fx.evaluate(hist, c, state.setdefault('fx', {}), today)


def latest_fx(cfg):
    if os.path.exists(paths.FX):
        rows = [json.loads(l) for l in open(paths.FX, encoding='utf-8') if l.strip()]
        if rows: return rows[-1]['rate']
    return (cfg.get('fx') or {}).get('fallback_rate')


def to_home(amount, currency, cfg):
    """Amount in the home currency (config currency); None when there is no rate for that currency."""
    if currency == cfg['currency']: return amount
    rate = latest_fx(cfg)
    return amount * rate if rate and currency == (cfg.get('fx') or {}).get('currency', 'USD') else None


def extra_tick(cfg, state, save=True):
    """Script-only parts of the tick after the fares: dollar, deadlines, miles searches to do, steps that need the model."""
    lines = fx_step(cfg, state, save)
    for trip in cfg.get('trips') or []:
        if trip.get('status', 'watching') not in ('watching', 'booked'): continue
        tstate = state.setdefault('trips', {}).setdefault(trip['id'], {})
        for l in timeline.due(timeline.items(trip, tstate), tstate, remind_days=tuple(cfg.get('remind_days', (14, 3, 1, 0)))):
            lines.append(f"{trip['id']}: {l}")
    p, bon = cfg.get('points') or {}, state.get('bonuses') or {}
    searched = state.setdefault('miles_searched', {})
    for prog in p.get('programs') or {}:
        pct = miles.active_bonus(bon, prog)
        key = f"{prog}:{pct}:{(bon.get(prog) or {}).get('until')}"
        if pct >= cfg.get('miles_search_bonus', 50) and searched.get(prog) != key:
            lines.append(f"== MILHAS: bônus de {pct:g}% para {prog} até {(bon.get(prog) or {}).get('until') or '?'}: pesquisar resgate nas "
                         f"datas do plano (site do programa, pelo Chrome, só leitura), registrar com --award e depois --done miles:{prog}")
    steps = steps_due(cfg, state)
    if steps: lines.append('== PASSOS: ' + '; '.join(steps))
    return lines


STEPS = {'expedia': 'preço real do voo para o grupo no Expedia (--real)', 'hoteis': 'recotar os hotéis monitorados (--hotels)',
         'calendario': 'criar no calendário os prazos novos (--timeline calendar)', 'painel': 'republicar o painel (--dashboard)'}


def steps_due(cfg, state, now=None):
    """Daily steps that need the model's connectors; each one is marked with --done <step>."""
    now, done = now or time.time(), state.get('steps') or {}
    hours = cfg.get('steps_hours', 24)
    trips = [t for t in cfg.get('trips') or [] if t.get('status', 'watching') in ('watching', 'booked')]
    ts = lambda t: (state.get('trips') or {}).get(t['id'], {})
    want = []
    if cfg.get('expedia_flights') and any(t.get('status', 'watching') == 'watching' and not any(b['category'] == 'voo' for b in ts(t).get('bookings') or [])
                                          for t in trips): want.append('expedia')
    if any(t.get('hotels') for t in trips): want.append('hoteis')
    if cfg.get('calendar') and any(timeline.unsynced(timeline.items(t, ts(t)), ts(t)) for t in trips): want.append('calendario')
    if cfg.get('dashboard'): want.append('painel')
    return [f'{s}: {STEPS[s]}' for s in want if s == 'calendario' or (now - done.get(s, 0)) / 3600 >= hours]


def done_cmd(args):
    state = load_json(paths.STATE, {})
    if args[0].startswith('miles:'):
        prog = args[0].split(':', 1)[1]; b = (state.get('bonuses') or {}).get(prog) or {}
        state.setdefault('miles_searched', {})[prog] = f"{prog}:{miles.active_bonus(state.get('bonuses'), prog)}:{b.get('until')}"
    else: state.setdefault('steps', {})[args[0]] = int(time.time())
    save_json(paths.STATE, state); print(f'{args[0]}: feito')


# ---------- real fares for the whole party (Expedia, or typed in) ----------

def opts_from(args, *flags):
    out = {}
    for flag in flags:
        if flag in args: i = args.index(flag); out[flag[2:].replace('-', '_')] = args[i + 1]; del args[i:i + 2]
    return out


def real_cmd(args):
    """--real TRIP OUT RET PRICE CURRENCY [--source S] [--flight TEXT] [--bags TEXT]"""
    o = opts_from(args, '--source', '--flight', '--bags')
    source = o.get('source', 'manual')
    cfg = config(); trip = next(t for t in cfg['trips'] if t['id'] == args[0])
    price, cur = float(args[3]), args[4].upper()
    home = to_home(price, cur, cfg)
    if home is None: print('sem câmbio para converter: rode um tick (dólar) ou informe o preço em reais'); return
    ret = args[2] if args[2] not in ('', '-') else None
    old = [r for r in load_alerts() if r.get('source') == source and (r['out'], r['ret']) == (args[1], ret)]
    row = {'ts': dt.datetime.now().isoformat(timespec='minutes'), 'o': trip['origins'][0], 'd': trip['destinations'][0],
           'out': args[1], 'ret': ret, 'adults': trip.get('adults') or 1, 'children': trip.get('children') or 0,
           'price': round(home), 'was': max(old, key=lambda r: r['ts'])['price'] if old else 0, 'currency': cfg['currency'],
           'trend': '', 'flight': ' | '.join(x for x in (o.get('flight', ''), f"bagagem: {o['bags']}" if o.get('bags') else '') if x),
           'source': source, 'orig': f'{cur} {price:.2f}' if cur != cfg['currency'] else ''}
    if row['was']: row['trend'] = 'down' if row['price'] < row['was'] else 'up'
    _, lines = record_alerts([row], cfg)
    for l in lines: print(f'[{source}] {l}')
    if not lines: print(f"registrado: {args[1]}→{ret} {money(row['price'], cfg['currency'])} para {row['adults'] + row['children']} ({source})")


# ---------- hotels being watched ----------

def booking_of(state, trip_id, hotel_id):
    return next((b for b in ((state.get('trips') or {}).get(trip_id, {}).get('bookings') or []) if b.get('hotel') == hotel_id), None)


def hotels_cmd(only=None):
    cfg, state = config(), load_json(paths.STATE, {})
    for trip in cfg.get('trips') or []:
        if only and trip['id'] != only: continue
        for h in trip.get('hotels') or []:
            b, ages = booking_of(state, trip['id'], h['id']), trip.get('children_ages') or []
            print(f"{trip['id']} {h['id']}: {h['name']} {h['checkin']}→{h['checkout']}, {trip.get('adults', 1)} adultos"
                  + (f", crianças {ages}" if ages else '') + f" | fontes: {', '.join(h.get('sources') or ['expedia'])}"
                  + (f" | RESERVADO por {money(b['amount'], b['currency'])}, cancela grátis até {b.get('cancel_by') or '?'}" if b else ''))
    print('para cada preço: agent.py --hotel-price TRIP HOTEL PRECO_TOTAL MOEDA --source S; no fim: agent.py --done hoteis')


def hotel_price_cmd(args):
    """--hotel-price TRIP HOTEL TOTAL CURRENCY [--source S]: records a quote; says whether it beats the booking or the best seen."""
    source = opts_from(args, '--source').get('source', '')
    cfg, state = config(), load_json(paths.STATE, {})
    trip = next(t for t in cfg['trips'] if t['id'] == args[0]); h = next(x for x in trip['hotels'] if x['id'] == args[1])
    price, cur = float(args[2]), args[3].upper()
    home = to_home(price, cur, cfg)
    old = [json.loads(l) for l in open(paths.HOTEL_PRICES, encoding='utf-8') if l.strip()] if os.path.exists(paths.HOTEL_PRICES) else []
    mine = [r for r in old if (r['trip'], r['hotel'], r['checkin'], r['checkout']) == (trip['id'], h['id'], h['checkin'], h['checkout'])]
    paths.mkdirs()
    with open(paths.HOTEL_PRICES, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'ts': dt.datetime.now().isoformat(timespec='minutes'), 'trip': trip['id'], 'hotel': h['id'],
                            'checkin': h['checkin'], 'checkout': h['checkout'], 'price': price, 'currency': cur, 'home': home,
                            'source': source}, ensure_ascii=False) + '\n')
    drop, cur_home = cfg['min_drop_pct'] / 100, cfg['currency']
    nights = (dt.date.fromisoformat(h['checkout']) - dt.date.fromisoformat(h['checkin'])).days
    what = f"{h['name']} {h['checkin'][8:10]}/{h['checkin'][5:7]}→{h['checkout'][8:10]}/{h['checkout'][5:7]} ({nights} noites)"
    b = booking_of(state, trip['id'], h['id'])
    if b and home is not None:
        paid = to_home(b['amount'], b['currency'], cfg)
        can = not b.get('cancel_by') or b['cancel_by'] >= dt.date.today().isoformat()
        if paid and home <= paid * (1 - drop) and can:
            print(f"HOTEL CAIU: {what} {money(home, cur_home)} em {source or '?'}, reservado por {money(paid, cur_home)} (economia "
                  f"{money(paid - home, cur_home)}); cancelamento grátis até {b.get('cancel_by') or '?'}: reservar de novo e cancelar a antiga")
            return
    lows = [r['home'] for r in mine if r.get('home') is not None]
    if home is not None and lows and home <= min(lows) * (1 - drop):
        print(f"HOTEL: menor preço já visto para {what}: {money(home, cur_home)} em {source or '?'} (antes {money(min(lows), cur_home)})")
    else:
        print(f"registrado: {what} " + (money(home, cur_home) if home is not None else f'{cur} {price:,.0f}') + f" ({source or '?'})")


# ---------- bookings ----------

def booked_cmd(args):
    """--booked TRIP CATEGORY AMOUNT CURRENCY [--label T] [--ref R] [--cancel-by D] [--sender E] [--hotel ID] [--out D] [--ret D]"""
    o = opts_from(args, '--label', '--ref', '--cancel-by', '--sender', '--hotel', '--out', '--ret')
    state = load_json(paths.STATE, {})
    tstate = state.setdefault('trips', {}).setdefault(args[0], {})
    b = {'category': args[1], 'amount': float(args[2]), 'currency': args[3].upper(), 'label': o.get('label', ''), 'ref': o.get('ref', ''),
         'cancel_by': o.get('cancel_by'), 'sender': o.get('sender', ''), 'hotel': o.get('hotel'), 'out': o.get('out'),
         'ret': o.get('ret'), 'on': dt.date.today().isoformat()}
    same = lambda x: (x['category'], x.get('hotel'), x.get('label')) == (b['category'], b['hotel'], b['label'])
    tstate['bookings'] = [x for x in tstate.get('bookings') or [] if not same(x)] + [b]
    q = {'category': b['category'], 'amount': b['amount'], 'currency': b['currency'], 'label': f"RESERVADO: {b['label']}",
         'booked': True, 'seen': b['on']}
    if b['out'] and b['ret'] and b['category'] != 'voo':
        q['nights'] = (dt.date.fromisoformat(b['ret']) - dt.date.fromisoformat(b['out'])).days
    state.setdefault('quotes', {}).setdefault(args[0], {})[b['category']] = q
    save_json(paths.STATE, state)
    print(f"reserva registrada: {b['category']} {money(b['amount'], b['currency'])}" + (f", cancela grátis até {b['cancel_by']}" if b['cancel_by'] else ''))


# ---------- deadlines and calendar ----------

def timeline_cmd(args):
    cfg, state = config(), load_json(paths.STATE, {})
    if args[:1] == ['synced']:
        state.setdefault('trips', {}).setdefault(args[1], {}).setdefault('calendar', {})[args[2]] = args[3]
        save_json(paths.STATE, state); print('evento gravado'); return
    for trip in cfg.get('trips') or []:
        tstate = (state.get('trips') or {}).get(trip['id'], {})
        its = timeline.items(trip, tstate)
        if args[:1] == ['calendar']: its = timeline.unsynced(its, tstate)
        for i in its: print(f"{trip['id']} {i['id']} {i['date']}: {i['title']}" + (f" | {i['notes']}" if i['notes'] else ''))
    if args[:1] == ['calendar']:
        print('para cada um: evento de dia inteiro no calendário e agent.py --timeline synced TRIP ID EVENT_ID; no fim: agent.py --done calendario')


# ---------- trip page ----------

def dashboard_ctx(trip, cfg, state):
    tstate = (state.get('trips') or {}).get(trip['id'], {})
    past = past_trip_for(trip)
    quotes = dict((state.get('quotes') or {}).get(trip['id']) or {})
    rate, home = latest_fx(cfg), cfg['currency']
    plan = trip.get('plan') or {}
    reals = sorted([r for r in load_alerts() if (r['out'], r['ret']) == (plan.get('out'), plan.get('ret'))
                    and (r['adults'], r['children']) == (trip.get('adults') or 1, trip.get('children') or 0)], key=lambda r: r['ts'])
    flight = {}
    if reals:
        last = reals[-1]
        flight = {'price': last['price'], 'source': {'expedia': 'Expedia', 'manual': 'informado', 'google-page': 'Google Flights'}.get(last.get('source'), 'alerta do Google'),
                  'detail': f"Última leitura em {last['ts'][:10]}" + (f": {last['flight']}" if last.get('flight') else '')}
    elif 'voo' in quotes: flight = {'price': quotes['voo']['amount'], 'source': 'cotação', 'detail': quotes['voo'].get('label', '')}
    rows = {r['category']: r for r in compare.rows(past or {'items': []}, quotes)}
    costs, total = [], 0
    for cat, q in quotes.items():
        amt = q['amount'] if q['currency'] == home else (q['amount'] * rate if rate else None)
        total += amt or 0
        r = rows.get(cat, {})
        unit = '' if r.get('unit') in ('total', '', None) else f"/{r['unit']}"
        costs.append({'category': cat, 'label': q.get('label', ''), 'brl': amt, 'delta': r.get('delta_pct'),
                      'before': (dashboard.usd(r['before'], r['currency']) + unit) if r.get('before') is not None else ('?' if r.get('label_before') else 'não teve'),
                      'now': (dashboard.usd(r['now'], r['currency']) + unit) if r.get('now') is not None else dashboard.usd(q['amount'], q['currency']),
                      'status': 'reservado' if q.get('booked') else 'estimativa' if 'ESTIMATIVA' in q.get('label', '').upper() else 'cotado'})
    costs.sort(key=lambda c: -(c['brl'] or 0))
    past_total = None
    if past:
        prate = past.get('fx_rate') or rate or 0
        past_total = sum(i['amount'] if i['currency'] == home else i['amount'] * prate for i in past['items'] if i.get('amount'))
    upcoming = [i for i in timeline.items(trip, tstate) if i['date'] >= dt.date.today().isoformat()]
    f = cfg.get('fx') or {}
    note = ''
    if rate and f.get('budget'): note = f"{f.get('currency', 'USD')} {f['budget']:,.0f}".replace(',', '.') + f" de gastos = {money(f['budget'] * rate, home)}"
    return {'trip': trip, 'flight': flight, 'total': total, 'past_total': past_total, 'fx': rate, 'fx_note': note,
            'next_deadline': (f"{upcoming[0]['date'][8:10]}/{upcoming[0]['date'][5:7]}", upcoming[0]['title']) if upcoming else None,
            'costs': costs, 'deadlines': upcoming, 'price_points': [(r['ts'], r['price']) for r in reals],
            'updated': dt.datetime.now().strftime('%d/%m/%Y %H:%M')}


def dashboard_cmd(trip_id):
    cfg, state = config(), load_json(paths.STATE, {})
    trip = next(t for t in cfg['trips'] if t['id'] == trip_id)
    os.makedirs(paths.DASHBOARD_DIR, exist_ok=True)
    out = os.path.join(paths.DASHBOARD_DIR, f'{trip_id}.html')
    with open(out, 'w', encoding='utf-8') as fh: fh.write(dashboard.render(dashboard_ctx(trip, cfg, state)))
    print(out)
    url = ((cfg.get('dashboard') or {}).get('urls') or {}).get(trip_id)
    print(f'publicar: Artifact com este arquivo' + (f' e url={url}' if url else ' (primeira vez: guardar a url em dashboard.urls no config)') + '; depois agent.py --done painel')


# ---------- Google Sheets ----------

def sheet_due(cfg, state, now=None):
    s = cfg.get('sheet') or {}
    if not s.get('id') or not os.path.exists(paths.GOOGLE_KEY): return False
    last = (state.get('sheet') or {}).get('pushed', 0)
    return ((now or time.time()) - last) / 3600 >= s.get('every_hours', 6)


def sheet_tables(cfg, state):
    trips = [t for t in cfg.get('trips') or [] if t.get('status', 'watching') in ('watching', 'booked')]
    ctxs = {t['id']: dashboard_ctx(t, cfg, state) for t in trips}
    jl = lambda p: [json.loads(l) for l in open(p, encoding='utf-8') if l.strip()] if os.path.exists(p) else []
    items = [(t, i) for t in trips for i in timeline.items(t, (state.get('trips') or {}).get(t['id'], {}))]
    return sheets.tables(cfg, state, ctxs, load_alerts(), jl(paths.HOTEL_PRICES), jl(paths.FX), items, dashboard.cat_name)


def sheet_push(cfg, state):
    n = sheets.push(cfg['sheet']['id'], paths.GOOGLE_KEY, sheet_tables(cfg, state), state.setdefault('sheet', {}))
    state['sheet']['pushed'] = int(time.time())
    return n


def sheet_cmd():
    cfg, state = config(), load_json(paths.STATE, {})
    if not (cfg.get('sheet') or {}).get('id'): print('sem sheet.id no config.json'); return
    n = sheet_push(cfg, state); save_json(paths.STATE, state)
    print(f"planilha atualizada ({n} abas): https://docs.google.com/spreadsheets/d/{cfg['sheet']['id']}")


# ---------- Planou cards ----------

def jsonl(path):
    if not os.path.exists(path): return []
    with open(path, encoding='utf-8') as f: return [json.loads(l) for l in f if l.strip()]


def plan_cards_cmd():
    """--plan-cards: {code: item} of every trip plan as JSON (cards.py), without searching or saving anything."""
    cfg, state = config(), load_json(paths.STATE, {})
    items = cards.build(cfg, state, load_alerts(), jsonl(paths.HOTEL_PRICES), jsonl(paths.PRICES),
                        lambda amount, cur: to_home(amount, cur, cfg), money)
    print(json.dumps(items, ensure_ascii=False))


# ---------- report ----------

def report(only=None):
    cfg = config()
    obs = [json.loads(l) for l in open(paths.PRICES, encoding='utf-8')] if os.path.exists(paths.PRICES) else []
    for trip in cfg.get('trips') or []:
        if only and trip['id'] != only: continue
        mine = [o for o in obs if o['trip'] == trip['id']]
        print(header(trip) + f" [{trip.get('status', 'watching')}]")
        if not mine: print('  sem observações ainda'); continue
        last = max(o['ts'] for o in mine)
        now = [o for o in mine if o['ts'] == last]
        outs = sorted({o['out'] for o in now}); rets = sorted({o['ret'] or '' for o in now})
        pax = travellers(trip)
        print(f"  grade de {last[:16].replace('T', ' ')} (total de {pax:g} pessoas, em mil; * = mais conexões sai mais barato):")
        print('  ida \\ volta ' + ' '.join(f'{r[8:10]}/{r[5:7]}'.rjust(9) for r in rets))
        for a in outs:
            row = []
            for r in rets:
                o = next((x for x in now if x['out'] == a and (x['ret'] or '') == r), None)
                if not o or not o['ok']: row.append('-'.rjust(9)); continue
                star = '*' if o['extra'] and o['extra']['price'] < o['ok']['price'] else ' '
                row.append(f"{o['ok']['price'] * pax / 1000:>8.1f}{star}")
            print(f'  {a[8:10]}/{a[5:7]}        ' + ' '.join(row))
        pax, ticks = travellers(trip), sorted({o['ts'] for o in mine})
        best = lambda group: min((o['ok']['price'] for o in group if o['ok']), default=None)
        first, low = best([o for o in mine if o['ts'] == ticks[0]]), best(mine)
        cur = best(now)
        lo_obs = min((o for o in mine if o['ok']), key=lambda o: o['ok']['price'], default=None)
        if cur is not None and lo_obs:
            print(f"  agora: {money(cur * pax, cfg['currency'])} | menor já visto: {money(low * pax, cfg['currency'])} em "
                  f"{lo_obs['ts'][:10]} ({lo_obs['out']}→{lo_obs['ret']}) | primeira leitura: {money(first * pax, cfg['currency'])} | {len(ticks)} leituras")
        ref = (trip.get('reference') or {})
        if ref.get('total'): print(f"  referência: {money(ref['total'], cfg['currency'])} ({ref.get('label', '')})")
        for l in alerts_summary(trip, cfg): print(l)


# ---------- Gmail promotions step ----------

def gmail(args):
    state = load_json(paths.STATE, {})
    if args[:1] == ['mark']:
        state['gmail_cursor'] = int(args[1]); save_json(paths.STATE, state); print('cursor gravado'); return
    cfg = config(); g = cfg.get('gmail') or {}
    cursor = state.get('gmail_cursor') or int(time.time()) - 3 * 86400
    senders = g.get('senders') or []
    print(f'cursor: {cursor} ({dt.datetime.fromtimestamp(cursor):%d/%m %H:%M})')
    print(f"query: from:({' OR '.join(senders)}) after:{cursor}" if senders else 'query: (sem remetentes em gmail.senders)')
    words = sorted({w for t in cfg.get('trips') or [] if t.get('status', 'watching') == 'watching' for w in t.get('keywords') or []})
    print('destinos: ' + (', '.join(words) or '-'))
    print('programas: ' + (', '.join((cfg.get('points') or {}).get('programs') or {}) or '-'))
    print(f'depois: agent.py --gmail mark {int(time.time())}')


# ---------- points ----------

def points(args):
    """Award ticket (miles + taxes) against paying cash, valuing the points by what they would give back as cash."""
    def opt(name, default=None):
        if name in args: i = args.index(name); v = args[i + 1]; del args[i:i + 2]; return v
        return default
    bonus = float(opt('--bonus', 0)) / 100
    cfg = config(); p = cfg.get('points') or {}
    program = opt('--program') or next(iter(p.get('programs') or {}), '')
    miles, taxes, cash = (float(x) for x in args[:3])
    balance, value = p.get('balance') or 0, p.get('cash_value') or 0
    ratio = ((p.get('programs') or {}).get(program) or {}).get('ratio', 1.0)
    cur = cfg['currency']
    if not balance or not value: print('faltam points.balance e points.cash_value no config.json'); return
    per_point = value / balance
    needed = miles / (ratio * (1 + bonus))
    cost = needed * per_point + taxes
    print(f'seus pontos valem {money(per_point * 1000, cur)} por mil em dinheiro; {program} {ratio:g}:1'
          + (f' com bônus de {bonus * 100:.0f}%' if bonus else ''))
    print(f'resgate: {miles:,.0f} milhas = {needed:,.0f} pontos ({money(needed * per_point, cur)} de pontos) + {money(taxes, cur)} de taxas'
          .replace(',', '.'))
    print(f"custo real: {money(cost, cur)} contra {money(cash, cur)} em dinheiro -> "
          + ('MILHAS COMPENSAM' if cost < cash else 'DINHEIRO COMPENSA') + f" ({money(abs(cash - cost), cur)} de diferença)")
    if needed > balance: print(f'faltam {needed - balance:,.0f} pontos'.replace(',', '.'))


def main(argv):
    a = list(argv)
    if '--report' in a:
        i = a.index('--report'); report(a[i + 1] if len(a) > i + 1 else None); return 0
    if '--gmail' in a: gmail(a[a.index('--gmail') + 1:]); return 0
    if '--gflights' in a: gflights_cmd(a[a.index('--gflights') + 1:]); return 0
    for flag, fn in (('--real', real_cmd), ('--hotel-price', hotel_price_cmd), ('--booked', booked_cmd), ('--timeline', timeline_cmd),
                     ('--done', done_cmd)):
        if flag in a: fn(a[a.index(flag) + 1:]); return 0
    if '--hotels' in a:
        i = a.index('--hotels'); hotels_cmd(a[i + 1] if len(a) > i + 1 else None); return 0
    if '--dashboard' in a: dashboard_cmd(a[a.index('--dashboard') + 1]); return 0
    if '--sheet' in a: sheet_cmd(); return 0
    if '--plan-cards' in a: plan_cards_cmd(); return 0
    if '--points' in a: points(a[a.index('--points') + 1:]); return 0
    if '--bonus' in a: bonus_cmd(a[a.index('--bonus') + 1:]); return 0
    if '--quote' in a: quote_cmd(a[a.index('--quote') + 1:]); return 0
    if '--compare' in a:
        i = a.index('--compare'); compare_cmd(a[i + 1] if len(a) > i + 1 else None); return 0
    if '--award' in a: award_cmd(a[a.index('--award') + 1:]); return 0
    if '--miles' in a:
        i = a.index('--miles'); miles_cmd(a[i + 1] if len(a) > i + 1 else None); return 0
    only = a[a.index('--trip') + 1] if '--trip' in a else None
    return tick(only, save='--dry' not in a)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
