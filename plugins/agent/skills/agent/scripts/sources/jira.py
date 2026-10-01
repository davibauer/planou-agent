#!/usr/bin/env python3
"""Source jira: what changed in the issues that touch me in one Jira Cloud site.

Read-only, REST v3 with Basic auth (e-mail + API token read from a text file: e-mail on line 3, token on line 5 by
default). Two things:
  - ISSUES THAT CHANGED since the cursor (JQL: assignee/reporter/watcher = me AND updated >= cursor): for each one,
    what changed (changelog by other people: status, assignee, priority, sprint...) and NEW COMMENTS by other people
    (with @mention of me flagged).
  - MY OPEN ISSUES (assignee = me AND statusCategory != Done): newly assigned to me, closed/left.
There is no JQL for "mentioned me" outside the issues I follow: a mention in someone else's issue only shows if I watch it.

Pending queue: a comment by someone else on my issue (assigned or reported by me) or with an @mention enters as 'jira';
it resolves when I comment after it or the issue is done.

Config ("fontes": [{"tipo": "jira", ...}]):
  site            https://<site>.atlassian.net (required)
  token_file      path of the credential file (required)
  linha_email     line of the e-mail in that file (default 3)
  linha_token     line of the token (default 5)
  lembrar_horas   business hours until the 1st reminder (default 11 = one business day)
  mensagens       {"sem_token": ..., "token_recusado": ...}: the text of "FONTE QUEBRADA (jira): ..." when the
                  credential file is missing / Jira answers 401 ({token_file}, {linha_token} are filled in). Defaults
                  are generic; an instance that wants its own hint (where the file lives, how to renew) sets it here

Extra CLI: --jira all | show KEY (read-only).
"""
import os, re, sys, json, base64, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta

import core, paths
from watch_core import slowfs
from adapters import Fonte as Base, sumidos

JIRA = None
API = None
TOKEN_FILE = None
LINHA_EMAIL, LINHA_TOKEN = 3, 5
MSG_PADRAO = {'sem_token': 'token do Jira nao encontrado em {token_file}',
       'token_recusado': 'Jira recusou o token (401): gerar outro em id.atlassian.com e regravar a linha {linha_token} de {token_file}'}
MSG = dict(MSG_PADRAO)
BRT = timezone(timedelta(hours=-3))
CAMPOS = 'summary,status,assignee,reporter,updated,created,issuetype,priority,parent,comment,duedate'

# ---------------- transporte ----------------
_auth = None
def _cred():
    global _auth
    if _auth: return _auth
    try: linhas = slowfs.read_text(TOKEN_FILE).replace('\r', '').split('\n')     # with a deadline on a Windows drive
    except FileNotFoundError: sys.exit(_msg('sem_token'))
    ie, it = LINHA_EMAIL - 1, LINHA_TOKEN - 1
    email, tok = (linhas[ie].strip() if len(linhas) > ie else ''), (linhas[it].strip() if len(linhas) > it else '')
    if not email or not tok: sys.exit(f'{TOKEN_FILE} nao tem {LINHA_TOKEN} linhas (e-mail na {LINHA_EMAIL}a, token na {LINHA_TOKEN}a)')
    _auth = 'Basic ' + base64.b64encode(f'{email}:{tok}'.encode()).decode()
    return _auth

def _msg(k):
    return MSG[k].format(token_file=TOKEN_FILE, linha_token=LINHA_TOKEN, linha_email=LINHA_EMAIL)

def get(path, params=None):
    url = API + path + (('&' if '?' in path else '?') + urllib.parse.urlencode(params) if params else '')
    req = urllib.request.Request(url, headers={'Authorization': _cred(), 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)
    except urllib.error.HTTPError as e:
        corpo = e.read().decode(errors='replace')[:300]
        if e.code == 401: sys.exit(_msg('token_recusado'))
        if e.code == 404: raise LookupError(f'404 em {path}: {corpo}')
        raise RuntimeError(f'HTTP {e.code} em {path}: {corpo}')
    except urllib.error.URLError as e:
        sys.exit(f'Jira sem resposta: {e.reason}')

def buscar(jql, campos=CAMPOS, expand=None, max_paginas=10):
    """/search/jql pagina por nextPageToken (sem total/startAt)."""
    out, token = [], None
    for _ in range(max_paginas):
        p = {'jql': jql, 'fields': campos, 'maxResults': 100}
        if expand: p['expand'] = expand
        if token: p['nextPageToken'] = token
        d = get('/search/jql', p)
        out += d.get('issues', [])
        token = d.get('nextPageToken')
        if d.get('isLast', True) or not token: break
    return out

def hora(iso):
    return dt(iso).astimezone(BRT).strftime('%d/%m %H:%M') if iso else '??'

def dt(iso):
    if not iso: return None
    iso = re.sub(r'([+-]\d\d)(\d\d)$', r'\1:\2', iso.replace('Z', '+00:00'))
    d = datetime.fromisoformat(iso)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

def adf(n, me=None, achou=None):
    """ADF -> texto simples; marca achou['mencao']=True se houver @mencao ao accountId `me`."""
    if achou is None: achou = {}
    if isinstance(n, list): return ''.join(adf(x, me, achou) for x in n)
    if not isinstance(n, dict): return ''
    t = n.get('type')
    if t == 'text': return n.get('text', '')
    if t == 'mention':
        if me and (n.get('attrs') or {}).get('id') == me: achou['mencao'] = True
        return (n.get('attrs') or {}).get('text', '@?')
    if t == 'hardBreak': return '\n'
    if t == 'inlineCard': return (n.get('attrs') or {}).get('url', '')
    if t == 'emoji': return (n.get('attrs') or {}).get('text', '')
    dentro = adf(n.get('content', []), me, achou)
    if t in ('paragraph', 'heading', 'blockquote', 'codeBlock', 'listItem', 'tableRow'): return dentro + '\n'
    if t == 'tableCell' or t == 'tableHeader': return dentro.strip() + ' | '
    return dentro

def texto(node, me=None):
    achou = {}
    s = re.sub(r'\n{3,}', '\n\n', adf(node, me, achou)).strip() if node else ''
    return s, bool(achou.get('mencao'))

# ---------------- coleta ----------------
def eu():
    m = get('/myself'); return m['accountId'], m.get('displayName', '?')

def _issue(i, me):
    f = i['fields']
    return {'key': i['key'], 'titulo': f.get('summary', ''), 'status': (f.get('status') or {}).get('name', '?'),
            'tipo': (f.get('issuetype') or {}).get('name', '?'), 'prioridade': (f.get('priority') or {}).get('name'),
            'assignee': (f.get('assignee') or {}).get('displayName'), 'minha': (f.get('assignee') or {}).get('accountId') == me,
            'reporter': (f.get('reporter') or {}).get('displayName'), 'reporter_eu': (f.get('reporter') or {}).get('accountId') == me, 'pai': (f.get('parent') or {}).get('key'),
            'criado': f.get('created'), 'atualizado': f.get('updated'), 'url': f'{JIRA}/browse/{i["key"]}',
            'vence': f.get('duedate') or None}

def minhas_abertas(me):
    return {i['key']: _issue(i, me) for i in buscar('assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC')}

def _filtra_changelog(hists, desde, me):
    out = []
    for h in hists:
        if dt(h['created']) < desde or (h.get('author') or {}).get('accountId') == me: continue
        itens = [f"{x.get('field')}: {x.get('fromString') or '-'} -> {x.get('toString') or '-'}" for x in h.get('items', [])
                 if x.get('field') not in ('description', 'IssueParentAssociation', 'Rank')]
        if itens: out.append({'quando': h['created'], 'de': (h.get('author') or {}).get('displayName', '?'), 'itens': itens})
    return sorted(out, key=lambda h: h['quando'])

def changelog(key, desde, me):
    """Entradas do changelog de outras pessoas desde `desde`, pelo endpoint proprio (quando o expand da busca trunca)."""
    hists, start = [], 0
    while True:
        d = get(f'/issue/{key}/changelog', {'startAt': start, 'maxResults': 100})
        vals = d.get('values', []); hists += vals
        if d.get('isLast', True) or not vals: return _filtra_changelog(hists, desde, me)
        start += len(vals)

def mudancas(desde, me):
    """Issues que me tocam e mudaram desde `desde`: comentarios novos de outros (+mencao) e changelog de outros."""
    minutos = max(1, int((datetime.now(timezone.utc) - desde).total_seconds() // 60) + 2)
    jql = f'(assignee = currentUser() OR reporter = currentUser() OR watcher = currentUser()) AND updated >= -{minutos}m ORDER BY updated ASC'
    out = []
    for i in buscar(jql, expand='changelog'):
        it = _issue(i, me); f = i['fields']
        coms = []
        for c in (f.get('comment') or {}).get('comments', []):
            if (c.get('author') or {}).get('accountId') == me or dt(c['created']) < desde: continue
            t, mencao = texto(c.get('body'), me)
            coms.append({'id': c['id'], 'quando': c['created'], 'de': (c.get('author') or {}).get('displayName', '?'), 'texto': t[:500], 'mencao': mencao})
        exp = i.get('changelog') or {}
        if exp.get('total', 0) <= len(exp.get('histories') or []): ch = _filtra_changelog(exp.get('histories') or [], desde, me)
        else:
            try: ch = changelog(i['key'], desde, me)
            except (RuntimeError, LookupError): ch = []
        if coms or ch:
            it.update(comentarios=coms, changelog=ch); out.append(it)
    return out

def fechada_como(key):
    try: return (get(f'/issue/{key}', {'fields': 'status,assignee'})['fields'].get('status') or {}).get('name', '?')
    except Exception: return '?'

def saiu_como(key, me):
    """How an issue missing from my open issues left, when Jira confirms it: its status (category done), reassigned or
    removed (404). None = still mine and open, or no answer (it stays in the list, sumidos.confirma)."""
    try: f = get(f'/issue/{key}', {'fields': 'status,assignee'})['fields']
    except LookupError: return 'removida'
    except (Exception, SystemExit): return None
    status = f.get('status') or {}
    if (status.get('statusCategory') or {}).get('key') == 'done': return status.get('name') or 'fechada'
    dono = f.get('assignee') or {}
    if dono.get('accountId') != me: return f"reatribuida ({dono.get('displayName') or 'ninguem'})"
    return None

def delta(agora, antes):
    novas = [i for k, i in agora.items() if k not in antes]
    sumidas = [i for k, i in antes.items() if k not in agora]
    return novas, sumidas

def issue_demanda(it):
    """Demanda: comentario de outro (em issue minha/aberta por mim, ou com mencao) ou status/assignee mudado por outro em issue minha."""
    if any(c['mencao'] or it['minha'] or it.get('reporter_eu') for c in it.get('comentarios', [])): return True
    return any(x.startswith(('status:', 'assignee:')) for h in it.get('changelog', []) for x in h['itens']) and it['minha']

# ---------------- detalhe (--jira show) ----------------
def show(key, me):
    i = get(f'/issue/{key}', {'fields': CAMPOS + ',description,subtasks,issuelinks,labels,duedate'})
    f = i['fields']; d = _issue(i, me)
    d['descricao'], _ = texto(f.get('description'), me)
    d['labels'] = f.get('labels') or []; d['prazo'] = f.get('duedate')
    d['filhas'] = [f"{s['key']} [{(s['fields'].get('status') or {}).get('name')}] {s['fields'].get('summary', '')}" for s in f.get('subtasks') or []]
    d['links'] = []
    for l in f.get('issuelinks') or []:
        o = l.get('outwardIssue'); i2 = l.get('inwardIssue')
        if o: d['links'].append(f"{l['type']['outward']} {o['key']} [{(o['fields'].get('status') or {}).get('name')}] {o['fields'].get('summary', '')}")
        if i2: d['links'].append(f"{l['type']['inward']} {i2['key']} [{(i2['fields'].get('status') or {}).get('name')}] {i2['fields'].get('summary', '')}")
    d['comentarios'] = []
    for c in (f.get('comment') or {}).get('comments', []):
        t, mencao = texto(c.get('body'), me)
        d['comentarios'].append({'quando': c['created'], 'de': (c.get('author') or {}).get('displayName', '?'), 'texto': t[:800], 'mencao': mencao})
    return d

def imprime_show(d):
    print(f"{d['key']} · {d['tipo']} · {d['status']} · assignee: {d['assignee'] or '-'} · reporter: {d['reporter'] or '-'}"
          + (f" · pai {d['pai']}" if d['pai'] else '') + (f" · prazo {d['prazo']}" if d['prazo'] else '') + (f" · {d['prioridade']}" if d['prioridade'] else ''))
    print(f"{d['titulo']}\n{d['url']}")
    if d['labels']: print('labels: ' + ', '.join(d['labels']))
    if d['descricao']: print(f"\nDESCRICAO:\n{d['descricao']}")
    if d['filhas']: print(f"\nSUB-TASKS ({len(d['filhas'])}):\n  " + '\n  '.join(d['filhas']))
    if d['links']: print('\nLINKS:\n  ' + '\n  '.join(d['links']))
    if d['comentarios']:
        print(f"\nCOMENTARIOS ({len(d['comentarios'])}):")
        for c in d['comentarios']: print(f"  [{hora(c['quando'])}] {c['de']}{' (MENCIONA VOCE)' if c['mencao'] else ''}: {c['texto']}")

# ---------------- impressao ----------------
def linha_issue(i, prefixo=''):
    quem = 'sua' if i['minha'] else (i['assignee'] or 'sem assignee')
    return f"{prefixo}{i['key']} [{i['status']}] ({quem}){' · pai ' + i['pai'] if i['pai'] else ''}\n   {i['titulo']}\n   {i['url']}"

def imprime_delta(novas, sumidas, mudadas):
    if not (novas or sumidas or mudadas): return False
    print('== JIRA')
    if novas:
        print(f'ATRIBUIDAS A VOCE ({len(novas)}):')
        for i in novas: print(linha_issue(i, '+ '))
    if mudadas:
        print(f'MUDARAM ({len(mudadas)}):')
        for i in mudadas:
            print(linha_issue(i, '~ ') + ('' if issue_demanda(i) else '   (so aviso)'))
            for h in i['changelog']: print(f"   [{hora(h['quando'])}] {h['de']}: {'; '.join(h['itens'])}")
            for c in i['comentarios']: print(f"   [{hora(c['quando'])}] {c['de']}{' (MENCIONA VOCE)' if c['mencao'] else ''}: {c['texto']}")
    if sumidas:
        print(f'SAIRAM DAS SUAS ABERTAS ({len(sumidas)}):')
        for i in sumidas: print(f"- {i['key']} [{i.get('fim', '?')}] {i['titulo']}")
    return True

# ---------------- tick ----------------
def tick_jira(s, desde, dry):
    st = s.setdefault('jira', {'issues': {}, 'ultima': None, 'primeira': None})
    me, nome = eu()
    agora_dt = datetime.now(timezone.utc)
    abertas = minhas_abertas(me)
    if desde is None and not st.get('ultima'):
        if not dry:
            ts = agora_dt.isoformat(); st.update(issues=abertas, ultima=ts, primeira=ts); sumidos.marca(st)
        return {'baseline': True, 'n': len(abertas)}
    fixo = desde is not None
    if not fixo: desde = core.dt(st['ultima'])
    novas, sumidas = ([i for i in abertas.values() if core.dt(i['criado']) >= desde], []) if fixo else delta(abertas, st['issues'])
    recompostas = []
    if not fixo:                                          # an issue only leaves when Jira confirms it (sumidos.py)
        sumidas = sumidos.confirma(sumidas, abertas, lambda i: i['key'],
                                   lambda its: {i['key']: saiu_como(i['key'], me) for i in its})
        novas, recompostas = sumidos.recompoe(st, st['issues'], novas, lambda i: core.dt(i.get('criado')), desde)
    mudadas = mudancas(desde, me)
    novas_keys = {i['key'] for i in novas}
    mudadas = [i for i in mudadas if i['key'] not in novas_keys or i.get('comentarios')]
    resolvidas = []
    if not dry:
        # resolve: comentei depois na issue (uma busca so, pelas keys das pendencias abertas)
        abertas_p = core.pend_abertas(s, 'jira')
        if abertas_p:
            keys = sorted({p['ref'].split('/')[0] for p in abertas_p})
            try:
                por_key = {i['key']: i for i in buscar(f'key in ({", ".join(keys)})', campos='comment,status')}
                for p in abertas_p:
                    key, _, cid = p['ref'].partition('/')
                    i = por_key.get(key)
                    if not i: continue
                    coms = (i['fields'].get('comment') or {}).get('comments', [])
                    meu_depois = any((c.get('author') or {}).get('accountId') == me and core.dt(c['created']) > core.dt(p['desde']) for c in coms)
                    fechada = ((i['fields'].get('status') or {}).get('statusCategory') or {}).get('key') == 'done'
                    if meu_depois or fechada:
                        como = 'voce respondeu' if meu_depois else 'issue fechada'
                        core.resolve(p, agora_dt, como); resolvidas.append((p, como))
            except (RuntimeError, LookupError): pass
        # enfileira: comentario de outro em issue minha/aberta por mim ou com @mencao (o modelo poda o que nao for demanda)
        for i in mudadas:
            for c in i.get('comentarios', []):
                if c['mencao'] or i['minha'] or i.get('reporter_eu'):
                    core.enfileira(s, 'jira', f"{i['key']}/{c['id']}", c['de'], f"{i['key']} {c['texto'][:70].replace(chr(10), ' ')}", c['quando'], agora_dt)
        st['issues'] = abertas; st['ultima'] = agora_dt.isoformat(); sumidos.marca(st)
    return {'baseline': False, 'novas': novas, 'sumidas': sumidas, 'mudadas': mudadas, 'resolvidas': resolvidas,
            'recompostas': recompostas}

def imprime_jira(r):
    if r['baseline']:
        print(f'== JIRA: baseline plantada agora ({r["n"]} issues abertas suas); a proxima rodada mostra o que mudar.'); return True
    algo = imprime_delta(r['novas'], r['sumidas'], r['mudadas'])
    if r.get('recompostas'):
        if not algo: print('== JIRA')
        print(f"lista de issues recomposta: {len(r['recompostas'])} issues abertas antes do ultimo tick voltaram (ja eram conhecidas, nada novo)")
        return True
    return algo


def _iso(x):
    try: return datetime.fromisoformat((x or '').replace('Z', '+00:00').replace('+0000', '+00:00')).isoformat()
    except ValueError: return None


class Fonte(Base):
    tipo = 'jira'
    lembrar = {'jira': 11.0}
    conserto = 'Jira down — Atlassian API token; see the tick error'

    def configura(self):
        global JIRA, API, TOKEN_FILE, LINHA_EMAIL, LINHA_TOKEN, BRT, MSG
        JIRA = (self.cfg.get('site') or sys.exit('jira: "site" faltando no config.json')).rstrip('/')
        API = JIRA + '/rest/api/3'
        TOKEN_FILE = paths.expande(self.cfg.get('token_file') or sys.exit('jira: "token_file" faltando no config.json'))
        LINHA_EMAIL, LINHA_TOKEN = int(self.cfg.get('linha_email', 3)), int(self.cfg.get('linha_token', 5))
        MSG = {**MSG_PADRAO, **(self.cfg.get('mensagens') or {})}
        BRT = core.BRT

    def tick(self, s, desde, dry): return tick_jira(s, desde, dry)
    def imprime(self, r): return imprime_jira(r)

    def prazo(self, it, p, s):
        """The duedate of the issue a row links to (or a pending item of an issue comment, ref KEY/comment)."""
        m = re.search(r'/browse/([A-Z][A-Z0-9]+-\d+)', (it or {}).get('link') or '')
        key = m.group(1) if m else (str((p or {}).get('ref') or '').split('/')[0] if (p or {}).get('fonte') == 'jira' else '')
        return (((s.get('jira') or {}).get('issues') or {}).get(key) or {}).get('vence') if key else None

    def tarefas_extras(self, s, agora):
        """My Jira issues (not in "tipos_fora", touched in the last N days): In Progress -> Doing; Done/Closed -> Done."""
        from watch_core import tasks as tc
        _corta = tc._corta
        t = self.cfg.get('tarefas') or {}
        janela = timedelta(days=int(t.get('janela_dias', 30)))
        fora = set(t.get('tipos_fora', ['Epic', 'Story']))
        feito = tuple(t.get('status_feito', ['done', 'closed', 'resolved']))
        out = {}
        for it in ((s.get('jira') or {}).get('issues') or {}).values():
            if not it.get('minha') or it.get('tipo') in fora: continue
            upd = _iso(it.get('atualizado'))
            if not upd or agora - datetime.fromisoformat(upd) > janela: continue      # old zombies stay in Jira
            st = (it.get('status') or '').lower()
            base = {'link': it.get('url') or f'{JIRA}/browse/{it["key"]}', 'entrou': _iso(it.get('criado')), 'origem': 'jira'}
            if st == 'in progress':
                out[f'j{it["key"]}'] = {**base, 'titulo': f'{it["key"]} {it["titulo"]}', 'curto': _corta(f'{it["key"]} {it["titulo"]}', 70), 'status': 'Fazendo', 'prio': 1,
                                        'deadline': it.get('vence'), 'ready_reason': f'card {it["key"]} em andamento no Jira', 'ready_reason_min': 'card em andamento no Jira'}
            elif st in feito and it.get('atualizado'):
                out[f'j{it["key"]}'] = {**base, 'titulo': f'{it["key"]} {it["titulo"]}', 'curto': _corta(f'{it["key"]} {it["titulo"]}', 70),
                                        'status': 'Feito', 'concluida': _iso(it['atualizado'])}
        return out

    def args(self, ap):
        ap.add_argument('--jira', nargs='+', metavar='all|show KEY', help='Jira: all (suas abertas) | show KEY')

    def cli(self, a, ctx):
        if not a.jira: return False
        me, nome = eu()
        if a.jira[0] == 'show' and len(a.jira) > 1:
            d = show(a.jira[1].strip().upper(), me)
            print(json.dumps(d, ensure_ascii=False, indent=1)) if a.json else imprime_show(d); return True
        if a.jira[0] == 'all':
            agora = minhas_abertas(me)
            if a.json: print(json.dumps(agora, ensure_ascii=False, indent=1)); return True
            print(f'{len(agora)} issues abertas atribuidas a {nome}:')
            for i in sorted(agora.values(), key=lambda x: x['atualizado'], reverse=True): print(linha_issue(i, '- '))
            return True
        sys.exit('--jira all | show KEY')
