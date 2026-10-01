#!/usr/bin/env python3
"""Hook diario: daily Jira summary of what you forgot (read-only).

The tick only sees what CHANGES; this looks at what is standing still. Business day, first tick from "hora" on, once a
day (state 'diario' = date): (1) my issues in the open sprint with no estimate, only in the "projetos_estimativa"
projects (where the board estimates); (2) my issues in statusCategory "In Progress" with no update for "parada_dias"+
days. Keys in state 'diario_ignorar' are skipped (--diario ignorar KEY... ; --diario ver KEY brings back). --diario alone
runs it now without writing.

Needs the jira source in the same instance (and a clean run of it in this tick).

Config ("ganchos": [{"tipo": "diario", ...}]):
  hora                 "HH:MM" (default "09:00")
  parada_dias          default 7
  projetos_estimativa  Jira project keys where a missing estimate matters (empty = skip part 1)
  campos_estimativa    estimate fields (default: timeoriginalestimate; add the story-point custom fields of the site)
"""
import sys
from datetime import datetime, timezone

import core
from adapters import Gancho as Base


class Gancho(Base):
    tipo = 'diario'
    json_key = 'diario'
    requer = ('jira',)

    def configura(self):
        h, m = (self.cfg.get('hora') or '09:00').split(':')
        self.hora = (int(h), int(m))
        self.parada = int(self.cfg.get('parada_dias', 7))
        self.projetos = tuple(self.cfg.get('projetos_estimativa') or ())
        self.est = ','.join(self.cfg.get('campos_estimativa') or ['timeoriginalestimate'])
        self.fonte = self.cfg.get('fonte', 'jira')
        self.requer = (self.fonte,)

    def _jv(self, ctx):
        f = ctx.fontes.get(self.fonte)
        if not f: sys.exit(f'diario: a fonte {self.fonte} nao esta configurada')
        return f.modulo

    def diario_jira(self, s, jv):
        """Devolve {'sem_estimativa': [...], 'paradas': [...], 'ignoradas': n} — cada item {key, status, tipo, titulo, dias, url}."""
        ign = set(s.get('diario_ignorar') or [])
        campos = 'summary,status,issuetype,updated,parent,' + self.est
        agora = datetime.now(timezone.utc)
        def item(i):
            f = i['fields']
            return {'key': i['key'], 'status': f['status']['name'], 'tipo': f['issuetype']['name'], 'titulo': f.get('summary', ''),
                    'pai': (f.get('parent') or {}).get('key'), 'dias': (agora - jv.dt(f['updated'])).days,
                    'url': f'{jv.JIRA}/browse/{i["key"]}'}
        sem = []
        if self.projetos:
            projs = ', '.join(self.projetos)
            sem = [item(i) for i in jv.buscar(f'assignee = currentUser() AND project in ({projs}) AND sprint in openSprints() '
                                              f'AND statusCategory != Done AND issuetype != Epic ORDER BY status', campos=campos)
                   if i['key'] not in ign and not any(i['fields'].get(k) for k in self.est.split(','))]
        paradas = [item(i) for i in jv.buscar(f'assignee = currentUser() AND statusCategory = "In Progress" '
                                               f'AND updated <= -{self.parada}d ORDER BY updated ASC', campos=campos)
                   if i['key'] not in ign]
        return {'sem_estimativa': sem, 'paradas': paradas, 'ignoradas': len(ign)}

    def diario_devido(self, s, agora):
        d = agora.astimezone(core.BRT)
        if d.weekday() >= 5 or (d.hour, d.minute) < self.hora: return False
        return s.get('diario') != d.strftime('%Y-%m-%d')

    def imprime_diario(self, r):
        if not (r['sem_estimativa'] or r['paradas']): return False
        print(f"== JIRA DIARIO ({datetime.now(core.BRT).strftime('%d/%m')})")
        if r['sem_estimativa']:
            print(f"SEM ESTIMATIVA NO SPRINT ({len(r['sem_estimativa'])}):")
            for i in r['sem_estimativa']:
                print(f"! {i['key']} [{i['status']}] {i['tipo']}{' · pai ' + i['pai'] if i['pai'] else ''}\n   {i['titulo']}\n   {i['url']}")
        if r['paradas']:
            print(f"PARADAS HA {self.parada}+ DIAS ({len(r['paradas'])}):")
            for i in r['paradas']:
                print(f"~ {i['key']} [{i['status']}] {i['tipo']} · {i['dias']} dias sem update\n   {i['titulo']}\n   {i['url']}")
        if r['ignoradas']: print(f"(+{r['ignoradas']} ignoradas: --diario ver KEY para voltar)")
        return True

    def run(self, ctx):
        if not self.diario_devido(ctx.s, ctx.agora): return None
        try:
            r = self.diario_jira(ctx.s, self._jv(ctx)); ctx.s['diario'] = ctx.agora.astimezone(core.BRT).strftime('%Y-%m-%d')
            return r
        except Exception as e:
            ctx.quebrados.append(('jira diario', f'{type(e).__name__}: {e}'))
            return None

    def texto(self, r):
        if not r: return None
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): ok = self.imprime_diario(r)
        return buf.getvalue().rstrip('\n') if ok else None

    def args(self, ap):
        ap.add_argument('--diario', nargs='*', metavar='ignorar|ver KEY', help='resumo diario do Jira agora (sem gravar); ignorar/ver KEY... ajusta a lista')

    def cli(self, a, ctx):
        if a.diario is None: return False
        s = ctx.s
        if a.diario and a.diario[0] in ('ignorar', 'ver'):
            ign = set(s.get('diario_ignorar') or [])
            keys = [k.upper() for k in a.diario[1:]]
            ign = (ign | set(keys)) if a.diario[0] == 'ignorar' else (ign - set(keys))
            s['diario_ignorar'] = sorted(ign); core.save_state(s); print(f'diario: {a.diario[0]} {" ".join(keys)} ({len(ign)} ignoradas)'); return True
        if not self.imprime_diario(self.diario_jira(s, self._jv(ctx))): print('jira diario: nada sem estimativa nem parado')
        return True
