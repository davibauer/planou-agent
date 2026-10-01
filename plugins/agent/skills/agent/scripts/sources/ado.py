"""Helper (not an adapter): Azure DevOps REST for one organization with a PAT (Basic auth), shared by the ado_workitems and
ado_prs sources (and by instance adapters that need the same session: ado.call, ado.ORG, ado.PROJETOS, pr functions).

The PAT is read from a git credential store line (https://<user>:<PAT>@dev.azure.com/<org>...) that contains
"credencial_marca"; configured by the first source that calls configura().

Config keys (in the ado_workitems / ado_prs source entries):
  org                https://dev.azure.com/<org> (required)
  projetos           projects to search (required)
  credencial         file with the credential line (default ~/.git-credentials)
  credencial_marca   text that identifies the line (default: the org name)
  tipos              work item types eligible for triage (default Task, Bug); the rest is notice only
  fechados           states that close an item (default Closed, Removed, Done, Resolved)
  mensagens          {"sem_pat", "recusado"} ({arquivo}, {marca} are filled in)
"""
import re, sys, json, base64, html, urllib.request, urllib.parse, urllib.error, os
from datetime import datetime, timezone, timedelta

import core, paths
from watch_core import slowfs

ORG = None
PROJETOS = []
TIPOS = ['Task', 'Bug']                       # elegiveis para triagem; o resto so avisa
FECHADOS = ['Closed', 'Removed', 'Done', 'Resolved']
CRED_FILE = os.path.expanduser('~/.git-credentials')
CRED_FILE_ROTULO = '~/.git-credentials'
CRED_MARCA = None
BRT = timezone(timedelta(hours=-3))
MSG_PADRAO = {'sem_pat': 'PAT do ADO nao encontrado em {arquivo} (linha {marca}).', 'recusado': 'ADO recusou o PAT (401).'}
MSG = dict(MSG_PADRAO)


def configura(cfg):
    global ORG, PROJETOS, TIPOS, FECHADOS, CRED_FILE, CRED_FILE_ROTULO, CRED_MARCA, MSG, BRT
    org = (cfg.get('org') or '').rstrip('/')
    if ORG and org and org != ORG: raise ValueError(f'duas organizacoes do ADO na mesma instancia: {ORG} e {org}')
    ORG = org or ORG or sys.exit('ado: "org" faltando no config.json')
    PROJETOS = list(cfg.get('projetos') or PROJETOS) or sys.exit('ado: "projetos" faltando no config.json')
    TIPOS = list(cfg.get('tipos') or TIPOS); FECHADOS = list(cfg.get('fechados') or FECHADOS)
    CRED_FILE_ROTULO = cfg.get('credencial') or CRED_FILE_ROTULO
    CRED_FILE = paths.expande(CRED_FILE_ROTULO)
    CRED_MARCA = cfg.get('credencial_marca') or CRED_MARCA or ORG.rsplit('/', 1)[-1]
    MSG = {**MSG_PADRAO, **(cfg.get('mensagens') or {})}
    BRT = core.BRT


CAMPOS = ['System.Id', 'System.WorkItemType', 'System.Title', 'System.State', 'System.ChangedDate',
          'System.CreatedDate', 'System.TeamProject', 'System.AreaPath', 'System.IterationPath',
          'System.Tags', 'Microsoft.VSTS.Common.Activity', 'System.AssignedTo', 'System.CreatedBy',
          'Microsoft.VSTS.Scheduling.OriginalEstimate', 'Microsoft.VSTS.Scheduling.CompletedWork', 'System.Parent']
# the Prazo (Planou deadline): Due Date, else Target Date. A process without them answers 400 to the whole call: the
# list is then read without them for the rest of the run (never costs the source)
CAMPOS_PRAZO = ['Microsoft.VSTS.Scheduling.DueDate', 'Microsoft.VSTS.Scheduling.TargetDate']
_SEM_PRAZO = []

def auth_header():
    try:
        line = next(l for l in slowfs.read_text(CRED_FILE).splitlines() if CRED_MARCA in l)   # with a deadline on a Windows drive
    except (StopIteration, FileNotFoundError):
        sys.exit(MSG['sem_pat'].format(arquivo=CRED_FILE_ROTULO, marca=CRED_MARCA))
    pat = urllib.parse.unquote(re.match(r'https://[^:]*:([^@]+)@', line.strip()).group(1))
    return 'Basic ' + base64.b64encode((':' + pat).encode()).decode()

def call(url, method='GET', body=None, ctype='application/json'):
    req = urllib.request.Request(url, method=method, headers={'Authorization': auth_header(), 'Content-Type': ctype})
    if body is not None: req.data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 203): sys.exit(MSG['recusado'])
        raise

def texto(h):
    """HTML do ADO -> texto simples, preservando quebras de linha e itens."""
    if not h: return ''
    h = re.sub(r'<br\s*/?>', '\n', h, flags=re.I)
    h = re.sub(r'</(p|div|li|tr|h\d)>', '\n', h, flags=re.I)
    h = re.sub(r'<li[^>]*>', '- ', h, flags=re.I)
    h = re.sub(r'<[^>]+>', '', h)
    return re.sub(r'\n{3,}', '\n\n', html.unescape(h)).strip()

def atribuidos():
    proj = ', '.join(f"'{p}'" for p in PROJETOS)
    fech = ', '.join(f"'{s}'" for s in FECHADOS)
    wiql = {'query': f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] IN ({proj}) "
                     f"AND [System.AssignedTo] = @Me AND [System.State] NOT IN ({fech}) ORDER BY [System.ChangedDate] DESC"}
    ids = [w['id'] for w in call(ORG + '/_apis/wit/wiql?api-version=7.0', 'POST', wiql)['workItems']]
    itens = []
    for i in range(0, len(ids), 200):
        lote = ','.join(map(str, ids[i:i+200]))
        campos = CAMPOS if _SEM_PRAZO else CAMPOS + CAMPOS_PRAZO
        try:
            itens += call(ORG + f'/_apis/wit/workitems?ids={lote}&fields={",".join(campos)}&api-version=7.0')['value']
        except urllib.error.HTTPError as e:
            if e.code != 400 or _SEM_PRAZO: raise
            _SEM_PRAZO.append(True)
            itens += call(ORG + f'/_apis/wit/workitems?ids={lote}&fields={",".join(CAMPOS)}&api-version=7.0')['value']
    from watch_core.deadlines import from_source
    out = {}
    for w in itens:
        f = w['fields']
        out[str(w['id'])] = {
            'id': w['id'], 'tipo': f['System.WorkItemType'], 'titulo': f['System.Title'], 'estado': f['System.State'],
            'changed': f['System.ChangedDate'], 'created': f['System.CreatedDate'], 'projeto': f['System.TeamProject'],
            'area': f.get('System.AreaPath'), 'sprint': f.get('System.IterationPath'), 'tags': f.get('System.Tags', ''),
            'activity': f.get('Microsoft.VSTS.Common.Activity'), 'estimativa': f.get('Microsoft.VSTS.Scheduling.OriginalEstimate'),
            'apontado': f.get('Microsoft.VSTS.Scheduling.CompletedWork'),
            'vence': from_source(f.get('Microsoft.VSTS.Scheduling.DueDate') or f.get('Microsoft.VSTS.Scheduling.TargetDate')),
            'criado_por': (f.get('System.CreatedBy') or {}).get('displayName'), 'pai': f.get('System.Parent'),
            'url': f"{ORG}/{urllib.parse.quote(f['System.TeamProject'])}/_workitems/edit/{w['id']}",
            'elegivel': f['System.WorkItemType'] in TIPOS,
        }
    return out

def show(wid):
    w = call(ORG + f'/_apis/wit/workitems/{wid}?$expand=all&api-version=7.0')
    f = w['fields']
    pai = None
    for r in w.get('relations') or []:
        if r['rel'] == 'System.LinkTypes.Hierarchy-Reverse':
            pid = r['url'].rsplit('/', 1)[1]
            p = call(ORG + f'/_apis/wit/workitems/{pid}?fields=System.Id,System.Title,System.WorkItemType,System.Description,Microsoft.VSTS.Common.AcceptanceCriteria&api-version=7.0')['fields']
            pai = {'id': int(pid), 'tipo': p['System.WorkItemType'], 'titulo': p['System.Title'],
                   'descricao': texto(p.get('System.Description')), 'criterios': texto(p.get('Microsoft.VSTS.Common.AcceptanceCriteria'))}
    coms = call(ORG + f"/{urllib.parse.quote(f['System.TeamProject'])}/_apis/wit/workItems/{wid}/comments?api-version=7.0-preview.3").get('comments', [])
    prs = [r['url'] for r in w.get('relations') or [] if r.get('attributes', {}).get('name') == 'Pull Request']
    return {
        'id': w['id'], 'tipo': f['System.WorkItemType'], 'titulo': f['System.Title'], 'estado': f['System.State'],
        'projeto': f['System.TeamProject'], 'area': f.get('System.AreaPath'), 'sprint': f.get('System.IterationPath'),
        'tags': f.get('System.Tags', ''), 'activity': f.get('Microsoft.VSTS.Common.Activity'),
        'estimativa': f.get('Microsoft.VSTS.Scheduling.OriginalEstimate'),
        'descricao': texto(f.get('System.Description')), 'repro': texto(f.get('Microsoft.VSTS.TCM.ReproSteps')),
        'criterios': texto(f.get('Microsoft.VSTS.Common.AcceptanceCriteria')),
        'comentarios': [{'quem': c['createdBy']['displayName'], 'quando': c['createdDate'], 'texto': texto(c['text'])} for c in coms],
        'pai': pai, 'prs_vinculadas': prs,
        'url': f"{ORG}/{urllib.parse.quote(f['System.TeamProject'])}/_workitems/edit/{w['id']}",
    }

def hora(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(BRT).strftime('%d/%m %H:%M')

def linha(it, prefixo=''):
    est = f' · {it["estimativa"]:g}h' if it.get('estimativa') else ''
    tags = f' · [{it["tags"]}]' if it.get('tags') else ''
    return (f'{prefixo}#{it["id"]} {it["tipo"]} · {it["estado"]} · {it["projeto"]}{est}{tags}\n'
            f'   {it["titulo"]}\n   {it["url"]}')

def delta(atual, anterior):
    novos, mudados, sumidos = [], [], []
    for k, it in atual.items():
        old = anterior.get(k)
        if not old: novos.append(it)
        elif old.get('estado') != it['estado'] or old.get('titulo') != it['titulo']:
            mudados.append((old, it))
    for k, old in anterior.items():
        if k not in atual: sumidos.append(old)
    return novos, mudados, sumidos


# ---------------- pull requests ----------------
VOTO = {10: 'aprovou', 5: 'aprovou com sugestoes', 0: 'sem voto', -5: 'aguardando autor', -10: 'reprovou'}

def me():
    return call(ORG + '/_apis/connectionData')['authenticatedUser']['id']

def _comentarios(pr, eu):
    """Comentarios humanos (commentType == 'text') de todas as threads, em ordem cronologica."""
    out = []
    for t in call(pr['url_api'] + '/threads?api-version=7.0').get('value', []):
        if t.get('isDeleted'): continue
        for c in t.get('comments', []):
            if c.get('commentType') != 'text' or c.get('isDeleted'): continue
            a = c.get('author') or {}
            out.append({'id': f"{t['id']}.{c['id']}", 'quando': c.get('publishedDate'), 'de': a.get('displayName'),
                        'eu': a.get('id') == eu, 'texto': (c.get('content') or '').strip()[:200], 'thread_status': t.get('status')})
    out.sort(key=lambda c: c['quando'] or '')
    return out

def _norm(pr, projeto, eu):
    rev = pr.get('reviewers') or []
    meu = next((r for r in rev if r['id'] == eu), None)
    return {
        'id': pr['pullRequestId'], 'projeto': projeto, 'repo': pr['repository']['name'], 'titulo': pr['title'],
        'autor': pr['createdBy']['displayName'], 'minha': pr['createdBy']['id'] == eu, 'revisor': meu is not None,
        'origem': pr['sourceRefName'].split('/', 2)[2], 'destino': pr['targetRefName'].split('/', 2)[2],
        'draft': bool(pr.get('isDraft')), 'status': pr['status'], 'criada': pr['creationDate'], 'merge': pr.get('mergeStatus'),
        'meu_voto': (meu or {}).get('vote', 0), 'obrigatorio': bool((meu or {}).get('isRequired')),
        'votos': {r['displayName']: r.get('vote', 0) for r in rev if r['id'] != eu and not r.get('isContainer')},
        'url_api': pr['url'],
        'url': f"{ORG}/{projeto}/_git/{pr['repository']['name']}/pullrequest/{pr['pullRequestId']}",
    }

def ativas(eu):
    """PRs ativas em que sou revisor ou autor: {id: pr}. 2 buscas x projeto (reviewerId e creatorId sao ANDed se juntos)."""
    out = {}
    for p in PROJETOS:
        for chave in ('reviewerId', 'creatorId'):
            r = call(ORG + f'/{p}/_apis/git/pullrequests?searchCriteria.status=active&searchCriteria.{chave}={eu}&$top=100&api-version=7.0')
            for pr in r.get('value', []):
                out[str(pr['pullRequestId'])] = _norm(pr, p, eu)
    for pr in out.values():
        pr['comentarios'] = _comentarios(pr, eu)
    return out

def por_id(projeto, pid, eu):
    """Uma PR pelo id (para saber como terminou uma que sumiu da lista de ativas)."""
    pr = call(ORG + f'/{projeto}/_apis/git/pullrequests/{pid}?api-version=7.0')
    return _norm(pr, projeto, eu)

def linha_pr(pr):
    papel = 'minha' if pr['minha'] else ('revisor' + (' obrigatorio' if pr['obrigatorio'] else ''))
    votos = ', '.join(f'{n.split()[0]}:{VOTO.get(v, v)}' for n, v in pr['votos'].items()) or 'sem revisor'
    draft = ' · DRAFT' if pr['draft'] else ''
    return (f"PR {pr['id']} [{pr['projeto']}/{pr['repo']}] {pr['autor'].split()[0]} · {pr['origem']} -> {pr['destino']}{draft} · {papel}"
            f" · votos: {votos} · {pr['criada'][:10]} · {pr['titulo'][:70]}\n   {pr['url']}")
