#!/usr/bin/env python3
"""Hook lunch: lunch status on Slack (writes the user's OWN profile: users.profile.set). Live instances only.

Business day, first tick inside the window [inicio, fim) of the instance timezone: sets the status text/emoji with
status_expiration at "fim", so Slack clears it by itself even if the watcher stops. Once a day (state 'lunch').
Does not overwrite a manual status (any text other than empty or its own). Off: --lunch off (state 'lunch'='off').

Config ("ganchos": [{"tipo": "lunch", ...}]):
  inicio, fim   "HH:MM" (default "12:00", "13:00")
  texto, emoji  status text and emoji (default "Lunch", ":knife_fork_plate:")
  fonte         the slack source that holds the session (default "slack")
"""
import json, sys

import core
from adapters import Gancho as Base


def _hm(x, pad):
    h, m = (x or pad).split(':'); return (int(h), int(m))


class Gancho(Base):
    tipo = 'lunch'
    json_key = 'lunch'

    def configura(self):
        self.ini, self.fim = _hm(self.cfg.get('inicio'), '12:00'), _hm(self.cfg.get('fim'), '13:00')
        self.txt, self.emoji = self.cfg.get('texto', 'Lunch'), self.cfg.get('emoji', ':knife_fork_plate:')
        self.fonte = self.cfg.get('fonte', 'slack')

    def lunch(self, s, agora, sp):
        """Devolve uma linha para imprimir, ou None se nao fez nada."""
        if s.get('lunch') == 'off': return None
        d = agora.astimezone(core.BRT)
        if d.weekday() >= 5 or not (self.ini <= (d.hour, d.minute) < self.fim): return None
        hoje = d.strftime('%Y-%m-%d')
        if s.get('lunch') == hoje: return None
        core.exige_live('slack users.profile.set (lunch)')
        try:
            atual = sp.api('users.profile.get')['profile']
            txt = atual.get('status_text') or ''
            if txt and txt != self.txt:
                s['lunch'] = hoje
                return f'LUNCH: status manual "{txt}" mantido, nao sobrescrevi'
            fim = d.replace(hour=self.fim[0], minute=self.fim[1], second=0, microsecond=0)
            sp.api('users.profile.set', profile=json.dumps({'status_text': self.txt, 'status_emoji': self.emoji,
                                                            'status_expiration': int(fim.timestamp())}))
            s['lunch'] = hoje
            return f'LUNCH: status {self.emoji} {self.txt} ate {fim.strftime("%H:%M")}'
        except core.Teste: raise
        except SystemExit as e:
            return f'LUNCH: nao consegui setar o status ({e.code})'
        except Exception as e:
            return f'LUNCH: nao consegui setar o status ({type(e).__name__}: {e})'

    def run(self, ctx):
        f = ctx.fontes.get(self.fonte)
        if not f: return None
        return self.lunch(ctx.s, ctx.agora, _Lazy(f))

    def args(self, ap):
        ap.add_argument('--lunch', choices=['on', 'off'], help='liga/desliga o status de almoco no Slack e sai')

    def cli(self, a, ctx):
        if not a.lunch: return False
        s = ctx.s; s['lunch'] = 'off' if a.lunch == 'off' else None; core.save_state(s)
        print(f'lunch: {a.lunch}'); return True


class _Lazy:
    """Imports slack_pull only when the hook really calls the API."""
    def __init__(self, fonte): self.f, self.sp = fonte, None
    def api(self, *a, **k):
        if self.sp is None: self.sp = self.f.api()
        return self.sp.api(*a, **k)
