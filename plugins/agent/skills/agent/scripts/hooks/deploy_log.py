#!/usr/bin/env python3
"""Hook deploy_log: wakes the session once per new deploy (behavior batch-release, "Aviso de deploy").

The integrator writes one line per deploy in a log file (tab separated: time, commit, version and what changed for the
people who use it). This hook reads that file on every heavy tick and prints `== DEPLOY NOVO (n)` with the lines written
since the last one it saw. The first read only records where the log is (nothing old is announced); a line still being
written (no newline yet) waits for the next tick; if the last seen line is gone (log rotated or edited), it starts over
from the end without announcing.

Config: {"type": "deploy_log", "path": "<log file>", "max_lines": 5, "to_planou": true, "release_file": "data/release_queue.json",
         "hold_h": 3}
path is required (~ and {instance} expanded; relative = inside the instance folder).

Version history in Planou: when the instance talks to Planou (a "planou" block with a project, live, with a key) and
"to_planou" is not false, each deploy line (and each rollback line the deploy script writes) goes to POST
/v1/agent/projects/<project>/releases: version, commit, time and what changed. It has its own cursor, apart from the
announcement: the first run sends the whole log (Planou ignores a line it already has), a failed send is retried on the
next tick (never announced again), at most `post_max` (10) lines per tick. Dry ticks send nothing.

Health check of the deploy script (PLN0108): the automatic rollback is a rollback line (announced as
`-- volta para vX (rollback)`, sent as kind rollback); the health alert line (time, commit, "alerta de saúde", text:
the new version stayed live with the check failing) is announced as `-- ALERTA DE SAUDE da vX (ficou no ar)` and is
never sent to the version history.

Deploy the session already announced (PLN0155, d-145): with batch-release the session announces the deploy when the
integrator comes back, and the wake of `== DEPLOY NOVO` then only read it again. The hook reads the release queue of the
instance (`release_file`, default the one of the release_due hook: data/release_queue.json):
- an integration running (`--release-queue start`, not done nor aborted, younger than `hold_h`, 3 h): new lines wait
  (they are read again on the next tick; the version history in Planou goes on at once);
- every new line inside the window of the last `--release-queue done` (from the start of that integration to the done,
  by the minute of the line): `== DEPLOY JA AVISADO (n)`, same lines, which the runner never wakes for (it goes with the
  next tick that wakes);
- anything else (a deploy by hand, a rollback, a release this session did not close): `== DEPLOY NOVO (n)`, as before.

Never raises in the tick: a missing or unreadable log is reported once (same error, same line) and the tick goes on.
"""
import json, re
from datetime import datetime, timedelta, timezone

import paths, planou_release
from adapters import Gancho as Base

VERSION_RE = re.compile(r'\bv\d+\.\d+\.\d+\b')
COMMIT_RE = re.compile(r'^[0-9a-fA-F]{4,40}$')


def parse(line):
    """A deploy log line as Planou's releases route takes it, or None (not a deploy line). The line: time, commit,
    version (with notes) and what changed, tab separated; the rollback line of the deploy script has three fields:
    time, commit and "rollback para vX.Y.Z de vA.B.C (...)". The health alert line (PLN0108, exit 7: the new version
    stayed live with the check failing) is not a deploy: time, commit, "alerta de saúde" and the text."""
    parts = [p.strip() for p in line.split('\t')]
    if len(parts) < 3 or not COMMIT_RE.match(parts[1]): return None
    if line_kind(line) == 'alert': return None
    m = VERSION_RE.search(parts[2])
    if not m: return None
    rollback = parts[2].lower().startswith('rollback')
    when = _line_time(parts[0])
    changes = '\t'.join(parts[3:]).strip() if len(parts) > 3 else (parts[2] if rollback else '')
    if rollback: changes = re.sub(r'\s*Backup\b.*$', '', changes).strip()      # the dump's name is not for people
    return {'version': m.group(0), 'commit': parts[1].lower(), 'deployed_at': when, 'changes': changes,
            'kind': 'rollback' if rollback else 'deploy'}


def line_kind(line):
    """'alert', 'rollback' or 'deploy' by the third field of a log line."""
    parts = line.split('\t')
    f = parts[2].strip().lower() if len(parts) >= 3 else ''
    return 'alert' if f.startswith('alerta') else 'rollback' if f.startswith('rollback') else 'deploy'


def _line_time(v):
    """ISO time of the first field (the deploy script writes minutes, the integrator also seconds), or None."""
    for fmt in ('%Y-%m-%d %H:%M %z', '%Y-%m-%d %H:%M:%S %z'):
        try: return datetime.strptime(v.strip(), fmt).isoformat()
        except ValueError: pass
    return None


def _safe(text):
    """What changed, without anything that looks like a credential ('' when nothing safe is left)."""
    try:
        from watch_core import planou
        return planou.clean_text(text, 4000) or ''
    except Exception:
        return ''


def _when(v):
    """An ISO time (any offset) as an aware datetime, or None."""
    if not isinstance(v, str) or not v: return None
    try: t = datetime.fromisoformat(v.replace('Z', '+00:00'))
    except ValueError: return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class Gancho(Base):
    tipo = 'deploy_log'
    json_key = 'deploy_log'
    roda_em_dry = True

    def configura(self):
        p = self.cfg.get('path')
        self.file = paths.expand(p) if isinstance(p, str) and p.strip() else None
        try: self.max_lines = max(1, int(self.cfg.get('max_lines', 5)))
        except (TypeError, ValueError): self.max_lines = 5
        try: self.post_max = max(1, int(self.cfg.get('post_max', 10)))
        except (TypeError, ValueError): self.post_max = 10
        self.to_planou = self.cfg.get('to_planou', True) is not False
        rf = self.cfg.get('release_file')
        if not isinstance(rf, str) or not rf.strip():
            rd = next((h for h in ((getattr(self.ctx, 'cfg', None) or {}).get('hooks') or [])
                       if isinstance(h, dict) and h.get('type') == 'release_due'), {})
            rf = rd.get('ready_file') if isinstance(rd.get('ready_file'), str) else 'data/release_queue.json'
        self.release_file = paths.expand(rf)
        try: self.hold_h = max(0.0, float(self.cfg.get('hold_h', 3)))
        except (TypeError, ValueError): self.hold_h = 3.0

    def release(self):
        """The release queue of the instance ({} when missing or unreadable: every deploy is new)."""
        try:
            with open(self.release_file, encoding='utf-8') as f: q = json.load(f)
            return q if isinstance(q, dict) else {}
        except (OSError, ValueError):
            return {}

    def holding(self, q, now=None):
        """True while an integration this session started is running (the session announces its deploy on return)."""
        since = _when(q.get('integrating_since'))
        now = now or datetime.now(timezone.utc)
        return bool(since) and now - since < timedelta(hours=self.hold_h)

    def announced(self, q, line):
        """True for a deploy line inside the window of the last `--release-queue done` (by the minute of the line)."""
        r = q.get('released') if isinstance(q.get('released'), dict) else {}
        since, at = _when(r.get('since')), _when(r.get('at'))
        rel = parse(line)
        t = _when(rel and rel['deployed_at'])
        if not (since and at and t): return False
        return since.replace(second=0, microsecond=0) <= t <= at

    def lines(self):
        with open(self.file, encoding='utf-8', errors='replace') as f: text = f.read()
        if text and not text.endswith('\n'): text = text[:text.rfind('\n') + 1]     # the last line is still being written
        return [l.rstrip('\r') for l in text.split('\n') if l.strip()]

    def run(self, ctx):
        st = ctx.s.setdefault('deploy_log', {})
        try:
            if not self.file: raise ValueError('o gancho deploy_log precisa de "path" no config')
            lines = self.lines()
        except Exception as e:
            msg = f'{type(e).__name__}: {e}'[:300]
            if st.get('err') == msg: return None
            st['err'] = msg
            return {'kind': 'error', 'error': msg}
        st.pop('err', None)
        if not ctx.dry and self.to_planou: self.post(st, lines)
        if 'last' not in st:                                   # first read: remember where the log is, announce nothing
            st['last'] = lines[-1] if lines else ''
            return None
        last = st['last']
        if last:
            idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i] == last), None)
            if idx is None:                                    # rotated or edited: start over from the end, quietly
                st['last'] = lines[-1] if lines else ''
                return None
            new = lines[idx + 1:]
        else:
            new = lines
        if not new: return None
        q = self.release()
        if self.holding(q): return None                        # the integrator is still out: its return announces it
        st['last'] = new[-1]
        kind = 'announced' if all(self.announced(q, l) for l in new) else 'new'
        return {'kind': kind, 'lines': new[-self.max_lines:], 'skipped': max(0, len(new) - self.max_lines)}

    def post(self, st, lines):
        """Sends the lines after st['posted'] to Planou's version history; stops at the first failure (next tick retries).
        A line that is not a deploy line is skipped."""
        if not lines or not planou_release.active(): return
        posted = st.get('posted')
        idx = next((i for i in range(len(lines) - 1, -1, -1) if lines[i] == posted), None) if posted else None
        pending = lines[idx + 1:] if idx is not None else lines          # never sent, or the log was rewritten: all
        for line in pending[:self.post_max]:
            rel = parse(line)
            if rel:
                ok, why = planou_release.post_release(rel['version'], rel['commit'], rel['deployed_at'],
                                                      _safe(rel['changes']), rel['kind'])
                if not ok:
                    st['post_err'] = why
                    return
            st['posted'] = line
        st.pop('post_err', None)

    def texto(self, v):
        if not v: return None
        if v['kind'] == 'error':
            return f'== DEPLOY: log de deploys ilegivel\n-- {v["error"]}'
        head = 'DEPLOY JA AVISADO' if v['kind'] == 'announced' else 'DEPLOY NOVO'
        out = [f'== {head} ({len(v["lines"]) + v["skipped"]})']
        if v['skipped']: out.append(f'   ({v["skipped"]} mais antigos fora da lista: ver o log)')
        for l in v['lines']:
            parts = l.split('\t')
            m = VERSION_RE.search(l)
            kind = line_kind(l)
            if kind == 'alert': head = 'ALERTA DE SAUDE' + (f' da {m.group(0)} (ficou no ar)' if m else '')
            elif kind == 'rollback': head = (f'volta para {m.group(0)} (rollback)') if m else 'rollback'
            else: head = (m.group(0) + ' no ar') if m else 'deploy'
            if len(parts) >= 3:
                out.append(f'-- {head} · {parts[0].strip()} · commit {parts[1].strip()}')
                out.append('   ' + '\t'.join(parts[2:]).strip()[:2000])
            else:
                out.append(f'-- {head}')
                out.append('   ' + l.strip()[:2000])
        return '\n'.join(out)
