#!/usr/bin/env python3
"""Hook convite_email (phase cruza): an invite also arrives as an e-mail (sometimes with the raw subject, no "Convite:").
The calendar is the source of the meeting, so an open 'email' pending item whose subject matches an INVITE of this tick
is resolved here ("convite (ver CALENDARIO)") instead of being chased twice. The resolution is listed with the e-mail
source's own resolutions.

Config ("ganchos": [{"tipo": "convite_email", ...}]):
  fonte_email       default "email"
  fonte_calendario  default "calendar"
"""
import core
from adapters import Gancho as Base
from adapters.outlook_calendar import norm_titulo as _norm_titulo


class Gancho(Base):
    tipo = 'convite_email'

    def configura(self):
        self.email = self.cfg.get('fonte_email', 'email')
        self.cal = self.cfg.get('fonte_calendario', 'calendar')

    def cruza(self, ctx):
        res, s, agora = ctx.res, ctx.s, ctx.agora
        itens = res.get(self.cal, {}).get('itens', [])
        titulos = {_norm_titulo(it['ev']['titulo']) for it in itens if it['tipo'] == 'convite'}
        titulos |= {t[:80] for t in titulos} | {_norm_titulo(it['ev']['titulo'][:80]) for it in itens if it['tipo'] == 'convite'}
        for p in core.pend_abertas(s, 'email') if self.email in res else []:
            if _norm_titulo(p['assunto']) in titulos:
                core.resolve(p, agora, 'convite (ver CALENDARIO)'); res[self.email].setdefault('resolvidas', []).append((p, 'convite (ver CALENDARIO)'))
        return []
