#!/usr/bin/env python3
"""The trip spreadsheet in Google Sheets, written by the agent itself (no model, no connector) through a Google service account.

Setup (once, see SKILL.md): a Google Cloud project with the Sheets API on, a service account and its JSON key in
~/.config/travel-agent/secrets/google-service-account.json, and a spreadsheet the user owns shared with the service account
as editor (`sheet.id` in config.json). google-auth and requests live in the venv; they are imported only in `push`.

`tables()` is pure: config + state + observations -> {tab: rows}. The agent owns those tabs and rewrites them whole on every
push; the user's own tabs (any other name) are never touched. The first push also adds a line chart of the real fare.
"""
import datetime as dt

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
API = 'https://sheets.googleapis.com/v4/spreadsheets/'
SUMMARY, FARES, FARE_LOG, HOTELS, FX, DEADLINES = 'Resumo', 'Voo', 'Voo (leituras)', 'Hotéis', 'Dólar', 'Prazos'
SOURCES = {'expedia': 'Expedia', 'manual': 'informado', 'google-page': 'Google Flights'}


def plan_tab(trip):
    return f"Plano: {trip.get('name', trip['id'])}"[:90]


def tables(cfg, state, ctxs, alerts, hotel_rows, fx_rows, deadline_items, names):
    """ctxs: {trip_id: dashboard_ctx}; alerts: real-fare rows; hotel_rows/fx_rows: the jsonl logs; deadline_items: [(trip, item)]."""
    trips = [t for t in cfg.get('trips') or [] if t['id'] in ctxs]
    now = dt.datetime.now().strftime('%d/%m/%Y %H:%M')
    out = {}
    summary = [['Plano', 'Datas', 'Noites', 'Voo agora (para todos)', 'Total estimado', 'Viagem anterior', 'Diferença', 'Atualizado']]
    for t in trips:
        c, plan = ctxs[t['id']], t.get('plan') or {}
        nights = (dt.date.fromisoformat(plan['ret']) - dt.date.fromisoformat(plan['out'])).days if plan.get('out') and plan.get('ret') else ''
        diff = round(c['total'] - c['past_total']) if c.get('past_total') else ''
        summary.append([t.get('name', t['id']), plan.get('label', ''), nights, (c.get('flight') or {}).get('price', ''),
                        round(c['total']), round(c['past_total']) if c.get('past_total') else '', diff, now])
    out[SUMMARY] = summary
    for t in trips:
        c = ctxs[t['id']]
        rows = [['Roteiro'], ['Dia', 'Programa', 'Hotel']]
        for i in t.get('itinerary') or []:
            rows.append([i['date'], i['text'], i.get('where', '')])
        rows += [[], ['Custos (para todos)'], ['Item', 'Detalhe', 'Viagem anterior', 'Agora', 'Diferença %', 'Em reais', 'Situação']]
        for x in c['costs']:
            rows.append([names(x['category']), x['label'], x['before'], x['now'], round(x['delta']) if x.get('delta') is not None else '',
                         round(x['brl']) if x.get('brl') is not None else '', x['status']])
        rows.append(['Total', '', '', '', '', round(c['total']), ''])
        if c.get('fx'): rows += [[], [f"Itens em dólar convertidos a R$ {c['fx']:.4f}".replace('.', ',')]]
        out[plan_tab(t)] = rows
    # real fare of each trip's plan, one column per trip, one row per day (last reading of the day)
    pairs = [(t, (t.get('plan') or {}).get('out'), (t.get('plan') or {}).get('ret')) for t in trips]
    by_day = {}
    for r in alerts:
        for k, (t, o, rt) in enumerate(pairs):
            if (r['out'], r['ret']) == (o, rt) and (r['adults'], r['children']) == (t.get('adults') or 1, t.get('children') or 0):
                by_day.setdefault(r['ts'][:10], {})[k] = r['price']
    fares = [['Data'] + [f"{t.get('name', t['id'])} ({(t.get('plan') or {}).get('label', '')})" for t, _, _ in pairs]
             + ['Meta', 'Viagem anterior']]
    ref = next(((t.get('reference') or {}).get('total') for t in trips if (t.get('reference') or {}).get('total')), '')
    target = next((t.get('target_total') for t in trips if t.get('target_total')), '')
    for day in sorted(by_day):
        fares.append([day] + [by_day[day].get(k, '') for k in range(len(pairs))] + [target, ref])
    out[FARES] = fares
    log = [['Quando', 'Ida', 'Volta', 'Pessoas', 'Total', 'Antes', 'Fonte', 'Voo']]
    for r in sorted(alerts, key=lambda r: r['ts'], reverse=True):
        if r['out'] < dt.date.today().isoformat(): continue   # past trips (old alert history)
        log.append([r['ts'].replace('T', ' '), r['out'], r['ret'] or '', r['adults'] + r['children'], r['price'], r.get('was') or '',
                    SOURCES.get(r.get('source'), 'alerta do Google'), r.get('flight', '')])
    out[FARE_LOG] = log
    hotels = [['Quando', 'Viagem', 'Hotel', 'Entrada', 'Saída', 'Total', 'Moeda', 'Em reais', 'Fonte']]
    hnames = {(t['id'], h['id']): h['name'] for t in cfg.get('trips') or [] for h in t.get('hotels') or []}
    for r in sorted(hotel_rows, key=lambda r: r['ts'], reverse=True):
        hotels.append([r['ts'].replace('T', ' '), r['trip'], hnames.get((r['trip'], r['hotel']), r['hotel']), r['checkin'], r['checkout'],
                       r['price'], r['currency'], round(r['home']) if r.get('home') is not None else '', r.get('source', '')])
    out[HOTELS] = hotels
    out[FX] = [['Data', 'Cotação']] + [[r['date'], r['rate']] for r in fx_rows]
    dl = [['Data', 'Viagem', 'Prazo', 'Notas', 'No calendário']]
    for trip, it in sorted(deadline_items, key=lambda x: x[1]['date']):
        dl.append([it['date'], trip['id'], it['title'], it.get('notes', ''), 'sim' if it['id'] in ((state.get('trips') or {}).get(trip['id'], {}).get('calendar') or {}) else ''])
    out[DEADLINES] = dl
    return out


# ---------- look ----------

ACCENT, ACCENT_SOFT, INK, MUTED, BAND, LINE = '0F766E', 'D9F0EC', '17212B', '5B6B7A', 'F3F6F8', 'DDE3E9'
GOOD, GOOD_SOFT, WARN, WARN_SOFT, BAD, BAD_SOFT = '15803D', 'DCFCE7', 'B45309', 'FEF3C7', 'B91C1C', 'FEE2E2'
SERIES = ['0F766E', '7C3AED', '2563EB', 'D97706', '15803D', 'B91C1C']
TAB_COLOR = {SUMMARY: ACCENT, FARES: '2563EB', FARE_LOG: '94A3B8', HOTELS: '94A3B8', FX: '94A3B8', DEADLINES: 'D97706'}
MONEY = {'type': 'CURRENCY', 'pattern': '"R$" #,##0'}
PCT = {'type': 'NUMBER', 'pattern': '+0"%";-0"%";0"%"'}
DAY = {'type': 'DATE', 'pattern': 'ddd dd/mm'}
DATE = {'type': 'DATE', 'pattern': 'dd/mm/yyyy'}
STAMP = {'type': 'DATE_TIME', 'pattern': 'dd/mm/yyyy hh:mm'}
# per tab: {column: number format}, column widths in pixels
LAYOUT = {
    SUMMARY: ({2: {'type': 'NUMBER', 'pattern': '0'}, 3: MONEY, 4: MONEY, 5: MONEY, 6: MONEY}, [230, 90, 70, 170, 140, 140, 120, 140]),
    FARES: ({0: DATE}, [100, 260, 260, 110, 130]),
    FARE_LOG: ({0: STAMP, 1: DATE, 2: DATE, 4: MONEY, 5: MONEY}, [140, 95, 95, 70, 110, 110, 120, 460]),
    HOTELS: ({0: STAMP, 3: DATE, 4: DATE, 5: {'type': 'NUMBER', 'pattern': '#,##0'}, 7: MONEY}, [140, 120, 330, 95, 95, 90, 70, 110, 90]),
    FX: ({0: DATE, 1: {'type': 'NUMBER', 'pattern': '0.0000'}}, [100, 90]),
    DEADLINES: ({0: {'type': 'DATE', 'pattern': 'ddd dd/mm/yyyy'}}, [130, 150, 520, 380, 110]),
}
PLAN_WIDTHS = [150, 520, 150, 130, 100, 120, 110]


def rgb(hexs):
    return {'red': int(hexs[0:2], 16) / 255, 'green': int(hexs[2:4], 16) / 255, 'blue': int(hexs[4:6], 16) / 255}


def cells(sid, fmt, fields, r0=None, r1=None, c0=None, c1=None):
    rng = {'sheetId': sid}
    for k, v in (('startRowIndex', r0), ('endRowIndex', r1), ('startColumnIndex', c0), ('endColumnIndex', c1)):
        if v is not None: rng[k] = v
    return {'repeatCell': {'range': rng, 'cell': {'userEnteredFormat': fmt}, 'fields': fields}}


def header(sid, row, ncols):
    return cells(sid, {'backgroundColor': rgb(ACCENT), 'verticalAlignment': 'MIDDLE', 'wrapStrategy': 'CLIP',
                       'textFormat': {'bold': True, 'foregroundColor': rgb('FFFFFF'), 'fontSize': 10}},
                 'userEnteredFormat(backgroundColor,verticalAlignment,wrapStrategy,textFormat)', row, row + 1, 0, ncols)


def widths(sid, px):
    return [{'updateDimensionProperties': {'range': {'sheetId': sid, 'dimension': 'COLUMNS', 'startIndex': i, 'endIndex': i + 1},
                                           'properties': {'pixelSize': w}, 'fields': 'pixelSize'}} for i, w in enumerate(px)]


def height(sid, row, px):
    return {'updateDimensionProperties': {'range': {'sheetId': sid, 'dimension': 'ROWS', 'startIndex': row, 'endIndex': row + 1},
                                          'properties': {'pixelSize': px}, 'fields': 'pixelSize'}}


def status_rules(sid, col, r0=0):
    rng = [{'sheetId': sid, 'startRowIndex': r0, 'startColumnIndex': col, 'endColumnIndex': col + 1}]
    rule = lambda text, fg, bg: {'addConditionalFormatRule': {'index': 0, 'rule': {'ranges': rng, 'booleanRule': {
        'condition': {'type': 'TEXT_EQ', 'values': [{'userEnteredValue': text}]},
        'format': {'backgroundColor': rgb(bg), 'textFormat': {'foregroundColor': rgb(fg), 'bold': True}}}}}}
    return [rule('reservado', GOOD, GOOD_SOFT), rule('cotado', ACCENT, ACCENT_SOFT), rule('estimativa', WARN, WARN_SOFT)]


def delta_rules(sid, col, limit, r0=1):
    rng = [{'sheetId': sid, 'startRowIndex': r0, 'startColumnIndex': col, 'endColumnIndex': col + 1}]
    rule = lambda kind, v, fg: {'addConditionalFormatRule': {'index': 0, 'rule': {'ranges': rng, 'booleanRule': {
        'condition': {'type': kind, 'values': [{'userEnteredValue': str(v)}]}, 'format': {'textFormat': {'foregroundColor': rgb(fg), 'bold': True}}}}}}
    return [rule('NUMBER_GREATER', limit, BAD), rule('NUMBER_LESS', -limit, GOOD)]


def style(tabs, have, sstate):
    """Batch requests that give every agent tab its look. Cell formats are reset and reapplied on every push (rows move when
    the data changes); banding, conditional colors, tab colors and the locale are added once (marked in sstate)."""
    req, once = [], sstate.setdefault('styled', [])
    if 'locale' not in once:
        req.append({'updateSpreadsheetProperties': {'properties': {'locale': 'pt_BR', 'timeZone': 'America/Sao_Paulo',
                    'defaultFormat': {'textFormat': {'fontFamily': 'Roboto', 'fontSize': 10, 'foregroundColor': rgb(INK)}}},
                    'fields': 'locale,timeZone,defaultFormat.textFormat'}})
        once.append('locale')
    for t, rows in tabs.items():
        sid, plan = have[t], t.startswith('Plano: ')
        ncols = max((len(r) for r in rows), default=1)
        req.append(cells(sid, {}, 'userEnteredFormat'))                                  # reset
        req.append(cells(sid, {'verticalAlignment': 'MIDDLE', 'textFormat': {'foregroundColor': rgb(INK), 'fontSize': 10}},
                         'userEnteredFormat(verticalAlignment,textFormat)'))
        if plan:
            req += widths(sid, PLAN_WIDTHS)
            for i, r in enumerate(rows):
                if len(r) == 1 and r[0] in ('Roteiro', 'Custos (para todos)'):
                    req.append(cells(sid, {'textFormat': {'bold': True, 'fontSize': 13, 'foregroundColor': rgb(ACCENT)}},
                                     'userEnteredFormat.textFormat', i, i + 1, 0, 1))
                    req.append(height(sid, i, 34))
                    req.append(header(sid, i + 1, len(rows[i + 1])))
                elif r and r[0] == 'Total':
                    req.append(cells(sid, {'textFormat': {'bold': True, 'fontSize': 11}, 'backgroundColor': rgb(ACCENT_SOFT),
                                           'borders': {'top': {'style': 'SOLID_MEDIUM', 'colorStyle': {'rgbColor': rgb(ACCENT)}}}},
                                     'userEnteredFormat(textFormat,backgroundColor,borders)', i, i + 1, 0, 7))
                elif r and str(r[0]).startswith('Itens em dólar'):
                    req.append(cells(sid, {'textFormat': {'italic': True, 'foregroundColor': rgb(MUTED)}}, 'userEnteredFormat.textFormat', i, i + 1, 0, 1))
            costs = next((i for i, r in enumerate(rows) if r == ['Custos (para todos)']), None)
            first_day = 2
            last_day = (costs - 1) if costs else len(rows)
            req.append(cells(sid, {'numberFormat': DAY, 'horizontalAlignment': 'LEFT'}, 'userEnteredFormat(numberFormat,horizontalAlignment)', first_day, last_day, 0, 1))
            req.append(cells(sid, {'wrapStrategy': 'WRAP'}, 'userEnteredFormat.wrapStrategy', first_day, last_day, 1, 2))
            if costs:
                req.append(cells(sid, {'numberFormat': MONEY}, 'userEnteredFormat.numberFormat', costs + 2, None, 5, 6))
                req.append(cells(sid, {'numberFormat': PCT, 'horizontalAlignment': 'RIGHT'}, 'userEnteredFormat(numberFormat,horizontalAlignment)', costs + 2, None, 4, 5))
                req.append(cells(sid, {'textFormat': {'foregroundColor': rgb(MUTED), 'fontSize': 9}}, 'userEnteredFormat.textFormat', costs + 2, None, 1, 2))
                req.append(cells(sid, {'horizontalAlignment': 'RIGHT'}, 'userEnteredFormat.horizontalAlignment', costs + 2, None, 2, 4))
                req.append(cells(sid, {'horizontalAlignment': 'CENTER'}, 'userEnteredFormat.horizontalAlignment', costs + 2, None, 6, 7))
                if f'rules:{t}' not in once:
                    req += status_rules(sid, 6, costs + 2) + delta_rules(sid, 4, 5, costs + 2)
                    once.append(f'rules:{t}')
        else:
            fmts, px = LAYOUT.get(t, ({}, []))
            req += widths(sid, px)
            req.append(header(sid, 0, ncols))
            req.append(height(sid, 0, 32))
            for col, nf in fmts.items():
                req.append(cells(sid, {'numberFormat': nf}, 'userEnteredFormat.numberFormat', 1, None, col, col + 1))
            if t == SUMMARY:
                req.append(cells(sid, {'textFormat': {'bold': True, 'fontSize': 11}}, 'userEnteredFormat.textFormat', 1, len(rows), 0, 1))
                req.append(cells(sid, {'textFormat': {'bold': True, 'fontSize': 12, 'foregroundColor': rgb(ACCENT)}}, 'userEnteredFormat.textFormat', 1, len(rows), 4, 5))
                for i in range(1, len(rows)): req.append(height(sid, i, 30))
                if f'rules:{t}' not in once:
                    rng = [{'sheetId': sid, 'startRowIndex': 1, 'startColumnIndex': 6, 'endColumnIndex': 7}]
                    req.append({'addConditionalFormatRule': {'index': 0, 'rule': {'ranges': rng, 'gradientRule': {
                        'minpoint': {'color': rgb(GOOD_SOFT), 'type': 'MIN'}, 'maxpoint': {'color': rgb(BAD_SOFT), 'type': 'MAX'}}}}})
                    once.append(f'rules:{t}')
            if t == DEADLINES and f'rules:{t}' not in once:
                rng = [{'sheetId': sid, 'startRowIndex': 1, 'startColumnIndex': 0, 'endColumnIndex': 5}]
                req.append({'addConditionalFormatRule': {'index': 0, 'rule': {'ranges': rng, 'booleanRule': {
                    'condition': {'type': 'CUSTOM_FORMULA', 'values': [{'userEnteredValue': '=AND($A2<>"";$A2-TODAY()<=14)'}]},
                    'format': {'backgroundColor': rgb(WARN_SOFT), 'textFormat': {'bold': True}}}}}})
                once.append(f'rules:{t}')
            if t not in (SUMMARY,) and f'band:{t}' not in once:
                req.append({'addBanding': {'bandedRange': {'range': {'sheetId': sid, 'startRowIndex': 1, 'startColumnIndex': 0, 'endColumnIndex': ncols},
                            'rowProperties': {'firstBandColor': rgb('FFFFFF'), 'secondBandColor': rgb(BAND)}}}})
                once.append(f'band:{t}')
        if f'props:{t}' not in once:
            props = {'sheetId': sid, 'gridProperties': {'hideGridlines': True, 'frozenRowCount': 0 if plan else 1},
                     'tabColorStyle': {'rgbColor': rgb(TAB_COLOR.get(t, ACCENT_SOFT if plan else ACCENT))}}
            req.append({'updateSheetProperties': {'properties': props, 'fields': 'gridProperties.hideGridlines,gridProperties.frozenRowCount,tabColorStyle'}})
            once.append(f'props:{t}')
    return req


def push(spreadsheet_id, key_file, tabs, sstate):
    """Writes every tab (creating the missing ones), formats headers once and adds the fare chart once. Returns the tab count."""
    from google.oauth2 import service_account
    from google.auth.transport.requests import AuthorizedSession
    s = AuthorizedSession(service_account.Credentials.from_service_account_file(key_file, scopes=SCOPES))
    base = API + spreadsheet_id

    def ok(r):
        if r.status_code >= 300: raise RuntimeError(f'Sheets API {r.status_code}: {r.text[:200]}')
        return r.json() if r.text else {}

    meta = ok(s.get(base, params={'fields': 'sheets(properties,charts.chartId)'}))
    have = {x['properties']['title']: x['properties']['sheetId'] for x in meta.get('sheets', [])}
    if sstate.get('chart') and not sstate.get('chart_v2'):                     # chart made before the colors: redo it once
        old = [c['chartId'] for x in meta.get('sheets', []) if x['properties']['title'] == FARES for c in x.get('charts', [])]
        if old: ok(s.post(base + ':batchUpdate', json={'requests': [{'deleteEmbeddedObject': {'objectId': c}} for c in old]}))
        sstate['chart'] = False
    sstate['chart_v2'] = True
    add = [{'addSheet': {'properties': {'title': t}}} for t in tabs if t not in have]
    if add:
        rep = ok(s.post(base + ':batchUpdate', json={'requests': add}))
        for r in rep.get('replies', []):
            p = r['addSheet']['properties']; have[p['title']] = p['sheetId']
    stale = [sid for title, sid in have.items() if title.startswith('Plano: ') and title not in tabs]   # renamed or removed plans
    if stale:
        ok(s.post(base + ':batchUpdate', json={'requests': [{'deleteSheet': {'sheetId': sid}} for sid in stale]}))
        have = {k: v for k, v in have.items() if v not in stale}
    if 'Sheet1' in have and len(have) > 1 and not sstate.get('removed_default'):
        ok(s.post(base + ':batchUpdate', json={'requests': [{'deleteSheet': {'sheetId': have.pop('Sheet1')}}]}))
        sstate['removed_default'] = True
    ok(s.post(base + '/values:batchClear', json={'ranges': [f"'{t}'" for t in tabs]}))
    ok(s.post(base + '/values:batchUpdate', json={'valueInputOption': 'USER_ENTERED',
                                                   'data': [{'range': f"'{t}'!A1", 'values': rows} for t, rows in tabs.items()]}))
    fmt = style(tabs, have, sstate)
    if not sstate.get('chart') and FARES in tabs and len(tabs[FARES]) > 1:
        sid, ncols = have[FARES], len(tabs[FARES][0])
        series = [{'series': {'sourceRange': {'sources': [{'sheetId': sid, 'startRowIndex': 0, 'endRowIndex': 1000,
                                                            'startColumnIndex': c, 'endColumnIndex': c + 1}]}}, 'targetAxis': 'LEFT_AXIS'}
                  for c in range(1, ncols)]
        for i, sr in enumerate(series):
            sr['colorStyle'] = {'rgbColor': rgb(SERIES[i % len(SERIES)])}
            if i >= ncols - 3: sr['lineStyle'] = {'type': 'MEDIUM_DASHED'}   # target and reference
        fmt.append({'addChart': {'chart': {'spec': {'title': 'Preço real do voo (para todos)', 'basicChart': {
            'chartType': 'LINE', 'legendPosition': 'BOTTOM_LEGEND', 'headerCount': 1,
            'axis': [{'position': 'BOTTOM_AXIS', 'title': 'Data'}, {'position': 'LEFT_AXIS', 'title': 'R$'}],
            'domains': [{'domain': {'sourceRange': {'sources': [{'sheetId': sid, 'startRowIndex': 0, 'endRowIndex': 1000,
                                                                  'startColumnIndex': 0, 'endColumnIndex': 1}]}}}],
            'series': series}},
            'position': {'overlayPosition': {'anchorCell': {'sheetId': sid, 'rowIndex': 1, 'columnIndex': ncols + 1}}}}}})
        sstate['chart'] = True
    if fmt: ok(s.post(base + ':batchUpdate', json={'requests': fmt}))
    return len(tabs)
