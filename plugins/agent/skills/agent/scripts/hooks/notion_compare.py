#!/usr/bin/env python3
"""Hook notion_compare: once a business day, compares the tasks of the daily Notion page with what Planou has, read only.

Part of the Notion exit plan (docs/notion-exit.md): while an instance still writes its `## <CODE>` section of the day
page (hook `tarefas`) and syncs the same list to Planou (planou_tick), this hook checks that both sides say the same
thing. After `days` business days in a row without a difference it says so once; turning Notion off is the person's
call (they take the `daily` and `tarefas` hooks out of the instance config). This hook never turns anything off and
never writes to Notion or to Planou.

What it reads (nothing is written outside the instance folder):
  Notion side  "from": "local" (default): the section the tasks engine last published for the day page
               (state tarefas_notion.render[<day>], the exact markdown that went to Notion), no network.
               "from": "api": the same section read from Notion (GET only) with the token the instance already
               uses (watch_core's secrets/notion.env), on the page id the engine recorded (state daily.notion)
  Planou side  cache/planou/state.json: what Planou confirmed on the last sync (pid, title, state per task code). The
               agent API (/v1) has no route that lists an agent's tasks, so an edit the person made in Planou counts
               once its event reached the agent (the next tick)

Rules (the same the sync applies, so a clean instance compares clean):
  - the key is the task code (a3, p12, ...); lines show the page id <CODE>-N and the Planou pid
  - state by group: blocked, waiting, closed, open (todo, in progress, decision, backlog); a task Planou keeps in
    backlog or in refinement is compared only as open or closed
  - a task on the page that Planou does not know, a task open in Planou that the page does not show, a state that
    differs and a title that differs are differences. Closed tasks Planou has and the page does not show (older than
    yesterday, "Sem acao", "Descartada") are not
  - titles: under "confidentiality": "minimum" Planou gets a neutral title by design, so only the page id is checked
    and the output never carries a title

When: the first live tick of a business day (Mon-Fri, instance timezone) at or after `at_hour`. Dry ticks and test
mode never run it. Every run appends one line to data/notion_compare.jsonl: {day, at, page, from, status (ok | diff |
skipped), reason, notion, planou, diffs: [{code, uid, pid, kind}], streak}. No titles go to the file.

Streak: business days with a clean comparison, counted back from the latest; a difference resets it; a skipped day
(nothing published on Notion that day, no Planou cache) neither counts nor resets; a gap of more than `max_gap_days`
calendar days between two comparisons (runner off) resets it.

Output:
  == NOTION x PLANOU (n)               one line per difference: `-- <CODE>-N: <kind> · Planou <pid> · "<short title>"`
  == NOTION PODE DESLIGAR (<instance>) once, the day the streak reaches `days`

Config ("hooks": [{"type": "notion_compare", ...}]; missing = off):
  at_hour       local hour from which the day's comparison runs (default 20)
  days          business days without a difference before the announcement (default 14)
  max_gap_days  longest gap between two comparisons that keeps the streak (default 4: a weekend plus a holiday)
  from          "local" (default) | "api"
  code          the page section code (default: the instance "code" / "sigla")
CLI: agent.py <instance> --notion-compare            last result and the streak
     agent.py <instance> --notion-compare agora      compares now (same rules, recorded as today's run)
"""
import json, os, re, urllib.request
from datetime import date, datetime, timezone

import core, paths
from adapters import Gancho as Base

FILE = 'notion_compare.jsonl'
API = 'https://api.notion.com/v1'
OPEN = 'open'
GROUP = {'todo': OPEN, 'in_progress': OPEN, 'backlog': OPEN, 'waiting': 'waiting', 'blocked': 'blocked',
         'closed': 'closed'}
GROUP_PT = {OPEN: 'aberta', 'waiting': 'aguardando', 'blocked': 'impedimento', 'closed': 'fechada'}
LABEL_GROUP = {'impedimentos': 'blocked', 'a_fazer': OPEN, 'backlog': OPEN, 'feito': 'closed',
               'aguardando': 'waiting', 'ontem': 'closed', 'top': None}
PREFIXES = ('Decidir: ', 'Decidido: ', 'Decide: ', 'Decided: ')
BOLD = re.compile(r'\*\*(.+?)\*\*')


def _now(default=None):
    """The clock; a test (WATCH_CORE_TEST=1) may pin it with NOTION_COMPARE_NOW (ISO)."""
    t = os.environ.get('NOTION_COMPARE_NOW') if os.environ.get('WATCH_CORE_TEST') == '1' else None
    if t: return datetime.fromisoformat(t)
    return default or datetime.now(timezone.utc)


def labels():
    """Section label (pt and en) -> group; None = skip the section (the Top of the day repeats tasks listed below)."""
    from watch_core import tasks as tc
    out = {}
    for rot in (tc.ROT_PT, tc.ROT_EN):
        for k, g in LABEL_GROUP.items(): out[rot[k].strip().lower()] = g
    return out


def _label(text):
    """'Ontem (26/09)' / 'Backlog (3)' -> 'ontem' / 'backlog'."""
    return re.sub(r'\s*\([^)]*\)\s*$', '', text.strip()).strip().lower()


def _norm(t):
    """A title as the page prints it (engine: backticks and brackets swapped), without the decision prefix."""
    t = ' '.join((t or '').replace('`', "'").replace('[', '(').replace(']', ')').split())
    for p in PREFIXES:
        if t.startswith(p): return t[len(p):]
    return t


def parse_md(md, code):
    """{uid: {'group', 'title'}} from the section markdown the engine published."""
    lab, cur, out = labels(), OPEN, {}
    item = re.compile(r'^\s*-\s*(?:\[( |x)\]\s*)?`(' + re.escape(code) + r'-\d+)(-[A-Z])?`\s*(.*)$')
    for line in (md or '').splitlines():
        s = line.strip()
        if not s: continue
        m = re.fullmatch(r'\*\*(.+?)\*\*(\s*\(.*\))?', s)
        if m or s.startswith('▸ '):
            cur = lab.get(_label(m.group(1) if m else s[2:]), OPEN)
            continue
        m = item.match(line)
        if not m or m.group(3) or cur is None: continue           # an option of a decision, or the Top of the day
        b = BOLD.search(m.group(4))
        out[m.group(2)] = {'group': 'closed' if m.group(1) == 'x' else cur, 'title': _norm(b.group(1) if b else '')}
    return out


# ---------------- Notion API (GET only) ----------------
def _get(base, path, tok):
    req = urllib.request.Request(base.rstrip('/') + path, method='GET',
                                 headers={'Authorization': f'Bearer {tok}', 'Notion-Version': '2022-06-28'})
    with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read())


def _children(base, bid, tok):
    out, cursor = [], None
    while True:
        r = _get(base, f'/blocks/{bid}/children?page_size=100' + (f'&start_cursor={cursor}' if cursor else ''), tok)
        out += r.get('results') or []
        if not r.get('has_more'): return out
        cursor = r.get('next_cursor')


def _rich(b):
    return (b.get(b.get('type'), {}) or {}).get('rich_text') or []


def _plain(b):
    return ''.join(x.get('plain_text', '') for x in _rich(b))


def parse_blocks(base, page, code, tok):
    """{uid: {'group', 'title'}} from the `## <code>` section of the day page, read from Notion."""
    lab, out = labels(), {}
    item = re.compile(r'^(' + re.escape(code) + r'-\d+)(-[A-Z])?(?![\w-])')

    def entry(b, cur):
        if b['type'] not in ('to_do', 'bulleted_list_item') or cur is None: return
        m = item.match(_plain(b).strip())
        if not m or m.group(2): return
        bold = next((x.get('plain_text', '') for x in _rich(b) if (x.get('annotations') or {}).get('bold')), '')
        checked = b['type'] == 'to_do' and (b.get('to_do') or {}).get('checked')
        out[m.group(1)] = {'group': 'closed' if checked else cur, 'title': _norm(bold)}

    inside, cur = False, OPEN
    for b in _children(base, page, tok):
        if b['type'] in ('heading_1', 'heading_2', 'heading_3'):
            if inside: break
            inside = b['type'] == 'heading_2' and _plain(b).strip().upper() == code.upper()
            continue
        if not inside: continue
        if b['type'] == 'paragraph':
            t = _plain(b).strip()
            if t and not item.match(t): cur = lab.get(_label(t), OPEN)
            continue
        if b['type'] == 'toggle':
            g = lab.get(_label(_plain(b).lstrip('▸ ')), OPEN)
            for c in _children(base, b['id'], tok) if b.get('has_children') else []: entry(c, g)
            continue
        entry(b, cur)
    return out


# ---------------- comparison ----------------
def compare(notion, planou_tasks, ids, code, minimum):
    """[{code, uid, pid, kind, title}] for {uid: page item} x {task code: Planou entry}; ids = {task code: N}."""
    uid_of = {c: f'{code}-{n}' for c, n in ids.items()}
    code_of = {u: c for c, u in uid_of.items()}
    diffs = []

    def add(c, uid, pid, kind, title=''):
        diffs.append({'code': c, 'uid': uid, 'pid': pid, 'kind': kind, 'title': '' if minimum else (title or '')[:50]})

    for uid, it in sorted(notion.items(), key=lambda kv: int(kv[0].rsplit('-', 1)[1])):
        c = code_of.get(uid)
        pe = (planou_tasks.get(c) or {}) if c else {}
        if not pe.get('pid'):
            add(c or '', uid, None, 'falta no Planou', it['title']); continue
        g = GROUP.get(pe.get('state'), OPEN)
        loose = pe.get('backlog') or pe.get('refining')
        ng = it['group']
        if loose: g, ng = ('closed' if g == 'closed' else OPEN), ('closed' if ng == 'closed' else OPEN)
        if g != ng:
            add(c, uid, pe['pid'], f'estado: Notion {GROUP_PT[ng]}, Planou {GROUP_PT[g]}', it['title']); continue
        if minimum:
            continue
        if _norm(pe.get('title')) != it['title']:
            add(c, uid, pe['pid'], 'titulo difere', it['title'])
    for c, pe in sorted(planou_tasks.items()):
        if not pe.get('pid') or GROUP.get(pe.get('state'), OPEN) == 'closed': continue
        uid = uid_of.get(c)
        if uid in notion: continue
        add(c, uid or c, pe['pid'], 'aberta no Planou, fora do Notion', pe.get('title'))
    return diffs


def _read_jsonl(f):
    out = []
    try:
        with open(f, encoding='utf-8') as fh:
            for line in fh:
                try: r = json.loads(line)
                except ValueError: continue
                if isinstance(r, dict): out.append(r)
    except OSError:
        pass
    return out


def streak(records, max_gap_days=4):
    """(business days clean in a row up to the latest comparison, first day of that run). The last record of a day wins."""
    by_day = {}
    for r in records:
        try: date.fromisoformat(str(r.get('day')))
        except ValueError: continue
        if r.get('status') in ('ok', 'diff', 'skipped'): by_day[r['day']] = r
    n, start, prev = 0, None, None
    for day in sorted(by_day, reverse=True):
        r = by_day[day]
        d = date.fromisoformat(day)
        if prev and (prev - d).days > max_gap_days: break
        prev = d
        if r['status'] == 'skipped': continue
        if r['status'] == 'diff': break
        n, start = n + 1, day
    return n, start


class Gancho(Base):
    tipo = 'notion_compare'
    json_key = 'notion_compare'
    posicao = 'fim'

    def configura(self):
        def num(k, d, lo=1, hi=10000):
            try: return min(hi, max(lo, int(self.cfg.get(k, d))))
            except (TypeError, ValueError): return d
        self.at_hour = num('at_hour', 20, 0, 23)
        self.days = num('days', 14)
        self.max_gap = num('max_gap_days', 4)
        self.mode = 'api' if self.cfg.get('from') == 'api' else 'local'
        self.api = (self.cfg.get('api_base') or API)

    # ---- helpers
    def _file(self): return os.path.join(paths.DATA_DIR, FILE)

    def _code(self, cfg):
        return self.cfg.get('code') or cfg.get('code') or cfg.get('sigla')

    def _instance(self, cfg):
        try: return paths.agent(cfg)
        except Exception: return getattr(paths, 'NAME', None) or 'agent'

    def _minimum(self, cfg):
        pc = cfg.get('planou') if isinstance(cfg.get('planou'), dict) else {}
        return (pc.get('confidentiality') or 'minimum') == 'minimum'

    def _notion(self, s, code, day):
        """(items or None, reason when None)."""
        st = s.get('tarefas_notion') or {}
        if self.mode == 'local':
            md = (st.get('render') or {}).get(day)
            if md is None: return None, f'secao {code} de {day} nao publicada no Notion'
            return parse_md(md, code), None
        dn = (s.get('daily') or {}).get('notion') or {}
        if dn.get('data') != day or not dn.get('pagina_dia'): return None, f'pagina do dia {day} sem id no estado'
        from watch_core import notion_decisions
        tok = notion_decisions.token()
        if not tok: return None, 'sem token do Notion'
        return parse_blocks(self.api, dn['pagina_dia'], code, tok), None

    def _planou(self):
        f = os.path.join(paths.CACHE_DIR, 'planou', 'state.json')
        try:
            with open(f, encoding='utf-8') as fh: t = (json.load(fh) or {}).get('tasks') or {}
            return t if isinstance(t, dict) else None
        except (OSError, ValueError, AttributeError): return None

    def compare_now(self, ctx, now=None):
        """Runs the comparison, appends it to the jsonl and returns the record (with titles, for the output only)."""
        now = now or _now()
        from watch_core import daily
        code = self._code(ctx.cfg)
        day = now.astimezone(core.BRT).date().isoformat()
        page = daily.data_daily(now.astimezone(daily.BRT))
        rec = {'day': day, 'at': now.isoformat(timespec='seconds'), 'page': page, 'from': self.mode}
        if not code:
            rec.update(status='skipped', reason='instancia sem "code"')
        else:
            try:
                notion, why = self._notion(ctx.s, code, page)
            except Exception as e:
                notion, why = None, f'Notion ilegivel: {type(e).__name__}'
            pl = self._planou()
            if notion is None: rec.update(status='skipped', reason=why)
            elif pl is None: rec.update(status='skipped', reason='sem cache do Planou (cache/planou/state.json)')
            else:
                try:
                    ids = ((ctx.s.get('tarefas_notion') or {}).get('ids') or {})
                    pl = {c: e for c, e in pl.items() if isinstance(e, dict)}
                    diffs = compare(notion, pl, ids, code, self._minimum(ctx.cfg))
                    rec.update(status='diff' if diffs else 'ok', notion=len(notion),
                               planou=sum(1 for e in pl.values() if e.get('pid')), diffs=diffs)
                except Exception as e:
                    rec.update(status='skipped', reason=f'comparacao falhou: {type(e).__name__}')
        f = self._file()
        records = _read_jsonl(f) + [rec]
        rec['streak'] = streak(records, self.max_gap)[0]
        os.makedirs(os.path.dirname(f), exist_ok=True)
        line = {k: v for k, v in rec.items() if k != 'diffs'}
        if 'diffs' in rec: line['diffs'] = [{k: v for k, v in d.items() if k != 'title'} for d in rec['diffs']]
        with open(f, 'a', encoding='utf-8') as fh: fh.write(json.dumps(line, ensure_ascii=False) + '\n')
        return rec, records

    def _announce(self, st, records):
        n, start = streak(records, self.max_gap)
        if n < self.days or not start or st.get('announced') == start: return None
        st['announced'] = start
        return {'streak': n, 'since': start}

    # ---- tick
    def run(self, ctx):
        now = _now(ctx.agora)
        local = now.astimezone(core.BRT)
        st = ctx.s.setdefault('notion_compare', {})
        if local.weekday() >= 5 or local.hour < self.at_hour: return None
        if st.get('last_day') == local.date().isoformat(): return None
        st['last_day'] = local.date().isoformat()          # one try a day, even when it fails
        try:
            rec, records = self.compare_now(ctx, now)
            return {'rec': rec, 'announce': self._announce(st, records), 'instance': self._instance(ctx.cfg)}
        except Exception as e:                             # never breaks the tick
            st['err'] = f'{type(e).__name__}: {e}'[:200]
            return None

    def texto(self, v):
        if not v: return None
        out = []
        rec = v['rec']
        if rec.get('status') == 'diff':
            out.append(f'== NOTION x PLANOU ({len(rec["diffs"])})')
            for d in rec['diffs']:
                pid = f' · Planou {d["pid"]}' if d.get('pid') else ''
                tit = f' · "{d["title"]}"' if d.get('title') else ''
                out.append(f'-- {d["uid"]}: {d["kind"]}{pid}{tit}')
            out.append(f'   pagina {rec["page"]} ({rec["from"]}); sequencia sem diferenca zerada')
        a = v.get('announce')
        if a:
            if out: out.append('')
            out += [f'== NOTION PODE DESLIGAR ({v["instance"]})',
                    f'-- {a["streak"]} dias uteis seguidos sem diferenca entre a pagina do dia e o Planou (desde '
                    f'{a["since"][8:]}/{a["since"][5:7]})',
                    '   desligar e decisao do usuario: tirar os ganchos "daily" e "tarefas" do config da instancia; '
                    'nada foi desligado']
        return '\n'.join(out) or None

    # ---- CLI
    def args(self, ap):
        ap.add_argument('--notion-compare', nargs='*', metavar='ARG', dest='notion_compare',
                        help='comparador Notion x Planou: ultimo resultado e a sequencia; "agora" compara ja (so leitura)')

    def cli(self, a, ctx):
        if getattr(a, 'notion_compare', None) is None: return False
        records = _read_jsonl(self._file())
        if a.notion_compare[:1] == ['agora']:
            try: rec, records = self.compare_now(ctx)
            except Exception as e: print(f'comparacao falhou: {type(e).__name__}: {str(e)[:160]}'); return True
            ctx.s.setdefault('notion_compare', {})['last_day'] = rec['day']
            ann = self._announce(ctx.s['notion_compare'], records)
            core.save_state(ctx.s)
            txt = self.texto({'rec': rec, 'announce': ann, 'instance': self._instance(ctx.cfg)})
            print(txt or f'sem diferenca ({rec["status"]}' + (f': {rec["reason"]}' if rec.get('reason') else '') + ')')
        n, start = streak(records, self.max_gap)
        last = records[-1] if records else None
        if not last: print('nenhuma comparacao ainda'); return True
        why = f' ({last["reason"]})' if last.get('reason') else ''
        print(f'ultima: {last["day"]} {last["status"]}{why}, {len(last.get("diffs") or [])} diferenca(s); '
              f'sequencia: {n}/{self.days} dias uteis' + (f' desde {start}' if start else ''))
        return True
