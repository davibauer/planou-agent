#!/usr/bin/env python3
"""Source gitlab: what asks for my action on one GitLab instance. Three things, all read-only:
  - pending TODOS (/todos?state=pending): GitLab's own "needs you" queue -- assigned, review_requested, mentioned,
    directly_addressed, approval_required, build_failed/unmergeable (on my MRs).
  - OPEN MRs in the watched groups: new MR from someone else, Draft turned ready, new commits, conflict, MR gone
    (merged/closed). Does not use scope=all on the instance (an admin account would bring the whole GitLab).
  - new NOTES from other people on the open MRs of the groups (a comment on my MR without @mention does not create a todo).
Also warns (once a day) when the API token expires within "token_aviso_dias", and renews it by itself within
"token_rotacionar_dias" (PLN0364): POST /personal_access_tokens/self/rotate with an expires_at as far as the current
token's lifetime (30 to 365 days), the new value written atomically (0600) over the same token_file, under a lock and
never printed. Only while the token still works: an expired or refused (401) token is never rotated, it becomes a
"Precisa de você" ask (planou_tick.pedidos_credenciais). The decision goes to Planou as a low alert "Decidi: renovei o
token ..." (planou_tick.decisoes), from the note left in s['gitlab']['rotacao'].

Transport: curl (configurable binary: a Windows curl.exe reaches hosts only the Windows network sees, e.g. behind a
corporate proxy client; if the WSL interop binfmt is missing it is called through /init). The token header goes through
curl's stdin (-H @-, curl 7.55+), never on its command line; a 401 right after another instance rotated the shared
token file is retried once with the file's new value.

Config ("fontes": [{"tipo": "gitlab", ...}]):
  url               https://gitlab.example (required)
  token_file        file with the personal access token (required)
  grupos            groups whose open MRs are watched (required)
  curl              curl binary (default "curl")
  rotulo            short name in messages (default: the host)
  token_aviso_dias  default 10
  token_rotacionar_dias  default 7: rotate the token when it expires within this many days (0 = off). Needs the token
                    scope "api" or "self_rotate"; without it (403/404) the day falls back to the warning
  mensagens         {"sem_token", "timeout", "rede", "recusado", "vence"}: texts of the errors/warning ({nome},
                    {token_file}, {rc}, {dias}, {data} are filled in); defaults are generic
Extra CLI: --gitlab all | show <path!iid | path#iid | todo-id> (read-only).
"""
import os, re, sys, json, html, subprocess, urllib.parse
from datetime import datetime, timezone, timedelta, date

import core, paths
from watch_core import slowfs, fileio
from adapters import Fonte as Base, sumidos

GITLAB = API = TOKEN_FILE = NOME = FONTE = None
GRUPOS = []
CURL = 'curl'
BRT = timezone(timedelta(hours=-3))
TOKEN_AVISO_DIAS = 10
TOKEN_ROTACIONAR_DIAS = 7
ROTACAO_MIN_DIAS, ROTACAO_MAX_DIAS, ROTACAO_PADRAO_DIAS = 30, 365, 90
MSG_PADRAO = {'sem_token': 'token do {nome} nao encontrado em {token_file}',
              'timeout': '{nome} sem resposta (timeout)',
              'rede': '{nome} sem resposta (curl rc={rc})',
              'recusado': 'token do {nome} recusado (401): gerar outro e regravar {token_file}',
              'vence': 'token do {nome} vence em {dias} dia(s) ({data}) — gerar outro e regravar {token_file}'}
MSG = dict(MSG_PADRAO)
# acoes de todo que sao demanda (alguem pediu algo) x acoes que sao "sua MR quebrou/travou" (acao sua, ninguem pediu)
ACOES_PEDIDO = {'assigned', 'review_requested', 'mentioned', 'directly_addressed', 'approval_required', 'marked',
                'member_access_requested', 'review_submitted'}
ACOES_MINHA_MR = {'build_failed', 'unmergeable', 'merge_train_removed'}


def _msg(k, **kw):
    return MSG[k].format(nome=NOME, token_file=TOKEN_FILE, **kw)

# ---------------- transporte ----------------
def _token():
    try: return slowfs.read_text(TOKEN_FILE).strip()       # with a deadline on a Windows drive (PLN0245)
    except FileNotFoundError:
        sys.exit(_msg('sem_token'))

def _curl(args, entrada=None):
    """Roda o curl do Windows; sem binfmt (Exec format error) chama pelo /init com argv0 explicito. `entrada` vai pela
    entrada padrao nos dois caminhos (o cabecalho do token, PLN0364: nunca na linha de comando)."""
    try:
        return subprocess.run([CURL] + args, input=entrada, capture_output=True, timeout=90)
    except OSError as e:
        if getattr(e, 'errno', None) != 8: raise           # 8 = Exec format error
        return subprocess.run(['/init', CURL, os.path.basename(CURL)] + args, input=entrada, capture_output=True,
                              timeout=90)

def _com_token(args):
    """curl with the PRIVATE-TOKEN header read from stdin (-H @-, curl 7.55+), so the token never shows in ps or
    /proc/<pid>/cmdline. On a 401 the token file is read again: when it changed (another instance sharing the file
    has just rotated it), one more try with the new value. Returns (CompletedProcess, http status, body); a failure
    of curl itself comes back with status '' and the caller looks at returncode. TimeoutExpired propagates."""
    tok = None
    for _ in range(2):
        antes, tok = tok, _token()
        if antes is not None and tok == antes: break
        r = _curl(['-H', '@-'] + args, entrada=f'PRIVATE-TOKEN: {tok}\n'.encode())
        if r.returncode: return r, '', ''
        corpo, _, status = r.stdout.decode(errors='replace').rpartition('\n')
        if status != '401': break
    tok = None
    return r, status, corpo

def raw(path, params=None):
    """GET; devolve (status, corpo em texto)."""
    url = API + path + (('&' if '?' in path else '?') + urllib.parse.urlencode(params) if params else '')
    try:
        r, status, corpo = _com_token(['-s', '-m', '60', '-w', '\n%{http_code}', url])
    except subprocess.TimeoutExpired:
        sys.exit(_msg('timeout'))
    if r.returncode in (6, 7, 28, 35):
        sys.exit(_msg('rede', rc=r.returncode))
    if r.returncode:
        sys.exit(f'{os.path.basename(CURL)} falhou (rc={r.returncode}): {r.stderr.decode(errors="replace")[:200]}')
    if status == '401': sys.exit(_msg('recusado'))
    if status == '403': sys.exit(f'{NOME} negou acesso (403) em {path}')
    return int(status or 0), corpo

def get(path, params=None):
    st, corpo = raw(path, params)
    if st >= 400: raise RuntimeError(f'HTTP {st} em {path}: {corpo[:200]}')
    return json.loads(corpo) if corpo.strip() else None

def paginado(path, params=None):
    out, pg = [], 1
    while True:
        lote = get(path, {**(params or {}), 'per_page': 100, 'page': pg}) or []
        out += lote
        if len(lote) < 100 or pg >= 10: return out
        pg += 1

def texto(md):
    """Markdown/HTML do GitLab -> texto simples (o suficiente para ler)."""
    if not md: return ''
    md = re.sub(r'<br\s*/?>', '\n', md, flags=re.I)
    md = re.sub(r'<[^>]+>', '', md)
    return re.sub(r'\n{3,}', '\n\n', html.unescape(md)).strip()

def hora(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(BRT).strftime('%d/%m %H:%M') if iso else '??'

# ---------------- coleta ----------------
def eu():
    return get('/user')['username']

def token_self():
    """GET /personal_access_tokens/self (name, created_at, expires_at...) or None when it cannot be read."""
    try: return get('/personal_access_tokens/self')
    except Exception: return None

def token_info(t=None):
    """(dias ate o token vencer, data AAAA-MM-DD) ou (None, None) se nao der para ler."""
    t = t if t is not None else token_self()
    try:
        if not t or not t.get('expires_at'): return None, None
        return (date.fromisoformat(t['expires_at']) - date.today()).days, t['expires_at']
    except (ValueError, TypeError, AttributeError):
        return None, None

def _post(path, data):
    """POST without the failure paths of raw()/get(): (status, body). The body of a rotation holds the new token: the
    caller never puts it in a message, an exception or a log."""
    try:
        r, status, corpo = _com_token(['-s', '-m', '60', '-w', '\n%{http_code}', '-X', 'POST',
                                       '-H', 'Content-Type: application/json', '--data-binary', json.dumps(data),
                                       API + path])
    except subprocess.TimeoutExpired:
        return 0, ''
    if r.returncode: return 0, ''
    try: return int(status or 0), corpo
    except ValueError: return 0, ''

def _nova_validade(t):
    """The expires_at of the rotated token: today + the current token's lifetime (expires_at - created_at), within
    30..365 days (GitLab refuses more than a year; without expires_at it would give a week and rotate every day)."""
    dias = ROTACAO_PADRAO_DIAS
    try:
        ini = date.fromisoformat(str(t.get('created_at'))[:10]); fim = date.fromisoformat(str(t.get('expires_at'))[:10])
        if fim > ini: dias = (fim - ini).days
    except (ValueError, TypeError):
        pass
    return (date.today() + timedelta(days=max(ROTACAO_MIN_DIAS, min(ROTACAO_MAX_DIAS, dias)))).isoformat()

def _grava_token(valor):
    """Writes the new token over the token file (the symlink's target), 0600, atomically. If that fails, into a sibling
    '<file>.novo' (0600): the old token is already revoked, the new one must not be lost. Returns (path, ok_in_place)."""
    alvo = os.path.realpath(TOKEN_FILE)
    try:
        fileio.write(alvo, valor + '\n', mode=0o600)
        return alvo, True
    except OSError:
        reserva = alvo + '.novo'
        fileio.write(reserva, valor + '\n', mode=0o600)       # may raise: the caller reports the path, never the value
        return reserva, False

def rotaciona(st, dias):
    """Renews the token when 0 < dias <= TOKEN_ROTACIONAR_DIAS. Under a lock on the token file, the token is read again
    (another instance sharing the file may have just rotated it: GitLab revokes the whole chain when a rotated-out token
    is used). Returns a line for the tick output, or None when nothing was tried. Never a token value."""
    if not TOKEN_ROTACIONAR_DIAS or dias is None or not 0 < dias <= TOKEN_ROTACIONAR_DIAS: return None
    hoje = date.today().isoformat()
    if st.get('rotacao_falhou') == hoje: return None
    with fileio.locked(os.path.realpath(TOKEN_FILE)):
        t = token_self()                                     # read again under the lock, with the file's current token
        dias, vence = token_info(t)
        if dias is None or not 0 < dias <= TOKEN_ROTACIONAR_DIAS: return None    # someone else already rotated it
        nova = _nova_validade(t)
        status, corpo = _post('/personal_access_tokens/self/rotate', {'expires_at': nova})
        try: res = json.loads(corpo) if status in (200, 201) else None
        except ValueError: res = None
        valor = (res or {}).get('token') if isinstance(res, dict) else None
        if not valor:
            st['rotacao_falhou'] = hoje
            motivo = {401: 'token recusado', 403: 'sem o escopo api ou self_rotate', 404: 'GitLab sem a rota de rotação',
                      400: 'validade recusada pela política do GitLab', 0: 'sem resposta'}.get(status, f'HTTP {status}')
            return f'nao consegui renovar sozinho o token do {NOME} ({motivo}); {_msg("vence", dias=dias, data=vence)}'
        vence_novo = str(res.get('expires_at') or nova)[:10]
        try: date.fromisoformat(vence_novo)
        except ValueError: vence_novo = nova                 # an odd expires_at in the answer: the one asked for
        try:
            onde, no_lugar = _grava_token(valor)
        except OSError as e:
            st['rotacao_falhou'] = hoje
            st['rotacao'] = {'fonte': FONTE, 'vence': vence_novo, 'quando': datetime.now(timezone.utc).isoformat(),
                             'gravado': False, 'arquivo': '', 'erro': type(e).__name__}
            return (f'renovei o token do {NOME}, mas nao consegui gravar o novo ({type(e).__name__}): o antigo ja foi '
                    f'revogado, gere outro e regrave {TOKEN_FILE}')
        finally:
            valor = None
        st['token_dias'] = (date.fromisoformat(vence_novo) - date.today()).days
        st['token_vence'] = vence_novo
        st['rotacao'] = {'fonte': FONTE, 'vence': vence_novo, 'quando': datetime.now(timezone.utc).isoformat(),
                         'gravado': no_lugar, 'arquivo': '' if no_lugar else onde}
        if not no_lugar:
            return (f'renovei o token do {NOME} (vence em {vence_novo}), mas gravei o novo em {onde}: mova para '
                    f'{TOKEN_FILE} (0600)')
        return f'renovei sozinho o token do {NOME}: vence em {vence_novo} (o antigo foi revogado; gravado em {TOKEN_FILE})'

def aviso_token(st, dry):
    """Uma consulta ao token por dia; o aviso de vencimento sai so no primeiro tick do dia. Perto de vencer, renova."""
    hoje = date.today().isoformat()
    if st.get('token_dia') == hoje: return []
    dias, vence = token_info()
    if not dry: st['token_dia'] = hoje; st['token_dias'] = dias; st['token_vence'] = vence   # vence: Planou (Ferramentas)
    if not dry:
        linha = rotaciona(st, dias)
        if linha: return [linha]
    if dias is not None and dias <= TOKEN_AVISO_DIAS:
        return [_msg('vence', dias=dias, data=(date.today() + timedelta(days=dias)).isoformat())]
    return []

def todos(me):
    out = {}
    for t in paginado('/todos', {'state': 'pending'}):
        alvo = t.get('target') or {}
        proj = (t.get('project') or {}).get('path_with_namespace', '?')
        sep = '!' if t['target_type'] == 'MergeRequest' else '#'
        out[str(t['id'])] = {
            'id': t['id'], 'acao': t['action_name'], 'tipo': t['target_type'],
            'alvo': f"{proj}{sep}{alvo.get('iid')}" if alvo.get('iid') else proj,
            'projeto': proj, 'titulo': alvo.get('title') or '', 'autor': (t.get('author') or {}).get('name'),
            'alvo_meu': (alvo.get('author') or {}).get('username') == me,
            'criado': t['created_at'], 'url': t.get('target_url'), 'corpo': texto(t.get('body'))[:300],
            'vence': alvo.get('due_date') or None,                  # issues only (the Prazo in Planou)
            # 'assigned' em MR minha e o GitLab se auto-atribuindo ao abrir: nao e pedido de ninguem
            'pedido': t['action_name'] in ACOES_PEDIDO and not (t['action_name'] == 'assigned' and (alvo.get('author') or {}).get('username') == me),
        }
    return out

def mrs_abertas(me):
    out = {}
    for g in GRUPOS:
        for m in paginado(f'/groups/{urllib.parse.quote(g, safe="")}/merge_requests', {'state': 'opened', 'scope': 'all'}):
            k = m['references']['full']
            out[k] = {
                'key': k, 'project_id': m['project_id'], 'iid': m['iid'], 'titulo': m['title'],
                'autor': m['author']['username'], 'autor_nome': m['author']['name'], 'minha': m['author']['username'] == me,
                'draft': bool(m.get('draft')), 'sha': m.get('sha'), 'conflito': m.get('detailed_merge_status') == 'conflict',
                'alvo': m.get('target_branch'), 'criado': m['created_at'], 'atualizado': m['updated_at'], 'url': m['web_url'],
                'revisores': [r['username'] for r in m.get('reviewers') or []],
            }
    return out

def notas_novas(mrs, desde, me):
    """Comentarios (nao-sistema) de outras pessoas nas MRs abertas dos grupos, desde `desde` (datetime aware)."""
    out = []
    for k, m in mrs.items():
        if datetime.fromisoformat(m['atualizado'].replace('Z', '+00:00')) < desde: continue   # updated_at sobe a cada comentario
        try:
            notas = get(f"/projects/{m['project_id']}/merge_requests/{m['iid']}/notes",
                        {'sort': 'desc', 'order_by': 'updated_at', 'per_page': 20}) or []
        except RuntimeError: continue
        novas = [n for n in notas if not n.get('system') and n['author']['username'] != me
                 and datetime.fromisoformat(n['created_at'].replace('Z', '+00:00')) >= desde]
        if novas:
            out.append({'mr': k, 'titulo': m['titulo'], 'minha': m['minha'], 'url': m['url'],
                        'notas': [{'quando': n['created_at'], 'de': n['author']['name'], 'texto': texto(n['body'])[:400]} for n in reversed(novas)]})
    return out

def mr_fechada_como(m):
    """MR que sumiu da lista de abertas: merged, closed ou (se nao achar) ?."""
    try: return get(f"/projects/{m['project_id']}/merge_requests/{m['iid']}").get('state', '?')
    except Exception: return '?'

def mr_saiu_como(m):
    """MR missing from the open MRs: 'merged'/'closed' when GitLab confirms it; None = still open or no answer."""
    try: f = mr_fechada_como(m)
    except SystemExit: return None
    return f if f in ('merged', 'closed') else None

def todos_feitos(ts):
    """Todos missing from the pending ones that GitLab lists as done (the last 100 done): {id: 'feito'}."""
    try: feitos = {str(t.get('id')) for t in (get('/todos', {'state': 'done', 'per_page': 100}) or [])}
    except SystemExit: return {}
    return {str(t['id']): 'feito' for t in ts if str(t['id']) in feitos}

def delta(todos_agora, todos_antes, mrs_agora, mrs_antes):
    t_novos = [t for k, t in todos_agora.items() if k not in todos_antes]
    t_sumidos = [t for k, t in todos_antes.items() if k not in todos_agora]
    m_novas = [m for k, m in mrs_agora.items() if k not in mrs_antes]
    m_mudadas = []
    for k, m in mrs_agora.items():
        o = mrs_antes.get(k)
        if not o: continue
        muds = []
        if o.get('draft') and not m['draft']: muds.append('saiu de Draft')
        if not o.get('draft') and m['draft']: muds.append('virou Draft')
        if o.get('sha') != m['sha']: muds.append('commits novos')
        if not o.get('conflito') and m['conflito']: muds.append('CONFLITO')
        if o.get('conflito') and not m['conflito']: muds.append('conflito resolvido')
        if o.get('titulo') != m['titulo']: muds.append('titulo mudou')
        if muds: m_mudadas.append((o, m, muds))
    m_sumidas = [m for k, m in mrs_antes.items() if k not in mrs_agora]
    return t_novos, t_sumidos, m_novas, m_mudadas, m_sumidas

# ---------------- detalhe (--show) ----------------
def _ref(s):
    """'grupo/proj!16' | 'grupo/proj#3' | '1!16' -> (project, tipo, iid)."""
    m = re.fullmatch(r'(.+?)([!#])(\d+)', s.strip())
    if not m: sys.exit(f'referencia invalida: {s} (use caminho!iid para MR, caminho#iid para issue, ou o id do todo)')
    return urllib.parse.quote(m.group(1), safe=''), ('mr' if m.group(2) == '!' else 'issue'), int(m.group(3))

def _limpa_trace(t):
    t = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', t)
    t = re.sub(r'section_(start|end):\d+:[^\r\n]*', '', t)
    t = re.sub(r'(?m)^\d{4}-\d\d-\d\dT[\d:.]+Z \d\dO\+? ?', '', t)      # prefixo de timestamp do log do GitLab 18
    t = re.sub(r'token=\S+', 'token=…', t)
    return [l.rstrip() for l in t.replace('\r', '\n').split('\n') if l.strip()]

def show_mr(proj, iid, linhas_log=40):
    m = get(f'/projects/{proj}/merge_requests/{iid}')
    pid = m['project_id']
    d = {'ref': m['references']['full'], 'titulo': m['title'], 'estado': m['state'], 'draft': m.get('draft'),
         'autor': m['author']['name'], 'origem': m['source_branch'], 'alvo': m['target_branch'],
         'merge_status': m.get('detailed_merge_status'), 'criado': m['created_at'], 'atualizado': m['updated_at'],
         'url': m['web_url'], 'descricao': texto(m.get('description')), 'labels': m.get('labels') or [],
         'revisores': [r['name'] for r in m.get('reviewers') or []], 'assignees': [a['name'] for a in m.get('assignees') or []]}
    try:
        ap = get(f'/projects/{pid}/merge_requests/{iid}/approvals')
        d['aprovacoes'] = {'aprovada': ap.get('approved'), 'exigidas': ap.get('approvals_required'),
                           'por': [a['user']['name'] for a in ap.get('approved_by') or []]}
    except Exception: d['aprovacoes'] = None
    hp = m.get('head_pipeline') or {}
    d['pipeline'] = {'id': hp.get('id'), 'status': hp.get('status'), 'url': hp.get('web_url'), 'falhos': []}
    if hp.get('id'):
        for j in get(f"/projects/{pid}/pipelines/{hp['id']}/jobs", {'per_page': 100}) or []:
            if j['status'] == 'failed':
                st, tr = raw(f"/projects/{pid}/jobs/{j['id']}/trace")
                d['pipeline']['falhos'].append({'job': j['name'], 'stage': j['stage'], 'allow_failure': j.get('allow_failure'),
                                                'url': j.get('web_url'), 'log': _limpa_trace(tr)[-linhas_log:] if st < 400 else []})
    disc = get(f'/projects/{pid}/merge_requests/{iid}/discussions', {'per_page': 100}) or []
    d['discussoes_abertas'] = []
    for x in disc:
        n0 = x['notes'][0]
        if n0.get('resolvable') and not n0.get('resolved'):
            d['discussoes_abertas'].append({'de': n0['author']['name'], 'quando': n0['created_at'],
                                            'arquivo': (n0.get('position') or {}).get('new_path'), 'texto': texto(n0['body'])[:400]})
    notas = get(f'/projects/{pid}/merge_requests/{iid}/notes', {'sort': 'desc', 'order_by': 'created_at', 'per_page': 30}) or []
    d['comentarios'] = [{'de': n['author']['name'], 'quando': n['created_at'], 'texto': texto(n['body'])[:400]}
                        for n in reversed(notas) if not n.get('system')][-10:]
    try:
        difs = get(f'/projects/{pid}/merge_requests/{iid}/diffs', {'per_page': 100}) or []
        d['arquivos'] = [x['new_path'] + (' (novo)' if x.get('new_file') else ' (removido)' if x.get('deleted_file') else '') for x in difs]
    except Exception: d['arquivos'] = []
    return d

def show_issue(proj, iid):
    i = get(f'/projects/{proj}/issues/{iid}')
    notas = get(f"/projects/{i['project_id']}/issues/{iid}/notes", {'sort': 'asc', 'order_by': 'created_at', 'per_page': 50}) or []
    return {'ref': i['references']['full'], 'titulo': i['title'], 'estado': i['state'], 'autor': i['author']['name'],
            'assignees': [a['name'] for a in i.get('assignees') or []], 'labels': i.get('labels') or [],
            'criado': i['created_at'], 'prazo': i.get('due_date'), 'url': i['web_url'], 'descricao': texto(i.get('description')),
            'comentarios': [{'de': n['author']['name'], 'quando': n['created_at'], 'texto': texto(n['body'])[:400]}
                            for n in notas if not n.get('system')]}

def imprime_show(d):
    if 'origem' in d:      # MR
        print(f"{d['ref']} · {'Draft · ' if d['draft'] else ''}{d['estado']} · {d['origem']} -> {d['alvo']} · {d['merge_status']}")
        print(f"{d['titulo']}\n{d['url']}\nautor: {d['autor']} · revisores: {', '.join(d['revisores']) or '-'} · labels: {', '.join(d['labels']) or '-'}")
        if d['aprovacoes']: print(f"aprovacoes: {'ok' if d['aprovacoes']['aprovada'] else 'pendente'} (exigidas {d['aprovacoes']['exigidas']}, por: {', '.join(d['aprovacoes']['por']) or '-'})")
        p = d['pipeline']
        print(f"pipeline: {p['status'] or 'nenhum'}" + (f" · {p['url']}" if p['url'] else ''))
        for j in p['falhos']:
            print(f"\n  JOB FALHOU: {j['job']} ({j['stage']}){' [allow_failure]' if j['allow_failure'] else ''} · {j['url']}")
            for l in j['log']: print(f'    {l}')
        if d['descricao']: print(f"\nDESCRICAO:\n{d['descricao']}")
        if d['arquivos']: print(f"\nARQUIVOS ({len(d['arquivos'])}):\n  " + '\n  '.join(d['arquivos'][:60]))
        if d['discussoes_abertas']:
            print(f"\nDISCUSSOES NAO RESOLVIDAS ({len(d['discussoes_abertas'])}):")
            for x in d['discussoes_abertas']: print(f"  [{hora(x['quando'])}] {x['de']}{' @ ' + x['arquivo'] if x['arquivo'] else ''}: {x['texto']}")
    else:                  # issue
        print(f"{d['ref']} · {d['estado']} · autor {d['autor']} · assignees: {', '.join(d['assignees']) or '-'} · labels: {', '.join(d['labels']) or '-'}" + (f" · prazo {d['prazo']}" if d['prazo'] else ''))
        print(f"{d['titulo']}\n{d['url']}")
        if d['descricao']: print(f"\nDESCRICAO:\n{d['descricao']}")
    if d['comentarios']:
        print(f"\nCOMENTARIOS ({len(d['comentarios'])}):")
        for c in d['comentarios']: print(f"  [{hora(c['quando'])}] {c['de']}: {c['texto']}")

# ---------------- impressao ----------------
def linha_todo(t, prefixo=''):
    quem = f" · por {t['autor']}" if t['autor'] and not t['alvo_meu'] else ''
    tag = ' (sua MR)' if t['alvo_meu'] and t['tipo'] == 'MergeRequest' else ''
    return (f"{prefixo}todo {t['id']} {t['acao']} · {t['alvo']}{tag}{quem}\n"
            f"   {t['titulo']}\n   {t['url']}" + (f"\n   > {t['corpo'][:200]}" if t['corpo'] and t['acao'] in ('mentioned', 'directly_addressed') else ''))

def linha_mr(m, prefixo=''):
    flags = ' · Draft' if m['draft'] else ''
    flags += ' · CONFLITO' if m['conflito'] else ''
    quem = 'sua' if m['minha'] else m['autor_nome']
    return f"{prefixo}{m['key']} ({quem}){flags} -> {m['alvo']}\n   {m['titulo']}\n   {m['url']}"

def imprime_delta(t_novos, t_sumidos, m_novas, m_mudadas, m_sumidas, notas, avisos):
    for a in avisos: print(f'AVISO (gitlab): {a}')
    if not (t_novos or t_sumidos or m_novas or m_mudadas or m_sumidas or notas): return bool(avisos)
    print('== GITLAB')
    if t_novos:
        print(f'TODOS NOVOS ({len(t_novos)}):')
        for t in t_novos: print(linha_todo(t, '+ ') + ('' if t['pedido'] or t['acao'] in ACOES_MINHA_MR else '   (so aviso)'))
    if notas:
        print(f'COMENTARIOS NOVOS ({sum(len(n["notas"]) for n in notas)} em {len(notas)} MRs):')
        for n in notas:
            print(f"-- {n['mr']} ({'sua MR' if n['minha'] else 'MR de outro'}) · {n['titulo']}\n   {n['url']}")
            for x in n['notas']: print(f"   [{hora(x['quando'])}] {x['de']}: {x['texto']}")
    if m_novas:
        print(f'MRs NOVAS ({len(m_novas)}):')
        for m in m_novas: print(linha_mr(m, '+ '))
    if m_mudadas:
        print(f'MRs MUDARAM ({len(m_mudadas)}):')
        for o, m, muds in m_mudadas: print(linha_mr(m, f"~ [{', '.join(muds)}] "))
    if m_sumidas:
        print(f'MRs SAIRAM DA LISTA ({len(m_sumidas)}):')
        for m in m_sumidas: print(f"- {m['key']} [{m.get('fim', '?')}] {m['titulo']}")
    if t_sumidos:
        print(f'TODOS RESOLVIDOS ({len(t_sumidos)}):')
        for t in t_sumidos: print(f"- todo {t['id']} {t['acao']} · {t['alvo']} · {t['titulo']}" + (f" [{t['fim']}]" if t.get('fim') not in (None, 'feito') else ''))
    return True


# ---------------- tick ----------------
def tick_gitlab(s, desde, dry):
    dt = core.dt
    st = s.setdefault('gitlab', {'todos': {}, 'mrs': {}, 'ultima': None, 'primeira': None})
    me = eu()
    t_agora, m_agora = todos(me), mrs_abertas(me)
    avisos = aviso_token(st, dry)
    if desde is None and not st.get('ultima'):           # primeira rodada: planta a baseline
        if not dry:
            ts = datetime.now(timezone.utc).isoformat()
            st.update(todos=t_agora, mrs=m_agora, ultima=ts, primeira=ts); sumidos.marca(st)
        return {'baseline': True, 'n_todos': len(t_agora), 'n_mrs': len(m_agora), 'avisos': avisos}
    if desde is not None:                                # --since: janela fixa, sem estado
        t_novos = [t for t in t_agora.values() if dt(t['criado']) >= desde]
        m_novas = [m for m in m_agora.values() if dt(m['criado']) >= desde]
        m_mudadas = [(m, m, ['atualizada']) for m in m_agora.values() if dt(m['atualizado']) >= desde and dt(m['criado']) < desde]
        t_sumidos, m_sumidas = [], []
        recompostos = []
    else:
        t_novos, t_sumidos, m_novas, m_mudadas, m_sumidas = delta(t_agora, st['todos'], m_agora, st['mrs'])
        # an MR or a todo only leaves when GitLab confirms it (sumidos.py): an empty answer is not "all closed"
        m_sumidas = sumidos.confirma(m_sumidas, m_agora, lambda m: m['key'],
                                     lambda ms: {m['key']: mr_saiu_como(m) for m in ms})
        t_sumidos = sumidos.confirma(t_sumidos, t_agora, lambda t: str(t['id']), todos_feitos)
        desde = dt(st['ultima'])
        m_novas, m_recompostas = sumidos.recompoe(st, st['mrs'], m_novas, lambda m: dt(m.get('criado')), desde)
        t_novos, t_recompostos = sumidos.recompoe(st, st['todos'], t_novos, lambda t: dt(t.get('criado')), desde)
        recompostos = m_recompostas + t_recompostos
    notas = notas_novas(m_agora, desde, me)
    for col, agora in (('todos', t_agora), ('mrs', m_agora)):
        for k, it in agora.items():
            for extra in ('status', 'nota'):
                if k in st[col] and extra in st[col][k]: it[extra] = st[col][k][extra]
    if not dry:
        st['todos'] = t_agora; st['mrs'] = m_agora; st['ultima'] = datetime.now(timezone.utc).isoformat(); sumidos.marca(st)
    return {'baseline': False, 'todos_novos': t_novos, 'todos_resolvidos': t_sumidos, 'mrs_novas': m_novas,
            'mrs_mudadas': [(o, n, mu) for o, n, mu in m_mudadas], 'mrs_sumidas': m_sumidas, 'notas': notas, 'avisos': avisos,
            'recompostos': recompostos}


def imprime_gitlab(r):
    if r['baseline']:
        print(f'== GITLAB: baseline plantada agora ({r["n_todos"]} todos pendentes, {r["n_mrs"]} MRs abertas em {", ".join(GRUPOS)}); a proxima rodada mostra o que mudar.')
        for a in r['avisos']: print(f'AVISO (gitlab): {a}')
        return True
    algo = imprime_delta(r['todos_novos'], r['todos_resolvidos'], r['mrs_novas'], r['mrs_mudadas'], r['mrs_sumidas'], r['notas'], r['avisos'])
    if r.get('recompostos'):
        if not algo: print('== GITLAB')
        print(f"lista recomposta: {len(r['recompostos'])} MRs/todos de antes do ultimo tick voltaram (ja eram conhecidos, nada novo)")
        return True
    return algo


class Fonte(Base):
    tipo = 'gitlab'
    lembrar = {}
    conserto = 'GitLab down or token refused — see the tick error'

    def configura(self):
        global GITLAB, API, TOKEN_FILE, NOME, GRUPOS, CURL, TOKEN_AVISO_DIAS, TOKEN_ROTACIONAR_DIAS, MSG, BRT, FONTE
        GITLAB = (self.cfg.get('url') or sys.exit('gitlab: "url" faltando no config.json')).rstrip('/')
        API = GITLAB + '/api/v4'
        TOKEN_FILE = paths.expande(self.cfg.get('token_file') or sys.exit('gitlab: "token_file" faltando no config.json'))
        NOME = self.cfg.get('rotulo') or urllib.parse.urlparse(GITLAB).hostname
        GRUPOS = list(self.cfg.get('grupos') or [])
        CURL = self.cfg.get('curl') or 'curl'
        TOKEN_AVISO_DIAS = int(self.cfg.get('token_aviso_dias', 10))
        TOKEN_ROTACIONAR_DIAS = max(0, int(self.cfg.get('token_rotacionar_dias', 7)))
        FONTE = self.nome
        MSG = {**MSG_PADRAO, **(self.cfg.get('mensagens') or {})}
        BRT = core.BRT

    def tick(self, s, desde, dry): return tick_gitlab(s, desde, dry)
    def prazo(self, it, p, s):
        """The due_date of the GitLab issue a row links to (kept from the to-do list)."""
        link = ((it or {}).get('link') or '').split('#')[0]
        if '/-/issues/' not in link: return None
        return next((t.get('vence') for t in ((s.get('gitlab') or {}).get('todos') or {}).values()
                     if t.get('vence') and (t.get('url') or '').split('#')[0] == link), None)
    def vence(self, s):
        v = (s.get('gitlab') or {}).get('token_vence')
        return f'{v}T23:59:59-03:00' if v and 'T' not in v else v
    def imprime(self, r): return imprime_gitlab(r)
    def api_get(self, path, params=None): return get(path, params)

    def args(self, ap):
        ap.add_argument('--gitlab', nargs='+', metavar='all|show REF', help='GitLab: all (todos + MRs abertas) | show caminho!iid | caminho#iid | id do todo')

    def cli(self, a, ctx):
        if not a.gitlab: return False
        if a.gitlab[0] == 'show' and len(a.gitlab) > 1:
            ref = a.gitlab[1]
            if ref.isdigit():                       # id de todo -> alvo
                t = next((t for t in paginado('/todos') if str(t['id']) == ref), None)
                if not t: sys.exit(f'todo {ref} nao encontrado')
                ref = t['target']['references']['full'] if t.get('target', {}).get('references') else sys.exit(f'todo {ref} sem alvo navegavel')
            proj, tipo, iid = _ref(ref)
            d = show_mr(proj, iid) if tipo == 'mr' else show_issue(proj, iid)
            print(json.dumps(d, ensure_ascii=False, indent=1)) if a.json else imprime_show(d); return True
        if a.gitlab[0] == 'all':
            me = eu(); t_agora, m_agora = todos(me), mrs_abertas(me)
            if a.json: print(json.dumps({'todos': t_agora, 'mrs': m_agora}, ensure_ascii=False, indent=1)); return True
            print(f'{len(t_agora)} todos pendentes:')
            for t in sorted(t_agora.values(), key=lambda x: x['criado'], reverse=True): print(linha_todo(t, '- '))
            print(f"\n{len(m_agora)} MRs abertas em {', '.join(GRUPOS)}:")
            for m in sorted(m_agora.values(), key=lambda x: x['atualizado'], reverse=True): print(linha_mr(m, '- '))
            return True
        sys.exit('--gitlab all | show REF')
