#!/usr/bin/env python3
"""Hook cruza_mr (phase cruza): a chat pending item that cites a merge request (p['mr'], set by the teams source's
"ref_regex") and the merge request itself are the same demand. MR merged/closed resolves the pending item; MR marked
status=ignorar ignores it. The other way round: --pendente pN status=ignorar marks the cited MR as ignorar too.

Config ("ganchos": [{"tipo": "cruza_mr", ...}]):
  fonte_pendencia  queue of the pending items (default "teams")
  fonte_mr         the source whose result has mrs_sumidas and whose state has mrs (default "gitlab")
"""
import core
from adapters import Gancho as Base


class Gancho(Base):
    tipo = 'cruza_mr'

    def configura(self):
        self.fila = self.cfg.get('fonte_pendencia', 'teams')
        self.fonte = self.cfg.get('fonte_mr', 'gitlab')

    def cruza(self, ctx):
        """MR mergeada/fechada resolve a pendencia; MR marcada 'ignorar' ignora a pendencia."""
        s, res, agora = ctx.s, ctx.res, ctx.agora
        out = []
        if self.fonte not in res: return out
        fim = {m['key']: m.get('fim') or 'fechada' for m in res[self.fonte].get('mrs_sumidas', [])}
        marcas = s.get(self.fonte, {}).get('mrs', {})
        for p in core.pend_abertas(s, self.fila):
            ref = p.get('mr')
            if not ref: continue
            if ref in fim: core.resolve(p, agora, f'MR {fim[ref]}'); out.append((p, f'MR {fim[ref]}'))
            elif marcas.get(ref, {}).get('status') == 'ignorar':
                p['status'] = 'ignorar'; p['nota'] = f'MR {ref} marcada ignorar'; out.append((p, f'ignorada (MR {ref})'))
        return out

    def ao_anotar(self, ctx, p):
        mr = ctx.s.get(self.fonte, {}).get('mrs', {}).get(p.get('mr') or '')
        if mr is not None and p.get('status') == 'ignorar':      # ignorar a pendencia ignora a MR citada nela
            mr['status'] = 'ignorar'; mr['nota'] = f'pendencia {p["chave"]} ignorada'; print(f'MR {p["mr"]}: status=ignorar')
