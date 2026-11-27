#!/usr/bin/env python3
"""Hook release_due: wakes the session when a batch release is due (behavior batch-release).

Development workers end on a branch ready for release; the session adds it to the release queue of the instance (a JSON
file inside the instance folder). This hook reads that file on every heavy tick and prints `== RELEASE DEVIDO` when
there are `min_branches` or more ready branches, or one waiting more than `max_wait_min` minutes. It prints once per set
of branches (again after `remind_min` minutes if nothing happened) and stays quiet while an integration is running
(`start` ... `done`/`abort`); an integration older than `stale_h` hours is reported once.

The project decides who goes alone (PLN0566): a release only goes without the person in an autonomous project, the
current autonomy of the project of each branch's task, whatever the config says (provisioner.py wrote "can" once, from
the default project). Otherwise `== RELEASE DEVIDO` says it needs her OK and `--release-queue start` refuses until
`start ok`. No answer from Planou is no decision: the release waits and `start` is repeated, with no OK to ask for. It
only looks stricter: an instance that does not talk to Planou keeps its config.

Config: {"type": "release_due", "ready_file": "data/release_queue.json", "min_branches": 2, "max_wait_min": 30,
         "remind_min": 60, "stale_h": 3}
ready_file is relative to the instance folder (or absolute, ~ and {instance} expanded).

Rules from Planou: when the instance talks to Planou (a "planou" block with a project, live, with a key), min_branches,
max_wait_min and e2e_every_h come from the project's Release section (the "release" block of GET
/v1/agent/projects/<project>, cached 10 min); the hook options above (and behavior_config.batch-release.e2e_every_h)
are the fallback when Planou does not answer. The queue is mirrored to Planou (PUT .../release-queue) after every CLI
change and on the heavy tick whenever the local file differs from the last queue sent; the local file stays the source
of truth.

The file: {"branches": [{"branch": "feat/x", "ready_at": ISO UTC, "pid": "ABC0001"}], "integrating_since": ISO|null,
           "integrating": ["feat/x", ...], "released": {"since": ISO, "at": ISO},
           "released_log": [{"since": ISO, "at": ISO, "branches": [...]}]}   (released: the last `done`, PLN0155; released_log: the last 30, PLN0482)

CLI (the session never edits the file by hand):
  agent.py <instance> --release-queue [list]          the queue and whether a release is due
  agent.py <instance> --release-queue add BRANCH [PID] a branch is ready (again: ready_at is now); with a PID and the
                                                       batch release repository configured, the PNGs the branch adds or
                                                       changes in docs/ go to the task as attachments (PLN1034)
  agent.py <instance> --release-queue start [ok]       the integrator starts with the branches in the queue ("ok": the
                                                       person's OK, needed when the project is not autonomous)
  agent.py <instance> --release-queue drop BRANCH      the culprit goes back to its worker; the integration goes on
  agent.py <instance> --release-queue done [BRANCH...] released (default: the branches of the integration); ends it
  agent.py <instance> --release-queue abort            the integration ended without release; branches stay queued

Never raises in the tick: an unreadable file is reported once (same error, same line) and the tick goes on.
"""
import hashlib, json, os, re, shutil, subprocess, sys
from datetime import datetime, timedelta, timezone

import core, paths, planou_release
from watch_core import column_roles, fileio
from adapters import Gancho as Base

BRANCH_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,200}')

# PLN1034: the screenshots a dev worker takes in docs/<delivery>/ go to the task when the branch enters the queue, so the
# person reviews the delivery from the task. Review order: desktop, phone, tablet, then the rest; at most SHOTS_MAX per delivery.
# The base is origin/main only (PLN1040): a local main may be behind and its diff would bring screenshots of other
# deliveries, so without origin/main nothing is attached and the add says why.
SHOT_WIDTHS = ('1360', '390', '834')
SHOTS_MAX = 9
SHOTS_SKIP = 'docs/fase4/'
SHOTS_BASES = ('origin/main',)


def _now():
    return datetime.now(timezone.utc)


def _num(v, default):
    try: return float(v) if v is not None else default
    except (TypeError, ValueError): return default


def _minutes(iso, now):
    try: return max(0.0, (now - core.dt(iso)).total_seconds() / 60)
    except Exception: return 0.0


class Gancho(Base):
    tipo = 'release_due'
    json_key = 'release_due'
    roda_em_dry = True

    def configura(self):
        c = self.cfg
        self.ready_file = c.get('ready_file') if isinstance(c.get('ready_file'), str) else 'data/release_queue.json'
        self.min_branches = max(1, int(_num(c.get('min_branches'), 2)))
        self.max_wait = _num(c.get('max_wait_min'), 30)
        self.remind = _num(c.get('remind_min'), 60)
        self.stale_h = _num(c.get('stale_h'), 3)
        br = ((getattr(self.ctx, 'cfg', None) or {}).get('behavior_config') or {}).get('batch-release') or {}
        self.local = {'min_branches': self.min_branches, 'max_wait_min': self.max_wait,
                      'e2e_every_h': _num(br.get('e2e_every_h'), 24) if isinstance(br, dict) else 24.0}
        self.rules = dict(self.local, source='local')

    def load_rules(self, now=None):
        """The rules in force: Planou's (cached 10 min) or, when it does not answer, the local config."""
        r = planou_release.rules(now)
        if r:
            self.rules = {'min_branches': max(1, int(_num(r.get('min_branches'), self.local['min_branches']))),
                          'max_wait_min': _num(r.get('max_wait_min'), self.local['max_wait_min']),
                          'e2e_every_h': _num(r.get('e2e_every_h'), self.local['e2e_every_h']),
                          'version_rule': r.get('version_rule') if isinstance(r.get('version_rule'), str) else None,
                          'integrator': ((r.get('integrator') or {}).get('name') if isinstance(r.get('integrator'), dict) else None),
                          'source': 'planou'}
        else:
            self.rules = dict(self.local, source='local')
        self.min_branches, self.max_wait = self.rules['min_branches'], self.rules['max_wait_min']
        return self.rules

    def rules_line(self):
        r = self.rules
        out = (f'regras ({"Planou" if r["source"] == "planou" else "config local"}): {r["min_branches"]} branches ou '
               f'{r["max_wait_min"]:.0f} min; e2e completo a cada {r["e2e_every_h"]:.0f} h')
        if r.get('version_rule'): out += f'; versao: {r["version_rule"]}'
        if r.get('integrator'): out += f'; integra: {r["integrator"]}'
        return out

    def mirror(self, q, quiet=False):
        """Mirrors the queue to Planou; prints one warning line on failure (CLI) unless quiet."""
        ok, why = planou_release.mirror_queue(q)
        if not ok and why != 'off' and not quiet:
            print(f'AVISO (planou): fila de release nao espelhada ({why}); vai no proximo tick')
        return ok

    def path(self):
        return paths.expand(self.ready_file)

    # ------------------------------------------------------------ the file

    def read(self):
        """The queue as a dict. Missing file = empty queue. Broken file: ValueError with the reason."""
        p = self.path()
        try:
            with open(p, encoding='utf-8') as f: q = json.load(f)
        except FileNotFoundError:
            return {'branches': [], 'integrating_since': None, 'integrating': []}
        except (OSError, ValueError) as e:
            raise ValueError(f'{p}: {type(e).__name__}: {e}')
        if not isinstance(q, dict) or not isinstance(q.get('branches', []), list):
            raise ValueError(f'{p}: esperado {{"branches": [...]}}')
        q.setdefault('branches', []); q.setdefault('integrating_since', None); q.setdefault('integrating', [])
        q['branches'] = [b for b in q['branches'] if isinstance(b, dict) and isinstance(b.get('branch'), str)]
        return q

    def write(self, q):
        p = self.path()
        if not paths.inside(p): core.exige_live(f'gravar {p}')
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fileio.write_json(p, q, ensure_ascii=False, indent=1)

    def due(self, q, now):
        """(due, oldest wait in minutes)."""
        bs = q.get('branches') or []
        if not bs: return False, 0.0
        oldest = max(_minutes(b.get('ready_at'), now) for b in bs)
        return (len(bs) >= self.min_branches or oldest > self.max_wait), oldest

    # ------------------------------------------------------------ the tick

    def run(self, ctx):
        st = ctx.s.setdefault('release_due', {})
        now = ctx.agora or _now()
        self.load_rules(now)
        try:
            q = self.read()
        except ValueError as e:
            msg = str(e)[:300]
            if st.get('err') == msg: return None
            st['err'] = msg
            return {'kind': 'error', 'error': msg}
        st.pop('err', None)
        if not ctx.dry: self.mirror(q, quiet=True)          # only when it differs from the last queue sent
        since = q.get('integrating_since')
        if since:
            st.pop('sig', None)
            if _minutes(since, now) >= self.stale_h * 60 and st.get('stale') != since:
                st['stale'] = since
                return {'kind': 'stale', 'since': since, 'hours': _minutes(since, now) / 60,
                        'branches': q.get('integrating') or []}
            return None
        st.pop('stale', None)
        due, oldest = self.due(q, now)
        if not due:
            st.pop('sig', None); st.pop('at', None)
            return None
        held = planou_release.limit(q['branches'], now)
        sig = ' '.join(sorted(b['branch'] for b in q['branches'])) + (f' | {planou_release.limit_text(held)}' if held else '')
        if st.get('sig') == sig and _minutes(st.get('at'), now) < self.remind: return None
        st['sig'], st['at'] = sig, now.isoformat()
        return {'kind': 'due', 'branches': q['branches'], 'oldest_min': oldest, 'rules': self.rules_line(), 'held': held}

    def texto(self, v):
        if not v: return None
        if v['kind'] == 'error':
            return f'== RELEASE: fila de release ilegivel\n-- {v["error"]}\n   conserto: agent.py {paths.NAME} --release-queue list'
        if v['kind'] == 'stale':
            return (f'== RELEASE: integracao aberta desde {core.hora(v["since"])} ({v["hours"]:.1f} h)\n'
                    f'-- branches: {", ".join(v["branches"]) or "(nenhuma anotada)"}\n'
                    f'   o integrador ainda roda? Se nao: agent.py {paths.NAME} --release-queue abort (ou done)')
        bs = v['branches']
        out = [f'== RELEASE DEVIDO ({len(bs)} branch{"es" if len(bs) != 1 else ""} pronta{"s" if len(bs) != 1 else ""}; '
               f'a mais antiga espera ha {v["oldest_min"]:.0f} min)']
        for b in bs:
            pid = f'{b["pid"]}, ' if b.get('pid') else ''
            out.append(f'-- {b["branch"]} ({pid}pronta {core.hora(b.get("ready_at"))})')
        if v.get('rules'): out.append(f'   {v["rules"]}')
        if v.get('held'):
            out += [f'   {line}' for line in planou_release.limit_lines(v['held'], paths.NAME)]
        else:
            out.append(f'   disparar o integrador (batch-release): agent.py {paths.NAME} --release-queue start')
        return '\n'.join(out)

    # ------------------------------------------------------------ CLI

    def args(self, ap):
        ap.add_argument('--release-queue', nargs='*', metavar='ARG',
                        help='fila de release: list | add BRANCH [PID] | start [ok] | drop BRANCH | done [BRANCH...] | abort')

    def cli(self, a, ctx):
        args = getattr(a, 'release_queue', None)
        if args is None: return False
        cmd, rest = (args[0], args[1:]) if args else ('list', [])
        now = _now()
        self.load_rules(now)
        try: q = self.read()
        except ValueError as e: sys.exit(f'fila de release ilegivel: {e}')
        names = [b['branch'] for b in q['branches']]
        if cmd == 'list':
            due, oldest = self.due(q, now)
            if not q['branches']: print('fila de release vazia')
            for b in q['branches']:
                pid = f' {b["pid"]}' if b.get('pid') else ''
                print(f'-- {b["branch"]}{pid} · pronta {core.hora(b.get("ready_at"))} ({_minutes(b.get("ready_at"), now):.0f} min)')
            if q.get('integrating_since'):
                print(f'integrando desde {core.hora(q["integrating_since"])}: {", ".join(q.get("integrating") or [])}')
            else:
                print('release devido: ' + ('sim' if due else f'nao (dispara com {self.min_branches} ou mais, '
                                                               f'ou 1 esperando mais de {self.max_wait:.0f} min)'))
            print(self.rules_line())
            held = planou_release.limit(q['branches'], now)
            for line in planou_release.limit_lines(held, paths.NAME): print(line)
            return True
        if cmd == 'add':
            if not rest or not BRANCH_RE.fullmatch(rest[0]): sys.exit('uso: --release-queue add BRANCH [PID]')
            entry = {'branch': rest[0], 'ready_at': now.isoformat(), 'pid': rest[1] if len(rest) > 1 else None}
            q['branches'] = [b for b in q['branches'] if b['branch'] != rest[0]] + [entry]
            self.write(q)
            self.mirror(q)
            due, _ = self.due(q, now)
            print(f'{rest[0]} na fila de release ({len(q["branches"])} pronta(s))'
                  + ('; integracao em andamento: entra no proximo lote' if q.get('integrating_since') else
                     '; release devido: sim' if due else ''))
            if entry['pid']:            # after the queue is saved and mirrored: nothing here undoes or blocks the add
                cfg = getattr(ctx, 'cfg', None) or getattr(self.ctx, 'cfg', None) or {}
                line = attach_screenshots(cfg, rest[0], entry['pid'])
                if line: print(line)
            return True
        if cmd == 'start':
            if not q['branches']: sys.exit('fila de release vazia: nada a integrar')
            if q.get('integrating_since'): sys.exit(f'ja ha integracao desde {core.hora(q["integrating_since"])} (done ou abort antes)')
            # read now, not from the cache: this is the moment the release goes
            held = planou_release.limit(q['branches'], now, ttl=timedelta(0))
            if held and rest[:1] != ['ok']:
                sys.exit('release parado. ' + '; '.join(planou_release.limit_lines(held, paths.NAME)))
            q['integrating_since'], q['integrating'] = now.isoformat(), names
            self.write(q); self.mirror(q)
            print('integrando: ' + ', '.join(names)); return True
        if cmd == 'drop':
            if not rest: sys.exit('uso: --release-queue drop BRANCH')
            gone = [b for b in q['branches'] if b['branch'] in rest]
            q['branches'] = [b for b in q['branches'] if b['branch'] not in rest]
            q['integrating'] = [n for n in q.get('integrating') or [] if n not in rest]
            self.write(q); self.mirror(q)
            clean_shots(q, dropped=[b.get('pid') for b in gone])
            print('fora da fila: ' + (', '.join(_label(b) for b in gone) or '(nenhuma)')); return True
        if cmd == 'done':
            which = rest or q.get('integrating') or []
            if not which: sys.exit('uso: --release-queue done BRANCH... (sem integracao aberta)')
            gone = [b for b in q['branches'] if b['branch'] in which]
            q['branches'] = [b for b in q['branches'] if b['branch'] not in which]
            # the window of this release (PLN0155): a deploy the integrator logged inside it was announced by the session
            # when the integrator came back, and the hook deploy_log prints it without waking (`== DEPLOY JA AVISADO`)
            q['released'] = {'since': q.get('integrating_since') or now.isoformat(), 'at': now.isoformat()}
            # PLN0482: `released` is only the last one; the runner may read the deploy log after two dones, so every window
            # stays (last 30) with the branches it closed, and the hook matches a line against any of them
            q['released_log'] = ((q.get('released_log') if isinstance(q.get('released_log'), list) else [])
                                 + [dict(q['released'], branches=list(which))])[-30:]
            q['integrating_since'], q['integrating'] = None, []
            self.write(q); self.mirror(q)
            clean_shots(q)
            print('no release: ' + (', '.join(_label(b) for b in gone) or '(nenhuma da fila)')
                  + (f'; continuam na fila: {", ".join(b["branch"] for b in q["branches"])}' if q['branches'] else ''))
            return True
        if cmd == 'abort':
            q['integrating_since'], q['integrating'] = None, []
            self.write(q); self.mirror(q)
            print(f'integracao encerrada sem release; na fila: {", ".join(b["branch"] for b in q["branches"]) or "(nada)"}')
            return True
        sys.exit(f'--release-queue {cmd}: comando desconhecido (list, add, start, drop, done, abort)')


def _label(b):
    return b['branch'] + (f' ({b["pid"]})' if b.get('pid') else '')


# ---------------------------------------------------------------- screenshots of the delivery (PLN1034)

def _git(repo, *args, binary=False):
    r = subprocess.run(['git', '-C', repo, *args], capture_output=True, timeout=60)
    if r.returncode:
        err = r.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise RuntimeError(f'git {args[0]}: {(err or ["falhou"])[-1][:200]}')
    return r.stdout if binary else r.stdout.decode('utf-8', 'replace')


def _ref(repo, names):
    for n in names:
        r = subprocess.run(['git', '-C', repo, 'rev-parse', '--verify', '--quiet', f'{n}^{{commit}}'],
                           capture_output=True, timeout=30)
        if r.returncode == 0: return n
    return None


def _width_rank(path):
    """0, 1, 2 for a name that starts or ends with 1360, 390, 834 (home-1360.png, 390_login.png); 3 for the rest."""
    stem = os.path.splitext(os.path.basename(path))[0]
    for i, w in enumerate(SHOT_WIDTHS):
        if re.match(rf'{w}(?!\d)', stem) or re.search(rf'(?<!\d){w}$', stem): return i
    return len(SHOT_WIDTHS)


def screenshots(repo, branch):
    """(ref, [paths]) of the PNGs `branch` adds or changes under docs/ against origin/main (the merge base), in review
    order: 1360, 390, 834, then the rest. Only what is committed on the branch; never docs/fase4/. LookupError when
    the branch or the base is not in the repository."""
    ref = _ref(repo, (branch, f'origin/{branch}'))
    if not ref: raise LookupError(f'branch {branch} nao encontrada em {repo}')
    base = _ref(repo, SHOTS_BASES)
    if not base: raise LookupError(f'sem origin/main em {repo} (o main local pode estar defasado): nada anexado; rode git fetch')
    out = _git(repo, 'diff', '--name-only', '--no-renames', '--diff-filter=AM', '-z', f'{base}...{ref}', '--', 'docs/')
    files = [f for f in out.split('\0') if f.lower().endswith('.png') and not f.startswith(SHOTS_SKIP)]
    return ref, sorted(files, key=lambda f: (_width_rank(f), f))


def shot_name(path):
    """The attachment name of a screenshot: its folder and its name (docs/pln0001-x/home-1360.png ->
    pln0001-x-home-1360.png), the same on every add, so a new version replaces the old one in the task. The folder is
    docs/<delivery>, chosen by the dev: a credential-looking word in it ("senha", "token") is taken out of the name, so
    the local gate (planou.attachable, which reads the file name) judges only the file's own name (PLN1040). A folder
    changed that way, or one deeper than docs/<delivery>/, gets a short hash of its whole path (PLN1046): docs/pln0001-
    senha/ and docs/pln0001/ (or docs/a/tela/ and docs/b/tela/) would give the same name, and the second screenshot would
    replace the first one in the task. A plain docs/<delivery>/ keeps the name it had, so what is on a task stays."""
    from watch_core import planou
    parent = os.path.dirname(path)
    folder = plain = os.path.basename(parent) or 'docs'
    if planou._DENY_NAME.search(folder):
        while True:
            clean = planou._DENY_NAME.sub('', folder)
            if clean == folder: break
            folder = clean
        folder = re.sub(r'[-_.]{2,}', '-', folder).strip('-_.') or 'docs'
    if folder != plain or os.path.dirname(parent) not in ('docs', ''):      # hex never matches the credential words
        folder += '-' + hashlib.sha256(parent.encode('utf-8', 'replace')).hexdigest()[:8]
    return f'{folder}-{os.path.basename(path)}'


def shots_dir(pid):
    """data/release_shots/<pid>/: where the screenshots of a task wait for Planou (the tick sends what is pending)."""
    return os.path.join(paths.DATA_DIR, 'release_shots', re.sub(r'[^A-Za-z0-9_-]', '_', str(pid)))


def clean_shots(q, dropped=()):
    """Removes from data/release_shots/ the screenshots of every task no longer in the release queue (after `done` and
    `drop`): the ones Planou already has, refused for good or the person deleted; a branch added again takes them from
    git again (PLN1040). One that has not gone up yet (network, a 5xx) stays, for the tick to send (PLN1046), with one
    line in warnings.log (the task and how many, never the folder: its name is the delivery's); the folder goes once
    empty. `dropped`: the pids of the branches dropped now: their screenshots go whole, the pending ones too (the line
    says so). Never raises."""
    base = os.path.join(paths.DATA_DIR, 'release_shots')
    keep = {os.path.basename(shots_dir(b['pid'])) for b in q.get('branches') or [] if b.get('pid')}
    gone = {os.path.basename(shots_dir(p)) for p in dropped if p} - keep
    try:
        names = os.listdir(base)
    except OSError:
        return
    planou = None
    try:
        from watch_core import planou
        planou.configure_agent(paths.NAME, paths.ROOT)
    except Exception:
        planou = None                               # nothing tells what went up: only the dropped folders go
    for n in names:
        if n in keep: continue
        d = os.path.join(base, n)
        if planou is None:
            if n in gone: shutil.rmtree(d, ignore_errors=True)
            continue
        try:
            pending = [f for f in sorted(os.listdir(d)) if _shot_pending(planou, n, os.path.join(d, f))]
        except Exception:
            pending = []
        if n in gone or not pending:
            shutil.rmtree(d, ignore_errors=True)
        else:
            for f in os.listdir(d):
                if f not in pending:
                    try: os.remove(os.path.join(d, f))
                    except OSError: pass
        if pending:
            said = ('apagada(s) com a branch fora da fila (drop)' if n in gone else
                    'guardada(s) no computador ate o tick enviar')
            try:
                planou.note_warnings(n, [f'{len(pending)} captura(s) da entrega ainda nao subiram para o Planou: {said}'])
            except Exception:
                pass


def _shot_pending(planou, pid, path):
    """True while a screenshot in data/release_shots/<pid>/ waits for the tick: registered to go (attach_files.json)
    and Planou does not have its content yet (attachments.json: the same sha256 with an id, under its key or another
    one of the task). Sent, refused for good or deleted by the person is not pending; neither is a file nobody will send
    (not registered: Planou was off when it was read)."""
    key = planou.file_key(path)
    if key not in (planou._registry().get(str(pid)) or {}): return False
    code = planou._task_ref(pid)[0]
    cache = planou._load('attachments.json', {})
    e = cache.get(f'{code}|{key}') or {}
    if e.get('result') == 'ignored': return False
    with open(path, 'rb') as f: sha = hashlib.sha256(f.read()).hexdigest()
    if e.get('sha') == sha and (e.get('id') or e.get('refused')): return False
    return not any(k.startswith(f'{code}|') and isinstance(v, dict) and v.get('sha') == sha and v.get('id')
                   for k, v in cache.items())


def _on_task(planou, pid):
    """(source_keys, how many) of the attachments on the task now (GET; Planou hides the ones the person sent to the
    Lixeira), or None when Planou does not answer: then the agent's own record counts (known_attachments), which only
    learns of a deletion when a new version is refused and knows nothing of the person's attachments. The count is what
    Planou counts for its own limit (Attachments:MaxPerTask, 100 by default): every live attachment, the person's ones
    without source_key too (PLN1046)."""
    try:
        res = planou.task_attachments(pid)
    except planou.PlanouError:
        return None
    rows = res.get('attachments') if isinstance(res, dict) else None
    if not isinstance(rows, list): return None
    rows = [a for a in rows if isinstance(a, dict)]
    return {str(a['source_key']) for a in rows if a.get('source_key')}, len(rows)


def attach_screenshots(cfg, branch, pid, dest=None, now=None):
    """Attaches to task `pid` the screenshots `branch` delivers (screenshots()), read with `git show` into
    data/release_shots/<pid>/ (kept while the branch is in the queue: the tick sends again what Planou did not take now;
    `done` and `drop` remove what went up). At most SHOTS_MAX screenshots per delivery: the ones of this branch already on
    the task count, so a task back from rework with new screenshots gets them (the person reviews the new delivery, not
    the old screens). The task as a whole never goes past MAX_PER_TASK attachments, counted on every live attachment on it,
    the person's ones and older deliveries' screenshots too (PLN1046). Both are counted on the task now (a screenshot the
    person deleted frees its place; the local folder is gone after a `done`, the task's attachments are not). A file with
    the same name replaces the old one and takes no new place; an unchanged file is not sent again (attach_file), and a
    file with a credential-looking name never goes. Returns one line for the output, or None
    (nothing to attach, no batch release repository, Planou off). Never raises: a failure is one AVISO line, and the add
    stands."""
    repo = column_roles.release_repo(cfg or {})
    if not repo or not pid: return None
    try:
        from watch_core import planou
        planou.configure_agent(paths.NAME, paths.ROOT)
        if not planou.active(): return None
        ref, files = screenshots(repo, branch)
        if not files: return None
        dest = dest or shots_dir(pid)
        os.makedirs(dest, exist_ok=True)
        listed, known = _on_task(planou, pid), planou.known_attachments(pid)
        live = listed[0] if listed else None
        on_task = live if live is not None else known
        # SHOTS_MAX is per delivery: this branch's screenshots already on the task (PLN1046: the local folder goes with
        # `done`, so the task is what counts); a deleted one frees its place. The room holds the task's total.
        placed = {planou.file_key(shot_name(f)) for f in files} & set(on_task)
        room = planou.MAX_PER_TASK - (listed[1] if listed else len(known))
        counts, warn, held, skipped = {}, None, False, {}
        for f in files:
            if planou._DENY_NAME.search(os.path.basename(f)):        # the file's own name: never read nor written
                counts['blocked'] = counts.get('blocked', 0) + 1
                warn = warn or f'{os.path.basename(f)[:80]}: nome de credencial, nao vai como anexo'
                continue
            name = shot_name(f)
            new_there = planou.file_key(name) not in on_task
            if new_there and (len(placed) >= SHOTS_MAX or room <= 0):
                why = 'delivery' if len(placed) >= SHOTS_MAX else 'task'
                skipped[why] = skipped.get(why, 0) + 1; continue
            data = _git(repo, 'show', f'{ref}:{f}', binary=True)
            local = os.path.join(dest, name)
            _write(local, data)
            if held:                                    # Planou did not answer: the tick sends the rest
                planou.register_files(pid, [local], now); outcome, lines = 'pending', []
            else:
                outcome, lines = planou.attach_file(pid, local, now=now)
                if outcome == 'pending' and lines: held = True
            if new_there and live is not None and outcome == 'unchanged' and planou.file_key(name) in known:
                outcome = 'ignored'                     # sent before and no longer on the task: the person deleted it
            if new_there and outcome in ('sent', 'pending'):
                placed.add(planou.file_key(name)); room -= 1
            counts[outcome] = counts.get(outcome, 0) + 1
            if lines and outcome in ('refused', 'blocked', 'inactive') and not warn: warn = lines[0]
        return _shots_line(branch, pid, counts, skipped, warn, planou.MAX_PER_TASK)
    except Exception as e:
        return f'AVISO (planou): capturas de {branch} nao anexadas a {pid} ({type(e).__name__}: {e})'[:400]


def _write(path, data):
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'wb') as f: f.write(data)
    os.replace(tmp, path)


SHOTS_SAID = (('sent', 'enviada(s)'), ('unchanged', 'igual(is) ao que ja esta la'),
              ('pending', 'vao no proximo tick'), ('refused', 'recusada(s) pelo Planou'),
              ('blocked', 'barrada(s) no filtro local'), ('missing', 'sumida(s)'),
              ('not_found', 'sem a tarefa no Planou'), ('ignored', 'apagada(s) pela pessoa'),
              ('inactive', 'nao enviada(s): Planou desligado ou confidencialidade minima'))


def _shots_line(branch, pid, counts, skipped, warn, max_task):
    """`skipped` says which limit held each one left out: the delivery's SHOTS_MAX, or the task's attachments (only this
    one asks the person to delete an old attachment)."""
    parts = [f'{counts[k]} {said}' for k, said in SHOTS_SAID if counts.get(k)]
    if skipped.get('delivery'): parts.append(f'{skipped["delivery"]} fora (limite de {SHOTS_MAX} capturas da entrega)')
    if skipped.get('task'): parts.append(f'{skipped["task"]} fora (limite de {max_task} anexos da tarefa)')
    if not parts: return None
    head = 'AVISO (planou): ' if warn else ''
    return f'{head}capturas de {branch} em {pid}: ' + ', '.join(parts) + (f' ({warn})' if warn else '')
