#!/usr/bin/env python3
"""Source outlook_calendar (result key 'calendar'): Outlook calendar changes through the `work-inbox` plugin
(calendar_outlook.py, Office 365 connector). One query of the whole window (1 h ago to the horizon) serves the diff
against the last photo, the 30-minute warning and the pending resolution:

  CONVITE (new event by someone else), MOVIDO (time/title changed), CANCELADO, SUMIU DA AGENDA (gone before ending),
  ALTERADO (--since only), EM BREVE (starts within "em_breve_min", once per event).

Pending queue ('calendar'): an invite with no answer from you that has not started; it resolves when you answer, it is
cancelled or it passes.

Config ("fontes": [{"tipo": "outlook_calendar", ...}]):
  perfil         work-inbox profile (required)
  em_breve_min   default 30
  lembrar_horas  default 4
Extra CLI: --agenda-outlook [proximos HORAS | evento ID] (read-only).
"""
import re, sys
from datetime import datetime, timezone, timedelta

import core
from adapters import Fonte as Base, ms365

EM_BREVE = timedelta(minutes=30)


def _marca(e):
    """Impressao digital do evento guardada no estado: so mudanca aqui vira relato (RSVP e edicao de corpo bumpam lastModified)."""
    return {'ini': e['ini'], 'fim': e['fim'], 'titulo': e['titulo'], 'resposta': e['minha_resposta'], 'serie': e['serie'],
            'organizador': e['organizador'], 'org_eu': e['org_eu']}


def _relevante(e):
    """Bloco seu de OOF/livre nao e' reuniao: fora do diff e do EM BREVE."""
    return not (e['org_eu'] and e['showAs'] in ('oof', 'free'))


def tick_calendar(s, desde, dry):
    dt, enfileira, resolve, pend_abertas = core.dt, core.enfileira, core.resolve, core.pend_abertas
    kv = ms365.calendario()
    st = s.setdefault('calendar', {'cursor': None, 'eventos': {}, 'avisados': []})
    agora = datetime.now(timezone.utc)
    fixo = desde is not None
    if desde is None:
        desde = dt(st['cursor']) if st.get('cursor') else None
    # uma consulta so: a janela inteira (1 h atras ate o horizonte) serve ao diff, ao EM BREVE e a resolucao de pendencias
    atuais = {e['id']: e for e in kv.janela(agora - timedelta(hours=1), agora + kv.HORIZONTE) if _relevante(e)}
    if desde is None:                      # primeira rodada: planta o cursor e fotografa a agenda (base do diff)
        if not dry:
            st['cursor'] = agora.isoformat()
            st['eventos'] = {k: _marca(e) for k, e in atuais.items()}
        return {'baseline': True, 'itens': []}
    conhecidos = {} if fixo else st.setdefault('eventos', {})    # --since: recorte pela data, sem foto (CONVITE/ALTERADO)
    itens, series_vistas = [], set()
    for k, e in atuais.items():
        ant = conhecidos.get(k)
        chave = e['serie'] or k                # serie recorrente: uma linha por serie, nao por ocorrencia
        if ant is None:
            if e['cancelado_no_titulo']:       # excecao ja cancelada ("Cancelado: X") nao e' convite
                if (fixo or (dt(e['criado']) and dt(e['criado']) >= desde)) and chave not in series_vistas:
                    series_vistas.add(chave); itens.append({'tipo': 'cancelado', 'ev': e})
            elif dt(e['criado']) and dt(e['criado']) >= desde:
                if not e['org_eu'] and chave not in series_vistas:
                    series_vistas.add(chave); itens.append({'tipo': 'convite', 'ev': e})
            elif fixo and dt(e['atualizado']) and dt(e['atualizado']) >= desde and chave not in series_vistas:
                series_vistas.add(chave); itens.append({'tipo': 'alterado', 'ev': e})
            # criado antes do cursor e sem foto: instancia que entrou no horizonte agora; so fotografa
        elif not e['org_eu']:                  # evento seu: voce mesmo mexeu, nao e' relato
            if e['cancelado_no_titulo'] and not kv.CANCELADO.match(ant.get('titulo') or ''):
                if chave not in series_vistas: series_vistas.add(chave); itens.append({'tipo': 'cancelado', 'ev': e})
            elif (ant['ini'], ant['fim']) != (e['ini'], e['fim']) or ant.get('titulo') != e['titulo']:
                if chave not in series_vistas: series_vistas.add(chave); itens.append({'tipo': 'movido', 'ev': e, 'de': ant})
            # so a sua resposta / corpo / RSVP de terceiro: silencioso, atualiza a foto
    # sumiu da janela e ainda nao tinha terminado: cancelado ou apagado (o conector nao tem status nem showDeleted).
    # Remarcar a SERIE troca o id de todas as ocorrencias: a serie continua viva com ids novos -> MOVIDO, nao SUMIU.
    frescas = {}
    for k, e in atuais.items():
        if e['serie'] and k not in conhecidos: frescas.setdefault(e['serie'], []).append(e)
    for k, ant in sorted(conhecidos.items(), key=lambda kv_: kv_[1].get('ini') or ''):
        if k in atuais or not ant.get('fim') or dt(ant['fim']) < agora or ant.get('org_eu'): continue
        if (ant.get('serie') or k) in series_vistas: continue
        series_vistas.add(ant.get('serie') or k)
        novas = frescas.get(ant.get('serie')) if ant.get('serie') else None
        if novas:
            e = min(novas, key=lambda x: abs((dt(x['ini']) - dt(ant['ini'])).total_seconds()) if x['ini'] and ant.get('ini') else 0)
            if (ant['ini'], ant['fim']) != (e['ini'], e['fim']) or ant.get('titulo') != e['titulo']:
                itens.append({'tipo': 'movido', 'ev': e, 'de': ant})
            continue                           # mesma hora com id novo: so a foto muda
        ev = {'id': k, 'titulo': ant.get('titulo') or '?', 'ini': ant.get('ini'), 'fim': ant.get('fim'), 'dia_inteiro': False,
              'organizador': ant.get('organizador') or '?', 'org_eu': ant.get('org_eu', False), 'minha_resposta': ant.get('resposta'),
              'meet': None, 'link': None, 'recorrente': False, 'recorrencia': None}
        itens.append({'tipo': 'sumiu', 'ev': ev})
    # reuniao comecando em ate EM_BREVE (uma vez por evento; recusada nao conta)
    avisados = set() if fixo else set(st.get('avisados') or [])
    breve = []
    for k, e in atuais.items():
        if k in avisados or e['minha_resposta'] == 'declined' or e['dia_inteiro'] or not e['ini']: continue
        if e['org_eu'] and not e['convidados']: continue          # bloco seu sem convidado nao e' reuniao
        ini = dt(e['ini'])
        if ini < agora or ini - agora > EM_BREVE: continue
        breve.append({'tipo': 'em_breve', 'ev': e, 'min': int((ini - agora).total_seconds() // 60)})
    itens += breve
    resolvidas = []
    if not (dry or fixo):
        for p in pend_abertas(s, 'calendar'):
            e = atuais.get(p['ref'])
            if e is None:
                ant = conhecidos.get(p['ref'])
                x = 'passou' if ant and ant.get('ini') and dt(ant['ini']) < agora else 'cancelado'
            elif e['cancelado_no_titulo']: x = 'cancelado'
            elif dt(e['ini']) < agora: x = 'passou'
            elif e['minha_resposta'] in ('accepted', 'declined', 'tentative'): x = 'voce respondeu'
            else: continue
            resolve(p, agora, x); resolvidas.append((p, x))
        for it in itens:
            e = it['ev']
            if it['tipo'] == 'convite' and e['minha_resposta'] == 'needsAction' and e['ini'] and dt(e['ini']) > agora:
                p = enfileira(s, 'calendar', e['id'], kv.quem(e['organizador']), f'{e["titulo"]} · {kv.faixa(e)}'[:80], e['criado'] or agora.isoformat(), agora)
                p['link'] = e['link']
        st['avisados'] = (list(avisados) + [it['ev']['id'] for it in breve])[-200:]
        st['eventos'] = {k: _marca(e) for k, e in atuais.items()}
        st['cursor'] = agora.isoformat()
    return {'baseline': False, 'itens': itens, 'resolvidas': resolvidas}


def imprime_calendar(r):
    hora = core.hora
    kv = ms365.calendario()
    if r['baseline']:
        print('== CALENDARIO: cursor plantado agora (primeira rodada); agenda fotografada para o diff.'); return True
    if not r['itens']: return False
    print(f'== CALENDARIO ({len(r["itens"])} mudancas)')
    for it in r['itens']:
        e = it['ev']
        if it['tipo'] == 'convite':
            print(kv.linha(e, 'CONVITE: '))
            if e['convidados']: print('   convidados: ' + ', '.join(kv.quem(c) + (' (opc)' if c in e['opcionais'] else '') for c in e['convidados']))
            d = re.sub(r'(_{10,}|aviso legal:|this message).*', '', e['descricao'] or '', flags=re.S | re.I).strip()   # rodape do Teams / disclaimer
            if d: print('   ' + d.replace('\n', ' ')[:300])
        elif it['tipo'] == 'movido':
            de = it['de']
            print(f'-- MOVIDO: {e["titulo"]} · de {kv.faixa(dict(e, ini=de["ini"], fim=de["fim"]))} → {kv.faixa(e)}'
                  + ('' if e['org_eu'] else f' · de {kv.quem(e["organizador"])}') + (f' · era "{de["titulo"]}"' if de.get('titulo') != e['titulo'] else ''))
        elif it['tipo'] == 'cancelado': print(kv.linha(e, 'CANCELADO: '))
        elif it['tipo'] == 'sumiu': print(kv.linha(e, 'SUMIU DA AGENDA (cancelado ou apagado): '))
        elif it['tipo'] == 'alterado': print(kv.linha(e, 'ALTERADO: ') + f' · atualizado {hora(e["atualizado"])}')
        elif it['tipo'] == 'em_breve':
            print(f'-- EM BREVE: {e["titulo"]} · {kv.faixa(e)} (em {it["min"]} min)' + ('' if e['org_eu'] else f' · de {kv.quem(e["organizador"])}'))
        if e.get('meet') and it['tipo'] in ('convite', 'em_breve', 'movido'): print(f'   teams: {e["meet"]}')
        if it['tipo'] != 'em_breve': print(f'   id: {e["id"]}' + (f' · {e["link"]}' if e.get('link') else ''))
    return True


def norm_titulo(t):
    """Assunto de e-mail / titulo de convite sem prefixo (Convite:, RE:, FW:...) — o hook convite_email compara os dois."""
    t = re.sub(r'^\s*(convite|invitation|updated invitation|convite atualizado|fw|re|enc|res)\s*:\s*', '', t or '', flags=re.I)
    return re.sub(r'\s+', ' ', t).strip().lower()


class Fonte(Base):
    tipo = 'outlook_calendar'
    nome_padrao = 'calendar'
    lembrar = {'calendar': 4.0}
    conserto = 'Calendar down — same Office 365 connection as the e-mail (Fix connection)'

    def configura(self):
        global EM_BREVE
        EM_BREVE = timedelta(minutes=float(self.cfg.get('em_breve_min', 30)))
        ms365.configura(self.cfg.get('perfil') or sys.exit('outlook_calendar: "perfil" faltando no config.json'), self.cfg.get('teams_chat_dir'))

    def tick(self, s, desde, dry): return tick_calendar(s, desde, dry)
    def imprime(self, r): return imprime_calendar(r)
    def linha_ref(self, p): return f'   id: {p["ref"]}'
    def modulo_agenda(self): return ms365.calendario()

    def args(self, ap):
        ap.add_argument('--agenda-outlook', nargs='*', metavar='proximos HORAS|evento ID', help='agenda do Outlook (so leitura)')

    def cli(self, a, ctx):
        if a.agenda_outlook is None: return False
        kv = ms365.calendario()
        args = a.agenda_outlook
        if args and args[0] == 'evento':
            kv.imprime_evento(kv.evento(args[1])); return True
        horas = float(args[1]) if len(args) > 1 else 24
        for e in kv.proximos(datetime.now(timezone.utc) + timedelta(hours=horas)): print(kv.linha(e))
        return True
