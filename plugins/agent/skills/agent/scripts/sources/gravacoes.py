#!/usr/bin/env python3
"""Source gravacoes: OBS recordings crossed with this instance's calendar. Was the meeting recorded? Is the transcript
out? Is the minutes file written? Which actions are mine (they become pending items of queue 'reuniao')?

The state machine is the shared engine `watch_core.recordings` (this plugin's own watch_core). The only per-instance part is the calendar, chosen in config:

  "agenda": {"tipo": "cache", ...}    local cache fed by the model (adapters/agenda_cache.py); blocks filtered by
                                      duration and hand-given titles (min_minutos, max_horas, nao_reuniao)
  "agenda": {"tipo": "outlook"}       the instance's Outlook calendar (same work-inbox profile as the other Microsoft
                                      sources); events filtered by the engine's own rule (gravacoes_core.reuniao)
  "agenda": {"tipo": "google"}        the instance's Google Calendar (google_calendar source); same engine rule
  "agenda": {"modulo": "<name>"}      an instance adapter (~/.config/work-watch/<instance>/adapters/<name>.py) exposing
                                      janela(ini, fim) or eventos(ini, fim) -> [{'id', 'titulo', 'ini', 'fim'}]

Other config:
  janela_horas       how far back the calendar is crossed with the videos folder (default 48)
  min_minutos        shorter block is not a meeting (default 5)
  max_horas          longer block is out-of-office/focus, not a meeting (default 4)
  nao_reuniao        words in a title given by hand that mark a personal block (lunch, focus...)
  aviso_agenda_vencida  line printed under == GRAVACOES when the cache expired
  lembrar_horas      business hours until the 1st reminder of an action (default 11)

Minutes into the workspace (PLN0409): in the tick the minutes of a recording this instance claimed get ready, they are
copied to meetings/ of the instance workspace (workspace.copia_da_gravacao: never overwrites, nothing without a
workspace). A failed copy warns once per cause and retries quietly with a growing wait (the archive backoff).

In test mode (instance not live) the tick is always dry: no transcription is launched, no video is moved and the
shared owners file is not written.
"""
import os
from datetime import datetime, timezone, timedelta

import core, paths
from adapters import Fonte as Base, carrega

MIN_DUR = timedelta(minutes=5)
MAX_DUR = timedelta(hours=4)
NAO_REUNIAO = ()


def _base():
    from watch_core import recordings as gravacoes_core
    return gravacoes_core


def reuniao(e):
    """Bloco que vale como reuniao: duracao entre MIN_DUR e MAX_DUR, e sem titulo de bloco pessoal (almoco, foco, OOF)
    — esses sao batizados a mao e nao geram ata."""
    titulo = (e.get('titulo') or '').strip().lower()
    if any(x in titulo for x in NAO_REUNIAO): return False
    d = datetime.fromisoformat(e['fim']) - datetime.fromisoformat(e['ini'])
    return MIN_DUR <= d <= MAX_DUR


class _CopiaAta:
    """on_minutes of the engine: copies the ready minutes to meetings/ of the workspace (PLN0409). A failure never
    escapes; it is kept in the state (gravacoes.copia_falhas) with the archive backoff (5 min doubling up to 6 h) and
    warns once per cause."""

    def __init__(self, s):
        self.s, self.copias, self.avisos, self.vistas = s, [], [], set()
        self.agora = datetime.now(timezone.utc)

    def __call__(self, x):
        kg, v = _base(), x['video']
        falhas = self.s.setdefault('gravacoes', {}).setdefault('copia_falhas', {})
        self.vistas.add(v['ts'])
        if kg.espera_arquivo(falhas.get(v['ts']), self.agora): return
        try:
            import workspace
            dst = workspace.copia_da_gravacao(v['workdir'])
        except Exception as e:
            erro = f'{type(e).__name__}: {e}'
            if kg.registra_falha_arquivo(falhas, v['ts'], erro, self.agora):
                self.avisos.append(f'-- ERRO copiar a ata para meetings/ ({x["ev"]["titulo"]}): {erro}; tenta de novo sozinho, sem novo aviso')
            return
        falhas.pop(v['ts'], None)
        if dst: self.copias.append(dst)

    def fecha(self, r):
        st = self.s.get('gravacoes') or {}
        if st.get('copia_falhas'): st['copia_falhas'] = {t: f for t, f in st['copia_falhas'].items() if t in self.vistas}
        r['copias'], r['copia_avisos'] = self.copias, self.avisos


class _Outlook:
    def janela(self, ini, fim):
        from adapters import ms365
        return ms365.calendario().janela(ini, fim)


class Fonte(Base):
    tipo = 'gravacoes'
    lembrar = {'reuniao': 11.0}
    conserto = 'recordings — see the tick error'
    pend = ('reuniao',)

    def pronta(self, p, s):
        """An action of mine agreed in a recorded meeting: someone asked for it there."""
        return 'ação combinada comigo numa reunião gravada', 'ação combinada numa reunião'

    def configura(self):
        global MIN_DUR, MAX_DUR, NAO_REUNIAO
        MIN_DUR = timedelta(minutes=float(self.cfg.get('min_minutos', 5)))
        MAX_DUR = timedelta(hours=float(self.cfg.get('max_horas', 4)))
        NAO_REUNIAO = tuple(x.lower() for x in self.cfg.get('nao_reuniao') or ())
        self.janela_td = timedelta(hours=float(self.cfg.get('janela_horas', 48)))
        ag = self.cfg.get('agenda') or {'tipo': 'cache'}
        self.filtro_motor = ag.get('tipo') in ('outlook', 'google') or ag.get('filtro') == 'motor'
        if ag.get('modulo'):
            self.agenda = carrega(ag['modulo']); self.cache = False
        elif ag.get('tipo') == 'outlook':
            from adapters import ms365
            if ag.get('perfil'): ms365.configura(ag['perfil'])
            self.agenda = _Outlook(); self.cache = False
        elif ag.get('tipo') == 'google':
            from adapters import google_calendar
            self.agenda = google_calendar; self.cache = False
        else:
            from adapters import agenda_cache
            agenda_cache.configura(ag); self.agenda = agenda_cache; self.cache = True
        self.aviso = self.cfg.get('aviso_agenda_vencida') or '-- AGENDA VENCIDA: atualizar o cache da agenda (--agenda set)'

    def janela(self, ini, fim):
        fn = getattr(self.agenda, 'janela', None) or getattr(self.agenda, 'eventos')
        filtro = _base().reuniao if self.filtro_motor else reuniao
        return [e for e in fn(ini, fim) if filtro(e)]

    def agent(self):
        return paths.agent(self.ctx.cfg if self.ctx else None)

    def tick(self, s, desde, dry):
        """Motor compartilhado (watch_core.recordings)."""
        if not core.LIVE: dry = True                  # modo teste: nunca lanca transcricao, move video nem grava donos.json
        kg = _base()
        copia = None if (dry or desde is not None) else _CopiaAta(s)
        r = kg.tick(s, self.agent(), self.janela, core.enfileira, desde, dry, self.janela_td, on_minutes=copia)
        if copia: copia.fecha(r)
        if self.cache: r['agenda_vencida'] = self.agenda.precisa_atualizar(datetime.now(timezone.utc))
        return r

    def caminhos(self):
        """The recordings folders too (instance config, $ATA_VIDEOS_DIR or the shared recordings.*_dir): an archive on a
        Windows drive that stopped answering hung the whole tick (PLN0236)."""
        kg, base = _base(), getattr(super(), 'caminhos', None)       # the work-watch copy's base has no caminhos()
        return (base() if base else []) + [kg.VIDEOS, kg.ARQUIVO]

    def imprime(self, r):
        extras = ([self.aviso] if r.get('agenda_vencida') else []) + list(r.get('copia_avisos') or ())
        if r.get('copias') and (extras or r.get('todas') or r.get('mudancas') or r.get('acoes')):   # copia sozinha nao acorda a sessao
            extras += [f'-- ata copiada para meetings/: {os.path.basename(d)}' for d in r['copias']]
        return _base().imprime_bloco(r, extras)

    def args(self, ap):
        if self.cache: ap.add_argument('--agenda', nargs='*', metavar='set JSON|nomear ID TITULO|precisa', help='cache da agenda (sem argumento: blocos de 24 h)')

    def cli(self, a, ctx):
        if not self.cache or a.agenda is None: return False
        self.agenda.cli(a.agenda); return True
