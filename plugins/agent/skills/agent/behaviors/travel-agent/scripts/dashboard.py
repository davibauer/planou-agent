#!/usr/bin/env python3
"""The trip page: itinerary, every cost against the last trip to the same destination, the real flight price over time and
the deadlines. `render(ctx)` is pure (ctx built by agent.py --dashboard); the model publishes the file as an Artifact.
"""
import html, datetime as dt

MONTHS = ['jan', 'fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez']
WEEK = ['seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom']
E = html.escape
NAMES = {'voo': 'Voo', 'hotel': 'Hotel', 'hotel-disney': 'Hotel Disney', 'hotel-universal': 'Hotel Universal',
         'parques': 'Parques', 'parques-disney': 'Ingressos Disney', 'parques-universal': 'Ingressos Universal',
         'restaurantes': 'Comida', 'compras': 'Compras', 'fotos': 'Fotos', 'traslado': 'Traslados', 'carrinho': 'Carrinho',
         'seguro': 'Seguro', 'outros': 'Outros', 'carro': 'Carro'}


def cat_name(c):
    return NAMES.get(c, c.replace('-', ' ').capitalize())

CSS = """
:root{--bg:#f3f5f7;--surface:#ffffff;--ink:#17212b;--muted:#5b6b7a;--line:#dde3e9;--accent:#0f766e;--accent-soft:#d9f0ec;
--good:#15803d;--warn:#b45309;--bad:#b91c1c;--chip:#eef2f5}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#0e1419;--surface:#161f27;--ink:#e6edf3;
--muted:#93a4b3;--line:#26323d;--accent:#3cc3ad;--accent-soft:#16332f;--good:#4ade80;--warn:#f59e0b;--bad:#f87171;--chip:#1f2a34}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#0e1419;--surface:#161f27;--ink:#e6edf3;--muted:#93a4b3;--line:#26323d;
--accent:#3cc3ad;--accent-soft:#16332f;--good:#4ade80;--warn:#f59e0b;--bad:#f87171;--chip:#1f2a34}
body{background:var(--bg);color:var(--ink);font:15px/1.55 'Source Sans 3',system-ui,sans-serif;padding-inline:16px;padding-block:24px 40px}
.wrap{max-width:980px;margin:0 auto;display:grid;gap:28px}
h1,h2{font-family:'Barlow Condensed','Arial Narrow',sans-serif;font-weight:600;letter-spacing:.01em;text-wrap:balance;margin:0}
h1{font-size:clamp(34px,6vw,52px);line-height:1}
h2{font-size:22px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.mono,td.num,.num{font-family:'IBM Plex Mono',ui-monospace,monospace;font-variant-numeric:tabular-nums}
.route{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px 18px}
.iata{font-family:'Barlow Condensed',sans-serif;font-size:clamp(44px,9vw,72px);font-weight:700;letter-spacing:.04em;line-height:.9}
.arrow{color:var(--accent);font-size:32px}
.sub{color:var(--muted)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;display:grid;gap:4px}
.tile .k{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.tile .v{font-size:26px;font-weight:600}
.pill{display:inline-block;font-size:12px;padding:2px 8px;border-radius:999px;background:var(--chip);color:var(--muted);white-space:nowrap}
.pill.good{color:var(--good)}.pill.warn{color:var(--warn)}.pill.bad{color:var(--bad)}.pill.acc{background:var(--accent-soft);color:var(--accent)}
.days{display:grid;gap:0;border-top:1px solid var(--line)}
.day{display:grid;grid-template-columns:92px 1fr auto;gap:12px;padding:10px 0;border-bottom:1px solid var(--line);align-items:baseline}
.day .d{font-family:'IBM Plex Mono',monospace;font-size:13px;color:var(--muted)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:560px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
td.num,th.num{text-align:right}
tr.total td{font-weight:700;border-top:2px solid var(--ink)}
.note{font-size:13px;color:var(--muted)}
.chart{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:12px}
.chart svg{width:100%;height:auto;display:block}
.chart text{fill:var(--muted);font:11px 'IBM Plex Mono',monospace}
.dl{display:grid;gap:8px}
.dl div{display:grid;grid-template-columns:92px 1fr;gap:12px}
footer{font-size:12px;color:var(--muted)}
@media (max-width:520px){.day{grid-template-columns:78px 1fr}.day .pill{grid-column:2}}
"""


def brl(v):
    return 'R$ ' + f'{v:,.0f}'.replace(',', '.') if v is not None else '—'


def usd(v, cur='USD'):
    sym = {'USD': 'US$', 'BRL': 'R$', 'EUR': '€'}.get(cur, cur)
    return f'{sym} ' + f'{v:,.0f}'.replace(',', '.') if v is not None else '—'


def day_label(iso):
    d = dt.date.fromisoformat(iso)
    return f'{WEEK[d.weekday()]} {d.day:02d}/{d.month:02d}'


def chart(points, target=None, ref=None):
    """points: [(iso date, value)]; draws a line with a faint grid and dashed target/reference lines."""
    if len(points) < 2: return '<p class="note">Poucos pontos ainda: o gráfico aparece a partir da segunda leitura de preço real.</p>'
    W, H, L, R, T, B = 640, 220, 64, 16, 14, 30
    xs = [dt.date.fromisoformat(p[0][:10]).toordinal() for p in points]
    vals = [p[1] for p in points] + [v for v in (target, ref) if v]
    lo, hi = min(vals) * 0.95, max(vals) * 1.05
    x0, x1 = min(xs), max(xs) if max(xs) > min(xs) else min(xs) + 1
    X = lambda x: L + (x - x0) / (x1 - x0) * (W - L - R)
    Y = lambda v: T + (hi - v) / (hi - lo) * (H - T - B)
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Preço real do voo ao longo do tempo">']
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--line)" stroke-width="1"/>'
                   f'<text x="{L - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{v / 1000:.1f} mil</text>')
    for v, name, color in ((target, 'meta', 'var(--good)'), (ref, 'ano passado', 'var(--warn)')):
        if v: out.append(f'<line x1="{L}" x2="{W - R}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="{color}" stroke-dasharray="5 4"/>'
                         f'<text x="{W - R}" y="{Y(v) - 4:.1f}" text-anchor="end" style="fill:{color}">{name}</text>')
    pts = ' '.join(f'{X(x):.1f},{Y(p[1]):.1f}' for x, p in zip(xs, points))
    area = f'{X(xs[0]):.1f},{H - B} ' + pts + f' {X(xs[-1]):.1f},{H - B}'
    out.append(f'<polygon points="{area}" fill="var(--accent-soft)" opacity=".7"/>')
    out.append(f'<polyline points="{pts}" fill="none" stroke="var(--accent)" stroke-width="2"/>')
    lx, lp = xs[-1], points[-1]
    out.append(f'<circle cx="{X(lx):.1f}" cy="{Y(lp[1]):.1f}" r="4" fill="var(--accent)"/>')
    for x in sorted({xs[0], xs[len(xs) // 2], xs[-1]}):
        d = dt.date.fromordinal(x)
        out.append(f'<text x="{X(x):.1f}" y="{H - 10}" text-anchor="middle">{d.day:02d}/{MONTHS[d.month - 1]}</text>')
    out.append('</svg>')
    return ''.join(out)


def render(ctx):
    t = ctx['trip']
    o, d = (t.get('origins') or ['?'])[0], (t.get('destinations') or ['?'])[0]
    plan = t.get('plan') or {}
    who = f"{t.get('adults', 1)} adultos" + (f" + {t.get('children')} criança" + ('s' if (t.get('children') or 0) > 1 else '') if t.get('children') else '')
    head = f"""<title>{E(t.get('name', 'Viagem'))}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=IBM+Plex+Mono:wght@400;500&family=Source+Sans+3:wght@400;600;700&display=swap">
<style>{CSS}</style>"""
    route = (f'<header class="route"><span class="iata">{E(o)}</span><span class="arrow">⇄</span><span class="iata">{E(d)}</span>'
             f'<div><h1>{E(t.get("name", ""))}</h1><div class="sub">{E(plan.get("label", ""))} · {E(who)}</div></div></header>')
    f = ctx['flight']
    tiles = [('Total estimado', brl(ctx['total']), f"2025: {brl(ctx['past_total'])}" if ctx.get('past_total') else ''),
             ('Voo agora, para todos', brl(f.get('price')), f"meta {brl(t.get('target_total'))} · {E(f.get('source', ''))}"),
             ('Dólar hoje', f"R$ {ctx['fx']:.2f}".replace('.', ',') if ctx.get('fx') else '—', E(ctx.get('fx_note', ''))),
             ('Próximo prazo', E(ctx['next_deadline'][0]) if ctx.get('next_deadline') else '—', E(ctx['next_deadline'][1]) if ctx.get('next_deadline') else '')]
    tiles_html = '<section class="tiles">' + ''.join(
        f'<div class="tile"><span class="k">{k}</span><span class="v num">{v}</span><span class="note">{n}</span></div>' for k, v, n in tiles) + '</section>'
    days = ''.join(f'<div class="day"><span class="d">{day_label(i["date"])}</span><span>{E(i["text"])}</span>'
                   + (f'<span class="pill acc">{E(i["where"])}</span>' if i.get('where') else '<span></span>') + '</div>'
                   for i in t.get('itinerary') or [])
    rows = []
    for r in ctx['costs']:
        status = {'reservado': 'good', 'cotado': 'acc', 'estimativa': 'warn'}.get(r['status'], '')
        delta = ''
        if r.get('delta') is not None:
            cls = 'bad' if r['delta'] > 5 else 'good' if r['delta'] < -5 else ''
            delta = f'<span class="pill {cls}">{r["delta"]:+.0f}%</span>'
        rows.append(f'<tr><td>{E(cat_name(r["category"]))}<div class="note">{E(r["label"])}</div></td><td class="num">{E(r["before"])}</td>'
                    f'<td class="num">{E(r["now"])}</td><td>{delta}</td><td class="num">{brl(r["brl"])}</td>'
                    f'<td><span class="pill {status}">{E(r["status"])}</span></td></tr>')
    rows.append(f'<tr class="total"><td>Total</td><td></td><td></td><td></td><td class="num">{brl(ctx["total"])}</td><td></td></tr>')
    costs = ('<div class="scroll"><table><thead><tr><th>Item</th><th class="num">2025</th><th class="num">Agora</th><th>Dif.</th>'
             '<th class="num">Em reais</th><th>Situação</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
             f'<p class="note">Itens em dólar convertidos a R$ {ctx.get("fx") or 0:.2f}; 2025 comparado na mesma moeda e unidade (hotel por noite, parques e comida por dia).</p>'.replace('.', ',', 1))
    dl = ''.join(f'<div><span class="mono">{E(i["date"][8:10])}/{E(i["date"][5:7])}</span><span>{E(i["title"])}</span></div>'
                 for i in ctx['deadlines'])
    body = (f'<main class="wrap">{route}{tiles_html}'
            f'<section><h2>Roteiro</h2><div class="days">{days}</div></section>'
            f'<section><h2>Custos contra a viagem anterior</h2>{costs}</section>'
            f'<section><h2>Preço real do voo {E(plan.get("label", ""))}</h2><div class="chart">'
            f'{chart(ctx["price_points"], t.get("target_total"), (t.get("reference") or {}).get("total"))}</div>'
            f'<p class="note">{E(f.get("detail", ""))}</p></section>'
            f'<section><h2>Prazos</h2><div class="dl">{dl or "<p class=note>Nenhum prazo cadastrado.</p>"}</div></section>'
            f'<footer>Atualizado em {E(ctx["updated"])} pelo travel-agent.</footer></main>')
    return head + body
