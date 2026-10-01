#!/usr/bin/env python3
"""Product radar (PLN0226, behavior `product-radar`): what similar products ship becomes at most 3 ideas a week.

Sibling of the tech radar (radar.py), which follows the stacks; this one follows the product. The collection reuses the
tech radar's fetch, feed parser, week gate and seen set; only the sources and the output differ:

  product_feeds   public changelogs, release notes and RSS of task apps, of agent products and of discovery sites
  product_github  repositories of the week under the GitHub topics of the config (public search API)

Each source runs once per ISO week (`weekday`/`hour` of `behavior_config.product-radar`) and saves what is new in
data/product-radar/<week>.json; the tick shows `== RADAR DE PRODUTO <source> <week> (n novas)`.

The ideas are written by the session (it reads the links and says it in its own words: only the idea, never the
brand, the text, the look or the code of the other product). This script does the mechanical part:

  python3 product_radar.py <instance> [--dry] [--fixtures DIR] [--week W] [--known FILE]
      context of the week: the candidates (the week's items not proposed before), what Planou already has (the
      project's tasks by GET /v1/agent/projects/{key}/flow, the README and the CHANGELOG of `planou_repo`) and how
      many ideas the week still takes.
  python3 product_radar.py <instance> --ideas FILE|- [--dry] [--week W] [--known FILE]
      checks the ideas (link, what, why, sketch of up to 3 lines, size P/M/G, risk), drops the ones Planou already has
      or that were proposed before (same link or same title), keeps at most `max_ideas` a week (never more than 3,
      counting what was published this week), writes the backlog.py plan in data/product-radar/plan-<week>.json and
      shows its preview. `--dry` writes nothing.

Ideas file (JSON list, or {"ideas": [...]}; texts in the instance language):
  [{"title": "Lembrete por local", "competitor": "App X", "url": "https://...", "what": "o que o outro produto faz",
    "why": "por que serviria ao Planou", "sketch": ["linha 1", "linha 2", "linha 3"], "size": "P|M|G",
    "risk": "o risco", "force": false}]
"force": true skips only the comparison with Planou's tasks and docs (after the session checked it was a false match);
the link and title of an idea proposed before are never proposed again.

Exit 0 ok, 2 bad command, config or ideas file.
"""
import argparse, json, os, re, sys, unicodedata, urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import radar   # noqa: E402

BEHAVIOR = 'product-radar'
FOLDER = 'product-radar'
HEADER = 'RADAR DE PRODUTO'
SOURCES = ('product_feeds', 'product_github')
HARD_MAX = 3
DEFAULTS = {'project': '', 'max_ideas': HARD_MAX, 'terms': [], 'weekday': 0, 'hour': 9, 'window_days': 7,
            'planou_repo': '', 'known_days': 180}
SIZES = ('P', 'M', 'G')
LEDGER = 'ideas.json'
TASK_SIMILAR, DOC_SIMILAR = 0.6, 0.75
STOP = set('para como com uma umas uns dos das que por sem mais pela pelo pelos pelas quando cada entre sobre agora '
           'numa num nos nas ele ela eles elas isso este esta esse essa tudo todo toda todos todas outro outra '
           'the and for with from this that into your you are was were how what when will can new now '
           'novo nova novos novas tarefa tarefas task tasks planou fazer faz feito ideia'.split())


def options(cfg):
    o = radar.options(cfg, BEHAVIOR, DEFAULTS)
    try: o['max_ideas'] = max(1, min(HARD_MAX, int(o['max_ideas'])))
    except (TypeError, ValueError): o['max_ideas'] = HARD_MAX
    return o


# ---------------------------------------------------------------- text comparison

def _fold(text):
    t = unicodedata.normalize('NFKD', str(text or '').lower())
    return ''.join(c for c in t if not unicodedata.combining(c))


def words(text):
    out = set()
    for w in re.findall(r'[a-z0-9]{4,}', _fold(text)):
        if w in STOP: continue
        out.add(w[:-1] if w.endswith('s') and len(w) > 4 else w)
    return out


def similar(title, text, threshold):
    """True when most words of the idea title are in the text (at least 2 of them)."""
    a = words(title)
    if len(a) < 2: return False
    shared = a & words(text)
    return len(shared) >= 2 and len(shared) / len(a) >= threshold


def norm_url(url):
    u = urllib.parse.urlsplit(str(url or '').strip())
    return f'{(u.hostname or "").lower().removeprefix("www.")}{u.path.rstrip("/")}'.lower()


# ---------------------------------------------------------------- what Planou already has

def planou_tasks(instance, project, now, days):
    """([{pid, title, state}], warning): the project's open tasks and the ones closed in the last `days`, by
    GET /v1/agent/projects/{key}/flow (a read: works in test mode too)."""
    from watch_core import planou as P
    try:
        P.configure_agent(instance)
    except Exception as e:
        return [], f'Planou não configurado ({type(e).__name__})'
    if not (P._S.get('base_url') and P._key()):
        return [], 'sem chave ou URL do Planou nesta instância: comparei só com o README, o CHANGELOG e --known'
    f, t = (now - timedelta(days=max(1, min(365, int(days))))).date(), now.date()
    try:
        _, data = P._call('GET', f'/agent/projects/{urllib.parse.quote(project)}/flow?from={f}&to={t}')
    except P.PlanouError as e:
        return [], f'o Planou recusou a lista de tarefas de {project}: {e.status} {e.message}'
    except Exception as e:
        return [], f'o Planou não respondeu a lista de tarefas de {project}: {type(e).__name__}'
    out = [{'pid': x.get('pid'), 'title': x.get('title') or '', 'state': x.get('state')}
           for x in (data or {}).get('tasks') or [] if isinstance(x, dict) and x.get('title')]
    return out, None


def doc_lines(repo):
    """[(where, text)]: the headings and table rows of README.md and the items of CHANGELOG.md of the Planou repo."""
    out = []
    if not repo: return out
    for name in ('README.md', 'CHANGELOG.md'):
        try:
            with open(os.path.join(os.path.expanduser(repo), name), encoding='utf-8', errors='replace') as f:
                for line in f:
                    s = line.strip()
                    if s.startswith(('#', '- ', '* ', '|')) and len(s) > 3: out.append((name, s.lstrip('#-*| ')[:400]))
        except OSError:
            continue
    return out


def known_file(path):
    """Extra titles the session knows about: a JSON list of strings or {pid, title}, or one title per line."""
    if not path: return []
    raw = sys.stdin.read() if path == '-' else open(os.path.expanduser(path), encoding='utf-8').read()
    try:
        d = json.loads(raw)
        items = d.get('tasks') if isinstance(d, dict) else d
    except ValueError:
        items = [line.strip() for line in raw.splitlines() if line.strip()]
    out = []
    for x in items or []:
        if isinstance(x, str): out.append({'pid': None, 'title': x})
        elif isinstance(x, dict) and x.get('title'): out.append({'pid': x.get('pid'), 'title': str(x['title'])})
    return out


def _ledger_path():
    return os.path.join(radar.inbox_dir(FOLDER), LEDGER)


def ledger():
    try: return json.load(open(_ledger_path(), encoding='utf-8'))
    except (OSError, ValueError): return {}


def _goal(week):
    return f'product-radar-{week}'


def proposed(now_week=None):
    """([{pid, title, url, week}] published before, published this week): the ideas that became tasks (backlog.py's
    product_plans.json) with the link of the idea (the ledger)."""
    import backlog
    from watch_core import planou as P
    plans, led = P._load(backlog.PLANS, {}), ledger()
    out, this_week = [], 0
    for slug_, p in plans.items():
        if not slug_.startswith('product-radar-'): continue
        for code, t in (p.get('tasks') or {}).items():
            if not t.get('pid'): continue
            e = led.get(f'{slug_}:{code}') or {}
            out.append({'pid': t['pid'], 'title': t.get('title') or e.get('title') or '', 'url': e.get('url') or '',
                        'week': e.get('week') or slug_.removeprefix('product-radar-')})
            if now_week and slug_ == backlog.slug(_goal(now_week)): this_week += 1
    return out, this_week


# ---------------------------------------------------------------- ideas

class IdeasError(Exception):
    pass


def _one(v, n=None):
    t = ' '.join(str(v or '').split())
    return t[:n] if n else t


def validate(raw):
    ideas = raw.get('ideas') if isinstance(raw, dict) else raw
    if not isinstance(ideas, list) or not ideas: raise IdeasError('as ideias: uma lista JSON (ou {"ideas": [...]}) com pelo menos uma')
    err, out, codes = [], [], set()
    for n, x in enumerate(ideas, 1):
        where = f'ideia {n}'
        if not isinstance(x, dict): err.append(f'{where}: precisa ser um objeto'); continue
        i = {k: _one(x.get(k)) for k in ('title', 'competitor', 'url', 'what', 'why', 'risk')}
        for k, label in (('title', 'título'), ('competitor', 'quem faz'), ('what', 'o que o concorrente faz'),
                         ('why', 'por que serviria ao Planou'), ('risk', 'o risco')):
            if not i[k]: err.append(f'{where}: "{k}" ({label}) vazio')
        if len(i['title']) > 80: err.append(f'{where}: "title" até 80 caracteres')
        u = urllib.parse.urlsplit(i['url'])
        if u.scheme != 'https' or not u.hostname: err.append(f'{where}: "url": o link público (https) do que o concorrente faz')
        sk = x.get('sketch')
        if isinstance(sk, str): sk = sk.splitlines()
        sk = [_one(s, 200) for s in sk if _one(s)] if isinstance(sk, list) else []
        if not 1 <= len(sk) <= 3: err.append(f'{where}: "sketch": o esboço de como ficaria no Planou, de 1 a 3 linhas')
        size = _one(x.get('size')).upper()[:1]
        if size not in SIZES: err.append(f'{where}: "size": P, M ou G')
        code = re.sub(r'[^a-z0-9]+', '-', _fold(i['title'])).strip('-')[:36].strip('-') or f'ideia-{n}'
        while code in codes: code = f'{code[:33]}-{n}'
        codes.add(code)
        out.append(dict(i, sketch=sk, size=size, code=code, force=x.get('force') is True))
    if err: raise IdeasError('\n'.join(err))
    return out


def screen(ideas, tasks, docs, before, room):
    """(kept, dropped): dropped = [(idea, why)]. Order: proposed before (link or title), Planou's tasks, the docs,
    then the week's limit."""
    kept, dropped = [], []
    urls = {norm_url(b['url']): b for b in before if b.get('url')}
    for i in ideas:
        b = urls.get(norm_url(i['url'])) or next((b for b in before if similar(i['title'], b['title'], TASK_SIMILAR)), None)
        if b:
            dropped.append((i, f'já proposta na semana {b["week"]} ({b["pid"]}: {b["title"]})')); continue
        if not i['force']:
            t = next((t for t in tasks if similar(i['title'], t['title'], TASK_SIMILAR)), None)
            if t:
                dropped.append((i, f'já existe: {t.get("pid") or "tarefa"} {t["title"]}')); continue
            d = next((d for d in docs if similar(i['title'], d[1], DOC_SIMILAR)), None)
            if d:
                dropped.append((i, f'o Planou já tem ({d[0]}): {d[1][:120]}')); continue
        if len(kept) >= room:
            dropped.append((i, 'limite da semana')); continue
        kept.append(i)
    return kept, dropped


def build_plan(kept, week, project):
    tasks = []
    for i in kept:
        tasks.append({'code': i['code'], 'title': i['title'],
                      'what': f'{i["competitor"]}: {i["what"]} Link: {i["url"]}',
                      'why': i['why'],
                      'details': ['Esboço no Planou:'] + [f'- {s}' for s in i['sketch']] +
                                 [f'Tamanho: {i["size"]}', f'Risco: {i["risk"]}',
                                  'Só a ideia: nada de marca, texto, visual ou código do outro produto.'],
                      'done_when': ['a decisão anotada na tarefa: fazer, esperar ou descartar'],
                      'priority': 'P4'})     # no agent_can_do: the idle pull would hand it back to this agent
    plan = {'goal': {'ref': _goal(week), 'title': f'Radar de produto da semana {week}'}, 'tasks': tasks}
    if project: plan['project'] = project
    return plan


# ---------------------------------------------------------------- CLI

def collect_now(cfg, fixtures=None, now=None):
    """The instance's product sources in dry mode (nothing written): (items, errors)."""
    import adapters
    now = now or datetime.now(timezone.utc)
    ctx, items, errors = radar._Ctx(cfg), [], []
    for e in cfg.get('sources') or []:
        if (e.get('type') or e.get('tipo')) not in SOURCES: continue
        e = dict(e)
        if fixtures: e['fixtures'] = fixtures
        src = adapters.instancia(e, 'Fonte', ctx)
        try:
            r = src.tick({}, None, True)
            items += (r or {}).get('items') or []
            errors += [f'{src.nome}: {x}' for x in (r or {}).get('errors') or []]
        except (Exception, SystemExit) as ex:
            errors.append(f'{src.nome}: {getattr(ex, "code", None) or ex}')
    return items, errors


def _week_items(week):
    try: d = json.load(open(os.path.join(radar.inbox_dir(FOLDER), f'{week}.json'), encoding='utf-8'))
    except (OSError, ValueError): return []
    return [i for v in d.values() for i in v if isinstance(i, dict)]


def main(argv=None):
    ap = argparse.ArgumentParser(prog='product_radar.py', description='Product radar: at most 3 ideas a week from similar products.')
    ap.add_argument('instance')
    ap.add_argument('--ideas', metavar='FILE', help="the session's ideas (JSON; '-' reads stdin)")
    ap.add_argument('--dry', action='store_true', help='collect now, in memory, and write nothing')
    ap.add_argument('--fixtures', help='answer every URL from this folder (tests, offline check)')
    ap.add_argument('--week', help='the week (default: the current one)')
    ap.add_argument('--known', metavar='FILE', help='more titles Planou already has (JSON list or one per line)')
    a = ap.parse_args(argv)
    import paths, schema
    paths.instance(a.instance)
    try:
        cfg = schema.normalize(json.load(open(paths.CONFIG, encoding='utf-8')))
    except (OSError, ValueError, schema.ConfigError) as e:
        print(f'ERRO: config de {a.instance}: {getattr(e, "code", e)}', file=sys.stderr)
        return 2
    if BEHAVIOR not in (cfg.get('behaviors') or []):
        print(f'ERRO: {a.instance} não tem o comportamento {BEHAVIOR} em "behaviors"', file=sys.stderr)
        return 2
    o, now = options(cfg), datetime.now(timezone.utc)
    week = a.week or radar.week_of(now, cfg)
    project = o['project'] or ((cfg.get('planou') or {}).get('project')) or ''
    if a.fixtures: print(f'FIXTURES: {a.fixtures} (dados de teste, sem rede)')
    if a.known == '-' and a.ideas == '-':
        print('ERRO: só um de --ideas e --known lê o stdin', file=sys.stderr); return 2
    try: extra = known_file(a.known)
    except (OSError, ValueError) as e:
        print(f'ERRO: --known: {e}', file=sys.stderr); return 2
    tasks, warn = planou_tasks(a.instance, project, now, o['known_days']) if project else ([], 'sem projeto (project)')
    if warn: print(f'AVISO (radar de produto): {warn}')
    tasks += extra
    docs = doc_lines(o['planou_repo'])
    before, this_week = proposed(week)
    room = max(0, o['max_ideas'] - this_week)

    if a.ideas is None:
        errors = []
        if a.dry: items, errors = collect_now(cfg, a.fixtures, now)
        else: items = _week_items(week)
        for e in errors: print(f'AVISO (radar de produto): {e}')
        seen = {norm_url(b['url']) for b in before if b.get('url')}
        items = [i for i in radar.rank(items) if norm_url(i['url']) not in seen]
        print(f'RADAR DE PRODUTO {week}: {len(items)} candidatos' + (' (simulado, nada gravado)' if a.dry else '')
              + f'; vagas da semana: {room} de {o["max_ideas"]}')
        for i in items[:30]: print(radar.line(i))
        if len(items) > 30: print(f'  ... e mais {len(items) - 30}')
        print(f'JA NO PLANOU: {len(tasks)} tarefas de {project or "?"}, {len(docs)} linhas do README e do CHANGELOG'
              + (f' ({o["planou_repo"]})' if o['planou_repo'] else ' (sem planou_repo)') + f', {len(before)} ideias já propostas')
        for t in tasks[:80]: print(f'  {t.get("pid") or "-"} {t["title"][:100]}')
        if len(tasks) > 80: print(f'  ... e mais {len(tasks) - 80}')
        for b in before[-10:]: print(f'  proposta {b["week"]}: {b["pid"]} {b["title"][:100]}')
        if not room: print('SEM VAGA: a semana já tem as ideias do limite; nada a propor')
        else: print(f'PROXIMO: escrever até {room} ideias (JSON) e rodar python3 {os.path.abspath(__file__)} {a.instance} --ideas <arquivo>')
        return 0

    try:
        raw = json.load(sys.stdin) if a.ideas == '-' else json.load(open(os.path.expanduser(a.ideas), encoding='utf-8'))
        ideas = validate(raw)
    except (OSError, ValueError) as e:
        print(f'ERRO: --ideas: {e}', file=sys.stderr); return 2
    except IdeasError as e:
        print('ERRO: ideias incompletas (nada foi gravado):\n' + str(e), file=sys.stderr); return 2
    kept, dropped = screen(ideas, tasks, docs, before, room)
    for i, why in dropped:
        print(f'DESCARTADA: {i["title"]}: ' + (f'limite de {o["max_ideas"]} por semana ({this_week} já publicadas)'
                                                if why == 'limite da semana' else why))
    if not kept:
        print(f'RADAR DE PRODUTO {week}: nenhuma ideia nova para propor')
        return 0
    plan = build_plan(kept, week, project)
    for i in kept: print(f'IDEIA: [{i["size"]}] {i["title"]} ({i["competitor"]}): {i["url"]}')
    target = None
    if not a.dry:
        from watch_core import fileio
        import backlog
        target = os.path.join(radar.inbox_dir(FOLDER), f'plan-{week}.json')
        fileio.write_json(target, plan, ensure_ascii=False, indent=1)
        led = ledger()
        for i in kept:
            led[f'{backlog.slug(_goal(week))}:{i["code"]}'] = {'url': i['url'], 'title': i['title'], 'week': week,
                                                                'competitor': i['competitor']}
        fileio.write_json(_ledger_path(), led, ensure_ascii=False, indent=1)
        print(f'PLANO: {target}')
    import backlog
    try:
        rc, lines = backlog.run(a.instance, plan, dry=True, max_tasks=len(plan['tasks']))
        print('PREVIA (backlog.py --dry):'); print('\n'.join(lines))
    except backlog.PlanError as e:
        print('AVISO (radar de produto): o plano não passa no backlog.py: ' + '; '.join(str(e).splitlines()))
        return 2
    if target:
        print(f'PUBLICAR: python3 {os.path.join(paths.SCRIPTS, "backlog.py")} {a.instance} {target} --max {len(plan["tasks"])}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
