"""The context pack: what the model knows about the user's work, built once at start (read-only) and kept compact so
each card call stays fast. Every item carries an id that the cards cite under "based on".

  context            the workspace CONTEXT.md (people, priorities, open decisions)
  handoff            the workspace handoff.md (what was done lately, what is pending)
  memory:<slug>      the memory index (one line per memory); the full text of the few that match a question goes
                     into that question's call (retrieve())
  ata:<folder>       the latest meeting minutes of the instance (only those before the meeting, so a replay never
                     sees its own minutes)
  <repo>#<n>         the user's open pull requests (gh, the account already logged in), and the PRs opened, updated
                     or merged in the last 14 days with their changed files and the text of the schema changes in them
                     (migrations, .sql, ORM entities/models), so "which table, which migration" has a concrete answer
  <KEY-n>            the user's open issues (Jira REST v3 with the instance's token file; the token is never printed)
"""
import base64, glob, hashlib, json, os, re, subprocess, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from . import config

STOP = set('''that this with from have what where when which there their about would could should into your they them
been were will just like does done also than then some more only very over here make made need want into after before
because while being other these those much many most such each even still yeah okay right think know mean said says
going sure well really thing things something'''.split())


def _read(p, limit=None):
    try:
        with open(p, encoding='utf-8') as f: s = f.read()
    except OSError:
        return ''
    return s[:limit] if limit else s


def words(s):
    return {w for w in re.findall(r'[a-zà-ú0-9][a-zà-ú0-9_\-]{3,}', s.lower()) if w not in STOP}


def memory_items(dirs):
    """[(slug, title_line, path)] from MEMORY.md indexes (or the *.md files when there is no index)."""
    seen, out = set(), []
    for d in dirs:
        d = os.path.expanduser(d)
        idx = _read(os.path.join(d, 'MEMORY.md'))
        for m in re.finditer(r'^- \[(.+?)\]\((.+?\.md)\)\s*[—-]*\s*(.*)$', idx, re.M):
            slug = m.group(2)[:-3]
            if slug in seen: continue
            seen.add(slug); out.append((slug, f'{m.group(1)}: {m.group(3)}', os.path.join(d, m.group(2))))
    return out


def recent_minutes(ws, before=None, n=3):
    out = []
    for p in sorted(glob.glob(os.path.join(ws, 'meetings', '*', 'minutes.md')), reverse=True):
        folder = os.path.basename(os.path.dirname(p))
        m = re.match(r'(\d{4}-\d{2}-\d{2})-(\d{4})', folder)
        # minutes folders carry the start minute only; anything starting less than 10 min before the replayed
        # recording is that same meeting
        if before and m and datetime.strptime(m.group(1) + m.group(2), '%Y-%m-%d%H%M') > before - timedelta(minutes=10): continue
        out.append((folder, p))
        if len(out) >= n: break
    return out


def _gh_prs(org, before=None):
    """Open PRs of the logged-in gh account (the search API: works on old gh versions too)."""
    q = 'is:pr is:open author:@me' + (f' org:{org}' if org else '')
    if before: q += ' created:<' + before.astimezone().isoformat(timespec='seconds')   # a replay sees only what existed then
    try:
        r = subprocess.run(['gh', 'api', '-X', 'GET', 'search/issues', '-f', f'q={q}', '-f', 'per_page=40'],
                           capture_output=True, text=True, timeout=25)
        d = json.loads(r.stdout or '{}')
        if r.returncode or 'items' not in d: return None
        items = d['items']
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return [{'repo': i['repository_url'].rsplit('/', 1)[-1], 'number': i['number'], 'title': i['title'],
             'updated': i['updated_at'][:10], 'draft': i.get('draft', False)} for i in items]


SCHEMA_RE = re.compile(r'(^|/)(db/migration|migrations?|alembic|drizzle|flyway|liquibase)/|\.sql$|'
                       r'(^|/)(entity|entities|schema|schemas)/(.+/)?[^/]+\.(java|kt|py|ts)$|'
                       r'(Entity|Model)\.(java|kt)$|(^|/)(schema|models)\.(ts|py|prisma)$|\.prisma$', re.I)
SCHEMA_FILE_CHARS, SCHEMA_PR_CHARS, SCHEMA_TOTAL_CHARS = 2500, 5000, 18000
PR_QUERY = """query($q:String!){search(query:$q,type:ISSUE,first:60){nodes{... on PullRequest{number title state createdAt
mergedAt closedAt updatedAt additions deletions repository{name owner{login}} files(first:100){nodes{path additions deletions changeType}}}}}}"""


def is_schema(path):
    return bool(SCHEMA_RE.search(path)) and '/test/' not in path and not path.startswith('test')


def _short(path):
    return re.sub(r'^src/(main|test)/(java|kotlin|resources)/(\w+/){0,3}', r'\1:', path)


def _touched(files):
    """Compact 'what it touched': the directories that changed, most files first."""
    c = Counter()
    for f in files:
        d = os.path.dirname(f['path'])
        d = re.sub(r'^src/(main|test)/(java|kotlin|resources)/(\w+/){0,3}', r'\1:', d)   # the Java package prefix says nothing
        c[d or '.'] += 1
    return ', '.join(f'{d} ({n})' if n > 1 else d for d, n in c.most_common(6)) + (' ...' if len(c) > 6 else '')


def _patch_text(patch, status):
    """An added file is its full text; a changed one keeps only its diff hunks."""
    if not patch: return ''
    if status == 'added':
        return '\n'.join(l[1:] for l in patch.splitlines() if l.startswith('+'))
    return '\n'.join(l for l in patch.splitlines() if l[:1] in '+-@')


def _gh_schema(owner, repo, number):
    try:
        r = subprocess.run(['gh', 'api', f'repos/{owner}/{repo}/pulls/{number}/files?per_page=100'],
                           capture_output=True, text=True, timeout=25)
        files = json.loads(r.stdout or '[]')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    out = []
    for f in files if isinstance(files, list) else []:
        if is_schema(f.get('filename', '')) and f.get('status') != 'removed':
            t = _patch_text(f.get('patch'), f.get('status'))
            if t: out.append((f['filename'], f.get('status'), t[:SCHEMA_FILE_CHARS] + (' ...(cut)' if len(t) > SCHEMA_FILE_CHARS else '')))
    return out


def _gh(path, timeout=25):
    try:
        r = subprocess.run(['gh', 'api', path], capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout or 'null')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _schema_of(files):
    """[(path, status, text)] of the schema files in a GitHub file list (pulls/n/files or compare)."""
    out = []
    for f in files or []:
        if is_schema(f.get('filename', '')) and f.get('status') != 'removed':
            t = _patch_text(f.get('patch'), f.get('status'))
            if t: out.append((f['filename'], f.get('status'), t[:SCHEMA_FILE_CHARS] + (' ...(cut)' if len(t) > SCHEMA_FILE_CHARS else '')))
    return out


def _gh_schema(owner, repo, number):
    files = _gh(f'repos/{owner}/{repo}/pulls/{number}/files?per_page=100')
    return _schema_of(files if isinstance(files, list) else [])


def _gh_as_of(owner, repo, number, before):
    """A PR as it was at `before` (replay): its commits up to then, compared from the parent of the first one.
    -> (files, schema) or None when it had no commit yet."""
    commits = _gh(f'repos/{owner}/{repo}/pulls/{number}/commits?per_page=100')
    if not isinstance(commits, list): return None
    def when(c): return datetime.fromisoformat(c['commit']['committer']['date'].replace('Z', '+00:00'))
    upto = [c for c in commits if when(c) < before]
    if not upto: return None
    base = (commits[0].get('parents') or [{}])[0].get('sha')
    if not base: return None
    cmp = _gh(f'repos/{owner}/{repo}/compare/{base}...{upto[-1]["sha"]}')
    if not isinstance(cmp, dict): return None
    raw = cmp.get('files') or []
    files = [{'path': f['filename'], 'additions': f.get('additions', 0), 'deletions': f.get('deletions', 0)} for f in raw]
    return files, _schema_of(raw)


def _gh_recent(org, author, before=None, days=14, cache=None):
    """The user's PRs opened, updated or merged in the last `days` (merged too), with changed files and schema text.
    In a replay only what existed at the recording: created before it, a PR merged later shows as open, and a PR that
    changed after the recording shows its files as of its last commit before it.
    cache: {'<owner>/<repo>#<n>@<updatedAt>': schema} kept by the ready pack, so a tick refetches only changed PRs."""
    now = before.astimezone() if before else datetime.now().astimezone()
    q = f'is:pr author:{author or "@me"} updated:>=' + (now - timedelta(days=days)).date().isoformat() + (f' org:{org}' if org else '')
    try:
        r = subprocess.run(['gh', 'api', 'graphql', '-f', 'query=' + PR_QUERY, '-f', f'q={q}'], capture_output=True, text=True, timeout=30)
        nodes = json.loads(r.stdout or '{}').get('data', {}).get('search', {}).get('nodes', [])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None

    def at(s): return datetime.fromisoformat(s.replace('Z', '+00:00')) if s else None
    prs = []
    for n in nodes:
        if not n.get('number'): continue
        created, merged, closed, updated = at(n['createdAt']), at(n.get('mergedAt')), at(n.get('closedAt')), at(n.get('updatedAt'))
        if before and created >= now: continue
        state = n['state']
        if before and state != 'OPEN':
            if (merged or closed) and (merged or closed) >= now: state = 'OPEN'
        if state == 'CLOSED': continue                     # abandoned: nothing to say about it
        files = n['files']['nodes']
        prs.append({'owner': n['repository']['owner']['login'], 'repo': n['repository']['name'], 'number': n['number'],
                    'title': n['title'], 'state': state.lower(), 'when': (merged if state == 'MERGED' else created).date().isoformat(),
                    'updated': n.get('updatedAt') or '', 'files': files, 'add': n.get('additions', 0), 'del': n.get('deletions', 0),
                    'schema': [], 'has_schema': any(is_schema(f['path']) for f in files),
                    'as_of': bool(before and updated and updated >= now)})
    if before:
        late = [p for p in prs if p['as_of']]
        with ThreadPoolExecutor(6) as ex:
            got = list(ex.map(lambda p: _gh_as_of(p['owner'], p['repo'], p['number'], now), late))
        for p, g in zip(late, got):
            if g is None: p['drop'] = True; continue
            p['files'], p['schema'] = g; p['has_schema'] = False
            p['add'] = sum(f['additions'] for f in g[0]); p['del'] = sum(f['deletions'] for f in g[0])
        prs = [p for p in prs if not p.get('drop')]
    key = lambda p: f"{p['owner']}/{p['repo']}#{p['number']}@{p['updated']}"
    todo = [p for p in prs if p['has_schema'] and not (cache is not None and key(p) in cache)]
    with ThreadPoolExecutor(6) as ex:
        for p, sch in zip(todo, ex.map(lambda p: _gh_schema(p['owner'], p['repo'], p['number']), todo)): p['schema'] = sch
    if cache is not None:
        for p in prs:
            if p['has_schema'] and key(p) in cache and p not in todo: p['schema'] = [tuple(x) for x in cache[key(p)]]
        cache.clear(); cache.update({key(p): p['schema'] for p in prs if p['has_schema']})
    for p in prs: p['fetched'] = p in todo
    return prs


Q = '"`'
NOT_COL = {'constraint', 'primary', 'foreign', 'unique', 'check', 'index', 'exclude'}


def _added_lines(text, status):
    """The text a schema file adds: a new file as is, a diff only its + lines."""
    if status == 'added': return text
    return '\n'.join(l[1:] for l in text.splitlines() if l.startswith('+') and not l.startswith('+++'))


def sql_facts(sql):
    """Counted facts of a SQL migration, so the model never counts: tables with their added/dropped columns, new
    tables, indexes and enum values. -> 'contract +6 columns (a, b, ...); project +1 (x); no index' or ''."""
    body = re.sub(r'--[^\n]*', '', sql)
    body = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
    add, drop, new, idx, enum, order = {}, {}, {}, [], [], []

    def tab(t):
        t = t.strip('"`').split('.')[-1].strip('"`')
        if t not in order: order.append(t)
        return t
    for st in body.split(';'):
        x = ' '.join(st.split())
        m = re.match(r'(?i)create\s+table\s+(?:if\s+not\s+exists\s+)?([\w."`]+)\s*\((.*)\)', x)
        if m:
            depth, cur, cols = 0, '', []
            for ch in m.group(2) + ',':
                if ch == '(': depth += 1
                elif ch == ')': depth -= 1
                if ch == ',' and depth == 0: cols.append(cur.strip()); cur = ''
                else: cur += ch
            names = [c.split()[0].strip('"`') for c in cols if c and c.split()[0].lower() not in NOT_COL]
            new[tab(m.group(1))] = names; continue
        m = re.match(r'(?i)alter\s+table\s+(?:only\s+)?(?:if\s+exists\s+)?([\w."`]+)\s+(.*)', x)
        if m:
            t = tab(m.group(1))
            for clause in re.split(r',(?![^()]*\))', m.group(2)):
                c = clause.strip()
                a = re.match(r'(?i)add\s+(?:column\s+)?(?:if\s+not\s+exists\s+)?([\w"`]+)', c)
                if a and a.group(1).lower() not in NOT_COL | {'column'}:
                    add.setdefault(t, []).append(a.group(1).strip('"`')); continue
                d = re.match(r'(?i)drop\s+(?:column\s+)?(?:if\s+exists\s+)?([\w"`]+)', c)
                if d and d.group(1).lower() not in NOT_COL | {'column'}:
                    drop.setdefault(t, []).append(d.group(1).strip('"`'))
            continue
        m = re.match(r'(?i)create\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?(?:[\w"`]+\s+)?on\s+(?:only\s+)?([\w."`]+)\s*(?:using\s+\w+\s*)?\(([^)]*)\)', x)
        if m: idx.append(f'{tab(m.group(1))}({m.group(2).strip()})'); continue
        m = re.match(r"(?i)alter\s+type\s+([\w.\"`]+)\s+add\s+value\s+(?:if\s+not\s+exists\s+)?'([^']+)'", x)
        if m: enum.append(f"enum {m.group(1).strip(Q)} +'{m.group(2)}'"); continue
        m = re.match(r"(?i)create\s+type\s+([\w.\"`]+)\s+as\s+enum\s*\((.*)\)", x)
        if m: enum.append(f"new enum {m.group(1).strip(Q)} ({m.group(2).strip()})")
    out = []
    for t in order:
        if t in new: out.append(f'{t} new table, {len(new[t])} columns ({", ".join(new[t])})')
        if t in add:
            n = len(add[t]); out.append(f'{t} +{n} column{"s" if n > 1 else ""} ({", ".join(add[t])})')
        if t in drop:
            n = len(drop[t]); out.append(f'{t} -{n} column{"s" if n > 1 else ""} ({", ".join(drop[t])})')
    if not out and not idx and not enum: return ''
    out += enum
    out.append('indexes: ' + ', '.join(idx) if idx else 'no index')
    return '; '.join(out)


def entity_facts(text):
    """Fields an ORM entity diff adds or removes (Java/Kotlin): 'Contract +2 fields (a, b)'."""
    added = re.findall(r'^\+\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:private|protected|public)\s+[\w<>,.? \[\]]+?\s+(\w+)\s*(?:=[^;]*)?;', text, re.M)
    gone = re.findall(r'^-\s*(?:private|protected|public)\s+[\w<>,.? \[\]]+?\s+(\w+)\s*(?:=[^;]*)?;', text, re.M)
    out = []
    if added: out.append(f'+{len(added)} field{"s" if len(added) > 1 else ""} ({", ".join(added)})')
    if gone: out.append(f'-{len(gone)} field{"s" if len(gone) > 1 else ""} ({", ".join(gone)})')
    return '; '.join(out)


def schema_facts(pr_ref, schema):
    """One counted line per migration or entity file of a PR."""
    lines = []
    for path, status, text in schema:
        base = os.path.basename(path)
        if path.lower().endswith('.sql'):
            f = sql_facts(_added_lines(text, status))
            if f: lines.append(f'{base.split("__")[0] if "__" in base else base} ({pr_ref}): {f}')
        elif re.search(r'\.(java|kt)$', path):
            src = text if status != 'added' else '\n'.join('+' + l for l in text.splitlines())
            f = entity_facts(src)
            if f: lines.append(f'{os.path.splitext(base)[0]} ({pr_ref}): {f}')
    return lines


def recent_prs_text(prs):
    """The PR section of the pack: one block per PR, schema text inside a total budget (newest first)."""
    out, budget = [], SCHEMA_TOTAL_CHARS
    for p in prs:
        paths = [f['path'] for f in p['files']]
        shown = [_short(x) for x in paths if '/test/' not in x][:10]
        head = (f"#### {p['repo']}#{p['number']} [{p['state']} {p['when']}] {p['title']}\n"
                f"touched: {_touched(p['files'])}; +{p['add']} -{p['del']} in {len(paths)} files\n"
                f"files: {', '.join(shown)}{' ...' if len(paths) > len(shown) else ''}")
        facts = schema_facts(f"{p['repo']}#{p['number']}", p['schema'])
        body = [('schema facts (counted from the files; use these for any count or list):\n' + '\n'.join('- ' + f for f in facts))] if facts else []
        for path, status, text in p['schema']:
            if budget <= 0: body.append(f'(schema change in {path} not shown: context budget)'); continue
            t = text[:min(SCHEMA_PR_CHARS, budget)]
            budget -= len(t)
            body.append(f'schema {"(new file)" if status == "added" else "(diff)"} {path}:\n```\n{t}\n```')
        out.append('\n'.join([head] + body))
    return '\n\n'.join(out)


def _jira(src):
    tf = src.get('token_file')
    if not tf or not os.path.exists(tf): return []
    lines = _read(tf).splitlines()
    try:
        email = lines[int(src.get('linha_email', 3)) - 1].strip(); tok = lines[int(src.get('linha_token', 5)) - 1].strip()
    except (IndexError, ValueError):
        return []
    body = json.dumps({'jql': 'assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC',
                       'fields': ['summary', 'status', 'parent'], 'maxResults': 40}).encode()
    req = urllib.request.Request(src['site'].rstrip('/') + '/rest/api/3/search/jql', data=body, method='POST',
                                 headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                                          'Authorization': 'Basic ' + base64.b64encode(f'{email}:{tok}'.encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=20) as r: d = json.load(r)
    except Exception:
        return None                                       # failed: the ready pack keeps what it had
    return [{'key': i['key'], 'summary': i['fields'].get('summary', ''), 'status': (i['fields'].get('status') or {}).get('name', ''),
             'parent': ((i['fields'].get('parent') or {}).get('key') or '')} for i in d.get('issues', [])]


class Pack:
    def __init__(self, instance, before=None, online=True):
        self.instance, self.before, self.online = instance, before, online
        self.user, self.aliases = config.user()
        self.ws = config.workspace(instance)
        extra = config.instance_extra(instance)
        dirs = ([os.path.join(self.ws, 'memory')] if self.ws else []) + list(extra.get('memory_dirs') or [])
        self.memories = memory_items(dirs)
        if before:   # a replay leaves out the memories written after the meeting
            self.memories = [m for m in self.memories if not os.path.exists(m[2]) or datetime.fromtimestamp(os.path.getmtime(m[2])) < before]
        self.extra_files = extra.get('extra_files') or []
        self.asr_terms = extra.get('asr_terms') or []
        self.sources = []          # [{'id', 'kind', 'label'}] for the UI
        self.text = ''
        self.prs_fetched = 0

    def _fp(self, *paths):
        out = []
        for p in paths:
            try: st = os.stat(p); out.append(f'{p}:{st.st_mtime_ns}:{st.st_size}')
            except OSError: out.append(f'{p}:-')
        return '|'.join(out)

    def parts(self, prev=None, pr_cache=None):
        """The pack in parts, in a fixed order: {name: {'fp', 'text', 'src': [[id, kind, label]], 'ok'}} and the names
        that changed against `prev`. A local part is rebuilt only when its files changed (mtime and size); an online
        part is fetched and rebuilt when the answer changed; a failed fetch keeps the previous text (ok False)."""
        prev = prev or {}
        out, changed = {}, []

        def put(name, fp, make, ok=True):
            old = prev.get(name)
            if old and old.get('fp') == fp:
                out[name] = dict(old, ok=ok); return
            text, src = make()
            out[name] = {'fp': fp, 'text': text, 'src': src, 'ok': ok}; changed.append(name)

        def keep(name, err):
            if prev.get(name): out[name] = dict(prev[name], ok=False, err=err)
            else: out[name] = {'fp': '', 'text': '', 'src': [], 'ok': False, 'err': err}

        ws = self.ws or ''
        ctx_p, ho_p = os.path.join(ws, 'CONTEXT.md'), os.path.join(ws, 'handoff.md')
        put('context', self._fp(ctx_p), lambda: (('### [context] Workspace context\n' + _read(ctx_p, 6000), [['context', 'file', 'CONTEXT.md']])
                                                  if ws and _read(ctx_p, 10) else ('', [])))
        put('handoff', self._fp(ho_p), lambda: (('### [handoff] Recent work and pending items\n' + _read(ho_p, 5000), [['handoff', 'file', 'handoff.md']])
                                                 if ws and _read(ho_p, 10) else ('', [])))
        ex = [os.path.expanduser(p) for p in self.extra_files]

        def extra():
            t, src = [], []
            for p in ex:
                x = _read(p, 5000)
                if x: sid = 'file:' + os.path.basename(p); t.append(f'### [{sid}]\n{x}'); src.append([sid, 'file', os.path.basename(p)])
            return '\n\n'.join(t), src
        put('extra', self._fp(*ex), extra)
        idx = sorted({os.path.join(os.path.dirname(m[2]), 'MEMORY.md') for m in self.memories})
        put('memory', self._fp(*idx) + f'|{len(self.memories)}', lambda: (
            ('### Memory index (cite as memory:<slug>)\n' + '\n'.join(f'- memory:{s}: {t}' for s, t, _ in self.memories),
             [['memory', 'memory', f'{len(self.memories)} memories']]) if self.memories else ('', [])))
        mins = recent_minutes(ws, self.before) if ws else []
        put('minutes', self._fp(*[p for _, p in mins]), lambda: (
            '\n\n'.join(f'### [ata:{f}] Minutes of a previous meeting\n' + _read(p, 3500) for f, p in mins),
            [[f'ata:{f}', 'ata', f] for f, _ in mins]))
        if self.online:
            gh = config.source(self.instance, 'github')
            prs = _gh_prs(gh.get('org', ''), self.before)
            if prs is None: keep('prs', 'gh search failed')
            else:
                put('prs', hashlib.sha1(json.dumps(prs, sort_keys=True).encode()).hexdigest(), lambda: (
                    ('### Open pull requests by the user (cite as repo#n)\n' + '\n'.join(
                        f"- {p['repo']}#{p['number']}: {p['title']}{' (draft)' if p['draft'] else ''} (updated {p['updated']})" for p in prs),
                     [['prs', 'github', f'{len(prs)} open PRs']]) if prs else ('', [])))
            recent = _gh_recent(gh.get('org', ''), gh.get('author') or gh.get('login'), self.before, cache=pr_cache)
            if recent is None: keep('recent_prs', 'gh graphql failed')
            else:
                fp = hashlib.sha1(json.dumps([(p['repo'], p['number'], p['updated'], p['state'], p['schema']) for p in recent],
                                             sort_keys=True).encode()).hexdigest()
                self.prs_fetched = sum(1 for p in recent if p.get('fetched'))
                put('recent_prs', fp, lambda: (
                    ('### Pull requests by the user in the last 14 days, with the files and the schema changes '
                     '(cite as repo#n; state as of the meeting)\n' + recent_prs_text(recent),
                     [['recent-prs', 'github', f"{len(recent)} recent PRs, {sum(1 for p in recent if p['schema'])} with schema"]]) if recent else ('', [])))
            jira = _jira(config.source(self.instance, 'jira'))
            if jira is None: keep('jira', 'jira search failed')
            else:
                put('jira', hashlib.sha1(json.dumps(jira, sort_keys=True).encode()).hexdigest(), lambda: (
                    ('### Open issues assigned to the user (cite as the key)\n' + '\n'.join(
                        f"- {i['key']} [{i['status']}]{' (sub of ' + i['parent'] + ')' if i['parent'] else ''}: {i['summary']}" for i in jira),
                     [['jira', 'jira', f'{len(jira)} open issues']]) if jira else ('', [])))
        return out, changed

    def use(self, parts):
        self.text = '\n\n'.join(p['text'] for p in parts.values() if p.get('text'))
        self.sources = [{'id': a, 'kind': b, 'label': c} for p in parts.values() for a, b, c in p.get('src') or []]
        return self.text

    def build(self):
        return self.use(self.parts()[0])

    def retrieve(self, query, k=2):
        """Full text of the memories that share the most words with the query: [(slug, text)]."""
        q = words(query)
        if not q: return []
        scored = []
        for slug, title, path in self.memories:
            body = _read(path, 2500)
            w = words(slug.replace('-', ' ') + ' ' + title + ' ' + body)
            s = len(q & w) + 2 * len(q & words(slug.replace('-', ' ') + ' ' + title))
            if s >= 4: scored.append((s, slug, body))
        scored.sort(reverse=True)
        return [(slug, re.sub(r'^---.*?---\s*', '', body, flags=re.S)[:1500]) for _, slug, body in scored[:k]]
