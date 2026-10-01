#!/usr/bin/env python3
"""Hook kpis: six indicators of how the agent is doing, one per question (24/09/2026).

  1. disponibilidade   is it up?            ticks run / ticks expected in business hours (runner metricas.tsv; the
                                            gap between a tick that woke the session and the relaunch counts against it)
  2. precisao          does it wake for     wakes marked useful / wakes marked (`--kpis acordou util|ruido`, one mark per
                       what matters?        triage; coverage = marked / wakes, from metricas.tsv)
  3. resposta          do you answer        business hours from a pending item's entry to its resolution by you (median
                       faster?              and p90 per source; items still open after the last reminder = "vencidas")
  4. triagem           what does watching   tokens of "triagem" (up to 0.18.x "vigia (triagem)") / all tokens
                       cost?
  5. marcacao          is the split         1 - tokens "(sem tarefa)" / all tokens (hook custos)
                       reliable?
  6. correcoes         do you depend less   "[aplicada" items in the day's lessons (state 'licoes'), per business day
                       on asking?

Runner history before the switch to work-watch is read from the old agent's metricas.tsv too
(~/.config/<old name>-vigia/runner/metricas.tsv, old names from watch_core.agents and the config's old "vigia" key), so
the series does not start at zero.

Config ("ganchos": [{"tipo": "kpis"}]); nothing to set. CLI:
  --kpis [hoje|ontem|semana|mes|AAAA-MM-DD[..AAAA-MM-DD]]      the table
  --kpis acordou util|ruido ["nota"]                           marks the wake just triaged (append-only file, never
                                                               the state the runner writes)
"""
import os, json, statistics
from datetime import datetime, timezone, timedelta

import core, paths
from adapters import Gancho as Base
from adapters import custos as cu

RESPONDEU = ('voce respondeu', 'issue fechada', 'marcada no Notion', 'resolvida no Notion')


def _ts(x):
    try:
        t = datetime.fromisoformat(str(x).replace('Z', '+00:00'))
        return t if t.tzinfo else t.replace(tzinfo=core.BRT)       # metricas.tsv is local time without offset
    except (TypeError, ValueError): return None


def _pct(x): return '—' if x is None else f'{100 * x:.0f}%'


def _h(x): return '—' if x is None else (f'{x:.1f} h' if x < 10 else f'{x:.0f} h')


def _mt(n): return f'{n / 1e6:.1f}M' if n >= 1e6 else f'{n / 1e3:.0f}k'


class Gancho(Base):
    tipo = 'kpis'
    json_key = None

    # ---- inputs --------------------------------------------------------------------------------------------------
    def _metricas(self):
        arqs = [os.path.join(paths.CACHE_DIR, 'runner', 'metricas.tsv')]
        cfg = self.ctx.cfg if self.ctx else {}
        from watch_core.agents import legacy_agent_names
        for velho in dict.fromkeys([cfg.get('vigia')] + legacy_agent_names(paths.agent(cfg))):
            if velho: arqs.append(os.path.expanduser(f'~/.config/{velho}-vigia/runner/metricas.tsv'))
        out = []
        for a in arqs:
            try:
                linhas = open(a, encoding='utf-8').read().splitlines()
            except OSError: continue
            for l in linhas:
                c = l.split('\t')
                # heavy ticks only: a light wake ("planou") and a cycle exit ("ciclo", PLN0144) are neither a tick run nor
                # a wake with content (a non-numeric exit used to end the reading of the whole file)
                if len(c) < 4 or not c[1].lstrip('-').isdigit(): continue
                t = _ts(c[0])
                try:
                    if t: out.append((t, int(c[1]), int(c[2] or 0)))
                except ValueError: continue
        return sorted(set(out))

    def _marcas_arq(self): return os.path.join(paths.DATA_DIR, 'acordares.jsonl')

    def _marcas(self):
        try: linhas = open(self._marcas_arq(), encoding='utf-8').read().splitlines()
        except FileNotFoundError: return []
        out = []
        for l in linhas:
            try: d = json.loads(l); out.append((_ts(d['ts']), d['util']))
            except (ValueError, KeyError): continue
        return [(t, u) for t, u in out if t]

    # ---- the six ---------------------------------------------------------------------------------------------------
    def calcula(self, s, ini, fim):
        cfg = self.ctx.cfg if self.ctx else {}
        intervalo = int(cfg.get('intervalo_s') or 300)
        met = [(t, rc, n) for t, rc, n in self._metricas() if ini <= t < fim]
        # 1. availability: ticks run in business hours / business seconds / interval (capped at 100%)
        seg_com, t = 0, ini
        agora = datetime.now(timezone.utc)
        while t < min(fim, agora):
            if core.comercial(t): seg_com += 60
            t += timedelta(minutes=1)
        feitos = sum(1 for t, _, _ in met if core.comercial(t))
        disp = min(1.0, feitos / (seg_com / intervalo)) if seg_com else None
        acordou = [t for t, rc, n in met if n > 0 or rc != 0]
        quebrados = sum(1 for _, rc, _ in met if rc != 0)
        # 2. precision of the wakes
        marcas = [(t, u) for t, u in self._marcas() if ini <= t < fim]
        uteis = sum(1 for _, u in marcas if u)
        prec = uteis / len(marcas) if marcas else None
        cobertura = min(1.0, len(marcas) / len(acordou)) if acordou else None
        # 3. response time per source
        por_fonte = {}
        vencidas = 0
        for p in core.pendencias(s):
            if p.get('status') == 'aberta' and (p.get('lembretes') or 0) >= core.MAX_LEMBRETES:
                d = core.dt(p.get('criada')) or core.dt(p.get('desde'))
                if d and d < fim: vencidas += 1
            if p.get('status') != 'resolvida' or p.get('como') not in RESPONDEU: continue
            r = core.dt(p.get('resolvida'))
            if not r or not (ini <= r < fim): continue
            e = core.dt(p.get('criada')) or core.dt(p.get('desde'))
            if e: por_fonte.setdefault(p['fonte'], []).append(core.horas_uteis(e, r))
        resposta = {f: (statistics.median(v), sorted(v)[max(0, int(round(0.9 * len(v))) - 1)], len(v))
                    for f, v in sorted(por_fonte.items())}
        # 4-5. triage share and marking coverage (hook custos, stored per day)
        tri = sem = tot = 0
        for dia, linhas in ((s.get('custos') or {}).get('dias') or {}).items():
            d = datetime.fromisoformat(dia).replace(tzinfo=core.BRT)
            if not (ini <= d < fim): continue
            for tarefa, x in linhas.items():
                n = x.get('tokens') or ((x.get('entrada') or 0) + (x.get('cache_grava') or 0) + (x.get('cache_le') or 0) + (x.get('saida') or 0))
                tot += n
                if tarefa in cu.TRIAGEM_TODOS: tri += n
                if tarefa == '(sem tarefa)': sem += n
        # 6. corrections: "[aplicada" items in the day's lessons, per business day of the window
        corr, dias_uteis = 0, 0
        d = ini
        while d < fim:
            if d.astimezone(core.BRT).weekday() < 5 and d <= agora: dias_uteis += 1
            texto = ((s.get('licoes') or {}).get(d.astimezone(core.BRT).date().isoformat()) or {}).get('texto') or ''
            corr += sum(1 for l in texto.splitlines() if l.lstrip('- ').startswith('[aplicada'))
            d += timedelta(days=1)
        return {
            'disponibilidade': disp, 'ticks': feitos, 'ticks_esperados': round(seg_com / intervalo) if seg_com else 0,
            'acordares': len(acordou), 'quebrados': quebrados,
            'precisao': prec, 'marcados': len(marcas), 'uteis': uteis, 'cobertura_marcas': cobertura,
            'resposta': resposta, 'vencidas': vencidas,
            'triagem': tri / tot if tot else None, 'tokens': tot, 'tokens_triagem': tri,
            'marcacao': 1 - sem / tot if tot else None, 'tokens_sem_tarefa': sem,
            'correcoes': corr, 'correcoes_por_dia': corr / dias_uteis if dias_uteis else None, 'dias_uteis': dias_uteis,
        }

    # ---- CLI -------------------------------------------------------------------------------------------------------
    def args(self, ap):
        ap.add_argument('--kpis', nargs='*', metavar='ARG',
                        help='indicadores: [hoje|ontem|semana|mes|AAAA-MM-DD[..AAAA-MM-DD]] | acordou util|ruido ["nota"]')

    def cli(self, a, ctx):
        if a.kpis is None: return False
        agora = datetime.now(timezone.utc)
        if a.kpis[:1] == ['acordou']:
            if len(a.kpis) < 2 or a.kpis[1] not in ('util', 'ruido'): raise SystemExit('uso: --kpis acordou util|ruido ["nota"]')
            os.makedirs(paths.DATA_DIR, exist_ok=True)
            with open(self._marcas_arq(), 'a', encoding='utf-8') as f:
                f.write(json.dumps({'ts': agora.isoformat(), 'util': a.kpis[1] == 'util',
                                    'nota': ' '.join(a.kpis[2:])}, ensure_ascii=False) + '\n')
            print(f'kpis: acordar marcado como {a.kpis[1]}'); return True
        from adapters.custos import Gancho as Custos
        ini, fim = Custos.periodo(None, a.kpis[0] if a.kpis else 'hoje', agora)
        k = self.calcula(ctx.s, ini, fim)
        fim_d = (fim - timedelta(days=1)).strftime('%d/%m')
        print(f'== KPIS {paths.NOME} ({ini:%d/%m}{"" if fim_d == ini.strftime("%d/%m") else " a " + fim_d})')
        print(f'1. disponibilidade     {_pct(k["disponibilidade"]):>5}   {k["ticks"]} de ~{k["ticks_esperados"]} ticks no horario comercial; '
              f'{k["quebrados"]} com fonte quebrada')
        print(f'2. precisao            {_pct(k["precisao"]):>5}   {k["uteis"]} uteis de {k["marcados"]} marcados; '
              f'{k["acordares"]} acordares (marcados: {_pct(k["cobertura_marcas"])})')
        if k['resposta']:
            fontes = '; '.join(f'{f} {_h(m)} (p90 {_h(p9)}, n={n})' for f, (m, p9, n) in k['resposta'].items())
        else:
            fontes = 'nenhuma resposta sua registrada no periodo'
        print(f'3. resposta (mediana)          {fontes}; {k["vencidas"]} vencida(s) sem resposta')
        print(f'4. custo da triagem    {_pct(k["triagem"]):>5}   {_mt(k["tokens_triagem"])} de {_mt(k["tokens"])} tokens')
        print(f'5. marcacao            {_pct(k["marcacao"]):>5}   {_mt(k["tokens_sem_tarefa"])} tokens sem tarefa')
        cpd = '—' if k['correcoes_por_dia'] is None else f'{k["correcoes_por_dia"]:.1f}'
        print(f'6. correcoes por dia   {cpd:>5}   {k["correcoes"]} nas licoes de {k["dias_uteis"]} dia(s) util(eis)')
        return True
