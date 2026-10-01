#!/usr/bin/env python3
"""Tech radar (PLN0118, behavior `tech-radar`): what is new in the stacks of the team becomes suggestions.

The collection is done by four sources of the instance (sources/radar_*.py, one per family, so a broken feed does not
hide the others and `--so` / `FONTE QUEBRADA` work per name):

  radar_deps      release notes of the dependencies of the repositories in the config (package.json, *.csproj,
                  Directory.Packages.props, requirements*.txt, pyproject.toml), latest version from npm, NuGet, PyPI
  radar_feeds     official blogs and curated YouTube channels, by RSS or Atom
  radar_hn        Hacker News stories of the week (Algolia search API), kept only when they match a stack
  radar_trending  GitHub Trending of the week per language, kept only when they match a stack

Each source runs once per ISO week (after `weekday`/`hour` of `behavior_config.tech-radar`, instance time), keeps a
seen set in the state (the same item never comes back) and saves what it found in data/radar/<week>.json. A dry tick
(`agent.py <instance> --dry`, and every tick in test mode) ignores the week gate and writes nothing.

The outside lookups are generic: package names of the repositories the config lists, public feed URLs and the stack
terms. Never a customer name, a repository path or the instance name. Client repositories are read only when the person
lists them (`client_repos` of radar_deps).

Tests and offline runs: `fixtures` in a source entry (or `--fixtures DIR` here) answers every URL from a local file,
`<DIR>/<host>/<path>[__<query>]` (fixture_path). AGENT_RADAR_URL_LOG=<file> appends every URL asked for.

CLI:
  python3 radar.py <instance> [--dry] [--fixtures DIR] [--week 2026-W40]
      weekly plan for the RADAR project: ranks the week's items, writes data/radar/plan-<week>.json (a plan of
      scripts/backlog.py, one task per suggestion, Backlog, no seal) and shows `backlog.py --dry` of it. `--dry`
      collects in memory (sources in dry mode) and writes nothing.
  python3 radar.py <instance> --match "<task text>" [--dry] [--fixtures DIR]
      column mode: the at most `per_task` items of the last 4 weeks that fit a task (title and description).
Exit 0 ok, 2 bad command or config.
"""
import argparse, glob, hashlib, html, http.client, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BEHAVIOR = 'tech-radar'
SOURCES = ('radar_deps', 'radar_feeds', 'radar_hn', 'radar_trending')
DEFAULTS = {'project': '', 'column': 'Radar técnico', 'per_task': 3, 'max_hours': 24, 'max_tasks': 8, 'stacks': [],
            'weekday': 0, 'hour': 9, 'window_days': 7}
UA = 'agent-tech-radar/1'
RETRY_WAITS = (1, 2)       # YouTube feeds: waits between the 3 tries (PLN0194)
RETRY_BUDGET = 10          # seconds per channel, tries and waits together
RETRY_TRY_TIMEOUT = 4      # seconds per try, so a hung try still leaves room for the next one
SEEN_DAYS = 120
VOLATILE_QUERY = ('numericFilters',)          # query params left out of the fixture file name (they carry the date)
DATE_IN_QUERY = re.compile(r'\d{4}-\d{2}-\d{2}')   # a date inside a kept query param is DATE in the fixture file name
SKIP_DIRS = {'.git', 'node_modules', 'bin', 'obj', 'dist', 'build', '.venv', 'venv', '__pycache__', '.tox', 'wt',
             '.worktrees', 'vendor', 'fixtures', 'e2e-results', 'test-results', 'playwright-report'}
STOP = set('para como com uma dos das que por sem mais the and for with from this that into your are was were how '
           'what when new novo nova tarefa task tela rota fazer quando pela pelo'.split())


# ---------------------------------------------------------------- config

def options(cfg, behavior=BEHAVIOR, defaults=None):
    """The behavior's options over its defaults. product-radar (product_radar.py) reuses it with its own name."""
    o = dict(DEFAULTS if defaults is None else defaults)
    bc = ((cfg or {}).get('behavior_config') or {}).get(behavior) or {}
    if isinstance(bc, dict): o.update({k: v for k, v in bc.items() if v is not None})
    for k in ('stacks', 'terms'):
        if k in o: o[k] = [s for s in (o.get(k) or []) if isinstance(s, str) and s.strip()]
    return o


def tz(cfg):
    try: return timezone(timedelta(hours=float((cfg or {}).get('tz_hours', -3))))
    except (TypeError, ValueError): return timezone(timedelta(hours=-3))


def week_of(now, cfg):
    y, w, _ = now.astimezone(tz(cfg)).isocalendar()
    return f'{y}-W{w:02d}'


def due(now, cfg, o=None):
    """True once the week's scan time (weekday 0 = Monday, hour) has passed, instance time. `o`: the behavior's
    options (default: tech-radar's)."""
    o, local = o or options(cfg), now.astimezone(tz(cfg))
    start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    try: at = start + timedelta(days=min(6, max(0, int(o['weekday']))), hours=min(23, max(0, int(o['hour']))))
    except (TypeError, ValueError): at = start
    return local >= at


# ---------------------------------------------------------------- fetch (network or fixtures)

def fixture_path(root, url):
    u = urllib.parse.urlsplit(url)
    parts = [p for p in urllib.parse.unquote(u.path).split('/') if p and p not in ('.', '..')]
    q = [(k, DATE_IN_QUERY.sub('DATE', v)) for k, v in urllib.parse.parse_qsl(u.query) if k not in VOLATILE_QUERY]
    name = [re.sub(r'[^A-Za-z0-9._@+-]', '_', p) for p in parts] or ['index']
    if q: name[-1] += '__' + re.sub(r'[^A-Za-z0-9._=-]', '_', '&'.join(f'{k}={v}' for k, v in q))
    return os.path.join(os.path.expanduser(root), re.sub(r'[^A-Za-z0-9.-]', '_', u.hostname or 'local'), *name)


def fetch(url, fixtures=None, timeout=20, missing_ok=True):
    """Bytes of the URL, or None when the fixture does not exist (a lookup that has nothing to say). Network errors
    raise (the source breaks with the reason). A network 404 is None too, unless missing_ok=False."""
    log = os.environ.get('AGENT_RADAR_URL_LOG')
    if log:
        with open(log, 'a', encoding='utf-8') as f: f.write(url + '\n')
    if fixtures:
        p = fixture_path(fixtures, url)
        try:
            with open(p, 'rb') as f: return f.read()
        except OSError:
            return None
    if not url.startswith('https://'): raise ValueError(f'só https ({url[:60]})')
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.read(3_000_000)
    except urllib.error.HTTPError as e:
        if e.code == 404 and missing_ok: return None
        raise


def fetch_retry(url, fixtures=None, waits=RETRY_WAITS, budget=RETRY_BUDGET, sleep=time.sleep, clock=time.monotonic):
    """fetch for a feed that fails for a while before it answers (YouTube RSS gives 404 and 500 for a few tries,
    PLN0194): len(waits) + 1 tries, waiting waits[i] between them, all within `budget` seconds. Here a 404 is a failure
    too (the channel exists; a persistent 404 becomes a warning instead of silence). The last error raises with the
    number of tries. Fixtures answer at once, no retry."""
    if fixtures: return fetch(url, fixtures)
    end, n = clock() + budget, 0
    while True:
        n += 1
        try:
            return fetch(url, timeout=min(RETRY_TRY_TIMEOUT, max(1.0, end - clock())), missing_ok=False)
        except (OSError, http.client.HTTPException) as e:   # HTTPError, URLError, timeout, connection dropped
            left = end - clock()
            if n > len(waits) or left <= waits[n - 1]:
                code = getattr(e, 'code', None)
                raise RuntimeError(f'{f"HTTP {code}" if code else type(e).__name__ + ": " + str(e)[:80]} em {n} '
                                   f'tentativa{"s" if n > 1 else ""}') from e
            sleep(waits[n - 1])


def fetch_json(url, fixtures=None):
    raw = fetch(url, fixtures)
    if raw is None: return None
    try: return json.loads(raw.decode('utf-8', 'replace'))
    except ValueError: return None


# ---------------------------------------------------------------- stacks

def _terms(stack):
    return [t.strip() for t in stack.split('|') if t.strip()]


def _term_re(term):
    return re.compile(r'(?<![\w.#+-])' + re.escape(term) + r'(?![\w#+])', re.I)


def match_stacks(text, stacks):
    """The stacks (their first term) whose term appears in the text. "EF Core|Entity Framework" = alternatives."""
    text = text or ''
    return [_terms(s)[0] for s in stacks if _terms(s) and any(_term_re(t).search(text) for t in _terms(s))]


def clean(text, n=300):
    t = re.sub(r'<[^>]+>', ' ', html.unescape(str(text or '')))
    t = ' '.join(t.split())
    return t if len(t) <= n else t[:n - 1].rstrip() + '…'


def item_id(*parts):
    return ':'.join(str(p) for p in parts)


def short_hash(text):
    return hashlib.sha1(str(text).encode()).hexdigest()[:10]


# ---------------------------------------------------------------- dependencies

def dep_files(repo, max_depth=5):
    root = os.path.expanduser(repo)
    out = []
    for d, dirs, fs in os.walk(root):
        depth = os.path.relpath(d, root).count(os.sep)
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith('.') and depth < max_depth)
        for f in sorted(fs):
            if f in ('package.json', 'Directory.Packages.props', 'pyproject.toml') or f.endswith('.csproj') or \
                    re.fullmatch(r'requirements[\w.-]*\.txt', f):
                out.append(os.path.join(d, f))
    return out


VER_RE = re.compile(r'(\d+)(?:\.(\d+))?(?:\.(\d+))?')


def vtuple(v):
    m = VER_RE.search(str(v or ''))
    return tuple(int(x or 0) for x in m.groups()) if m else None


def parse_deps(path):
    """[(ecosystem, name, version)] declared with a version in the file."""
    f = os.path.basename(path)
    try: text = open(path, encoding='utf-8', errors='replace').read()
    except OSError: return []
    out = []
    if f == 'package.json':
        try: d = json.loads(text)
        except ValueError: return []
        for k in ('dependencies', 'devDependencies'):
            for n, v in (d.get(k) or {}).items():
                if isinstance(v, str) and vtuple(v) and not re.match(r'(file|link|workspace|git|http)', v): out.append(('npm', n, v))
    elif f.endswith('.csproj') or f == 'Directory.Packages.props':
        for m in re.finditer(r'<Package(?:Reference|Version)\b[^>]*?\bInclude="([^"]+)"[^>]*?\bVersion="([^"]+)"', text):
            if vtuple(m.group(2)): out.append(('nuget', m.group(1), m.group(2)))
    elif f.startswith('requirements'):
        for line in text.splitlines():
            m = re.match(r'\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(==|>=|~=)\s*([0-9][^\s;,#]*)', line)
            if m: out.append(('pypi', m.group(1), m.group(3)))
    elif f == 'pyproject.toml':
        block = re.search(r'(?m)^dependencies\s*=\s*\[((?:[^\[\]]|\[[^\]]*\])*)\]', text)
        for m in re.finditer(r'["\']([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(==|>=|~=)\s*([0-9][^"\';,\s]*)',
                             block.group(1) if block else ''):
            out.append(('pypi', m.group(1), m.group(3)))
    return out


REGISTRY = {
    'npm': ('https://registry.npmjs.org/{q}/latest', 'https://www.npmjs.com/package/{n}/v/{v}'),
    'nuget': ('https://api.nuget.org/v3-flatcontainer/{l}/index.json', 'https://www.nuget.org/packages/{n}/{v}#releasenotes-body-tab'),
    'pypi': ('https://pypi.org/pypi/{n}/json', 'https://pypi.org/project/{n}/{v}/'),
}


def latest(eco, name, fixtures=None):
    api = REGISTRY[eco][0].format(q=urllib.parse.quote(name, safe='@'), l=name.lower(), n=name)
    d = fetch_json(api, fixtures)
    if not isinstance(d, dict): return None
    if eco == 'npm': return d.get('version')
    if eco == 'pypi': return (d.get('info') or {}).get('version')
    stable = [v for v in d.get('versions') or [] if isinstance(v, str) and '-' not in v and vtuple(v)]
    return max(stable, key=vtuple) if stable else None


def collect_deps(repos, stacks, fixtures=None, include_patch=False, max_lookups=80, skip=()):
    """Items for the dependencies that have a newer minor or major version. repos = [(label, path)]."""
    found = {}
    for label, path in repos:
        for f in dep_files(path):
            for eco, n, v in parse_deps(f):
                if any(re.fullmatch(p.replace('*', '.*'), n, re.I) for p in skip): continue
                e = found.setdefault((eco, n.lower()), {'eco': eco, 'name': n, 'cur': v, 'repos': []})
                if vtuple(v) < vtuple(e['cur']): e['cur'] = v
                if label not in e['repos']: e['repos'].append(label)
    items = []
    for (eco, _), e in sorted(found.items())[:max_lookups]:
        lat = latest(eco, e['name'], fixtures)
        cur, new = vtuple(e['cur']), vtuple(lat)
        if not new or not cur or new <= cur: continue
        kind = 'major' if new[0] > cur[0] else 'minor' if new[1] > cur[1] else 'patch'
        if kind == 'patch' and not include_patch: continue
        st = match_stacks(e['name'].replace('.', ' ').replace('-', ' ') + ' ' + e['name'], stacks)
        items.append({'id': item_id('dep', eco, e['name'].lower(), f'{new[0]}.{new[1]}' + (f'.{new[2]}' if kind == 'patch' else '')),
                      'kind': 'dep', 'level': kind, 'eco': eco, 'name': e['name'], 'version': lat,
                      'current': e['cur'].lstrip('^~>=v '), 'repos': e['repos'], 'stacks': st,
                      'title': f'{e["name"]} {lat} ({eco}; em uso {e["cur"].lstrip("^~>=v ")})',
                      'url': REGISTRY[eco][1].format(n=e['name'], v=lat), 'summary': '',
                      'score': (6 if kind == 'major' else 3 if kind == 'minor' else 1) + 2 * len(st)})
    return items


# ---------------------------------------------------------------- feeds (RSS 2.0 and Atom)

def _local(tag):
    return tag.rsplit('}', 1)[-1]


def _date(text):
    t = (text or '').strip()
    if not t: return None
    try:
        d = parsedate_to_datetime(t)
    except (TypeError, ValueError):
        try: d = datetime.fromisoformat(t.replace('Z', '+00:00'))
        except ValueError: return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(raw):
    """[{title, url, summary, at}] of an RSS or Atom document."""
    try: root = ET.fromstring(raw)
    except ET.ParseError: return []
    out = []
    for el in root.iter():
        if _local(el.tag) not in ('item', 'entry'): continue
        kids = {}
        for c in el:
            name = _local(c.tag)
            if name == 'link':
                href = c.get('href')
                if href and c.get('rel', 'alternate') == 'alternate': kids.setdefault('link', href)
                elif (c.text or '').strip(): kids.setdefault('link', c.text.strip())
            elif name == 'group':                                    # YouTube media:group
                for g in c:
                    if _local(g.tag) == 'description': kids.setdefault('summary', g.text or '')
            else:
                kids.setdefault(name, c.text or '')
        out.append({'title': clean(kids.get('title'), 200), 'url': kids.get('link') or '',
                    'summary': clean(kids.get('description') or kids.get('summary') or kids.get('content'), 300),
                    'at': _date(kids.get('pubDate') or kids.get('published') or kids.get('updated') or kids.get('date'))})
    return out


def collect_feeds(feeds, stacks, now, window_days=7, fixtures=None, errors=None, sleep=time.sleep, clock=time.monotonic):
    """Items of the feeds; a feed that fails adds one line to `errors`. YouTube feeds are tried again (fetch_retry)."""
    items = []
    for fd in feeds:
        try:
            if fd.get('kind') == 'youtube': raw = fetch_retry(fd['url'], fixtures, sleep=sleep, clock=clock)
            else: raw = fetch(fd['url'], fixtures)
        except RuntimeError as e:
            if errors is not None: errors.append(f'{fd["name"]}: {str(e)[:100]}')
            continue
        except Exception as e:
            if errors is not None: errors.append(f'{fd["name"]}: {type(e).__name__}: {str(e)[:100]}')
            continue
        for x in parse_feed(raw or b''):
            if not x['title'] or not x['url']: continue
            if x['at'] and x['at'] < now - timedelta(days=window_days): continue
            st = match_stacks(x['title'] + ' ' + x['summary'], stacks) or list(fd.get('stacks') or [])
            if fd.get('require_match') and not match_stacks(x['title'] + ' ' + x['summary'], stacks): continue
            items.append({'id': item_id('feed', short_hash(x['url'])), 'kind': fd.get('kind') or 'blog', 'title': x['title'],
                          'url': x['url'], 'summary': x['summary'], 'stacks': st, 'source': fd['name'],
                          'at': x['at'].isoformat() if x['at'] else None, 'score': 2 + 2 * len(st)})
    return items


# ---------------------------------------------------------------- Hacker News (Algolia) and GitHub Trending

HN_API = 'https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=200&numericFilters=created_at_i>{ts}'


def collect_hn(stacks, now, window_days=7, min_points=100, fixtures=None):
    d = fetch_json(HN_API.format(ts=int((now - timedelta(days=window_days)).timestamp())), fixtures) or {}
    items = []
    for h in d.get('hits') or []:
        title, pts = clean(h.get('title'), 200), int(h.get('points') or 0)
        st = match_stacks(title, stacks)
        if not title or not st or pts < min_points: continue
        hn = f'https://news.ycombinator.com/item?id={h.get("objectID")}'
        items.append({'id': item_id('hn', h.get('objectID')), 'kind': 'hn', 'title': title, 'url': h.get('url') or hn,
                      'discussion': hn, 'summary': f'{pts} pontos no Hacker News', 'stacks': st, 'points': pts,
                      'score': 1 + min(5, pts // 100) + 2 * len(st)})
    return items


TRENDING = 'https://github.com/trending/{lang}?since=weekly'


def parse_trending(raw):
    out = []
    text = (raw or b'').decode('utf-8', 'replace')
    for art in re.findall(r'(?s)<article\b.*?</article>', text):
        m = re.search(r'(?s)<h2\b.*?<a\b[^>]*href="/([^"/\s]+/[^"/\s]+)"', art)
        if not m: continue
        d = re.search(r'(?s)<p\b[^>]*>(.*?)</p>', art)
        s = re.search(r'([\d,]+)\s+stars\s+this\s+week', art)
        out.append({'repo': m.group(1), 'summary': clean(d.group(1) if d else '', 300),
                    'stars': int(s.group(1).replace(',', '')) if s else 0})
    return out


def collect_trending(languages, stacks, fixtures=None, min_stars=0, errors=None):
    items, seen = [], set()
    for lang in languages:
        try: raw = fetch(TRENDING.format(lang=urllib.parse.quote(lang.lower(), safe='')), fixtures)
        except Exception as e:
            if errors is not None: errors.append(f'{lang}: {type(e).__name__}: {str(e)[:100]}')
            continue
        for r in parse_trending(raw):
            st = match_stacks(r['repo'].replace('/', ' ').replace('-', ' ') + ' ' + r['summary'], stacks)
            if not st or r['stars'] < min_stars or r['repo'] in seen: continue
            seen.add(r['repo'])
            items.append({'id': item_id('gh', r['repo'].lower()), 'kind': 'trending', 'title': r['repo'],
                          'url': f'https://github.com/{r["repo"]}', 'summary': r['summary'], 'stacks': st,
                          'stars': r['stars'], 'score': 1 + min(4, r['stars'] // 500) + 2 * len(st)})
    return items


# ---------------------------------------------------------------- state shared by the sources

def safe(items):
    """Drops an item whose text looks like a secret (the backlog plan would be refused as a whole)."""
    try:
        from watch_core import planou as P
        return [i for i in items if not any(P.looks_secret(i.get(k) or '') for k in ('title', 'summary', 'url'))]
    except Exception:
        return items


def radar_state(s):
    r = s.setdefault('radar', {})
    r.setdefault('seen', {}); r.setdefault('weeks', {})
    return r


def gate_and_mark(src, s, items, dry, now, o=None, folder='radar'):
    """The new items of a source tick. Not dry: once per week after the scan time; marks them seen and saves them in
    data/<folder>/<week>.json. Returns None when it is not time yet. `o` and `folder`: another behavior's options and
    folder (product-radar: data/product-radar)."""
    cfg = src.ctx.cfg if src.ctx else {}
    r = radar_state(s)
    week = week_of(now, cfg)
    fresh = [i for i in safe(items()) if i['id'] not in r['seen']] if dry else None
    if dry: return {'week': week, 'items': fresh, 'dry': True}
    if r['weeks'].get(src.nome) == week or not due(now, cfg, o): return None
    new = [i for i in safe(items()) if i['id'] not in r['seen']]
    cut = (now - timedelta(days=SEEN_DAYS)).isoformat()
    r['seen'] = {k: v for k, v in r['seen'].items() if v >= cut}
    for i in new: r['seen'][i['id']] = now.isoformat()
    r['weeks'][src.nome] = week
    save_inbox(week, src.nome, new, folder)
    return {'week': week, 'items': new}


def inbox_dir(folder='radar'):
    import paths
    return os.path.join(paths.DATA_DIR, folder)


def save_inbox(week, source, items, folder='radar'):
    from watch_core import fileio
    p = os.path.join(inbox_dir(folder), f'{week}.json')
    try: d = json.load(open(p, encoding='utf-8'))
    except (OSError, ValueError): d = {}
    d[source] = items
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fileio.write_json(p, d, ensure_ascii=False, indent=1)


def load_inbox(weeks=None):
    out = []
    files = sorted(glob.glob(os.path.join(inbox_dir(), '20*-W*.json')))
    for p in files[-(weeks or 1):] if weeks else files[-1:]:
        try: d = json.load(open(p, encoding='utf-8'))
        except (OSError, ValueError): continue
        for v in d.values(): out += [i for i in v if isinstance(i, dict)]
    return out


LABEL = {'dep': 'dependência', 'blog': 'blog', 'youtube': 'vídeo', 'hn': 'Hacker News', 'trending': 'GitHub Trending',
         'tarefas': 'app de tarefas', 'agentes': 'agentes', 'descoberta': 'descoberta', 'github': 'GitHub'}


def line(i):
    lv = f' {i["level"]}' if i.get('level') else ''
    st = f' [{", ".join(i["stacks"])}]' if i.get('stacks') else ''
    where = f' em {", ".join(i["repos"])}' if i.get('repos') else (f' ({i["source"]})' if i.get('source') else '')
    return f'  [{LABEL.get(i["kind"], i["kind"])}{lv}]{st} {i["title"]}{where}: {i["url"]}'


def print_block(src, r, header='RADAR'):
    if not r or not r.get('items'): return False
    print(f'== {header} {src.nome} {r["week"]} ({len(r["items"])} nova{"s" if len(r["items"]) != 1 else ""}'
          + (', simulado' if r.get('dry') else '') + ')')
    for i in sorted(r['items'], key=lambda x: -x['score'])[:15]: print(line(i))
    if len(r['items']) > 15: print(f'  ... e mais {len(r["items"]) - 15}')
    return True


# ---------------------------------------------------------------- ranking, plan, match

def rank(items):
    by = {}
    for i in items:
        if i['id'] not in by or i['score'] > by[i['id']]['score']: by[i['id']] = i
    return sorted(by.values(), key=lambda i: (-i['score'], i['title']))


def _code(i, used):
    base = re.sub(r'[^a-z0-9]+', '-', f'{i["kind"]}-{i["title"]}'.lower()).strip('-')[:32].strip('-') or 'item'
    c, n = base, 2
    while c in used: c, n = f'{base[:29]}-{n}', n + 1
    used.add(c)
    return c


def _task(i, used):
    if i['kind'] == 'dep':
        title = f'Avaliar {i["name"]} {i["version"]} (em uso {i["current"]})'
        what = (f'Versão {i["level"]} nova de {i["name"]} ({i["eco"]}), usada em {", ".join(i["repos"])}: ler as notas '
                f'de versão ({i["url"]}) e ver o que muda para o projeto.')
        why = 'Dependência direta com versão nova: ' + ('pode ter quebra de compatibilidade.' if i['level'] == 'major'
                                                        else 'correções e novidades sem quebra, em tese.')
    else:
        title = f'Avaliar: {i["title"]}'
        what = f'{LABEL.get(i["kind"], i["kind"]).capitalize()}: {i["title"]}. ' + (f'{i["summary"]}. ' if i.get('summary') else '') + f'Link: {i["url"]}'
        why = f'Novidade da stack {", ".join(i["stacks"])}.' if i.get('stacks') else 'Novidade de um canal acompanhado pelo radar.'
    if len(title) > 80: title = title[:79].rstrip() + '…'
    return {'code': _code(i, used), 'title': title, 'what': what, 'why': why,
            'done_when': ['notas lidas e a decisão anotada na tarefa: adotar, esperar ou ignorar'],
            'priority': 'P3' if i.get('level') == 'major' or i['score'] >= 8 else 'P4'}


def build_plan(items, cfg, week):
    o = options(cfg)
    ranked = rank(items)[:max(1, int(o['max_tasks']))]
    used = set()
    plan = {'goal': {'ref': f'radar-{week}', 'title': f'Radar técnico da semana {week}'}, 'tasks': [_task(i, used) for i in ranked]}
    if o.get('project'): plan['project'] = o['project']
    return plan, ranked


def words(text):
    return {w for w in re.findall(r'[a-zà-ú0-9#+.]{4,}', (text or '').lower()) if w not in STOP}


def match(items, text, cfg):
    """At most per_task items that fit the task text: shared stacks weigh most, then shared words."""
    o = options(cfg)
    want, ws = set(match_stacks(text, o['stacks'])), words(text)
    out = []
    for i in rank(items):
        hit = len(want & set(i.get('stacks') or [])) * 5 + len(ws & words(f'{i["title"]} {i.get("summary") or ""} {i.get("name") or ""}')) * 2
        if hit: out.append((hit + i['score'] / 10, i))
    out.sort(key=lambda x: -x[0])
    return [i for _, i in out[:max(1, min(3, int(o['per_task'])))]]


# ---------------------------------------------------------------- CLI

class _Ctx:
    def __init__(self, cfg):
        self.cfg, self.s, self.dry, self.fontes = cfg, {}, True, {}


def collect_now(cfg, fixtures=None, now=None):
    """Runs the instance's radar sources in dry mode (nothing is written): (items, errors)."""
    import adapters
    now = now or datetime.now(timezone.utc)
    ctx, items, errors = _Ctx(cfg), [], []
    for e in cfg.get('sources') or []:
        if (e.get('type') or e.get('tipo')) not in SOURCES: continue
        e = dict(e)
        if fixtures: e['fixtures'] = fixtures
        src = adapters.instancia(e, 'Fonte', ctx)
        try:
            r = src.tick({}, None, True)
            items += (r or {}).get('items') or []
            errors += (r or {}).get('errors') or []
        except (Exception, SystemExit) as ex:
            errors.append(f'{src.nome}: {getattr(ex, "code", None) or ex}')
    return items, errors


def main(argv=None):
    ap = argparse.ArgumentParser(prog='radar.py', description='Tech radar: the week\'s news of the stacks become suggestions.')
    ap.add_argument('instance')
    ap.add_argument('--dry', action='store_true', help='collect now, in memory, and write nothing')
    ap.add_argument('--fixtures', help='answer every URL from this folder (tests, offline check)')
    ap.add_argument('--week', help='the week of the plan (default: the current one)')
    ap.add_argument('--match', metavar='TEXT', help="column mode: the items that fit a task ('-' reads stdin)")
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
    now = datetime.now(timezone.utc)
    week = a.week or week_of(now, cfg)
    if a.fixtures: print(f'FIXTURES: {a.fixtures} (dados de teste, sem rede)')
    errors = []
    if a.dry: items, errors = collect_now(cfg, a.fixtures, now)
    else: items = load_inbox(4 if a.match is not None else None) if not a.week else _week_items(week)
    for e in errors: print(f'AVISO (radar): {e}')
    if a.match is not None:
        text = sys.stdin.read() if a.match == '-' else a.match
        hits = match(items, text, cfg)
        print(f'RADAR PARA A TAREFA: {len(hits)} de {len(items)} itens' + ('' if items else
              ' (sem varredura guardada: rode com --dry, ou espere o tick da semana)'))
        for n, i in enumerate(hits, 1): print(f'{n}.' + line(i)[1:])
        return 0
    if not items:
        print(f'RADAR {week}: nada novo' + ('' if a.dry else ' guardado (a varredura da semana ainda não rodou?)'))
        return 0
    plan, ranked = build_plan(items, cfg, week)
    print(f'RADAR {week}: {len(items)} itens novos, {len(ranked)} sugestões' + (' (simulado, nada gravado)' if a.dry else ''))
    for i in ranked: print(line(i))
    target = None
    if not a.dry:
        from watch_core import fileio
        target = os.path.join(inbox_dir(), f'plan-{week}.json')
        fileio.write_json(target, plan, ensure_ascii=False, indent=1)
        print(f'PLANO: {target}')
    import backlog
    try:
        rc, lines = backlog.run(a.instance, plan, dry=True, max_tasks=len(plan['tasks']))
        print('PREVIA (backlog.py --dry):'); print('\n'.join(lines))
    except backlog.PlanError as e:
        print('AVISO (radar): o plano não passa no backlog.py: ' + '; '.join(str(e).splitlines()))
    if target:
        print(f'PUBLICAR: python3 {os.path.join(paths.SCRIPTS, "backlog.py")} {a.instance} {target} --max {len(plan["tasks"])}')
    return 0


def _week_items(week):
    try: d = json.load(open(os.path.join(inbox_dir(), f'{week}.json'), encoding='utf-8'))
    except (OSError, ValueError): return []
    return [i for v in d.values() for i in v if isinstance(i, dict)]


if __name__ == '__main__':
    sys.exit(main())
