#!/usr/bin/env python3
"""Hook daily: the == DAILY flow of watch_core.daily (this plugin's own watch_core).

Every non-dry tick stores its item lines as the day's evidence (state only). On live instances it also runs the
library's tick: re-prints the daily text once, the 18:00 closing, the live draft and the decision boxes read from Notion.
CLI: --daily [set "texto" | evidencia | codigos | secoes | visto | md "..." | publicar [arquivo] | pagina URL] and
--acao [add "texto" [balde=...] [quem=...] | done N | notion N URL | list]. Instances that are not live only accept the
read-only forms (--daily, --daily evidencia, --daily codigos, --acao list).

Config ("ganchos": [{"tipo": "daily"}]): nothing besides the instance "sigla" (section of the month page).
"""
import core, paths
from adapters import Gancho as Base

LEITURA_DAILY = ('evidencia', 'codigos')
LEITURA_ACAO = ('list',)


def dv(sigla=None):
    from watch_core import daily
    if sigla: daily.SIGLA = sigla
    return daily


class Gancho(Base):
    tipo = 'daily'
    posicao = 'depois'

    def publica(self, ctx, blocos):
        d = dv(ctx.cfg.get('sigla'))
        for b in blocos: d.registra_saida(ctx.s, b)
        if not core.LIVE: return []
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            if d.tick(ctx.s, ctx.dry): return [buf.getvalue().rstrip('\n')]
        return []

    def args(self, ap):
        ap.add_argument('--acao', nargs='*', metavar='ARG', help='acoes manuais (Pendente/Aguardando do Notion): add "texto" [balde=aguardando|decisao] [quem=Fulano] | done N | list (decisao = o agent espera resposta SUA; quem = de quem se espera)')
        ap.add_argument('--daily', nargs='*', metavar='ARG', help='--daily: report gravado; --daily set "texto": grava; --daily evidencia: o que o agent viu hoje')

    def cli(self, a, ctx):
        if a.daily is not None:
            if a.daily and a.daily[0] not in LEITURA_DAILY: core.exige_live(f'--daily {a.daily[0]}')
            dv(ctx.cfg.get('sigla')).cli(a.daily, ctx.s, core.save_state); return True
        if a.acao is not None:
            if a.acao and a.acao[0] not in LEITURA_ACAO: core.exige_live(f'--acao {a.acao[0]}')
            with core.state_lock():              # PLN0256: the runner may be saving; read again, change, save, all under the lock
                ctx.s = core.load_state()
                dv(ctx.cfg.get('sigla')).cli_acao(a.acao, ctx.s, core.save_state)
            return True
        return False
