#!/usr/bin/env python3
"""Hook release_due: wakes the session when a batch release is due (behavior batch-release).

Development workers end on a branch ready for release; the session adds it to the release queue of the instance (a JSON
file inside the instance folder). This hook reads that file on every heavy tick and prints `== RELEASE DEVIDO` when
there are `min_branches` or more ready branches, or one waiting more than `max_wait_min` minutes. It prints once per set
of branches (again after `remind_min` minutes if nothing happened) and stays quiet while an integration is running
(`start` ... `done`/`abort`); an integration older than `stale_h` hours is reported once.

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
           "integrating": ["feat/x", ...], "released": {"since": ISO, "at": ISO}}   (released: the last `done`, PLN0155)

CLI (the session never edits the file by hand):
  agent.py <instance> --release-queue [list]          the queue and whether a release is due
  agent.py <instance> --release-queue add BRANCH [PID] a branch is ready (again: ready_at is now)
  agent.py <instance> --release-queue start            the integrator starts with the branches in the queue
  agent.py <instance> --release-queue drop BRANCH      the culprit goes back to its worker; the integration goes on
  agent.py <instance> --release-queue done [BRANCH...] released (default: the branches of the integration); ends it
  agent.py <instance> --release-queue abort            the integration ended without release; branches stay queued

Never raises in the tick: an unreadable file is reported once (same error, same line) and the tick goes on.
"""
import json, os, re, sys
from datetime import datetime, timezone

import core, paths, planou_release
from watch_core import fileio
from adapters import Gancho as Base

BRANCH_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,200}')


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
        sig = ' '.join(sorted(b['branch'] for b in q['branches']))
        if st.get('sig') == sig and _minutes(st.get('at'), now) < self.remind: return None
        st['sig'], st['at'] = sig, now.isoformat()
        return {'kind': 'due', 'branches': q['branches'], 'oldest_min': oldest, 'rules': self.rules_line()}

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
        out.append(f'   disparar o integrador (batch-release): agent.py {paths.NAME} --release-queue start')
        return '\n'.join(out)

    # ------------------------------------------------------------ CLI

    def args(self, ap):
        ap.add_argument('--release-queue', nargs='*', metavar='ARG',
                        help='fila de release: list | add BRANCH [PID] | start | drop BRANCH | done [BRANCH...] | abort')

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
            return True
        if cmd == 'start':
            if not q['branches']: sys.exit('fila de release vazia: nada a integrar')
            if q.get('integrating_since'): sys.exit(f'ja ha integracao desde {core.hora(q["integrating_since"])} (done ou abort antes)')
            q['integrating_since'], q['integrating'] = now.isoformat(), names
            self.write(q); self.mirror(q)
            print('integrando: ' + ', '.join(names)); return True
        if cmd == 'drop':
            if not rest: sys.exit('uso: --release-queue drop BRANCH')
            gone = [b for b in q['branches'] if b['branch'] in rest]
            q['branches'] = [b for b in q['branches'] if b['branch'] not in rest]
            q['integrating'] = [n for n in q.get('integrating') or [] if n not in rest]
            self.write(q); self.mirror(q)
            print('fora da fila: ' + (', '.join(_label(b) for b in gone) or '(nenhuma)')); return True
        if cmd == 'done':
            which = rest or q.get('integrating') or []
            if not which: sys.exit('uso: --release-queue done BRANCH... (sem integracao aberta)')
            gone = [b for b in q['branches'] if b['branch'] in which]
            q['branches'] = [b for b in q['branches'] if b['branch'] not in which]
            # the window of this release (PLN0155): a deploy the integrator logged inside it was announced by the session
            # when the integrator came back, and the hook deploy_log prints it without waking (`== DEPLOY JA AVISADO`)
            q['released'] = {'since': q.get('integrating_since') or now.isoformat(), 'at': now.isoformat()}
            q['integrating_since'], q['integrating'] = None, []
            self.write(q); self.mirror(q)
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
