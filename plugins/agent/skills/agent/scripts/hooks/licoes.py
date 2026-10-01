#!/usr/bin/env python3
"""Hook licoes: the daily lessons closing of watch_core.lessons (this plugin's own watch_core).

Live instances only: from the library's closing hour on a business day, prints the == LICOES prompt until today's
lesson is stored. CLI: --licoes [set "texto" | regras | propostas | tudo]; `set` writes the shared LICOES.md and needs
a live instance.

Config ("ganchos": [{"tipo": "licoes"}]): the name used in the shared history is the instance's "agent" (canonical, e.g.
work-watch-acme; the old key "vigia" is still read; default: work-watch-<instance>) — paths.agent.
"""
import core, paths
from adapters import Gancho as Base

LEITURA = ('regras', 'propostas', 'tudo')


def lv():
    from watch_core import lessons
    return lessons


class Gancho(Base):
    tipo = 'licoes'

    def agent(self, ctx): return paths.agent(ctx.cfg)

    def publica(self, ctx, blocos):
        if not core.LIVE: return []
        m = lv()
        if not m.devida(ctx.s): return []
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            if m.tick(ctx.s, self.agent(ctx), ctx.dry): return [buf.getvalue().rstrip('\n')]
        return []

    def args(self, ap):
        ap.add_argument('--licoes', nargs='*', metavar='ARG', help='--licoes: ultimas licoes; set "texto": grava a de hoje (estado + LICOES.md compartilhado); regras; propostas; tudo')

    def cli(self, a, ctx):
        if a.licoes is None: return False
        if a.licoes and a.licoes[0] not in LEITURA: core.exige_live(f'--licoes {a.licoes[0]}')
        lv().cli(a.licoes, ctx.s, core.save_state, self.agent(ctx)); return True
