#!/usr/bin/env python3
"""Source ado_pipelines (result key 'pipelines'): Azure DevOps pipelines of one project. Builds that ended with an error
since the cursor (failed / partiallySucceeded; 'canceled' is not an error), with the failed tasks and, when the timeline
message says nothing ("exited with code 1"), the real error lines from the task log; builds stuck in the queue or running
too long (reported once per build, "liberado" when they leave).

Credential: the Azure CLI token (`az account get-access-token --tenant <tenant>`), so it breaks alone when `az login`
expires.

Pending queue ('pipelines'): one per pipeline definition; a new failed build updates it; it resolves when the last
completed build of the definition succeeds.

Config ("fontes": [{"tipo": "ado_pipelines", ...}]):
  org        https://dev.azure.com/<org> (required)
  projeto    project name (required)
  tenant     tenant for az get-access-token (required)
  resource   Azure DevOps application id in Entra, the az get-access-token --resource (required)
  conta      account named in the "az login" hint
  sobreposicao_min  default 2
  lembrar_horas     default 4
"""
import os, re, sys, json, subprocess, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta

import core
from adapters import Fonte as Base
from core import enfileira, resolve, pend_abertas

ORG = PROJETO = API = TENANT = None
CONTA = 'a conta do projeto'
SOBREPOSICAO = timedelta(minutes=2)


def dt(iso): return core.dt(iso)


def hora(iso): return core.hora(iso)


ADO_RESOURCE = None      # "resource" do config: o id do aplicativo do Azure DevOps no Entra
RESULTADOS_ERRO = ('failed', 'partiallySucceeded')           # 'canceled' nao e' erro (cancelamento em lote/supersede)
FILA_MAX = timedelta(minutes=30)      # notStarted ha mais que isso = agente offline/ocupado
RODANDO_MAX = timedelta(minutes=90)   # inProgress ha mais que isso = travado
MAX_ERROS = 3
# mensagem de erro do timeline que nao diz nada: ai o log da task e' aberto e as linhas de erro reais vem de la (o
# "Bash exited with code '1'" pode esconder um NU1101 de pacote nao encontrado)
GENERICO_RE = re.compile(r"(exited with code|exit code|returned exit code|Script failed|Process completed with exit code)", re.I)
LOG_ERRO_RE = re.compile(r"(error [A-Z]{1,4}\d{3,5}\b|##\[error\]|\bERROR\b|\berror\b:|npm ERR!|Unhandled exception|FAILED|fatal:|Exception:)")
LOG_RUIDO_RE = re.compile(r"(exited with code|Finishing:|##\[section\]|##\[debug\]|warning )", re.I)


class Sessao:
    """Um token por tick (az e' lento: ~1-2 s)."""
    def __init__(self):
        try:
            r = subprocess.run(['az', 'account', 'get-access-token', '--tenant', TENANT, '--resource', ADO_RESOURCE, '--query', 'accessToken', '-o', 'tsv'],
                               capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            sys.exit('az CLI nao encontrado no WSL')
        except subprocess.TimeoutExpired:
            sys.exit('az account get-access-token travou (60 s)')
        tok = r.stdout.strip()
        if r.returncode != 0 or not tok:
            sys.exit(f'sem token do Azure CLI (rodar `az login` como {CONTA}): {(r.stderr or "").strip()[:200]}')
        self.h = {'Authorization': f'Bearer {tok}', 'Accept': 'application/json'}

    def get(self, path, **params):
        params.setdefault('api-version', '7.1')
        url = f'{API}/{path}?' + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        req = urllib.request.Request(url, headers=self.h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                corpo, final = r.read(), r.geturl()
            # token recusado sem 401: o ADO devolve 302 para a tela de login e o urlopen segue o redirect (23/09/2026)
            if '_signin' in final or corpo.lstrip()[:1] == b'<':
                sys.exit(f'Azure DevOps recusou o token (302 para _signin): rodar `az login` de novo como {CONTA}')
            return json.loads(corpo)
        except urllib.error.HTTPError as e:
            if e.code in (401, 203): sys.exit('Azure DevOps recusou o token (401): rodar `az login` de novo')
            raise RuntimeError(f'HTTP {e.code} em {path}: {e.read()[:200]!r}')


def svc():
    return Sessao()


def norm(b):
    ti = b.get('triggerInfo') or {}
    quem = (b.get('requestedFor') or {}).get('displayName') or ''
    if 'VisualStudio.Services' in quem: quem = ''          # CI: o "quem" util e' o commit
    return {'id': b['id'], 'numero': b.get('buildNumber'), 'pipeline': b['definition']['name'], 'def_id': b['definition']['id'],
            'status': b.get('status'), 'resultado': b.get('result'), 'fila': b.get('queueTime'), 'inicio': b.get('startTime'),
            'fim': b.get('finishTime'), 'branch': re.sub(r'^refs/heads/', '', b.get('sourceBranch') or ''),
            'commit': (b.get('sourceVersion') or '')[:8], 'msg': (ti.get('ci.message') or '').strip().splitlines()[0][:120] if ti.get('ci.message') else '',
            'quem': quem, 'motivo': b.get('reason'), 'link': ((b.get('_links') or {}).get('web') or {}).get('href')}


def falhas(s, desde, top=100):
    """Builds terminados com erro (finishTime > desde), mais antigos primeiro."""
    r = s.get('build/builds', resultFilter=','.join(RESULTADOS_ERRO), queryOrder='finishTimeDescending', **{'$top': top})
    out = [norm(b) for b in r.get('value', []) if b.get('finishTime') and dt(b['finishTime']) > desde]
    return sorted(out, key=lambda b: b['fim'])


def presos(s, agora=None):
    """Builds na fila ha mais de FILA_MAX ou rodando ha mais de RODANDO_MAX."""
    agora = agora or datetime.now(timezone.utc)
    r = s.get('build/builds', statusFilter='notStarted,inProgress', queryOrder='queueTimeDescending', **{'$top': 50})
    out = []
    for b in map(norm, r.get('value', [])):
        if b['status'] == 'notStarted' and b['fila'] and agora - dt(b['fila']) > FILA_MAX:
            b['preso'] = 'fila'; b['ha'] = int((agora - dt(b['fila'])).total_seconds() // 60); out.append(b)
        elif b['status'] == 'inProgress' and b['inicio'] and agora - dt(b['inicio']) > RODANDO_MAX:
            b['preso'] = 'rodando'; b['ha'] = int((agora - dt(b['inicio'])).total_seconds() // 60); out.append(b)
    return out


def build(s, bid):
    return norm(s.get(f'build/builds/{bid}'))


def erros(s, bid):
    """Tasks que falharam e as primeiras mensagens de erro (timeline). Vazio em cancelamento/agente ausente."""
    try:
        t = s.get(f'build/builds/{bid}/timeline')
    except Exception:
        return []
    out = []
    for r in t.get('records') or []:
        if r.get('type') != 'Task' or r.get('result') not in ('failed', 'succeededWithIssues'): continue
        msgs = [i.get('message', '').strip() for i in r.get('issues') or [] if i.get('type') == 'error']
        msgs = [re.sub(r'\s+', ' ', m)[:200] for m in msgs if m]
        do_log = []
        if (not msgs or all(GENERICO_RE.search(m) for m in msgs)) and (r.get('log') or {}).get('id'):
            do_log = erros_do_log(s, bid, r['log']['id'])
        out.append({'task': r.get('name'), 'erros': msgs[:MAX_ERROS], 'log': do_log})
    return out


def erros_do_log(s, bid, log_id, n=4):
    """Linhas de erro reais do log da task (compilacao, restore, npm, docker), sem timestamp e sem repeticao."""
    url = f'{API}/build/builds/{bid}/logs/{log_id}?api-version=7.1'
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=s.h), timeout=60) as r: texto = r.read().decode('utf-8', 'replace')
    except Exception:
        return []
    try: linhas = json.loads(texto).get('value') or []       # com Accept: application/json a API devolve {"count", "value": [linhas]}
    except ValueError: linhas = texto.splitlines()
    vistos, out = set(), []
    for l in linhas:
        l = re.sub(r'^\S+Z\s+', '', l).strip()          # tira o timestamp ISO do inicio
        l = re.sub(r'^#\d+ [\d.]+ ', '', l)                # e o prefixo "#17 6.335 " do docker buildx
        if not l or LOG_RUIDO_RE.search(l) or not LOG_ERRO_RE.search(l): continue
        chave = re.sub(r'\[[^\]]*\]$', '', l.split(' : ', 1)[-1])[:120]   # mesma msg em projetos/arquivos diferentes conta uma vez
        if chave in vistos: continue
        vistos.add(chave); out.append(l[:220])
        if len(out) >= n: break
    return out


def ultimo_resultado(s, def_id):
    """Para resolver pendencias: (id, resultado, fim) do ultimo build terminado da definicao."""
    r = s.get('build/builds', definitions=def_id, statusFilter='completed', queryOrder='finishTimeDescending', **{'$top': 1})
    v = r.get('value') or []
    if not v: return None
    b = norm(v[0]); return b


def linha(b, tag=''):
    quem = f' · {b["quem"]}' if b['quem'] else ''
    msg = f' · "{b["msg"]}"' if b['msg'] else ''
    return f'-- {tag}{b["pipeline"]} #{b["numero"]} · {b["resultado"] or b["status"]} · {b["branch"]} @{b["commit"]}{msg}{quem}'


def imprime_build(s, b, com_erros=True):
    print(linha(b) + (f' · terminou {hora(b["fim"])}' if b['fim'] else ''))
    if com_erros:
        for e in erros(s, b['id']):
            print(f'   x {e["task"]}')
            for m in e['erros']: print(f'     {m}')
            for m in e.get('log') or []: print(f'     log: {m}')
    print(f'   id: {b["id"]} · {b["link"]}')


def imprime_presos(ps):
    for b in ps:
        onde = 'na fila' if b['preso'] == 'fila' else 'rodando'
        print(f'-- PRESO: {b["pipeline"]} #{b["numero"]} · {onde} ha {b["ha"]} min · {b["branch"]}' + (f' · {b["quem"]}' if b['quem'] else ''))
        print(f'   id: {b["id"]} · {b["link"]}')


builds_falhos = falhas


def tick_pipelines(s, desde, dry):
    ado = svc()
    st = s.setdefault('pipelines', {'cursor': None, 'vistos': [], 'presos': []})
    agora = datetime.now(timezone.utc)
    fixo = desde is not None
    if desde is None:
        desde = dt(st['cursor']) if st.get('cursor') else None
    if desde is None:                      # primeira rodada: confere que a API responde e planta o cursor
        presos(ado, agora)
        if not dry: st['cursor'] = agora.isoformat()
        return {'baseline': True, 'falhas': [], 'presos': [], 'liberados': []}
    vistos = set() if fixo else set(st.get('vistos') or [])
    falhas = [b for b in builds_falhos(ado, desde - SOBREPOSICAO) if b['id'] not in vistos]
    for b in falhas: b['tasks'] = erros(ado, b['id'])
    # presos: reporta uma vez por build (fica no estado ate sair da fila); "liberado" quando some
    presos_agora = presos(ado, agora)
    ja = set() if fixo else set(st.get('presos') or [])
    presos_novos = [b for b in presos_agora if b['id'] not in ja]
    ids_agora = {b['id'] for b in presos_agora}
    liberados = [] if fixo else [i for i in ja if i not in ids_agora]
    resolvidas = []
    if not (dry or fixo):
        # resolve: o ultimo build terminado da definicao passou (um GET por pendencia aberta)
        for p in pend_abertas(s, 'pipelines'):
            try: u = ultimo_resultado(ado, int(p['ref'].split(':')[1]))
            except Exception: u = None
            if u and u['resultado'] == 'succeeded' and u['fim'] and dt(u['fim']) > dt(p['desde']):
                resolve(p, agora, f'passou em #{u["numero"]}'); resolvidas.append((p, f'passou em #{u["numero"]}'))
        # enfileira por definicao: build novo com erro reabre/atualiza a pendencia da mesma pipeline
        for b in falhas:
            p = enfileira(s, 'pipelines', f'def:{b["def_id"]}', b['quem'] or b['branch'], f'{b["pipeline"]} #{b["numero"]} {b["resultado"]}'[:80], b['fim'], agora)
            p['link'] = b['link']
        st['vistos'] = (list(vistos) + [b['id'] for b in falhas])[-300:]
        st['presos'] = sorted(ids_agora)
        st['cursor'] = agora.isoformat()
    return {'baseline': False, 'falhas': falhas, 'presos': presos_novos, 'liberados': liberados, 'resolvidas': resolvidas}

def imprime_pipelines(r):
    if r['baseline']:
        print('== PIPELINES: cursor plantado agora (primeira rodada).'); return True
    if not (r['falhas'] or r['presos'] or r['liberados']): return False
    print(f'== PIPELINES ({len(r["falhas"])} com erro, {len(r["presos"])} presos)')
    for b in r['falhas']:
        print(linha(b, 'ERRO: ') + f' · terminou {hora(b["fim"])}')
        for e in b.get('tasks') or []:
            print(f'   x {e["task"]}')
            for m in e['erros']: print(f'     {m}')
            for m in e.get('log') or []: print(f'     log: {m}')
        print(f'   id: {b["id"]} · {b["link"]}')
    imprime_presos(r['presos'])
    for i in r['liberados']: print(f'   ✓ build {i} saiu da fila')
    return True


class Fonte(Base):
    tipo = 'ado_pipelines'
    nome_padrao = 'pipelines'
    lembrar = {'pipelines': 4.0}
    conserto = 'Azure DevOps down — az login'
    pend = ('pipelines',)

    def pronta(self, p, s):
        """A pipeline whose last build failed: already happening at the source."""
        return f'pipeline com falha: {" ".join(str(p.get("assunto") or "").split())[:120]}', 'pipeline com falha na origem'

    def configura(self):
        global ORG, PROJETO, API, TENANT, CONTA, SOBREPOSICAO, ADO_RESOURCE
        ORG = (self.cfg.get('org') or sys.exit('ado_pipelines: "org" faltando no config.json')).rstrip('/')
        PROJETO = self.cfg.get('projeto') or sys.exit('ado_pipelines: "projeto" faltando no config.json')
        API = f'{ORG}/{PROJETO}/_apis'
        TENANT = self.cfg.get('tenant') or sys.exit('ado_pipelines: "tenant" faltando no config.json')
        CONTA = self.cfg.get('conta') or CONTA
        ADO_RESOURCE = self.cfg.get('resource') or sys.exit('ado_pipelines: "resource" faltando no config.json (id do Azure DevOps no Entra)')
        SOBREPOSICAO = timedelta(minutes=float(self.cfg.get('sobreposicao_min', 2)))

    def tick(self, s, desde, dry): return tick_pipelines(s, desde, dry)
    def imprime(self, r): return imprime_pipelines(r)
    def linha_ref(self, p): return f'   ref: {p["ref"]}' + (f' · {p["link"]}' if p.get('link') else '')

    def args(self, ap):
        ap.add_argument('--pipelines', nargs='*', metavar='presos|build ID', help='Azure DevOps (so leitura): presos | build ID')

    def cli(self, a, ctx):
        if a.pipelines is None: return False
        s = svc()
        if a.pipelines and a.pipelines[0] == 'build':
            b = build(s, a.pipelines[1]); imprime_build(s, b); return True
        ps = presos(s); print(f'{len(ps)} builds presos'); imprime_presos(ps); return True
