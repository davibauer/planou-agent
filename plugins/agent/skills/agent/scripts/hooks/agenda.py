#!/usr/bin/env python3
"""Hook agenda: wakes the session at a fixed time with a block of text (a reminder written in the config).

From the first tick at or after "hora" (instance timezone), once a day per entry, it prints

  == AGENDA <nome>: <texto>

and, like any other hook block, that wakes the session. The state key is the entry name (state 'agenda' =
{nome: 'YYYY-MM-DD'}), so several agenda entries live side by side, each with its own day. A dry tick (test mode) prints
what is due but writes nothing. A tick that comes late the same day (runner stopped at "hora") still fires; the next day
starts clean. By default only on business days (Monday to Friday); "dias": "todos" includes the weekend.

Config ("hooks": [{"type": "agenda", ...}]):
  nome     required, unique among the agenda entries (the state key and the block title)
  texto    required, what the session must do when woken
  hora     "HH:MM" (default "09:00")
  dias     "uteis" (default) or "todos"
  semanal  optional, 0 (Monday) to 6 (Sunday): only on that weekday (with "uteis", 5 and 6 never fire)
"""
import core
from adapters import Gancho as Base


class Gancho(Base):
    tipo = 'agenda'
    json_key = None
    roda_em_dry = True

    def configura(self):
        h, m = (self.cfg.get('hora') or '09:00').split(':')
        self.hora = (int(h), int(m))
        self.texto_agenda = str(self.cfg.get('texto') or '').strip()
        self.todos = self.cfg.get('dias') == 'todos'
        self.semanal = self.cfg.get('semanal')

    def devido(self, s, agora):
        d = agora.astimezone(core.BRT)
        if not self.todos and d.weekday() >= 5: return False
        if self.semanal is not None and d.weekday() != self.semanal: return False
        if (d.hour, d.minute) < self.hora: return False
        return (s.get('agenda') or {}).get(self.nome) != d.strftime('%Y-%m-%d')

    def run(self, ctx):
        if not self.texto_agenda or not self.devido(ctx.s, ctx.agora): return None
        if not ctx.dry:
            marcas = ctx.s.get('agenda')
            if not isinstance(marcas, dict): marcas = ctx.s['agenda'] = {}
            marcas[self.nome] = ctx.agora.astimezone(core.BRT).strftime('%Y-%m-%d')
        return f'== AGENDA {self.nome}: {self.texto_agenda}'
