#!/usr/bin/env python3
"""Source github: what asks for my action in one GitHub organization.

Three things, all read-only, through `gh api` (token from `gh auth login`, scope repo):
  - new NOTIFICATIONS (/notifications?all=true&since=cursor): GitHub's own "needs you" queue -- review_requested, mention,
    team_mention, assign (someone asked); ci_activity (workflow failed: your action if on main or on the branch of a PR of
    yours); author/comment/state_change/subscribed (notice only: the detail comes in the comments).
  - watched OPEN PRs: mine (any repo of the org), the ones asking for my review, and other people's in the configured
    repos -- new PR, Draft turned ready, new commits, CONFLICT, CI BROKE, review (approved / changes requested),
    PR gone (merged/closed). One GraphQL query for the whole org.
  - new COMMENTS from other people on the watched PRs (issue comments, review comments and reviews with a body).

Config ("fontes": [{"tipo": "github", ...}]):
  org     the organization (required)
  repos   repos where a new PR from someone else matters (probable review). Outside them only my PRs and the ones asking
          for my review come in.
  lembrar_horas  business hours a PR of mine stays green and ready with no review before it becomes a pending item
          (default 4; null = off). Reminders then follow "max_lembretes" of the instance, like Slack. A PR with no
          activity for longer than tarefas.janela_dias (default 14) is left alone, as in the task rows.

Pending queue ('github'): my PR, not Draft, every check green, no review from anyone else (approved, changes requested
or a review comment; bots do not count). The clock starts when it became green and ready and only counts business hours
("comercial", "fuso_horas"); checks running after a push hold it. It resolves by itself when a review comes in, the PR
goes back to Draft or is merged / closed; red CI pauses it (out of the queue, the clock starts over when green again).
The item carries the PR link (task origin). No extra API call: reviews and check times come in the same PR query.

State: sub-tree 'github' of the instance state. Extra CLI: --github all | show <repo#N | repo#issue/N> (read-only).
"""
import re, sys, json, subprocess, urllib.parse
from datetime import datetime, timezone, timedelta

import core
from adapters import Fonte as Base

ORG = None
REPOS = []
BRT = timezone(timedelta(hours=-3))
# motivos de notificacao que sao demanda (alguem pediu algo) x os que sao so aviso (o detalhe sai nos comentarios/PRs)
MOTIVOS_PEDIDO = {'review_requested', 'mention', 'team_mention', 'assign', 'security_alert'}
MOTIVO_NOME = {'review_requested': 'pediram sua revisao', 'mention': 'mencionaram voce', 'team_mention': 'mencionaram seu time',
               'assign': 'atribuiram a voce', 'ci_activity': 'CI', 'author': 'atividade em PR sua', 'comment': 'atividade em thread sua',
               'state_change': 'mudou de estado', 'subscribed': 'atividade', 'security_alert': 'ALERTA DE SEGURANCA', 'manual': 'atividade'}

# ---------------- transporte ----------------
def gh(args, raw=False):
    """`gh api ...`; devolve JSON (ou texto com raw=True). Falha de auth/rede vira SystemExit com a causa."""
    try:
        r = subprocess.run(['gh', 'api'] + args, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        sys.exit('gh nao instalado')
    except subprocess.TimeoutExpired:
        sys.exit('GitHub sem resposta (timeout no gh api)')
    if r.returncode:
        err = (r.stderr or r.stdout).strip()
        if 'HTTP 401' in err or 'authentication' in err.lower() or 'gh auth login' in err:
            sys.exit(f'GitHub recusou o token (gh auth status / gh auth login): {err[:200]}')
        if 'HTTP 404' in err: raise LookupError(err[:200])
        if 'could not resolve' in err.lower() or 'dial tcp' in err.lower() or 'connection' in err.lower():
            sys.exit(f'GitHub sem resposta: {err[:200]}')
        raise RuntimeError(err[:300])
    return r.stdout if raw else (json.loads(r.stdout) if r.stdout.strip() else None)

def get(path, params=None, paginate=False):
    """GET; com paginate=True percorre page=1.. ate vir pagina curta (o --paginate do gh 2.4 concatena JSONs sem --slurp;
    e /notifications limita a 50 por pagina, entao o per_page pedido tem de ser o que o endpoint devolve)."""
    if not paginate:
        url = path + (('&' if '?' in path else '?') + urllib.parse.urlencode(params) if params else '')
        return gh([url])
    out, pg, por = [], 1, int((params or {}).get('per_page', 100))
    while pg <= 20:
        url = path + ('&' if '?' in path else '?') + urllib.parse.urlencode({**(params or {}), 'page': pg})
        lote = gh([url]) or []
        out += lote
        if len(lote) < por: break
        pg += 1
    return out

def graphql(query, **vars):
    args = ['graphql', '-f', f'query={query}']
    for k, v in vars.items(): args += ['-F' if isinstance(v, int) else '-f', f'{k}={v}']
    d = gh(args)
    if d.get('errors'): raise RuntimeError(f'graphql: {d["errors"][0].get("message")}')
    return d['data']

def hora(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(BRT).strftime('%d/%m %H:%M') if iso else '??'

def dt(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00')) if iso else None

def html_url(api_url):
    """https://api.github.com/repos/o/r/pulls/12 -> https://github.com/o/r/pull/12"""
    if not api_url: return None
    return api_url.replace('https://api.github.com/repos/', 'https://github.com/').replace('/pulls/', '/pull/')

def chave(repo_full, n):
    """'org/repo', 110 -> 'repo#110' (a org e fixa)."""
    return f"{repo_full.split('/')[-1]}#{n}"

def _ref(s):
    """'repo#110' | 'org/repo#110' | 'repo#issue/12' -> (repo, tipo, numero)."""
    m = re.fullmatch(r'(?:[\w.-]+/)?([\w.-]+)#(issue/)?(\d+)', s.strip())
    if not m: sys.exit(f'referencia invalida: {s} (use repo#N para PR, repo#issue/N para issue)')
    return m.group(1), ('issue' if m.group(2) else 'pr'), int(m.group(3))

# ---------------- coleta ----------------
def eu():
    return get('user')['login']

def notificacoes(desde):
    """Notificacoes atualizadas desde `desde` (datetime), lidas ou nao — o cursor e nosso, nao o 'unread' do GitHub."""
    params = {'all': 'true', 'per_page': 50}                 # o endpoint devolve no maximo 50 por pagina
    if desde: params['since'] = desde.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    out = {}
    for n in get('notifications', params, paginate=True) or []:
        sub, repo = n.get('subject') or {}, (n.get('repository') or {}).get('full_name', '?')
        num = re.search(r'/(\d+)$', sub.get('url') or '')
        branch = re.search(r' for (\S+) branch', sub.get('title') or '')
        out[n['id']] = {'id': n['id'], 'motivo': n['reason'], 'tipo': sub.get('type'), 'repo': repo,
                        'alvo': chave(repo, num.group(1)) if num else repo.split('/')[-1],
                        'titulo': sub.get('title') or '', 'branch': branch.group(1) if branch else None,
                        'quando': n['updated_at'], 'lida': not n.get('unread'), 'url': html_url(sub.get('url')) or f'https://github.com/{repo}/actions'}
    return out

PR_QUERY = '''query($q: String!, $after: String) { search(type: ISSUE, query: $q, first: 100, after: $after) {
  issueCount pageInfo { hasNextPage endCursor }
  nodes { ... on PullRequest { number title url isDraft mergeable reviewDecision headRefOid headRefName baseRefName createdAt updatedAt
    repository { nameWithOwner } author { login }
    reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login } ... on Team { name } } } }
    reviews(first: 30) { nodes { state author { login __typename } } }
    commits(last: 1) { nodes { commit { statusCheckRollup { state
      contexts(first: 30) { nodes { ... on CheckRun { name conclusion detailsUrl completedAt } ... on StatusContext { context state targetUrl createdAt } } } } } } } } } } }'''

def prs_abertas(me, info=None):
    """PRs abertas da org que interessam: minhas, com minha revisao pedida, ou de outros nos REPOS.

    info (dict, opcional) recebe o que a busca viu antes do filtro: 'vistas' (chaves de todas as PRs abertas que vieram)
    e 'incompleta' (vieram menos PRs que o issueCount da busca, ou nenhuma). A busca do GitHub as vezes volta vazia ou
    curta sem erro: quem compara com o tick anterior usa isso para nao dar como fechada uma PR que so nao veio."""
    out, after = {}, None
    vistas, total = set(), None
    while True:
        d = graphql(PR_QUERY, q=f'is:pr is:open org:{ORG}', **({'after': after} if after else {}))['search']
        if total is None: total = d.get('issueCount')
        for p in d['nodes']:
            if not p or not p.get('repository'): continue
            repo = p['repository']['nameWithOwner']; curto = repo.split('/')[-1]
            vistas.add(chave(repo, p['number']))
            autor = (p.get('author') or {}).get('login', '?')
            revisores = [(r.get('requestedReviewer') or {}).get('login') or (r.get('requestedReviewer') or {}).get('name') for r in p['reviewRequests']['nodes']]
            revisores = [r for r in revisores if r]
            if not (autor == me or me in revisores or curto in REPOS): continue
            roll = ((p['commits']['nodes'] or [{}])[0].get('commit') or {}).get('statusCheckRollup') or {}
            ctxs = (roll.get('contexts') or {}).get('nodes') or []
            falhos = [c.get('name') or c.get('context') for c in ctxs
                      if (c.get('conclusion') or c.get('state')) in ('FAILURE', 'ERROR', 'TIMED_OUT', 'CANCELLED')]
            # reviews from other people (a reply of mine in a review thread is a COMMENTED review authored by me; bots
            # such as automatic reviewers are not a person looking at the PR)
            reviews = [rv['state'] for rv in (p.get('reviews') or {}).get('nodes') or []
                       if rv and (rv.get('author') or {}).get('login') not in (None, me)
                       and (rv.get('author') or {}).get('__typename') != 'Bot' and rv.get('state') != 'PENDING']
            checks_em = max((c.get('completedAt') or c.get('createdAt') or '' for c in ctxs), default='') or None
            out[chave(repo, p['number'])] = {
                'key': chave(repo, p['number']), 'repo': repo, 'numero': p['number'], 'titulo': p['title'], 'url': p['url'],
                'autor': autor, 'minha': autor == me, 'draft': p['isDraft'], 'sha': p['headRefOid'], 'branch': p['headRefName'],
                'alvo': p['baseRefName'], 'conflito': {'CONFLICTING': True, 'MERGEABLE': False}.get(p['mergeable']),  # None = UNKNOWN (recalculando)
                'revisao': p.get('reviewDecision'),
                'revisores': revisores, 'pede_minha_revisao': me in revisores,
                'ci': roll.get('state'), 'ci_falhos': falhos, 'criado': p['createdAt'], 'atualizado': p['updatedAt'],
                'reviews': reviews, 'checks_em': checks_em,
            }
        if not d['pageInfo']['hasNextPage']: break
        after = d['pageInfo']['endCursor']
    if info is not None:
        info['vistas'] = vistas
        info['incompleta'] = not vistas or (total is not None and len(vistas) < min(total, 1000))
    return out

def comentarios_novos(prs, desde, me):
    """Comentarios/reviews de outras pessoas nas PRs monitoradas desde `desde` (datetime aware)."""
    out = []
    since = desde.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    for k, p in prs.items():
        if dt(p['atualizado']) < desde: continue                    # updatedAt sobe a cada comentario/review
        o, r = p['repo'].split('/'); n = p['numero']; novos = []
        try:
            for c in get(f'repos/{o}/{r}/issues/{n}/comments', {'since': since, 'per_page': 50}) or []:
                if c['user']['login'] != me and dt(c['created_at']) >= desde:
                    novos.append({'quando': c['created_at'], 'de': c['user']['login'], 'texto': (c.get('body') or '')[:400], 'onde': None})
            for c in get(f'repos/{o}/{r}/pulls/{n}/comments', {'since': since, 'per_page': 50}) or []:
                if c['user']['login'] != me and dt(c['created_at']) >= desde:
                    novos.append({'quando': c['created_at'], 'de': c['user']['login'], 'texto': (c.get('body') or '')[:400], 'onde': c.get('path')})
            for rv in get(f'repos/{o}/{r}/pulls/{n}/reviews', {'per_page': 50}) or []:
                if rv['user']['login'] == me or rv.get('state') in ('PENDING', 'COMMENTED') or not rv.get('submitted_at'): continue
                if dt(rv['submitted_at']) >= desde:
                    novos.append({'quando': rv['submitted_at'], 'de': rv['user']['login'], 'texto': f"[{rv['state']}] {(rv.get('body') or '')[:400]}", 'onde': None})
        except (RuntimeError, LookupError): continue
        if novos:
            novos.sort(key=lambda x: x['quando'])
            out.append({'pr': k, 'titulo': p['titulo'], 'minha': p['minha'], 'url': p['url'], 'notas': novos})
    return out

def pr_fechada_como(p):
    """PR que sumiu da lista: merged, closed ou ?."""
    try:
        o, r = p['repo'].split('/'); d = get(f"repos/{o}/{r}/pulls/{p['numero']}")
        return 'merged' + (f" por {d['merged_by']['login']}" if d.get('merged_by') else '') if d.get('merged') else d.get('state', '?')
    except Exception: return '?'

AUSENTE_MAX = 3               # ticks seguidos em que uma PR aberta pode faltar na busca antes de sair da lista

def confirma_sumidas(sumidas, info, prs_agora):
    """Uma PR so sai da lista confirmada. A que a busca viu e o filtro tirou (ex.: pediram e ja fiz a minha revisao)
    sai como antes. A que nem veio na busca e ainda esta aberta no GitHub (REST pulls/N) ou sem resposta com a busca
    suspeita (vazia ou curta) continua na lista com o registro do tick anterior: a busca falhou, a PR nao saiu. Depois
    de AUSENTE_MAX ticks seguidos fora de uma busca completa, sai como 'open (fora da busca)'. Devolve as que sairam."""
    vistas = (info or {}).get('vistas')
    suspeita = bool((info or {}).get('incompleta')) if info else False
    saem = []
    for p in sumidas:
        p['fim'] = pr_fechada_como(p)
        if vistas is not None and p['key'] in vistas:
            saem.append(p); continue
        aberta = p['fim'] == 'open' or (p['fim'] == '?' and suspeita)
        if not aberta:
            saem.append(p); continue
        ausente = (0 if suspeita else int(p.get('ausente') or 0) + 1)
        if ausente > AUSENTE_MAX:
            p['fim'] = 'open (fora da busca)'; saem.append(p); continue
        fica = {k: v for k, v in p.items() if k != 'fim'}
        fica['ausente'] = ausente
        prs_agora[p['key']] = fica
    return saem

def delta(n_agora, n_antes, prs_agora, prs_antes):
    n_novas = [n for k, n in n_agora.items() if k not in n_antes]
    p_novas = [p for k, p in prs_agora.items() if k not in prs_antes]
    p_mudadas = []
    for k, p in prs_agora.items():
        o = prs_antes.get(k)
        if not o: continue
        muds = []
        if p['conflito'] is None: p['conflito'] = o.get('conflito')   # UNKNOWN: o GitHub esta recalculando; nao e mudanca
        if o.get('draft') and not p['draft']: muds.append('saiu de Draft')
        if not o.get('draft') and p['draft']: muds.append('virou Draft')
        if o.get('sha') != p['sha']: muds.append('commits novos')
        if not o.get('conflito') and p['conflito']: muds.append('CONFLITO')
        if o.get('conflito') and not p['conflito']: muds.append('conflito resolvido')
        if p['ci'] in ('FAILURE', 'ERROR') and o.get('ci') not in ('FAILURE', 'ERROR'): muds.append('CI QUEBROU (' + ', '.join(p['ci_falhos'][:3]) + ')')
        if p['ci'] == 'SUCCESS' and o.get('ci') in ('FAILURE', 'ERROR'): muds.append('CI passou')
        if p['revisao'] != o.get('revisao') and p['revisao'] in ('APPROVED', 'CHANGES_REQUESTED'):
            muds.append('APROVADA' if p['revisao'] == 'APPROVED' else 'MUDANCAS PEDIDAS')
        if p['pede_minha_revisao'] and not o.get('pede_minha_revisao'): muds.append('PEDIRAM SUA REVISAO')
        if o.get('titulo') != p['titulo']: muds.append('titulo mudou')
        if muds: p_mudadas.append((o, p, muds))
    p_sumidas = [p for k, p in prs_antes.items() if k not in prs_agora]
    return n_novas, p_novas, p_mudadas, p_sumidas

def notif_demanda(n, prs, me):
    """Notificacao que e acao sua: alguem pediu, ou CI quebrou na main / no branch de uma PR sua."""
    if n['motivo'] in MOTIVOS_PEDIDO: return True
    if n['motivo'] == 'ci_activity':
        if n['branch'] in ('main', 'master'): return True
        return any(p['minha'] and p['branch'] == n['branch'] and p['repo'] == n['repo'] for p in prs.values())
    return False

# ---------------- my green PR waiting for review (pending queue 'github') ----------------
LEMBRAR_REVIEW = 4.0          # business hours green + ready + no review before it becomes a pending item; None = off
JANELA_REVIEW = timedelta(days=14)      # a PR untouched for longer is a zombie: no pending item (same rule as the task rows)
CI_VERMELHO = ('FAILURE', 'ERROR')
CI_ESPERA = ('PENDING', 'EXPECTED')     # checks running (e.g. after a push): the clock holds, nothing starts or resolves
# how a github pending item leaves the queue by itself; none of them is work the person did (tasks.SEM_ACAO)
COMO_REVIEW, COMO_DRAFT, COMO_MERGE, COMO_FECHADA, COMO_CI = ('review chegou', 'voltou a Draft', 'PR mergeada',
                                                               'PR fechada', 'CI vermelho (pausada)')


def ref_pr(pr):
    """Pending ref of a PR: 'org/repo#N' (the PR key without the org would collide between two orgs)."""
    return f"{pr['repo']}#{pr['numero']}"


def espera_review(pr):
    """Mine, ready (not Draft), all checks green and nobody else reviewed yet."""
    return bool(pr.get('minha') and not pr.get('draft') and pr.get('ci') == 'SUCCESS' and not pr.get('reviews'))


def assunto_review(pr, desde):
    rev = ', '.join(pr.get('revisores') or []) or 'nenhum'
    return (f"Cobrar review da PR {pr['key']} {pr['titulo']} · verde e sem review desde {core.hora(desde)}"
            f" · revisores pedidos: {rev}")


def pendencias_review(st, s, prs_agora, prs_antes, sumidas, agora, horas, janela=None):
    """Clock + queue for my PRs that are green and ready with no review.

    st['verde'][key] = when the PR became green and ready (ISO). Enqueued as pending 'github' once `horas` business hours
    passed; the item is born at that moment (criada = desde = green since), so the 1st reminder is due in the same tick and
    the next ones follow core.LEMBRAR / MAX_LEMBRETES like Slack. Resolved by itself when a review comes in, the PR goes
    back to Draft or leaves the list (merged / closed). Red CI pauses it: the item leaves the queue and the clock starts
    over when CI is green again. Returns [(pending, how)] resolved in this tick. Everything comes from the PR query of
    the tick (the reason of a PR that left the list comes from pr_fechada_como, already called by the tick); only a
    pending item whose PR is in neither list asks GitHub (pr_fechada_como) and stays open unless it is merged/closed.
    """
    primeira = 'verde' not in st                         # first tick with this feature: estimate when it went green
    verde = st.setdefault('verde', {})
    cobrada = st.setdefault('cobrada', {})              # key -> the green period that already became a pending item
    for k in list(verde):
        if k not in prs_agora: verde.pop(k)
    for k, pr in prs_agora.items():
        if espera_review(pr):
            if k not in verde:
                if primeira or k not in prs_antes:       # never seen: when the checks finished (or it opened), not before
                    marcas = [x for x in (core.dt(pr.get('checks_em')), core.dt(pr.get('criado'))) if x]
                    ini = min(max(marcas), agora) if marcas else agora
                else: ini = agora                        # seen last tick not waiting: it changed between ticks
                verde[k] = ini.isoformat()
        elif pr.get('ci') in CI_ESPERA and pr.get('minha') and not pr.get('draft') and not pr.get('reviews'):
            pass                                         # checks running: hold the clock as it is
        else:
            verde.pop(k, None)
    for k in list(cobrada):
        if cobrada[k] != verde.get(k): cobrada.pop(k)    # that green period ended: the next one may enqueue again
    resolvidas = []
    por_ref = {ref_pr(pr): pr for pr in prs_agora.values()}
    fim = {ref_pr(pr): pr.get('fim') or '' for pr in sumidas}
    for p in core.pend_abertas(s, 'github'):
        pr = por_ref.get(p['ref'])
        if pr is None:
            f = fim.get(p['ref'])
            if f is None:                                # not in the list and not a confirmed exit (lost list): ask GitHub
                repo, _, n = p['ref'].rpartition('#')
                f = pr_fechada_como({'repo': repo, 'numero': n, 'key': chave(repo, n)}) if n.isdigit() and '/' in repo else '?'
                if f in ('open', '?'): continue
            como = COMO_MERGE if f.startswith('merged') else COMO_FECHADA
        elif pr.get('reviews'): como = COMO_REVIEW
        elif pr.get('draft'): como = COMO_DRAFT
        elif pr.get('ci') in CI_VERMELHO: como = COMO_CI
        else: continue
        core.resolve(p, agora, como); resolvidas.append((p, como))
    if not horas: return resolvidas                     # lembrar_horas null: resolves what is open, enqueues nothing
    abertas = {p['ref'] for p in core.pend_abertas(s, 'github')}
    for k, ini in verde.items():
        pr = prs_agora[k]
        if not espera_review(pr): continue               # holding while checks run: no new item
        if janela and ref_pr(pr) not in abertas and agora - (core.dt(pr.get('atualizado')) or agora) > janela: continue
        desde = core.dt(ini)
        ref = ref_pr(pr)
        # cobrada: an item closed by hand (--resolver, Planou, ignorar) in this same green period stays closed
        if ref not in abertas and (cobrada.get(k) == ini or core.horas_uteis(desde, agora) < horas): continue
        quem = ', '.join(pr.get('revisores') or []) or '(sem revisor pedido)'
        # agora=desde: a new or reopened item is born when the PR went green, so the 1st reminder is already due
        p = core.enfileira(s, 'github', ref, quem, assunto_review(pr, ini), ini, desde)
        p['link'] = pr['url']
        cobrada[k] = ini
    return resolvidas

# ---------------- detalhe (--github show) ----------------
def _limpa_log(t):
    t = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', t)
    t = re.sub(r'(?m)^\d{4}-\d\d-\d\dT[\d:.]+Z ', '', t)          # prefixo de timestamp do log do Actions
    t = re.sub(r'token=\S+', 'token=…', t)
    return [l.rstrip() for l in t.replace('\r', '\n').split('\n') if l.strip() and not l.startswith('##[group]') and not l.startswith('##[endgroup]')]

def show_pr(repo, n, linhas_log=40):
    o, r = ORG, repo
    p = get(f'repos/{o}/{r}/pulls/{n}')
    d = {'ref': chave(f'{o}/{r}', n), 'titulo': p['title'], 'estado': 'merged' if p.get('merged') else p['state'], 'draft': p.get('draft'),
         'autor': p['user']['login'], 'origem': p['head']['ref'], 'alvo': p['base']['ref'], 'merge_status': p.get('mergeable_state'),
         'criado': p['created_at'], 'atualizado': p['updated_at'], 'url': p['html_url'], 'descricao': (p.get('body') or '').strip(),
         'labels': [l['name'] for l in p.get('labels') or []], 'revisores': [u['login'] for u in p.get('requested_reviewers') or []],
         'assignees': [u['login'] for u in p.get('assignees') or []], 'sha': p['head']['sha']}
    try:
        revs = get(f'repos/{o}/{r}/pulls/{n}/reviews', {'per_page': 100}) or []
        ultimo = {}
        for rv in revs:
            if rv.get('state') not in ('PENDING', 'COMMENTED'): ultimo[rv['user']['login']] = rv['state']
        d['reviews'] = ultimo
    except Exception: d['reviews'] = {}
    d['checks'] = {'falhos': []}
    try:
        cr = get(f"repos/{o}/{r}/commits/{d['sha']}/check-runs", {'per_page': 100}) or {}
        d['checks']['total'] = cr.get('total_count')
        for c in cr.get('check_runs') or []:
            if c.get('conclusion') in ('failure', 'timed_out', 'cancelled'):
                job = re.search(r'/job/(\d+)', c.get('details_url') or '')
                log = []
                if job:
                    try: log = _limpa_log(gh([f'repos/{o}/{r}/actions/jobs/{job.group(1)}/logs'], raw=True))[-linhas_log:]
                    except Exception as e: log = [f'(log indisponivel: {str(e)[:80]})']
                d['checks']['falhos'].append({'nome': c['name'], 'conclusao': c['conclusion'], 'url': c.get('html_url') or c.get('details_url'), 'log': log})
    except Exception: pass
    try:
        th = graphql('query($o:String!,$r:String!,$n:Int!){ repository(owner:$o,name:$r){ pullRequest(number:$n){ reviewThreads(first:50){ nodes { isResolved path comments(first:1){ nodes { author{login} body createdAt } } } } } } }',
                     o=o, r=r, n=n)['repository']['pullRequest']['reviewThreads']['nodes']
        d['threads_abertas'] = [{'de': (t['comments']['nodes'][0].get('author') or {}).get('login'), 'quando': t['comments']['nodes'][0]['createdAt'],
                                 'arquivo': t.get('path'), 'texto': t['comments']['nodes'][0]['body'][:400]} for t in th if not t['isResolved'] and t['comments']['nodes']]
    except Exception: d['threads_abertas'] = []
    try:
        cs = get(f'repos/{o}/{r}/issues/{n}/comments', {'per_page': 100}) or []
        d['comentarios'] = [{'de': c['user']['login'], 'quando': c['created_at'], 'texto': (c.get('body') or '')[:400]} for c in cs][-10:]
    except Exception: d['comentarios'] = []
    try:
        fs = get(f'repos/{o}/{r}/pulls/{n}/files', {'per_page': 100}, paginate=True) or []
        d['arquivos'] = [f['filename'] + (' (novo)' if f['status'] == 'added' else ' (removido)' if f['status'] == 'removed' else '') + f" +{f['additions']}/-{f['deletions']}" for f in fs]
    except Exception: d['arquivos'] = []
    return d

def show_issue(repo, n):
    i = get(f'repos/{ORG}/{repo}/issues/{n}')
    cs = get(f'repos/{ORG}/{repo}/issues/{n}/comments', {'per_page': 100}) or []
    return {'ref': f'{repo}#issue/{n}', 'titulo': i['title'], 'estado': i['state'], 'autor': i['user']['login'],
            'assignees': [u['login'] for u in i.get('assignees') or []], 'labels': [l['name'] for l in i.get('labels') or []],
            'criado': i['created_at'], 'url': i['html_url'], 'descricao': (i.get('body') or '').strip(),
            'comentarios': [{'de': c['user']['login'], 'quando': c['created_at'], 'texto': (c.get('body') or '')[:400]} for c in cs]}

def imprime_show(d):
    if 'origem' in d:      # PR
        print(f"{d['ref']} · {'Draft · ' if d['draft'] else ''}{d['estado']} · {d['origem']} -> {d['alvo']} · mergeable: {d['merge_status']}")
        print(f"{d['titulo']}\n{d['url']}\nautor: {d['autor']} · revisao pedida a: {', '.join(d['revisores']) or '-'} · labels: {', '.join(d['labels']) or '-'}")
        if d['reviews']: print('reviews: ' + ', '.join(f'{u} {s}' for u, s in d['reviews'].items()))
        c = d['checks']
        print(f"checks: {c.get('total', '?')} no total, {len(c['falhos'])} falharam")
        for j in c['falhos']:
            print(f"\n  CHECK FALHOU: {j['nome']} ({j['conclusao']}) · {j['url']}")
            for l in j['log']: print(f'    {l}')
        if d['descricao']: print(f"\nDESCRICAO:\n{d['descricao']}")
        if d['arquivos']: print(f"\nARQUIVOS ({len(d['arquivos'])}):\n  " + '\n  '.join(d['arquivos'][:60]))
        if d['threads_abertas']:
            print(f"\nTHREADS DE REVIEW NAO RESOLVIDAS ({len(d['threads_abertas'])}):")
            for x in d['threads_abertas']: print(f"  [{hora(x['quando'])}] {x['de']}{' @ ' + x['arquivo'] if x['arquivo'] else ''}: {x['texto']}")
    else:                  # issue
        print(f"{d['ref']} · {d['estado']} · autor {d['autor']} · assignees: {', '.join(d['assignees']) or '-'} · labels: {', '.join(d['labels']) or '-'}")
        print(f"{d['titulo']}\n{d['url']}")
        if d['descricao']: print(f"\nDESCRICAO:\n{d['descricao']}")
    if d['comentarios']:
        print(f"\nCOMENTARIOS ({len(d['comentarios'])}):")
        for c in d['comentarios']: print(f"  [{hora(c['quando'])}] {c['de']}: {c['texto']}")

# ---------------- impressao ----------------
def linha_notif(n, prefixo='', demanda=None):
    motivo = MOTIVO_NOME.get(n['motivo'], n['motivo'])
    if n['motivo'] == 'ci_activity': motivo = f"CI falhou em {n['branch'] or '?'}"
    return (f"{prefixo}{n['alvo']} · {motivo}" + ('' if demanda in (None, True) else ' (so aviso)') +
            f"\n   {n['titulo']}\n   {n['url']}")

def linha_pr(p, prefixo=''):
    flags = ' · Draft' if p['draft'] else ''
    flags += ' · CONFLITO' if p['conflito'] else ''
    flags += f" · CI {p['ci']}" if p['ci'] and p['ci'] != 'SUCCESS' else ''
    flags += f" · {p['revisao']}" if p['revisao'] else ''
    flags += ' · PEDE SUA REVISAO' if p['pede_minha_revisao'] else ''
    quem = 'sua' if p['minha'] else p['autor']
    return f"{prefixo}{p['key']} ({quem}){flags} -> {p['alvo']}\n   {p['titulo']}\n   {p['url']}"

def imprime_delta(n_novas, p_novas, p_mudadas, p_sumidas, notas, prs, me):
    if not (n_novas or p_novas or p_mudadas or p_sumidas or notas): return False
    print('== GITHUB')
    if n_novas:
        print(f'NOTIFICACOES NOVAS ({len(n_novas)}):')
        for n in sorted(n_novas, key=lambda x: x['quando']): print(linha_notif(n, '+ ', notif_demanda(n, prs, me)))
    if notas:
        print(f'COMENTARIOS NOVOS ({sum(len(n["notas"]) for n in notas)} em {len(notas)} PRs):')
        for n in notas:
            print(f"-- {n['pr']} ({'sua PR' if n['minha'] else 'PR de outro'}) · {n['titulo']}\n   {n['url']}")
            for x in n['notas']: print(f"   [{hora(x['quando'])}] {x['de']}{' @ ' + x['onde'] if x['onde'] else ''}: {x['texto']}")
    if p_novas:
        print(f'PRs NOVAS ({len(p_novas)}):')
        for p in p_novas: print(linha_pr(p, '+ '))
    if p_mudadas:
        print(f'PRs MUDARAM ({len(p_mudadas)}):')
        for o, p, muds in p_mudadas: print(linha_pr(p, f"~ [{', '.join(muds)}] "))
    if p_sumidas:
        print(f'PRs SAIRAM DA LISTA ({len(p_sumidas)}):')
        for p in p_sumidas: print(f"- {p['key']} [{p.get('fim', '?')}] {p['titulo']}")
    return True

# ---------------- tick ----------------
def tick_github(s, desde, dry):
    st = s.setdefault('github', {'notifs': {}, 'prs': {}, 'ultima': None, 'primeira': None})
    me = eu()
    info = {}
    prs_agora = prs_abertas(me, info=info)
    recompostas = []
    if desde is None and not st.get('ultima'):           # primeira rodada: planta a baseline
        if not dry:
            ts = datetime.now(timezone.utc).isoformat()
            st.update(notifs={}, prs=prs_agora, ultima=ts, primeira=ts)
        return {'baseline': True, 'n_prs': len(prs_agora), 'me': me}
    n_agora = st['notifs']
    cursor = desde is None
    if desde is not None:                                # --since: janela fixa, sem estado
        n_novas = list(notificacoes(desde).values())
        p_novas = [p for p in prs_agora.values() if core.dt(p['criado']) >= desde]
        p_mudadas = [(p, p, ['atualizada']) for p in prs_agora.values() if core.dt(p['atualizado']) >= desde and core.dt(p['criado']) < desde]
        p_sumidas = []
    else:
        desde = core.dt(st['ultima'])
        n_agora = notificacoes(desde)
        n_novas, p_novas, p_mudadas, p_sumidas = delta(n_agora, st['notifs'], prs_agora, st['prs'])
        p_sumidas = confirma_sumidas(p_sumidas, info, prs_agora)
        if not st['prs'] and p_novas:
            # lista anterior vazia fora da baseline (ex.: um tick em que a busca voltou vazia e a lista foi gravada
            # assim): as PRs abertas antes do tick anterior ja eram conhecidas; so as abertas depois dele sao novas
            recompostas = [p for p in p_novas if (core.dt(p.get('criado')) or desde) < desde]
            p_novas = [p for p in p_novas if p not in recompostas]
    notas = comentarios_novos(prs_agora, desde, me)
    for k, it in prs_agora.items():
        for extra in ('status', 'nota'):
            if k in st['prs'] and extra in st['prs'][k]: it[extra] = st['prs'][k][extra]
    resolvidas = []
    if not dry:
        if cursor:                                       # --since is a fixed window: it does not move the review clock
            resolvidas = pendencias_review(st, s, prs_agora, st['prs'], p_sumidas, datetime.now(timezone.utc), LEMBRAR_REVIEW,
                                           JANELA_REVIEW)
        st['notifs'] = n_agora; st['prs'] = prs_agora; st['ultima'] = datetime.now(timezone.utc).isoformat()
    return {'baseline': False, 'notifs_novas': n_novas, 'prs_novas': p_novas, 'prs_mudadas': p_mudadas,
            'prs_sumidas': p_sumidas, 'notas': notas, 'prs': prs_agora, 'me': me, 'resolvidas': resolvidas,
            'prs_recompostas': recompostas}

def imprime_github(r):
    if r['baseline']:
        print(f'== GITHUB: baseline plantada agora ({r["n_prs"]} PRs monitoradas em {ORG}); a proxima rodada mostra o que mudar.'); return True
    algo = imprime_delta(r['notifs_novas'], r['prs_novas'], r['prs_mudadas'], r['prs_sumidas'], r['notas'], r['prs'], r['me'])
    if r.get('prs_recompostas'):
        if not algo: print('== GITHUB')
        print(f"lista de PRs recomposta: {len(r['prs_recompostas'])} PRs abertas antes do ultimo tick voltaram (ja eram conhecidas, nada novo)")
        return True
    return algo


def _iso(x):
    try: return datetime.fromisoformat((x or '').replace('Z', '+00:00').replace('+0000', '+00:00')).isoformat()
    except ValueError: return None


class Fonte(Base):
    tipo = 'github'
    lembrar = {'github': LEMBRAR_REVIEW}
    conserto = 'GitHub down — gh auth / token; see the tick error'

    def configura(self):
        global ORG, REPOS, BRT, LEMBRAR_REVIEW, JANELA_REVIEW
        ORG = self.cfg.get('org') or sys.exit('github: "org" faltando no config.json')
        REPOS = list(self.cfg.get('repos') or [])
        BRT = core.BRT
        LEMBRAR_REVIEW = self.lembrar.get('github')      # "lembrar_horas" of the source (default 4); null = no review reminder
        JANELA_REVIEW = timedelta(days=int((self.cfg.get('tarefas') or {}).get('janela_dias', 14)))

    def tick(self, s, desde, dry): return tick_github(s, desde, dry)
    def imprime(self, r): return imprime_github(r)

    pend = ('github',)

    def pronta(self, p, s):
        """A green PR of mine waiting for review: already happening at the source."""
        return f'PR {p.get("ref")} aberta e esperando review', 'PR aberta e esperando review'

    def tarefas_extras(self, s, agora):
        """My open non-draft PRs touched in the last N days: CI failing / conflict -> To do; waiting on reviewers ->
        Waiting on; approved -> merge. Other people's PRs asking for my review -> review. A PR of mine with an open
        pending item (green with no review for too long) has no row here: the pending item is its task."""
        from watch_core import tasks as tc
        _corta = tc._corta
        t = self.cfg.get('tarefas') or {}
        janela = timedelta(days=int(t.get('janela_dias', 14)))
        sufixo = t.get('sufixo_login', '')
        tira = (lambda x: x.replace(sufixo, '')) if sufixo else (lambda x: x)
        out = {}
        cobrando = {p['ref'] for p in core.pend_abertas(s, 'github')}   # the pending item is the task while it is open
        for pr in ((s.get('github') or {}).get('prs') or {}).values():
            if pr.get('draft') or (pr.get('status') and pr['status'] != 'open'): continue
            if ref_pr(pr) in cobrando: continue
            repo = pr['repo'].split('/')[-1]; ref = f'{repo} #{pr["numero"]}'
            base = {'link': pr.get('url'), 'entrou': _iso(pr.get('criado')), 'origem': 'github'}
            if pr.get('minha'):
                upd = _iso(pr.get('atualizado'))
                if not upd or agora - datetime.fromisoformat(upd) > janela: continue      # zombie PRs stay in GitHub
                if pr.get('ci') == 'FAILURE':
                    out[f'gh{pr["key"]}'] = {**base, 'titulo': f'Fix CI on {ref} {pr["titulo"]}', 'curto': _corta(f'Fix CI on {ref}', 70), 'status': 'A fazer', 'prio': 1,
                                             'ready_reason': f'PR {ref} aberta com CI vermelho', 'ready_reason_min': 'PR aberta com CI vermelho'}
                elif pr.get('conflito'):
                    out[f'gh{pr["key"]}'] = {**base, 'titulo': f'Resolve the conflict on {ref} {pr["titulo"]}', 'curto': _corta(f'Resolve the conflict on {ref}', 70), 'status': 'A fazer', 'prio': 2,
                                             'ready_reason': f'PR {ref} aberta com conflito', 'ready_reason_min': 'PR aberta com conflito'}
                elif (pr.get('revisao') or '').upper() == 'APPROVED':
                    out[f'gh{pr["key"]}'] = {**base, 'titulo': f'Merge {ref} {pr["titulo"]}', 'curto': _corta(f'Merge {ref}', 70), 'status': 'A fazer', 'prio': 2,
                                             'ready_reason': f'PR {ref} aprovada, falta o merge', 'ready_reason_min': 'PR aprovada, falta o merge'}
                elif pr.get('revisores'):
                    out[f'gh{pr["key"]}'] = {**base, 'titulo': f'Review of {ref} {pr["titulo"]}', 'curto': _corta(f'Review of {ref}', 70), 'status': 'Aguardando',
                                             'quem': ', '.join(tira(r) for r in pr['revisores']), 'prio': 5,
                                             'ready_reason': f'PR {ref} aberta e esperando review', 'ready_reason_min': 'PR aberta e esperando review'}
            elif pr.get('pede_minha_revisao'):
                out[f'ghr{pr["key"]}'] = {**base, 'titulo': f'Review {ref} {pr["titulo"]} ({tira(pr.get("autor") or "?")})',
                                          'curto': _corta(f'Review {ref} ({tira(pr.get("autor") or "?")})', 70), 'status': 'A fazer', 'prio': 2,
                                          'ready_reason': f'PR {ref} pede a sua revisão', 'ready_reason_min': 'PR pede a sua revisão'}
        return out

    def args(self, ap):
        ap.add_argument('--github', nargs='+', metavar='all|show REF', help='GitHub: all (PRs monitoradas + nao lidas) | show repo#N | show repo#issue/N')

    def cli(self, a, ctx):
        if not a.github: return False
        cmd = a.github[0]
        if cmd == 'show' and len(a.github) > 1:
            repo, tipo, n = _ref(a.github[1])
            d = show_pr(repo, n) if tipo == 'pr' else show_issue(repo, n)
            print(json.dumps(d, ensure_ascii=False, indent=1)) if a.json else imprime_show(d); return True
        if cmd == 'all':
            me = eu(); prs_agora = prs_abertas(me)
            nao_lidas = {k: n for k, n in notificacoes(datetime.now(timezone.utc) - timedelta(days=30)).items() if not n['lida']}
            if a.json: print(json.dumps({'notifs': nao_lidas, 'prs': prs_agora}, ensure_ascii=False, indent=1)); return True
            print(f'{len(nao_lidas)} notificacoes nao lidas (30 dias):')
            for n in sorted(nao_lidas.values(), key=lambda x: x['quando'], reverse=True): print(linha_notif(n, '- ', notif_demanda(n, prs_agora, me)))
            print(f"\n{len(prs_agora)} PRs monitoradas (minhas + revisao pedida + outros em {', '.join(REPOS)}):")
            for p in sorted(prs_agora.values(), key=lambda x: x['atualizado'], reverse=True): print(linha_pr(p, '- '))
            return True
        sys.exit('--github all | show REF')
