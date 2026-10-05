#!/usr/bin/env python3
"""Hook credenciais: one panel with how long each credential of the instance still lasts.

Asked on 26/09/2026 after one day with four of them: the git-dev token near its date, the AWS SSO session of prd gone
(prd without checks until someone noticed), the az login and the Teams presence token. Each used to show up only when a
source broke. Now:
  - first tick of the day: the whole panel (one line per credential);
  - any other tick: only a credential that CHANGED state (ok -> near its date -> expired, or back to ok).
The session reads the block and, for a login that can be started from the terminal (`renovar` looks like a command),
starts it in the background and sends user + link + code (common rule of the watchers).

Config ("ganchos": [{"tipo": "credenciais", "itens": [...]}]); each item has "nome", optional "renovar" (command or
where to renew) and ONE way to know its state:
  "vence": "AAAA-MM-DD"                          fixed date (API token with a known expiry)
  "comando": "aws sts get-caller-identity ..."   exit 0 = valid (run at most every "cada_min", default 60)
  "json": "~/arquivo.json", "campo": "expira"    ISO date in a JSON field (dotted path allowed)
  "json": ..., "campo": "renovado", "validade_dias": 90    last renewal + validity
  "aviso_dias": 3                                "near" threshold (default 3 days; commands have no near state)
  "necessidade": "v-token"                       optional: the instance need (tasks.necessidades) that already asks
                                                 the user about this credential
  "fonte": "gitlab"                              optional: the source this credential opens; its expiry then shows on
                                                 that source's line in Planou instead of a line of its own
  "tipo": "token"                                optional: token, session_cookie, oauth, api_key, none, other (default
                                                 token with a date, other with a command)
  "impacto": "as checagens de prd"               optional: what stops working while it is expired (the "Precisa de
                                                 você" ask says it; default from "fonte" or a generic line)

Planou (0.28.0, "Ferramentas do agente"): each live tick keeps s['credenciais']['itens'][nome] = {'estado', 'detalhe',
'renovar', 'fonte', 'tipo', 'vence'} and the Planou step of the tick (planou_tick.ferramentas) turns each one into the
expiry of its source or a line of its own. Planou opens the alert (tool_expiring, tool_failing) by itself; the agent no
longer opens one per credential. Since PLN0364 an expired credential (a date in the past, or a command that failed with
an auth error such as "Token has expired and refresh failed") also opens ONE "Precisa de você" ask per episode
(planou_tick.pedidos_credenciais), from 'desde' (when it stopped being ok) and 'impacto'. Nothing here renews a login:
`aws sso login` and the like are interactive, the person runs the "renovar" command.
"""
import os, json, shlex, subprocess
from datetime import datetime, timedelta

import core
from adapters import Gancho as Base


def _data(v):
    if not v: return None
    try:
        d = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=core.BRT)


def _campo(obj, caminho):
    for p in caminho.split('.'):
        obj = obj.get(p) if isinstance(obj, dict) else None
    return obj


class Gancho(Base):
    tipo = 'credenciais'
    json_key = 'credenciais'
    posicao = 'depois'
    roda_em_dry = True

    def configura(self):
        self.itens = [i for i in self.cfg.get('itens') or [] if i.get('nome')]

    def _json(self, item):
        try:
            with open(os.path.expanduser(item['json']), encoding='utf-8') as fh: return json.load(fh)
        except (OSError, ValueError):
            return {}

    def fim(self, item):
        """When the credential expires (aware datetime), for the kinds with a date ("vence", "json"); None otherwise."""
        if item.get('vence'):
            return _data(item['vence'] + ('' if 'T' in item['vence'] else 'T23:59:59'))
        if item.get('json'):
            v = _data(_campo(self._json(item), item.get('campo', 'expira')))
            if v and item.get('validade_dias'): v = v + timedelta(days=float(item['validade_dias']))
            return v
        return None

    def estado(self, item, agora, cache):
        """('ok'|'perto'|'vencida'|'?', detalhe)"""
        aviso = timedelta(days=float(item.get('aviso_dias', 3)))
        if item.get('comando'):
            c = cache.get(item['nome']) or {}
            if c.get('quando') and agora - _data(c['quando']) < timedelta(minutes=float(item.get('cada_min', 60))):
                return c['estado'], c['detalhe']
            try:
                r = subprocess.run(shlex.split(os.path.expanduser(item['comando'])), capture_output=True, text=True, timeout=30)
                est, det = ('ok', 'válida') if r.returncode == 0 else ('vencida', (r.stderr or r.stdout).strip().splitlines()[-1][:120] if (r.stderr or r.stdout).strip() else f'exit {r.returncode}')
            except Exception as e:
                est, det = '?', f'{type(e).__name__}: {str(e)[:80]}'
            cache[item['nome']] = {'quando': agora.isoformat(), 'estado': est, 'detalhe': det}
            return est, det
        if item.get('json') and not item.get('vence'):
            try:
                with open(os.path.expanduser(item['json']), encoding='utf-8') as fh: json.load(fh)
            except (OSError, ValueError) as e:
                return '?', f'sem o arquivo ({type(e).__name__})'
            if not _data(_campo(self._json(item), item.get('campo', 'expira'))):
                return '?', f'campo {item.get("campo", "expira")} ausente'
        fim = self.fim(item)
        if not fim: return '?', 'sem regra (vence, comando ou json)'
        falta = fim - agora
        quando = fim.astimezone(core.BRT).strftime('%d/%m %H:%M')
        if falta <= timedelta(0): return 'vencida', f'venceu em {quando}'
        dias = falta.days
        det = f'vence em {dias} dia(s) ({quando})' if dias else f'vence hoje às {fim.astimezone(core.BRT).strftime("%H:%M")}'
        return ('perto' if falta <= aviso else 'ok'), det

    def run(self, ctx):
        if not self.itens:
            if not ctx.dry and isinstance(ctx.s.get('credenciais'), dict): ctx.s['credenciais'].pop('itens', None)
            return None
        s, agora = ctx.s, ctx.agora
        st = s.setdefault('credenciais', {})
        cache = st.setdefault('cache', {})
        antes = st.get('estado') or {}
        hoje = agora.astimezone(core.BRT).date().isoformat()
        linhas, novo, itens, mudou = [], {}, {}, False
        desde = dict(st.get('desde')) if isinstance(st.get('desde'), dict) else {}
        for i in self.itens:
            est, det = self.estado(i, agora, cache)
            novo[i['nome']] = est
            if est == 'vencida': desde.setdefault(i['nome'], agora.isoformat())
            else: desde.pop(i['nome'], None)
            fim = None if i.get('comando') else self.fim(i)
            itens[i['nome']] = {'estado': est, 'detalhe': det, 'renovar': i.get('renovar') or '',
                                'necessidade': i.get('necessidade') or '', 'fonte': i.get('fonte') or '',
                                'tipo': i.get('tipo') or ('other' if i.get('comando') else 'token'),
                                'vence': fim.isoformat() if fim else '', 'desde': desde.get(i['nome']) or '',
                                'comando': bool(i.get('comando')), 'impacto': i.get('impacto') or ''}
            if est != antes.get(i['nome']): mudou = True
            marca = {'ok': '✓', 'perto': '⚠', 'vencida': '✗', '?': '?'}[est]
            ren = f' · renovar: {i["renovar"]}' if i.get('renovar') and est != 'ok' else ''
            linhas.append((est, f'   {marca} {i["nome"]}: {det}{ren}'))
        diario = st.get('dia') != hoje
        if ctx.dry:
            return None if not (diario or mudou) else [l for _, l in linhas]
        st['estado'] = novo
        st['desde'] = {k: v for k, v in desde.items() if k in novo}
        st['itens'] = itens               # rebuilt every live tick: a credential taken out of the config leaves Planou
        if diario:
            st['dia'] = hoje
            return [l for _, l in linhas]
        if mudou:
            return [l for (est, l), i in zip(linhas, self.itens) if est != antes.get(i['nome'])]
        return None

    def texto(self, valor):
        if not valor: return None
        return '== CREDENCIAIS\n' + '\n'.join(valor)
