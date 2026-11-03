#!/usr/bin/env python3
"""Hook meeting_copilot: keeps the meeting copilot's context pack ready, and warms the model's prompt cache before a
meeting. Live instances only. It prints nothing and never breaks the tick.

Each heavy tick starts `meeting-copilot prepare` detached (the tick does not wait): it rebuilds only the parts of the
pack whose source changed (PRs, issues, minutes, memories, handoff) into <instance>/data/meeting-copilot/. The busy
blocks of the agenda cache (the gravacoes source's "agenda", blocks without title) that start in the next 15 minutes
go along: about 2 min before one starts, prepare finalizes the pack and sends one minimal call with the exact system
prompt of the card calls, once per block. Audio capture is never started here; that stays `meeting-copilot start`.
One line per run that changed something or failed goes to cache/runner/avisos.log; never FONTE QUEBRADA.

Config ("ganchos": [{"tipo": "meeting_copilot"}], opt-in per instance):
  agenda       false to skip the pre-warm (default true when the instance has an agenda cache)
  offline      true: the pack without GitHub and Jira
"""
import json, os, subprocess, sys
from datetime import timedelta

import core, paths
from adapters import Gancho as Base



def _copilot():
    """The meeting-copilot scripts: the sibling skill of this plugin, else the installed skill."""
    for d in (os.path.join(os.path.dirname(paths.SKILL_DIR), 'meeting-copilot', 'scripts'),
              os.path.expanduser('~/.claude/skills/meeting-copilot/scripts')):
        if os.path.isdir(os.path.join(d, 'meeting_copilot')): return os.path.realpath(d)
    return None


NEXT = timedelta(minutes=15)


def _log(msg):
    try:
        os.makedirs(paths.RUNNER_DIR, exist_ok=True)
        with open(os.path.join(paths.RUNNER_DIR, 'avisos.log'), 'a', encoding='utf-8') as f:
            from datetime import datetime
            f.write(f'{datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}\tmeeting-copilot: {msg}\n')
    except OSError:
        pass


class Gancho(Base):
    tipo = 'meeting_copilot'

    def blocos(self, ctx):
        """Busy blocks starting in the next 15 min, from the agenda cache of the gravacoes source (meetings only)."""
        if self.cfg.get('agenda') is False: return []
        src = next((f for f in ctx.cfg.get('sources') or ctx.cfg.get('fontes') or []
                    if f.get('type', f.get('tipo')) == 'gravacoes'), None)
        ag = (src or {}).get('agenda') or {}
        if ag.get('tipo', 'cache') != 'cache': return []
        from sources import agenda_cache
        agenda_cache.configura(ag)
        fora = [w.lower() for w in (src.get('nao_reuniao') or [])]
        out = []
        for b in agenda_cache.janela(ctx.agora, ctx.agora + NEXT):
            if any(w in (b.get('titulo') or '').lower() for w in fora): continue
            out.append({'id': b['id'], 'ini': b['ini']})
        return out

    def run(self, ctx):
        if not core.LIVE or ctx.dry: return None
        try:
            blocks = self.blocos(ctx)
        except Exception as e:
            blocks = []; _log(f'agenda unreadable ({type(e).__name__}: {str(e)[:120]}), refresh only')
        cop = _copilot()
        if not cop: _log('meeting-copilot skill not found, nothing to prepare'); return None
        cmd = [sys.executable, '-m', 'meeting_copilot', 'prepare', '--instance', paths.NAME,
               '--log', os.path.join(paths.RUNNER_DIR, 'avisos.log')]
        if blocks: cmd += ['--blocks', json.dumps(blocks)]
        if self.cfg.get('offline'): cmd.append('--offline')
        try:
            subprocess.Popen(cmd, cwd=cop, env=dict(os.environ, PYTHONPATH=cop), start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            _log(f'prepare did not start: {type(e).__name__}: {str(e)[:120]}')
        return None
