#!/usr/bin/env python3
"""Hook licoes: the daily lessons closing of watch_core.lessons (this plugin's own watch_core).

Live instances only: from the library's closing hour on a business day, prints the == LICOES prompt until today's
lesson is stored. CLI: --licoes [set "texto" | regras | propostas | tudo]; `set` writes the shared LICOES.md and needs
a live instance.

Every instance has it (PLN1024): agent.py adds it when no hook or source of the instance takes --licoes already, so
listing it in the config is optional. Config ("ganchos": [{"tipo": "licoes"}]): the name used in the shared history is
the instance's "agent" (canonical, e.g. work-watch-acme; the old key "vigia" is still read; default: the instance name) —
paths.agent.

An instance whose engine keeps its own state.json in another process (travel-agent) uses `standalone_cli` and
`standalone_block`: the same closing and the same lessons.md, with the dates kept in data/lessons.json.
"""
import json, os

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


def by_default():
    """Whether the closing comes to an instance that did not ask for it (agent.py's default hook, travel-agent). Off in
    the test suites (WATCH_CORE_TEST=1: their tick outputs must not change after 18h) unless AGENT_LESSONS_DEFAULT=1."""
    return os.environ.get('WATCH_CORE_TEST') != '1' or os.environ.get('AGENT_LESSONS_DEFAULT') == '1'


STANDALONE = 'lessons.json'      # data/: the lessons dates of an instance whose engine owns data/state.json


def _standalone_path(): return os.path.join(paths.DATA_DIR, STANDALONE)


def _standalone_load():
    try:
        with open(_standalone_path(), encoding='utf-8') as f: s = json.load(f)
    except (OSError, ValueError): s = {}
    return s if isinstance(s, dict) else {}


def _standalone_save(s):
    from watch_core import fileio
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    fileio.write_json(_standalone_path(), s, indent=1, ensure_ascii=False)


def standalone_cli(cfg, args):
    """--licoes [set "texto" | regras | propostas | tudo] for an instance outside the hooks (travel-agent). 0, or 2 when
    `set` runs in test mode ("live" not true)."""
    args = list(args or [])
    if args and args[0] not in LEITURA and cfg.get('live') is not True:
        print(f'modo teste: "--licoes {args[0]}" bloqueado (a instancia {paths.NAME} nao tem "live": true)')
        return 2
    lv().cli(args, _standalone_load(), _standalone_save, paths.agent(cfg))
    return 0


def standalone_block(cfg, dry=False):
    """The == LICOES block of the closing for a live tick of an instance outside the hooks; None when not due."""
    if cfg.get('live') is not True or dry or not by_default(): return None
    m, s = lv(), _standalone_load()
    if not m.devida(s): return None
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        if m.tick(s, paths.agent(cfg), dry): return buf.getvalue().rstrip('\n')
    return None
