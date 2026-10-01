#!/usr/bin/env python3
"""Source k8s (result key 'clusters'): Kubernetes clusters through kubectl, diffed against the last photo per cluster.
New problem, restart count that went up, problem that normalized. Contexts come from the local kubeconfig; one cluster
down does not hide the others.

Two scopes:
  escopo "cluster"    the whole cluster: nodes (NotReady, pressure, cordon), pods of every namespace (Pending/Failed,
                      CrashLoop/ImagePull..., Running not Ready, stuck Terminating, leftover Evicted per namespace),
                      deployments/statefulsets/daemonsets below desired, and optionally the Argo CD Applications (not
                      Healthy / not Synced / last sync failed; Progressing only after "progressing_ticks" ticks). A
                      context that needs an SSO login is reported once a day ("dica_sso"), not as broken.
  escopo "namespace"  one namespace ("namespace"): its pods and deployments/statefulsets, plus the nodes.

Config ("fontes": [{"tipo": "k8s", ...}]):
  envs               {"<env>": "<kubectl context>"} (required)
  escopo             "cluster" (default) | "namespace"
  namespace          the namespace (escopo namespace)
  argo               read Argo CD Applications (escopo cluster; default true)
  dica_sso           hint shown when a context's SSO session expired (escopo cluster; null = no SSO detection)
  request_timeout    kubectl --request-timeout (default "15s")
  timeout_s          process timeout in seconds (default 60)
  progressing_ticks  default 2
  saude              escopo namespace: once a business day from "saude_hora" (default 9; always with --since) a
                     == SAUDE line per cluster with node memory in % of allocatable (metrics-server), nodes at
                     "saude_alto"% (default 85) or more highlighted (default false)
"""
import sys, json, subprocess
from datetime import datetime, timezone, timedelta

import core
from adapters import Fonte as Base

ENVS = {}
ESCOPO = 'cluster'
NS = None
ARGO = True
DICA_SSO = None
TIMEOUT = '15s'
TIMEOUT_PROC = 60
PROGRESSING_TICKS = 2
SAUDE = False
SAUDE_ALTO = 85                          # % de memoria do alocavel a partir do qual o no e' destacado na foto de saude
SAUDE_HORA = 9
TERMINANDO_MAX = timedelta(minutes=10)   # pod em Terminating ha mais que isso = volume/finalizer preso
PENDING_MAX = timedelta(minutes=5)       # Pending mais novo que isso e' scale-up normal (Karpenter/ASG)
ARGO_OK_HEALTH = ('Healthy',)            # Progressing/Degraded/Missing/Unknown/Suspended viram problema
EVICTADOS = ('Evicted', 'Shutdown', 'NodeShutdown', 'Terminated')   # status.reason de pod Failed que e' so sobra
IGNORAR_NS = ()


def _dt(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00')) if iso else datetime.now(timezone.utc)


def _hora(iso):
    return _dt(iso).astimezone(core.BRT).strftime('%d/%m %H:%M') if iso else '??'


def kubectl(ctx, *args):
    cmd = ['kubectl', '--context', ctx, f'--request-timeout={TIMEOUT}', *args, '-o', 'json']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_PROC)
    except FileNotFoundError:
        sys.exit('kubectl nao encontrado no WSL')
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'{ctx}: kubectl travou ({TIMEOUT_PROC} s)')
    if r.returncode != 0:
        err = (r.stderr or '').strip()
        if DICA_SSO is not None and ('Token has expired' in err or 'sso' in err.lower() and 'expired' in err.lower()):
            raise SsoVencida(f'{ctx}: sessao SSO vencida ({DICA_SSO})')
        if DICA_SSO is not None and 'exit code 255' in err and 'getting credentials' in err:
            raise SsoVencida(f'{ctx}: aws nao emitiu token (perfil sem credencial valida)')
        ult = err.splitlines()[-1][:200] if err else 'kubectl falhou'
        raise RuntimeError(f'{ctx}: {ult}')
    return json.loads(r.stdout)


class SsoVencida(RuntimeError):
    """Perfil SSO sem sessao valida: nao e' cluster fora, e' credencial — o agent reporta uma vez por dia."""





def argo_apps(ctx):
    """Applications do Argo (todas as namespaces): [{ns, nome, sync, health, op, msg, revisao}]."""
    try:
        items = kubectl(ctx, 'get', 'applications.argoproj.io', '-A')['items']
    except RuntimeError as e:
        if 'the server doesn\'t have a resource type' in str(e) or 'NotFound' in str(e): return []   # cluster sem Argo
        raise
    out = []
    for a in items:
        st = a.get('status') or {}; op = st.get('operationState') or {}
        cond = [c.get('message', '') for c in st.get('conditions') or [] if 'Error' in (c.get('type') or '')]
        out.append({'ns': a['metadata']['namespace'], 'nome': a['metadata']['name'],
                    'sync': (st.get('sync') or {}).get('status') or '?', 'health': (st.get('health') or {}).get('status') or '?',
                    'op': op.get('phase'), 'op_fim': op.get('finishedAt'), 'msg': (op.get('message') or (cond[0] if cond else '') or '')[:160],
                    'revisao': ((st.get('sync') or {}).get('revision') or '')[:8]})
    return out


def foto_cluster(env, agora=None):
    """Estado do cluster: problemas (chave estavel -> texto), restarts por pod, ultimo motivo, apps do Argo."""
    agora = agora or datetime.now(timezone.utc)
    ctx = ENVS[env]
    nodes = kubectl(ctx, 'get', 'nodes')['items']
    pods = kubectl(ctx, 'get', 'pods', '-A')['items']
    wl = kubectl(ctx, 'get', 'deployments,statefulsets,daemonsets', '-A')['items']
    apps = argo_apps(ctx) if ARGO else []
    out = {'env': env, 'ctx': ctx, 'problemas': [], 'restarts': {}, 'motivos': {}, 'apps': apps, 'nos': len(nodes)}
    evicted = {}                                                           # ns -> n (sobras de eviction: um item por namespace, nao por pod)
    for n in nodes:
        nome = n['metadata']['name']; conds = {c['type']: c['status'] for c in (n.get('status') or {}).get('conditions') or []}
        if conds.get('Ready') != 'True':
            out['problemas'].append({'k': f'node:{nome}', 'o': f'no {nome} NotReady', 'sev': 'alta'})
        for pres in ('MemoryPressure', 'DiskPressure', 'PIDPressure'):
            if conds.get(pres) == 'True': out['problemas'].append({'k': f'node:{nome}:{pres}', 'o': f'no {nome} {pres}', 'sev': 'alta'})
        if (n.get('spec') or {}).get('unschedulable'):
            out['problemas'].append({'k': f'node:{nome}:cordon', 'o': f'no {nome} cordonado (unschedulable)', 'sev': 'media'})
    for p in pods:
        ns = p['metadata']['namespace']
        if ns in IGNORAR_NS: continue
        nome = f'{ns}/{p["metadata"]["name"]}'; st = p['status']; fase = st.get('phase')
        if fase == 'Succeeded': continue                                   # job concluido
        if fase == 'Failed' and st.get('reason') in EVICTADOS:              # sobra de eviction/shutdown: o RS ja repos, so ocupa a lista
            evicted[ns] = evicted.get(ns, 0) + 1; continue
        cs = st.get('containerStatuses') or []
        out['restarts'][nome] = sum(c.get('restartCount', 0) for c in cs)
        ult = [((c.get('lastState') or {}).get('terminated') or {}) for c in cs]
        ult = [u for u in ult if u.get('reason')]
        if ult: out['motivos'][nome] = f'{ult[0]["reason"]} {_hora(ult[0].get("finishedAt"))}'
        if p['metadata'].get('deletionTimestamp'):
            dur = agora - _dt(p['metadata']['deletionTimestamp'])
            if dur > TERMINANDO_MAX:
                out['problemas'].append({'k': f'pod:{nome}', 'o': f'{nome} em Terminating ha {int(dur.total_seconds() // 60)} min (volume/finalizer preso?)', 'sev': 'media'})
            continue
        esperando = [((c.get('state') or {}).get('waiting') or {}).get('reason') for c in cs]
        esperando = [e for e in esperando if e and e not in ('ContainerCreating', 'PodInitializing')]
        if fase in ('Pending', 'Failed', 'Unknown'):
            idade = agora - _dt(p['metadata']['creationTimestamp'])
            if fase != 'Pending' or idade > PENDING_MAX:
                out['problemas'].append({'k': f'pod:{nome}', 'o': f'{nome} {fase}' + (f' ({", ".join(esperando)})' if esperando else '') + f' ha {int(idade.total_seconds() // 60)} min', 'sev': 'media' if fase == 'Pending' else 'alta'})
        elif esperando:
            out['problemas'].append({'k': f'pod:{nome}', 'o': f'{nome} {", ".join(esperando)}' + (f' · ultimo: {out["motivos"][nome]}' if out['motivos'].get(nome) else ''), 'sev': 'alta'})
        elif fase == 'Running' and cs and not all(c.get('ready') for c in cs):
            out['problemas'].append({'k': f'pod:{nome}', 'o': f'{nome} Running mas nao Ready', 'sev': 'media'})
    for ns, n in sorted(evicted.items()):
        out['problemas'].append({'k': f'evicted:{ns}', 'o': f'{n} pod(s) Evicted sobrando em {ns} (ephemeral-storage/DiskPressure; kubectl delete pod --field-selector=status.phase=Failed limpa)', 'sev': 'baixa'})
    for d in wl:
        ns = d['metadata']['namespace']
        if ns in IGNORAR_NS: continue
        nome = f'{ns}/{d["metadata"]["name"]}'; sp = d.get('spec') or {}; st = d.get('status') or {}
        if d['kind'] == 'DaemonSet':
            quer = st.get('desiredNumberScheduled', 0) or 0; pronto = st.get('numberReady', 0) or 0
        else:
            quer = sp.get('replicas', 1); pronto = st.get('readyReplicas', 0) or 0
        if quer and pronto < quer:
            out['problemas'].append({'k': f'wl:{nome}', 'o': f'{d["kind"].lower()} {nome} {pronto}/{quer} prontos', 'sev': 'media'})
    for a in apps:
        ref = f'{a["ns"]}/{a["nome"]}'
        if a['health'] not in ARGO_OK_HEALTH:
            out['problemas'].append({'k': f'argo:{ref}:health', 'o': f'argo {ref} {a["health"]} (sync {a["sync"]})' + (f' · {a["msg"]}' if a['msg'] and a['health'] != 'Progressing' else ''),
                                     'sev': 'baixa' if a['health'] == 'Progressing' else 'alta'})
        if a['sync'] not in ('Synced',):
            out['problemas'].append({'k': f'argo:{ref}:sync', 'o': f'argo {ref} {a["sync"]}' + (f' @{a["revisao"]}' if a['revisao'] else ''), 'sev': 'baixa'})
        if a['op'] in ('Failed', 'Error'):
            out['problemas'].append({'k': f'argo:{ref}:op', 'o': f'argo {ref} ultima sync {a["op"]} {_hora(a["op_fim"])}' + (f' · {a["msg"]}' if a['msg'] else ''), 'sev': 'alta'})
    return out


def diff_restarts(antes, agora_f):
    """Pods cujo restartCount subiu desde a foto anterior: [(pod, delta, total, motivo)]."""
    out = []
    for pod, n in agora_f['restarts'].items():
        a = antes.get(pod)
        if a is not None and n > a:
            out.append((pod, n - a, n, agora_f['motivos'].get(pod)))
    return out


def imprime_foto_cluster(f, restarts=False, argo=False):
    print(f'== {f["env"].upper()} ({f["ctx"]}, {f["nos"]} nos, {len(f["apps"])} apps Argo): {len(f["problemas"])} problemas')
    for p in f['problemas']: print(f'-- {p["o"]}')
    if argo:
        for a in f['apps']:
            print(f'   {a["ns"]}/{a["nome"]:<24} {a["sync"]:<10} {a["health"]:<12} op={a["op"] or "-"}' + (f' · {a["msg"]}' if a['msg'] else ''))
    if restarts:
        for pod, n in sorted(f['restarts'].items(), key=lambda x: -x[1]):
            if n: print(f'   {pod}: {n} restarts' + (f' · ultimo: {f["motivos"][pod]}' if f['motivos'].get(pod) else ''))


# ---------------- escopo namespace ----------------
def _dep(p):
    """Nome logico do pod (sem o hash do ReplicaSet): api-78d7-7zqqq -> api."""
    refs = (p['metadata'].get('ownerReferences') or [{}])[0]
    n = refs.get('name') or p['metadata']['name']
    return n.rsplit('-', 1)[0] if refs.get('kind') == 'ReplicaSet' else n


def foto_ns(env, agora=None):
    """Estado do cluster: pods com problema, restarts por pod, deployments abaixo do desejado, nos NotReady/pressao."""
    agora = agora or datetime.now(timezone.utc)
    ctx = ENVS[env]
    pods = kubectl(ctx, 'get', 'pods', '-n', NS)['items']
    deps = kubectl(ctx, 'get', 'deployments,statefulsets', '-n', NS)['items']
    nodes = kubectl(ctx, 'get', 'nodes')['items']
    out = {'env': env, 'problemas': [], 'restarts': {}, 'motivos': {}}
    for p in pods:
        nome = p['metadata']['name']; st = p['status']; fase = st.get('phase')
        cs = st.get('containerStatuses') or []
        out['restarts'][nome] = sum(c.get('restartCount', 0) for c in cs)
        ult = [((c.get('lastState') or {}).get('terminated') or {}) for c in cs]
        ult = [u for u in ult if u.get('reason')]
        if ult: out['motivos'][nome] = f'{ult[0]["reason"]} {core.hora(ult[0].get("finishedAt"))}'
        if p['metadata'].get('deletionTimestamp'):
            if agora - core.dt(p['metadata']['deletionTimestamp']) > TERMINANDO_MAX:
                out['problemas'].append({'k': f'pod:{nome}', 'o': f'{nome} em Terminating ha {int((agora - core.dt(p["metadata"]["deletionTimestamp"])).total_seconds() // 60)} min (volume preso?)'})
            continue
        esperando = [((c.get('state') or {}).get('waiting') or {}).get('reason') for c in cs]
        esperando = [e for e in esperando if e and e != 'ContainerCreating']
        if fase in ('Pending', 'Failed', 'Unknown'):
            idade = int((agora - core.dt(p['metadata']['creationTimestamp'])).total_seconds() // 60)
            if fase != 'Pending' or idade > 5:
                out['problemas'].append({'k': f'pod:{nome}', 'o': f'{nome} {fase}' + (f' ({", ".join(esperando)})' if esperando else '') + f' ha {idade} min'})
        elif esperando:
            out['problemas'].append({'k': f'pod:{nome}', 'o': f'{nome} {", ".join(esperando)}' + (f' · ultimo: {out["motivos"].get(nome)}' if out['motivos'].get(nome) else '')})
        elif fase == 'Running' and cs and not all(c.get('ready') for c in cs):
            out['problemas'].append({'k': f'pod:{nome}', 'o': f'{nome} Running mas nao Ready'})
    for d in deps:
        nome = d['metadata']['name']; sp = d.get('spec') or {}; st = d.get('status') or {}
        quer = sp.get('replicas', 1); pronto = st.get('readyReplicas', 0) or 0
        if pronto < quer:
            out['problemas'].append({'k': f'deploy:{nome}', 'o': f'{d["kind"].lower()} {nome} {pronto}/{quer} prontos'})
    for n in nodes:
        nome = n['metadata']['name']; conds = {c['type']: c['status'] for c in (n.get('status') or {}).get('conditions') or []}
        if conds.get('Ready') != 'True':
            out['problemas'].append({'k': f'node:{nome}', 'o': f'no {nome} NotReady'})
        for pres in ('MemoryPressure', 'DiskPressure', 'PIDPressure'):
            if conds.get(pres) == 'True': out['problemas'].append({'k': f'node:{nome}:{pres}', 'o': f'no {nome} {pres}'})
        if (n.get('spec') or {}).get('unschedulable'):
            out['problemas'].append({'k': f'node:{nome}:cordon', 'o': f'no {nome} cordonado (unschedulable)'})
    return out


def imprime_foto_ns(f, restarts=False):
    print(f'== {f["env"].upper()} ({ENVS[f["env"]]}): {len(f["problemas"])} problemas')
    for p in f['problemas']: print(f'-- {p["o"]}')
    if restarts:
        for pod, n in sorted(f['restarts'].items(), key=lambda x: -x[1]):
            if n: print(f'   {pod}: {n} restarts' + (f' · ultimo: {f["motivos"][pod]}' if f['motivos'].get(pod) else ''))


def _bytes(q):
    """Quantidade do Kubernetes ('2970480Ki', '3Gi', '123456') em bytes."""
    for suf, mul in (('Ki', 2**10), ('Mi', 2**20), ('Gi', 2**30), ('Ti', 2**40), ('k', 10**3), ('M', 10**6), ('G', 10**9)):
        if q.endswith(suf): return int(float(q[:-len(suf)]) * mul)
    return int(q)


def saude(env):
    """Memoria de cada no em % do alocavel (a mesma % do `kubectl top nodes` e das regras de alerta de memoria de no).
    Sem esta foto o risco de capacidade (no de sistema a 90% sem alerta) so aparece quando alguem pergunta.
    [(no, pool, pct, usado_GiB, alocavel_GiB)], maior % primeiro."""
    ctx = ENVS[env]
    nodes = kubectl(ctx, 'get', 'nodes')['items']
    r = subprocess.run(['kubectl', '--context', ctx, f'--request-timeout={TIMEOUT}', 'get', '--raw',
                        '/apis/metrics.k8s.io/v1beta1/nodes'], capture_output=True, text=True, timeout=TIMEOUT_PROC)
    if r.returncode != 0: raise RuntimeError(f'{ctx}: metrics-server: {(r.stderr or "").strip()[:200]}')
    uso = {i['metadata']['name']: _bytes(i['usage']['memory']) for i in json.loads(r.stdout)['items']}
    out = []
    for n in nodes:
        nome = n['metadata']['name']; aloc = _bytes(n['status']['allocatable']['memory'])
        if nome not in uso or not aloc: continue
        pool = n['metadata'].get('labels', {}).get('agentpool') or nome.split('-')[1]
        out.append((nome, pool, round(100 * uso[nome] / aloc), uso[nome] / 2**30, aloc / 2**30))
    return sorted(out, key=lambda x: -x[2])


def imprime_saude(env, nos):
    alto = [x for x in nos if x[2] >= SAUDE_ALTO]
    print(f'   {env.upper()}: ' + ', '.join(f'{"**" if p >= SAUDE_ALTO else ""}{no.rsplit("-", 1)[-1]} ({pool}) {p}%'
                                          f'{"**" if p >= SAUDE_ALTO else ""}' for no, pool, p, _, _ in nos)
          + (f'  ← {len(alto)} no(s) >= {SAUDE_ALTO}%' if alto else ''))


# ---------------- tick ----------------
def tick_cluster(s, desde, dry):
    """Diff contra a foto anterior por cluster. prd sem sessao SSO nao e' 'quebrado': vira aviso uma vez por dia."""
    st = s.setdefault('clusters', {'cursor': None, 'restarts': {}, 'problemas': {}, 'progressing': {}, 'sso_aviso': None})
    agora = datetime.now(timezone.utc)
    fixo = desde is not None
    fotos, quebrados, sso = {}, [], []
    for env in ENVS:
        try: fotos[env] = foto_cluster(env, agora)
        except SsoVencida as e: sso.append(f'{env}: {e}')
        except Exception as e: quebrados.append(f'{env}: {e}')
    if not fotos: raise RuntimeError('; '.join(quebrados + sso))
    hoje = agora.astimezone(core.BRT).strftime('%Y-%m-%d')
    avisos = []
    if not dry: st['sso_vencida'] = [x.split(':')[0] for x in sso]   # para a pagina do dia (tarefas: necessidades)
    if sso and st.get('sso_aviso') != hoje:                 # uma linha por dia, sem push: prd fica invisivel ate o `aws sso login`
        avisos = sso
        if not dry: st['sso_aviso'] = hoje
    if not st.get('cursor') and not fixo:                  # primeira rodada: restarts e problemas atuais viram base
        if not dry:
            st['restarts'] = {env: f['restarts'] for env, f in fotos.items()}
            st['problemas'] = {env: {p['k']: p['o'] for p in f['problemas']} for env, f in fotos.items()}
            st['cursor'] = agora.isoformat()
        cron = {env: [(pod, n, f['motivos'].get(pod)) for pod, n in f['restarts'].items() if n >= 20] for env, f in fotos.items()}
        return {'baseline': True, 'envs': {}, 'quebrados': quebrados, 'sso': avisos, 'cronicos': cron, 'existentes': {env: f['problemas'] for env, f in fotos.items()}}
    # Progressing do Argo: so conta depois de PROGRESSING_TICKS ticks seguidos (sync normal passa por ele)
    prog = dict(st.get('progressing') or {})
    vistos = set()
    for env, f in fotos.items():
        for p in f['problemas'][:]:
            if p['k'].startswith('argo:') and p['k'].endswith(':health') and 'Progressing' in p['o']:
                chave = f'{env}:{p["k"]}'; vistos.add(chave)
                prog[chave] = (prog.get(chave) or 0) + 1
                if prog[chave] < PROGRESSING_TICKS and p['k'] not in ((st.get('problemas') or {}).get(env) or {}): f['problemas'].remove(p)   # ja na base: continua la
    for chave in list(prog):
        if chave.split(':', 1)[0] in fotos and chave not in vistos: prog.pop(chave)   # saiu de Progressing (ou virou outra coisa)
    if not (dry or fixo): st['progressing'] = prog
    out, base_nova = {}, {}
    for env, f in fotos.items():
        agora_p = {p['k']: p['o'] for p in f['problemas']}
        if not fixo and env not in (st.get('problemas') or {}):       # cluster que entrou em ENVS depois da baseline (ou prd no 1o login): planta sem reportar
            base_nova[env] = f['problemas']
            if not dry:
                st.setdefault('restarts', {})[env] = f['restarts']; st.setdefault('problemas', {})[env] = agora_p
            continue
        antes_r = {} if fixo else (st.get('restarts') or {}).get(env, {})
        antes_p = {} if fixo else (st.get('problemas') or {}).get(env, {})
        novos = [p for p in f['problemas'] if p['k'] not in antes_p]
        sumiram = [k for k in antes_p if k not in agora_p]
        rest = [r for r in diff_restarts(antes_r, f) if f'pod:{r[0]}' not in agora_p]   # pod ja quebrado: o restart e' o mesmo fato
        if novos or sumiram or rest:
            out[env] = {'novos': novos, 'sumiram': sumiram, 'restarts': rest, 'total': len(f['problemas'])}
        if not (dry or fixo):
            st.setdefault('restarts', {})[env] = f['restarts']
            st.setdefault('problemas', {})[env] = agora_p
    if not (dry or fixo): st['cursor'] = agora.isoformat()
    return {'baseline': False, 'envs': out, 'quebrados': quebrados, 'sso': avisos, 'base_nova': base_nova, 'totais': {env: len(f['problemas']) for env, f in fotos.items()}}

def imprime_cluster(r):
    if r['baseline']:
        print('== CLUSTERS: cursor plantado agora (primeira rodada); restarts e problemas atuais viram base.')
        for env, ps in r['existentes'].items():
            for p in ps: print(f'   {env}: {p["o"]} (ja existia)')
        for env, cs in r['cronicos'].items():
            for pod, n, mot in sorted(cs, key=lambda x: -x[1]): print(f'   {env}: {pod} ja tem {n} restarts' + (f' · ultimo: {mot}' if mot else ''))
        for q in r['quebrados']: print(f'   QUEBRADO {q}')
        for q in r['sso']: print(f'   SSO VENCIDA {q}')
        return True
    if not (r['envs'] or r['quebrados'] or r['sso'] or r.get('base_nova')): return False
    resumo = ', '.join(f'{e}: {n} problemas' for e, n in r.get('totais', {}).items())
    print(f'== CLUSTERS ({resumo})')
    for env, v in r['envs'].items():
        for p in v['novos']: print(f'-- {env.upper()}: {p["o"]}')
        for pod, d, n, mot in v['restarts']: print(f'-- {env.upper()}: {pod} reiniciou {d}x (total {n})' + (f' · {mot}' if mot else ''))
        for k in v['sumiram']: print(f'   ✓ {env}: {k} normalizou')
    for env, ps in (r.get('base_nova') or {}).items():
        print(f'   {env}: base plantada agora ({len(ps)} problemas ja existentes viram base)')
        for p in ps: print(f'   {env}: {p["o"]} (ja existia)')
    for q in r['quebrados']: print(f'   QUEBRADO {q}')
    for q in r['sso']: print(f'AVISO (clusters): {q} — prd fica sem checagem ate o login')
    return True


def tick_ns(s, desde, dry):
    st = s.setdefault('clusters', {'cursor': None, 'restarts': {}, 'problemas': {}})
    agora = datetime.now(timezone.utc)
    fixo = desde is not None
    fotos, quebrados = {}, []
    for env in ENVS:
        try: fotos[env] = foto_ns(env, agora)
        except Exception as e: quebrados.append(f'{env}: {e}')
    if not fotos: raise RuntimeError('; '.join(quebrados))
    if not st.get('cursor'):                # primeira rodada: fotografa restarts e problemas ja existentes (viram base)
        if not dry:
            st['restarts'] = {env: f['restarts'] for env, f in fotos.items()}
            st['problemas'] = {env: {p['k']: p['o'] for p in f['problemas']} for env, f in fotos.items()}
            st['cursor'] = agora.isoformat()
        cron = {env: [(pod, n, f['motivos'].get(pod)) for pod, n in f['restarts'].items() if n >= 20] for env, f in fotos.items()}
        return {'baseline': True, 'envs': {}, 'quebrados': quebrados, 'cronicos': cron, 'existentes': {env: f['problemas'] for env, f in fotos.items()}}
    out = {}
    for env, f in fotos.items():
        antes_r = {} if fixo else (st.get('restarts') or {}).get(env, {})
        antes_p = {} if fixo else (st.get('problemas') or {}).get(env, {})
        agora_p = {p['k']: p['o'] for p in f['problemas']}
        novos = [p for p in f['problemas'] if p['k'] not in antes_p]
        sumiram = [k for k in antes_p if k not in agora_p]
        rest = diff_restarts(antes_r, f)
        if novos or sumiram or rest:
            out[env] = {'novos': novos, 'sumiram': sumiram, 'restarts': rest, 'total': len(f['problemas'])}
        if not (dry or fixo):
            st.setdefault('restarts', {})[env] = f['restarts']
            st.setdefault('problemas', {})[env] = agora_p
    if not (dry or fixo): st['cursor'] = agora.isoformat()
    # foto de saude 1x por dia util a partir de SAUDE_HORA: memoria % do alocavel por no em cada cluster
    saude_, hoje_brt = {}, agora.astimezone(core.BRT)
    if SAUDE and (fixo or (hoje_brt.weekday() < 5 and hoje_brt.hour >= SAUDE_HORA and st.get('saude_dia') != hoje_brt.date().isoformat())):
        for env in fotos:
            try: saude_[env] = saude(env)
            except Exception as e: saude_[env] = str(e)
        if not (dry or fixo): st['saude_dia'] = hoje_brt.date().isoformat()
    return {'baseline': False, 'envs': out, 'quebrados': quebrados, 'saude': saude_}


def imprime_ns(r):
    if r['baseline']:
        print('== CLUSTERS: cursor plantado agora (primeira rodada); restarts e problemas atuais viram base.')
        for env, ps in r['existentes'].items():
            for p in ps: print(f'   {env}: {p["o"]} (ja existia)')
        for env, cs in r['cronicos'].items():
            for pod, n, mot in sorted(cs, key=lambda x: -x[1]): print(f'   {env}: {pod} ja tem {n} restarts' + (f' · ultimo: {mot}' if mot else ''))
        for q in r['quebrados']: print(f'   QUEBRADO {q}')
        return True
    if r.get('saude'):
        print(f'== SAUDE (memoria dos nos, % do alocavel; destaque >= {SAUDE_ALTO}%)')
        for env, nos in r['saude'].items():
            if isinstance(nos, str): print(f'   {env.upper()}: QUEBRADO: {nos}')
            else: imprime_saude(env, nos)
    if not (r['envs'] or r['quebrados']): return bool(r.get('saude'))
    resumo = ', '.join(f'{e}: {v["total"]} problemas' for e, v in r['envs'].items()) or 'sem problema ativo'
    print(f'== CLUSTERS ({resumo})')
    for env, v in r['envs'].items():
        for p in v['novos']: print(f'-- {env.upper()}: {p["o"]}')
        for pod, d, n, mot in v['restarts']: print(f'-- {env.upper()}: {pod} reiniciou {d}x (total {n})' + (f' · {mot}' if mot else ''))
        for k in v['sumiram']: print(f'   ✓ {env}: {k} normalizou')
    for q in r['quebrados']: print(f'   QUEBRADO {q}')
    return True

class Fonte(Base):
    tipo = 'k8s'
    nome_padrao = 'clusters'
    lembrar = {}
    conserto = 'clusters unreachable — kubeconfig / credentials of the contexts'

    def configura(self):
        global ENVS, ESCOPO, NS, ARGO, DICA_SSO, TIMEOUT, TIMEOUT_PROC, PROGRESSING_TICKS, SAUDE, SAUDE_ALTO, SAUDE_HORA
        ENVS = dict(self.cfg.get('envs') or {}) or sys.exit('k8s: "envs" faltando no config.json')
        ESCOPO = self.cfg.get('escopo', 'cluster')
        NS = self.cfg.get('namespace')
        if ESCOPO == 'namespace' and not NS: sys.exit('k8s: escopo namespace sem "namespace"')
        ARGO = bool(self.cfg.get('argo', True))
        DICA_SSO = self.cfg.get('dica_sso')
        TIMEOUT = self.cfg.get('request_timeout', '15s')
        TIMEOUT_PROC = int(self.cfg.get('timeout_s', 60))
        PROGRESSING_TICKS = int(self.cfg.get('progressing_ticks', 2))
        SAUDE = bool(self.cfg.get('saude'))
        SAUDE_ALTO = int(self.cfg.get('saude_alto', 85))
        SAUDE_HORA = int(self.cfg.get('saude_hora', 9))

    def tick(self, s, desde, dry):
        return tick_cluster(s, desde, dry) if ESCOPO == 'cluster' else tick_ns(s, desde, dry)

    def imprime(self, r):
        return imprime_cluster(r) if ESCOPO == 'cluster' else imprime_ns(r)

    def args(self, ap):
        ap.add_argument('--clusters', nargs='*', metavar='ENV|restarts|argo', help='foto atual dos clusters (so leitura): [env] [restarts] [argo]')

    def cli(self, a, ctx):
        if a.clusters is None: return False
        envs = [x for x in a.clusters if x in ENVS] or list(ENVS)
        for env in envs:
            try:
                f = foto_cluster(env) if ESCOPO == 'cluster' else foto_ns(env)
            except Exception as e:
                print(f'== {env.upper()}: {"SSO VENCIDA" if isinstance(e, SsoVencida) else "QUEBRADO"}: {e}'); continue
            if ESCOPO == 'cluster': imprime_foto_cluster(f, 'restarts' in a.clusters, 'argo' in a.clusters)
            else: imprime_foto_ns(f, 'restarts' in a.clusters)
        return True
