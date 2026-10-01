#!/usr/bin/env python3
"""Hook health: read-only health checks (a service, the machine, the team's runners). A check that stays broken opens a
task in Planou with the evidence, its recovery is noted on the same task, and a short report comes once a day.

Every check only reads: an HTTP GET, a command given as an argument list (never a shell), the disk usage of a path, the
age of the newest file of a glob, the runner files of the agents. Whatever the output, the text that leaves the hook
goes through watch_core.planou.clean_text (one line, no query strings, tokens or opaque sequences) and is cut short.

Config ({"type": "health", ...}):
  checks          [{id, kind, label, ...}]: the checks, in the order they are shown (id: [a-z0-9_-], unique)
  after           consecutive broken ticks before a check counts as broken (default 2): announced and a task opens
  open_tasks      true (default): a broken check opens a task (live instance with Planou and a project only)
  project         Planou project of the tasks (default: the instance's planou.project)
  priority        priority of the tasks, 1 (urgent) to 4 (default 2); a check may give its own
  max_tasks_day   tasks opened per day at most (default 5); the others wait for the next day
  reopen_h        a check that breaks again within this many hours of its recovery reuses the same task (default 12)
  report_hour     local hour of the daily report (== SAUDE DIARIA), first heavy tick at or after it; null = no report
  task_note       a line added to the end of every task description (e.g. what the person decides)
  timeout_s       seconds per check (default 20)

Check kinds (options besides id, kind and label; every check also takes "after", "priority" and "every_h": run at most
once every that many hours, keeping its last result in between):
  http     url; status (200); field (a dotted JSON field shown in the summary, e.g. "version"); contains (text the body
           must have)
  cmd      argv (list; "~" expanded; "{since}" becomes the epoch of this check's previous run, or now - window_s, default
           900); ok_exit (0, null = any); match (regex: broken when more than max_matches lines match, default 0;
           match_label names it in the summary, match_context adds the N lines after each match to the evidence);
           each (regex every non-empty line must match); min_lines. stdout and stderr are read together
  disk     path; min_free_gb and/or max_used_pct
  fresh    glob; max_age_h; min_bytes (the newest file of the glob; none = broken)
  runners  status_cmd (argv printing JSON {agent: {"session_open", "runner_alive", "last_tick_min"}}); ignore [agents];
           grace_min (default 15). Broken: an agent with its session open, its runner stopped and no tick for grace_min
           minutes (the session relaunches the runner after every wake; a closed session with a stopped runner is off).
           An agent with "instance": false (team-status: in agents.json, no folder) is left out
  sources  dirs (globs of runner folders <agent root>/cache/runner); ignore [agents]. Broken: an agent whose runner is
           alive (runner.pid) and whose quebrado.sig lists broken sources with no "voltou" line after them. The agent is
           the folder above cache/
A check that cannot run (command missing, timeout, network) is broken, with the error as evidence.

Tasks: the first time a check counts as broken the hook sends one sync (the same call as the suggestions hook) with a
task born in backlog (no state: the person decides), reported by this instance, titled with the check and its summary and
with the evidence and the time it broke in the description. Source key `<instance>:health-<id>-<YYYYMMDDHHMM>` (one per
episode). The recovery adds "Voltou ao normal em ..." to the description (the person closes the task). A person's edit
of the description is kept (ignored_fields); a conflict is sent again on the next tick (at most 3 times); a task the
person deleted is not created again for that episode. Test mode and dry ticks send nothing and say what they would open.

Extension point: a check's "action" (e.g. "rollback", a version rollback when the service breaks) is reserved and not
implemented; the hook only reports it next to the check ("acao <x> nao implementada"), never runs anything.

Output:
  dry tick (test mode)    `== SAUDE (teste, nada gravado): n ok, m quebradas`, a line per check and "abriria tarefa"
  live tick               only what changed: `== SAUDE (n)` with `-- QUEBROU`, `-- VOLTOU` and the task lines
  daily                   `== SAUDE DIARIA (DD/MM)`: a line per check and the last 24 h (breaks, recoveries, tasks)
CLI: agent.py <instance> --health   runs every check now (nothing saved) and lists the tasks opened

Subclass: hooks/security.py reuses all of this (class attributes state_key, key_prefix, cli_flag, T...) with its own
check kinds; a check that raises Unavailable shows "nao rodou" and keeps its state (never a break).
"""
import copy, glob, json, os, re, shutil, subprocess, time, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone

import core, paths
from adapters import Gancho as Base

KINDS = ('http', 'cmd', 'disk', 'fresh', 'runners', 'sources')
ID_RE = re.compile(r'[a-z0-9][a-z0-9_-]{0,40}')
MAX_RETRY = 3
MAX_DESCRIPTION = 8000
EVIDENCE_LINES = 5
NOT_RUN = 'nao rodou:'


def _now():
    return datetime.now(timezone.utc)


def _safe(text, limit=240):
    """One safe line (clean_text) or a neutral mark when nothing safe is left."""
    try:
        from watch_core import planou
        t = planou.clean_text(text, limit)
    except Exception:
        t = None
    return t or '[linha omitida]'


def _planou():
    try:
        from watch_core import planou
        planou.configure_agent(paths.NAME, paths.ROOT)
        return planou if planou.active() else None
    except Exception:
        return None


def _local(d):
    return d.astimezone(core.BRT)


def _hm(iso):
    try: return _local(datetime.fromisoformat(iso)).strftime('%d/%m %H:%M')
    except (TypeError, ValueError): return '?'


def _ago(seconds):
    m = int(max(0, seconds) // 60)
    if m < 60: return f'{m} min'
    if m < 48 * 60: return f'{m // 60} h'
    return f'{m // 1440} d'


def _argv(v, since=None):
    if not (isinstance(v, list) and v and all(isinstance(x, str) and x for x in v)):
        raise ValueError('"argv" precisa ser uma lista de textos (sem shell)')
    out = []
    for x in v:
        if x.startswith('~'): x = os.path.expanduser(x)
        if since is not None: x = x.replace('{since}', str(int(since)))
        out.append(x)
    return out


def _run(argv, timeout):
    """(exit code, stdout + stderr). Never a shell."""
    r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=timeout)
    return r.returncode, r.stdout.decode('utf-8', 'replace')


def _sig(summary, evidence):
    """A short fingerprint of what a broken check shows, to notice when it changes."""
    import hashlib
    ev = [re.sub(r':\d+:', ':', str(l)) for l in evidence or [] if not str(l).startswith(NOT_RUN)]
    # a part that did not run, or a finding that only moved to another line, is not a change
    return hashlib.sha256('\n'.join([summary or ''] + ev).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ checks: each returns (ok, summary, evidence lines)

def check_http(c, t, prev):
    url = c.get('url')
    if not isinstance(url, str) or not url.startswith(('http://', 'https://')): raise ValueError('"url" http(s)')
    want = int(c.get('status', 200))
    req = urllib.request.Request(url, headers={'User-Agent': 'agent-health/1'})
    try:
        with urllib.request.urlopen(req, timeout=t) as r: code, body = r.status, r.read(65536).decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        code, body = e.code, (e.read(4096) or b'').decode('utf-8', 'replace')
    summary, ok, ev = f'HTTP {code}', code == want, []
    if c.get('field'):
        try:
            v = json.loads(body)
            for k in str(c['field']).split('.'): v = v[k]
            summary += f', {c["field"]} {_safe(v, 60)}'
        except (ValueError, KeyError, TypeError):
            ok = False; summary += f', sem o campo {c["field"]}'
    if c.get('contains') and str(c['contains']) not in body:
        ok = False; summary += ' sem o texto esperado'
    if not ok:
        if code != want: summary += f' (esperado {want})'
        if body.strip(): ev.append('resposta: ' + _safe(body, 200))
    return ok, summary, ev


def check_cmd(c, t, prev):
    since = prev.get('last_run') or (time.time() - float(c.get('window_s', 900)))
    argv = _argv(c.get('argv'), since)
    code, out = _run(argv, t)
    lines = [l for l in out.splitlines() if l.strip()]
    ok, bits, ev = True, [], []
    ok_exit = c.get('ok_exit', 0)
    if ok_exit is not None and code != ok_exit:
        ok = False; bits.append(f'saiu com {code}')
        ev += [_safe(l) for l in lines[-3:]]
    if c.get('match'):
        rx = re.compile(c['match'])
        idx = [i for i, l in enumerate(lines) if rx.search(l)]
        mx, ctxn = int(c.get('max_matches', 0)), max(0, int(c.get('match_context', 0)))
        bits.append(f'{len(idx)} linha(s) com {c.get("match_label") or "o padrao"}')
        if len(idx) > mx:
            ok = False
            for i in idx[-EVIDENCE_LINES:]:
                ev.append(_safe(' / '.join(x.strip() for x in lines[i:i + 1 + ctxn])))
    if c.get('each'):
        rx = re.compile(c['each'])
        bad = [l for l in lines if not rx.search(l)]
        if bad:
            ok = False; bits.append(f'{len(bad)} de {len(lines)} fora do esperado')
            ev += [_safe(l) for l in bad[:EVIDENCE_LINES]]
        else:
            bits.append(f'{len(lines)} de {len(lines)} ok')
    if c.get('min_lines') is not None and len(lines) < int(c['min_lines']):
        ok = False; bits.append(f'{len(lines)} linha(s), esperado ao menos {int(c["min_lines"])}')
    if not bits: bits.append(f'saiu com {code}')
    return ok, ', '.join(bits), ev[:EVIDENCE_LINES]


def check_disk(c, t, prev):
    path = os.path.expanduser(str(c.get('path') or ''))
    if not path: raise ValueError('"path" do disco')
    u = shutil.disk_usage(path)
    free_gb, used = u.free / 1e9, 100.0 * (u.total - u.free) / u.total if u.total else 0.0
    ok = True
    if c.get('min_free_gb') is not None and free_gb < float(c['min_free_gb']): ok = False
    if c.get('max_used_pct') is not None and used > float(c['max_used_pct']): ok = False
    limits = []
    if c.get('min_free_gb') is not None: limits.append(f'minimo {c["min_free_gb"]} GB livres')
    if c.get('max_used_pct') is not None: limits.append(f'maximo {c["max_used_pct"]}% usado')
    return ok, f'{free_gb:.0f} GB livres ({used:.0f}% usado)', ([] if ok else [f'{path}: ' + ', '.join(limits)])


def check_fresh(c, t, prev):
    pat = os.path.expanduser(str(c.get('glob') or ''))
    if not pat: raise ValueError('"glob" dos arquivos')
    files = [f for f in glob.glob(pat) if os.path.isfile(f)]
    if not files: return False, 'nenhum arquivo', [f'nada em {pat}']
    f = max(files, key=os.path.getmtime)
    age_h, size = (time.time() - os.path.getmtime(f)) / 3600, os.path.getsize(f)
    ok, why = True, []
    if c.get('max_age_h') is not None and age_h > float(c['max_age_h']):
        ok = False; why.append(f'mais velho que {c["max_age_h"]} h')
    if c.get('min_bytes') is not None and size < int(c['min_bytes']):
        ok = False; why.append(f'menor que {int(c["min_bytes"])} bytes')
    summary = f'mais novo {os.path.basename(f)}, {_ago(age_h * 3600)} atras, {size / 1e6:.1f} MB, {len(files)} arquivo(s)'
    return ok, summary, ([f'{os.path.basename(f)}: ' + ', '.join(why)] if why else [])


def check_runners(c, t, prev):
    code, out = _run(_argv(c.get('status_cmd')), t)
    if code != 0: raise RuntimeError(f'status_cmd saiu com {code}: {_safe(out, 160)}')
    try: data = json.loads(out)
    except ValueError: raise RuntimeError('status_cmd nao imprimiu JSON')
    ignore = set(c.get('ignore') or [])
    agents = {a: v for a, v in (data or {}).items() if isinstance(v, dict) and a not in ignore
              and v.get('instance') is not False}   # listed in agents.json without a folder: no runner to be down
    grace = float(c.get('grace_min', 15))

    def stale(v):
        m = v.get('last_tick_min')
        return not isinstance(m, (int, float)) or m >= grace
    down = sorted(a for a, v in agents.items() if v.get('session_open') and not v.get('runner_alive') and stale(v))
    alive = sum(1 for v in agents.values() if v.get('runner_alive'))
    if not down: return True, f'{alive} runner(s) de pe, {len(agents)} agente(s)', []
    ev = [f'{a}: sessao aberta, runner parado, ultimo tick ha {agents[a].get("last_tick_min", "?")} min' for a in down]
    return False, f'runner parado com a sessao aberta: {", ".join(down)}', ev


def _agent_of(runner_dir):
    parts = os.path.normpath(runner_dir).split(os.sep)
    return parts[-3] if len(parts) >= 3 and parts[-2] == 'cache' else os.path.basename(runner_dir)


def _alive(runner_dir):
    try: pid = open(os.path.join(runner_dir, 'runner.pid')).read().strip()
    except OSError: return False
    return pid.isdigit() and os.path.exists(f'/proc/{pid}')


def check_sources(c, t, prev):
    ignore, seen, broken, ev = set(c.get('ignore') or []), set(), [], []
    dirs = c.get('dirs') or []
    if not isinstance(dirs, list) or not dirs: raise ValueError('"dirs": globs das pastas cache/runner')
    watched = 0
    for pat in dirs:
        for d in sorted(glob.glob(os.path.expanduser(str(pat)))):
            real = os.path.realpath(d)
            agent = _agent_of(d)
            if real in seen or agent in ignore or not os.path.isdir(d) or not _alive(d): continue
            seen.add(real); watched += 1
            try: lines = open(os.path.join(d, 'quebrado.sig'), encoding='utf-8', errors='replace').read().splitlines()
            except OSError: continue
            if not lines or not lines[0].strip() or any(l.startswith('voltou') for l in lines[1:]): continue
            names = re.findall(r'FONTE QUEBRADA \(([a-z0-9_]+)\)', lines[0])
            if not names: continue
            when = ''
            if len(lines) > 1 and lines[1].strip().isdigit():
                when = f' (acordou pela quebra ha {_ago(time.time() - int(lines[1].strip()))})'
            broken.append(agent)
            ev.append(f'{agent}: {", ".join(names)}{when}')
    if not broken: return True, f'nenhuma fonte quebrada em {watched} agente(s) com runner de pe', []
    return False, f'fonte quebrada em {", ".join(broken)}', ev[:EVIDENCE_LINES * 2]


CHECKS = {'http': check_http, 'cmd': check_cmd, 'disk': check_disk, 'fresh': check_fresh, 'runners': check_runners,
          'sources': check_sources}


class Unavailable(Exception):
    """A check that could not run for a reason that is not a problem of what it watches (a tool not installed, a
    project not restored, no network for an audit). The check shows "nao rodou" and keeps its previous state: it never
    counts as broken and never opens a task. The health checks never raise it; the security hook does."""


class Gancho(Base):
    tipo = 'health'
    json_key = 'health'
    roda_em_dry = True
    # what a subclass (hooks/security.py) changes: the state key, the task key prefix, the CLI flag, the defaults and the
    # words of the output. health keeps these values.
    state_key = 'health'
    key_prefix = 'health'
    cli_flag = 'health'
    default_after = 2
    default_report_hour = 8
    default_timeout = 20
    kind_priority = {}            # kind -> default task priority when neither the check nor the hook gives one
    note_changes = False          # True: a broken check whose evidence changes notes the change on its task
    cli_would = False             # True: the CLI also lists the tasks it would open
    kinds = KINDS
    checks_by_kind = CHECKS
    T = {'header': 'SAUDE', 'daily': 'SAUDE DIARIA', 'warn': 'saude', 'bad': 'com problema', 'broke': 'QUEBROU',
         'back': 'VOLTOU', 'changed': 'MUDOU', 'back_since': 'com problema desde', 'title': 'Saude: {label} com problema ({summary})',
         'back_note': 'Voltou ao normal em', 'again_note': 'Quebrou de novo em', 'changed_note': 'Mudou em',
         'task_back': 'voltou ao normal (a pessoa fecha)', 'task_again': 'quebrou de novo', 'task_changed': 'mudou',
         'now': 'saude agora', 'cli_help': 'roda as checagens de saude agora (nada gravado) e lista as tarefas',
         'tasks_by': 'tarefa(s) aberta(s) por esta checagem'}

    def configura(self):
        c = self.cfg
        self.checks, self.problems = [], []
        seen = set()
        for i, ch in enumerate(c.get('checks') or []):
            cid = ch.get('id') if isinstance(ch, dict) else None
            if not isinstance(cid, str) or not ID_RE.fullmatch(cid) or cid in seen:
                self.problems.append(f'checks[{i}]: "id" invalido ou repetido'); continue
            if ch.get('kind') not in self.kinds:
                self.problems.append(f'{cid}: "kind" {ch.get("kind")!r} ({", ".join(self.kinds)})'); continue
            seen.add(cid)
            self.checks.append(ch)

        def num(k, d, lo=None):
            try: v = int(c.get(k, d))
            except (TypeError, ValueError): v = d
            return max(lo, v) if lo is not None else v
        self.after = num('after', self.default_after, 1)
        self.priority = min(4, num('priority', 2, 1))
        self.priority_set = c.get('priority') is not None
        self.max_day = num('max_tasks_day', 5, 0)
        self.reopen_h = num('reopen_h', 12, 0)
        self.timeout = num('timeout_s', self.default_timeout, 1)
        rh = c.get('report_hour', self.default_report_hour)
        self.report_hour = rh if isinstance(rh, int) and not isinstance(rh, bool) and 0 <= rh <= 23 else None
        self.open_tasks = c.get('open_tasks', True) is not False
        self.project = c.get('project') if isinstance(c.get('project'), str) else None
        self.note = c.get('task_note') if isinstance(c.get('task_note'), str) else ''

    def label(self, ch):
        return ch.get('label') or ch['id']

    def task_priority(self, ch):
        if ch.get('priority'): return int(ch['priority'])
        if not self.priority_set and ch.get('kind') in self.kind_priority: return self.kind_priority[ch['kind']]
        return self.priority

    def run_checks(self, st, force=False):
        """[(check, ok, summary, evidence, started)] for every check due now. ok is None when the check did not run
        (Unavailable). A check with "every_h" that ran less than that many hours ago is left out."""
        out = []
        for ch in self.checks:
            prev = st.get('checks', {}).get(ch['id'], {})
            started = time.time()
            try: every = float(ch.get('every_h') or 0)
            except (TypeError, ValueError): every = 0
            if every and not force and prev.get('last_run') and started - prev['last_run'] < every * 3600: continue
            try:
                ok, summary, ev = self.checks_by_kind[ch['kind']](ch, self.timeout, prev)
            except Unavailable as e:
                ok, summary, ev = None, f'nao rodou: {e}', []
            except subprocess.TimeoutExpired:
                ok, summary, ev = False, f'nao respondeu em {self.timeout} s', []
            except Exception as e:
                ok, summary, ev = False, 'nao deu para checar', [_safe(f'{type(e).__name__}: {e}', 200)]
            out.append((ch, ok, _safe(summary, 200), ev, started))
        return out

    # ------------------------------------------------------------------ tick

    def run(self, ctx):
        st = ctx.s.setdefault(self.state_key, {})
        for k, v in (('checks', {}), ('tasks', {}), ('log', []), ('days', {})): st.setdefault(k, v)
        now = ctx.agora or _now()
        if ctx.dry: st = copy.deepcopy(st)          # a dry tick changes nothing, not even in memory
        results = self.run_checks(st)
        events = []
        for ch, ok, summary, ev, started in results:
            cid, after = ch['id'], int(ch.get('after') or self.after)
            c = st['checks'].setdefault(cid, {})
            c['last_run'], c['summary'] = int(started), summary
            day = _local(now).date().isoformat()
            if ok is None: continue                    # did not run: keeps its state, never a break nor a recovery
            if ok:
                if c.get('fails', 0) >= after:
                    events.append({'kind': 'back', 'id': cid, 'since': c.get('since'), 'summary': summary})
                    self.log(st, now, cid, 'voltou')
                c.update(fails=0, since=None, evidence=[], ok_at=now.isoformat())
                c.pop('sig', None)
                continue
            c['fails'] = int(c.get('fails', 0)) + 1
            c['since'] = c.get('since') or now.isoformat()
            c['evidence'] = ev
            sig = _sig(summary, ev)
            fd = c.setdefault('fail_ticks', {}); fd[day] = fd.get(day, 0) + 1
            for d in sorted(fd)[:-3]: fd.pop(d, None)
            if c['fails'] == after:
                events.append({'kind': 'broke', 'id': cid, 'since': c['since'], 'summary': summary, 'evidence': ev})
                self.log(st, now, cid, 'quebrou')
            elif self.note_changes and c['fails'] > after and c.get('sig') and c['sig'] != sig:
                events.append({'kind': 'changed', 'id': cid, 'since': c['since'], 'summary': summary, 'evidence': ev})
                self.log(st, now, cid, 'mudou')
            c['sig'] = sig
        tasks = self.tasks(ctx, st, now, events)
        report = self.report(st, now, results, ctx.dry)
        if report:
            report['lines'] = self.lines([(ch['id'], ok, s, ev) for ch, ok, s, ev, _ in results], st)
            report['bad'] = sum(1 for r in results if r[1] is False)
            report['n'] = len(results)
        if ctx.dry:
            return {'kind': 'dry', 'results': [(ch['id'], ok, s, ev) for ch, ok, s, ev, _ in results],
                    'would': self.would_open(results), 'report': report}
        if not events and not tasks.get('err') and not report and not tasks.get('lines'): return None
        return {'kind': 'live', 'events': events, 'tasks': tasks, 'report': report}

    def log(self, st, now, cid, what):
        st['log'].append({'at': now.isoformat(), 'id': cid, 'what': what})
        cut = (now - timedelta(days=7)).isoformat()
        st['log'] = [e for e in st['log'] if e['at'] >= cut][-300:]

    def would_open(self, results):
        if not self.open_tasks: return []
        return [self.title(ch, s) for ch, ok, s, ev, _ in results if ok is False]

    # ------------------------------------------------------------------ Planou tasks

    def title(self, ch, summary):
        t = self.T['title'].format(label=self.label(ch), summary=summary)
        return t if len(t) <= 200 else t[:199] + '…'

    def description(self, ch, c, summary):
        lines = [f'Checagem "{self.label(ch)}" ({ch["kind"]}) com problema desde {_hm(c.get("since"))}, '
                 f'vista pelo agente {paths.NAME} (so leitura).', '', f'Resumo: {summary}']
        if c.get('evidence'): lines += ['', 'Evidencia:'] + [f'- {l}' for l in c['evidence']]
        if ch.get('action'): lines += ['', f'Acao "{ch["action"]}" nao implementada: nada foi executado.']
        if self.note: lines += ['', self.note]
        return '\n'.join(lines)

    def source_key(self, key):
        return f'{paths.NAME}:{self.key_prefix}-{key}'

    def tasks(self, ctx, st, now, events):
        """Opens a task per check that counts as broken and has none for this episode; notes recoveries (and, with
        note_changes, a change of the evidence). Live only."""
        out = {'lines': [], 'err': None}
        if ctx.dry or not self.open_tasks: return out
        p = _planou()
        project = self.project or (p._S.get('project') if p else None)
        if not p or not project: return out
        by_id = {ch['id']: ch for ch in self.checks}
        day = _local(now).date().isoformat()
        opened_today = st['days'].get(day, 0)
        batch = {}
        for cid, c in st['checks'].items():
            ch = by_id.get(cid)
            if not ch: continue
            after = int(ch.get('after') or self.after)
            if c.get('fails', 0) < after or c.get('task'): continue
            reuse = self.reusable(st, cid, now)
            if reuse:
                t = st['tasks'][reuse]
                t['recovered'] = None
                t['description'] = self.extend(t, f'{self.T["again_note"]} {_hm(c["since"])}: {c.get("summary")}')
                t['note'] = 'again'
                c['task'] = reuse
                batch[reuse] = t
                continue
            if opened_today >= self.max_day:
                if c.get('limited') != day: out['lines'].append(f'-- limite de {self.max_day} tarefas hoje: {self.label(ch)} espera')
                c['limited'] = day
                continue
            key = f'{cid}-{_local(datetime.fromisoformat(c["since"])).strftime("%Y%m%d%H%M")}'
            t = {'check': cid, 'title': self.title(ch, c.get('summary')), 'description': self.description(ch, c, c.get('summary')),
                 'priority': self.task_priority(ch), 'pid': None, 'version': None,
                 'opened': now.isoformat(), 'recovered': None, 'new': True}
            st['tasks'][key] = t
            c['task'] = key
            opened_today += 1
            batch[key] = t
        for ev in events:
            if ev['kind'] == 'changed':
                key = (st['checks'].get(ev['id']) or {}).get('task')
                t = st['tasks'].get(key) if key else None
                if not t or t.get('deleted') or key in batch: continue
                t['description'] = self.extend(t, '\n'.join([f'{self.T["changed_note"]} {_hm(now.isoformat())}: {ev["summary"]}']
                                                            + [f'- {l}' for l in ev.get('evidence') or []]))
                t['note'] = 'changed'
                batch[key] = t
                ev['task'] = key
                continue
            if ev['kind'] != 'back': continue
            key = next((k for k, t in st['tasks'].items() if t.get('check') == ev['id'] and not t.get('recovered')
                        and not t.get('deleted')), None)
            if not key: continue
            t = st['tasks'][key]
            t['recovered'] = now.isoformat()
            t['description'] = self.extend(t, f'{self.T["back_note"]} {_hm(now.isoformat())}: {ev["summary"]}')
            batch[key] = t
            ev['task'] = key
        for c in st['checks'].values():
            if not c.get('fails'): c.pop('task', None)
        retry = {k: t for k, t in st['tasks'].items() if (t.get('retry') or t.get('pending')) and not t.get('deleted')}
        batch.update({k: t for k, t in retry.items() if k not in batch})
        if not batch: return out
        body = []
        for key, t in batch.items():
            b = {'source_key': self.source_key(key), 'project': project, 'title': t['title'],
                 'priority': t['priority'], 'reporter': paths.NAME}
            if not t.get('frozen'): b['description'] = t['description']
            if t.get('version') is not None: b['base_version'] = t['version']
            body.append(b)
        try:
            _, res = p._call('POST', '/agent/sync', {'agent': paths.NAME, 'generated_at': now.isoformat(), 'tasks': body})
        except p.PlanouError as ex:
            msg = _safe(ex.message if getattr(ex, 'status', 0) else ex, 200)
            for t in batch.values(): t['pending'] = True                  # goes again next tick
            if st.get('err') != msg: out['err'] = msg
            st['err'] = msg
            return out
        st.pop('err', None)
        by_sk = {self.source_key(k): k for k in batch}
        for r in (res or {}).get('tasks') or []:
            key = by_sk.get(r.get('source_key'))
            if not key: continue
            t, result = st['tasks'][key], r.get('result')
            t.pop('pending', None)
            note = t.pop('note', None)
            if result in ('created', 'updated', 'unchanged') and r.get('pid'):
                t.update(pid=r['pid'], version=r.get('version'))
                t.pop('retry', None)
                if 'description' in (r.get('ignored_fields') or []): t['frozen'] = True
                if t.pop('new', None):
                    st['days'][day] = st['days'].get(day, 0) + 1
                    out['lines'].append(f'-- tarefa {t["pid"]} aberta no backlog: {t["title"]}')
                elif t.get('recovered'):
                    out['lines'].append(f'-- tarefa {t["pid"]} anotada: {self.T["task_back"]}')
                elif note == 'changed':
                    out['lines'].append(f'-- tarefa {t["pid"]} anotada: {self.T["task_changed"]}')
                else:
                    out['lines'].append(f'-- tarefa {t["pid"]} anotada: {self.T["task_again"]}')
            elif result == 'conflict':
                if r.get('version') is not None: t['version'] = r['version']
                t['retry'] = int(t.get('retry') or 0) + 1
                if t['retry'] > MAX_RETRY: t.pop('retry', None); t['error'] = _safe(r.get('reason'), 200)
            elif result == 'ignored':
                t['deleted'] = True; t.pop('retry', None)
                out['lines'].append(f'-- {t["title"]}: a pessoa apagou essa tarefa; nao abro de novo neste episodio')
            else:
                t['error'] = _safe(r.get('reason') or result, 200); t.pop('retry', None)
                out['lines'].append(f'-- recusada pelo Planou: {t["title"]}: {t["error"]}')
        for d in sorted(st['days'])[:-14]: st['days'].pop(d, None)
        return out

    def reusable(self, st, cid, now):
        """The last task of this check, recovered less than reopen_h ago and not deleted; None otherwise."""
        best = None
        for k, t in st['tasks'].items():
            if t.get('check') != cid or t.get('deleted') or not t.get('recovered'): continue
            if best is None or t['recovered'] > st['tasks'][best]['recovered']: best = k
        if best and now - datetime.fromisoformat(st['tasks'][best]['recovered']) <= timedelta(hours=self.reopen_h): return best
        return None

    def extend(self, t, line):
        d = t['description'] + '\n' + line
        return d if len(d) <= MAX_DESCRIPTION else t['description']

    # ------------------------------------------------------------------ daily report

    def report(self, st, now, results, dry):
        if self.report_hour is None: return None
        loc = _local(now)
        today = loc.date().isoformat()
        if loc.hour < self.report_hour or st.get('report_day') == today: return None
        st['report_day'] = today
        cut = (now - timedelta(hours=24)).isoformat()
        log = [e for e in st['log'] if e['at'] >= cut]
        opened = [t['pid'] for t in st['tasks'].values() if t.get('pid') and (t.get('opened') or '') >= cut]
        flaky = []
        yday = (loc.date() - timedelta(days=1)).isoformat()
        for ch, ok, *_ in results:
            n = sum(v for d, v in (st['checks'].get(ch['id'], {}).get('fail_ticks') or {}).items() if d in (today, yday))
            if n and ok: flaky.append(f'{self.label(ch)} ({n} tick(s))')
        return {'day': loc.strftime('%d/%m'), 'breaks': sum(1 for e in log if e['what'] == 'quebrou'),
                'backs': sum(1 for e in log if e['what'] == 'voltou'), 'opened': opened, 'flaky': flaky}

    # ------------------------------------------------------------------ text

    def lines(self, results, st=None):
        by_id = {ch['id']: ch for ch in self.checks}
        out = []
        for cid, ok, summary, ev in results:
            ch = by_id[cid]
            mark = '--' if ok is None else 'ok' if ok else 'XX'
            extra = ''
            if ok is False and st:
                c = st['checks'].get(cid) or {}
                if c.get('since'): extra = f' (desde {_hm(c["since"])})'
            if ch.get('action'): extra += f' (acao {ch["action"]} nao implementada)'
            out.append(f'-- {mark} {self.label(ch)}: {summary}{extra}')
            if ok is False: out += [f'   {l}' for l in ev]
        return out

    def counts(self, results):
        """'n ok, m com problema' (and ', k nao rodou' when some did not run)."""
        bad = sum(1 for r in results if r[1] is False)
        skipped = sum(1 for r in results if r[1] is None)
        s = f'{len(results) - bad - skipped} ok, {bad} {self.T["bad"]}'
        return s + (f', {skipped} nao rodou' if skipped else '')

    def texto(self, v):
        if not v: return None
        out, T = [], self.T
        if v['kind'] == 'dry':
            out.append(f'== {T["header"]} (teste, nada gravado): {self.counts(v["results"])}')
            out += self.lines(v['results'])
            for t in v['would']: out.append(f'-- abriria tarefa (depois de {self.after} ticks seguidos): {t}')
            for p in self.problems: out.append(f'-- config: {p}')
            if v.get('report'): out.append(f'-- (o relatorio diario sairia agora: {v["report"]["day"]})')
            return '\n'.join(out)
        evs, tk = v['events'], v['tasks']
        body = []
        for e in evs:
            ch = next((c for c in self.checks if c['id'] == e['id']), {'id': e['id']})
            if e['kind'] == 'broke':
                body.append(f'-- {T["broke"]} {self.label(ch)} (desde {_hm(e["since"])}): {e["summary"]}')
                body += [f'   {l}' for l in e.get('evidence') or []]
                if ch.get('action'): body.append(f'   acao {ch["action"]} nao implementada: so a tarefa')
            elif e['kind'] == 'changed':
                body.append(f'-- {T["changed"]} {self.label(ch)} (desde {_hm(e["since"])}): {e["summary"]}')
                body += [f'   {l}' for l in e.get('evidence') or []]
            else:
                body.append(f'-- {T["back"]} {self.label(ch)} ({T["back_since"]} {_hm(e["since"])}): {e["summary"]}')
        body += tk.get('lines') or []
        if tk.get('err'): body.append(f'AVISO ({T["warn"]}): tarefas esperam o Planou ({tk["err"]}); vao no proximo tick')
        if body: out += [f'== {T["header"]} ({len(evs) or len(body)})'] + body
        r = v.get('report')
        if r:
            if out: out.append('')
            out.append(f'== {T["daily"]} ({r["day"]}): {r["n"] - r["bad"]} ok, {r["bad"]} {T["bad"]}')
            out += r['lines']
            out.append(f'-- ultimas 24 h: {r["breaks"]} quebra(s), {r["backs"]} volta(s), tarefas abertas: '
                       + (', '.join(r['opened']) or 'nenhuma'))
            if r['flaky']: out.append('-- falharam e voltaram sozinhas (ontem e hoje): ' + ', '.join(r['flaky']))
        return '\n'.join(out) or None

    # ------------------------------------------------------------------ CLI

    def args(self, ap):
        ap.add_argument(f'--{self.cli_flag}', action='store_true', help=self.T['cli_help'])

    def cli(self, a, ctx):
        if not getattr(a, self.cli_flag.replace('-', '_'), False): return False
        st = copy.deepcopy(ctx.s.get(self.state_key) or {})
        st.setdefault('checks', {})
        results = self.run_checks(st, force=True)
        print(f'{self.T["now"]}: {self.counts(results)} (nada gravado)')
        print('\n'.join(self.lines([(ch['id'], ok, s, ev) for ch, ok, s, ev, _ in results])))
        for p in self.problems: print(f'-- config: {p}')
        if self.open_tasks and self.cli_would:
            for ch, ok, s, ev, _ in results:
                if ok is False: print(f'-- abriria tarefa: {self.title(ch, s)}')
        tasks = (ctx.s.get(self.state_key) or {}).get('tasks') or {}
        if tasks: print(f'{len(tasks)} {self.T["tasks_by"]}:')
        for t in tasks.values():
            state = 'apagada' if t.get('deleted') else f'voltou {_hm(t["recovered"])}' if t.get('recovered') else 'aberta'
            print(f'-- {t.get("pid") or "?"} ({state}): {t["title"]}')
        return True
