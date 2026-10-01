#!/usr/bin/env python3
"""Hook presenca: Slack presence away after hours (users.setPresence on the user's own account). Live instances only.

Business day, first tick from "away" on: presence=away, once a day (state 'away' = date). A manual 'away' stays until
someone changes it, so the first tick from "auto" on the next business day sets presence=auto back (state 'auto' = date)
-- otherwise the user would look offline all day. Off: --away off.

Config ("ganchos": [{"tipo": "presenca", ...}]):
  away, auto   "HH:MM" (default "18:30", "08:00")
  fonte        the slack source that holds the session (default "slack")
"""
import core
from adapters import Gancho as Base
from adapters.lunch import _hm, _Lazy


class Gancho(Base):
    tipo = 'presenca'
    json_key = 'presenca'

    def configura(self):
        self.away, self.auto = _hm(self.cfg.get('away'), '18:30'), _hm(self.cfg.get('auto'), '08:00')
        self.fonte = self.cfg.get('fonte', 'slack')

    def presenca(self, s, agora, sp):
        """Devolve uma linha para imprimir, ou None se nao fez nada."""
        if s.get('away') == 'off': return None
        d = agora.astimezone(core.BRT)
        if d.weekday() >= 5: return None
        hoje = d.strftime('%Y-%m-%d'); hm = (d.hour, d.minute)
        if hm >= self.away and s.get('away') != hoje: alvo, chave = 'away', 'away'
        elif self.auto <= hm < self.away and s.get('auto') != hoje: alvo, chave = 'auto', 'auto'
        else: return None
        core.exige_live('slack users.setPresence')
        try:
            sp.api('users.setPresence', presence=alvo)
            s[chave] = hoje
            return f'PRESENCA: {alvo} no Slack' + (f' (ate o primeiro tick apos {self.auto[0]:02d}:{self.auto[1]:02d})' if alvo == 'away' else '')
        except core.Teste: raise
        except SystemExit as e:
            return f'PRESENCA: nao consegui setar {alvo} ({e.code})'
        except Exception as e:
            return f'PRESENCA: nao consegui setar {alvo} ({type(e).__name__}: {e})'

    def run(self, ctx):
        f = ctx.fontes.get(self.fonte)
        if not f: return None
        return self.presenca(ctx.s, ctx.agora, _Lazy(f))

    def args(self, ap):
        ap.add_argument('--away', choices=['on', 'off'], help='liga/desliga o away automatico no Slack depois do expediente e sai')

    def cli(self, a, ctx):
        if not a.away: return False
        s = ctx.s; s['away'] = 'off' if a.away == 'off' else None; core.save_state(s); print(f'away: {a.away}'); return True
